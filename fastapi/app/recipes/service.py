import asyncio
import json
import logging
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.adapters._cache import BoundedAsyncCache
from app.config import Settings
from app.errors import AppError
from app.recipes.ai import RecipeReasoner
from app.recipes.ai_contract import PROMPT_VERSION
from app.recipes.context import recommendation_context
from app.recipes.diagnostics import candidate_diagnostics
from app.recipes.evaluation import compare_rankings
from app.recipes.food_safety import FoodSafetyProvider
from app.recipes.planning import POLICY_VERSION, MealRanker, build_baseline, cover_expiring, diversify, vary_baseline
from app.recipes.preferences import preference_search_focus, preference_shortlist
from app.recipes.quantities import assess_quantities
from app.recipes.reliability import PreferenceDecisionCache, RecipeCircuit
from app.recipes.repository import RecipeRepository
from app.recipes.retrieval import MATCH_VERSION, TITLE_MATCH_VERSION, allowed, canonical, is_excluded, missing_named_ingredients, query_variants
from app.recipes.schemas import Recipe, RecommendationRequest, RecommendationResponse, normalize
from app.recipes.stock_names import stock_ingredient
from app.recipes.suitability import DEFAULT_PANTRY_NAMES, WATER_NAMES, assess_suitability, ingredient_role
from app.recipes.web_provider import WebRecipeProvider

logger = logging.getLogger(__name__)


class RecipeService:
    # DB·원천·원격 Gemma를 분리한다. 사진 분석 큐와 백엔드 재고는 수정하지 않는다.
    def __init__(self, settings: Settings, repository: RecipeRepository | None = None, provider: FoodSafetyProvider | None = None, reasoner: RecipeReasoner | None = None) -> None:
        self.settings = settings
        self.reasoner = reasoner or RecipeReasoner(settings)
        self._ai_slots = asyncio.Semaphore(2)
        self._ai_circuit = RecipeCircuit()
        self._preference_decisions = PreferenceDecisionCache()
        self.repository = repository or RecipeRepository(settings)
        self.provider = provider or FoodSafetyProvider(settings)
        self.web_provider = WebRecipeProvider(settings, reasoner=self.reasoner)
        self.cache = BoundedAsyncCache[RecommendationResponse](settings.reference_cache_max_size, settings.recipe_cache_ttl_seconds)

    # 재료 이름·제외 목록·날짜별 캐시를 검증하고 확정 재고 기반 추천을 반환한다.
    async def recommend(self, request: RecommendationRequest) -> RecommendationResponse:
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        excluded = sorted({normalize(name) for name in request.excludedIngredientNames if normalize(name)})
        requested_pantry = sorted({canonical(name) for name in request.pantryIngredientNames if not is_excluded(name, excluded)})
        pantry = sorted({canonical(name) for name in [*DEFAULT_PANTRY_NAMES, *request.pantryIngredientNames]
                         if not is_excluded(name, excluded) and ingredient_role("", name)[0] == "SEASONING"})
        key = json.dumps([POLICY_VERSION, MATCH_VERSION, TITLE_MATCH_VERSION, PROMPT_VERSION, self.settings.ollama_model, self.settings.recipe_ai_enabled, request.refrigeratorId,
                          request.inventoryFingerprint, excluded, request.recommendationMode, request.limit, request.targetServings,
                          request.strategies, requested_pantry, sorted(request.recentRecipeIds), self.settings.recipe_ai_max_candidates,
                          self.settings.recipe_ai_rank_timeout_seconds, self.settings.recipe_ai_input_max_bytes,
                          bool(self.settings.recipe_food_safety_api_key), self.settings.recipe_food_safety_storage_approved,
                          bool(self.settings.recipe_tavily_api_key), sorted(self.settings.recipe_tavily_approved_domains), today.isoformat()], ensure_ascii=False)
        key += json.dumps([request.preferenceTags, request.requestId], ensure_ascii=False)

        # 캐시 미스 시 DB를 우선 조회하고 부족하면 승인된 원천을 최대 한 번 보충한다.
        async def load() -> RecommendationResponse:
            started = monotonic()
            web_reports = []
            external_deadline = started + min(20, self.settings.recipe_ai_timeout_seconds / 2)
            original_stock = {item.ingredientId: item.name for item in request.inventory}
            inventory = [item.model_copy(update={"name": canonical(item.ingredientKey) if item.ingredientKey else stock_ingredient(item.name)}) for item in request.inventory
                         if not is_excluded(item.name, excluded) and not is_excluded(item.ingredientKey or stock_ingredient(item.name), excluded)
                         and canonical(item.ingredientKey or stock_ingredient(item.name)) not in WATER_NAMES
                         and (request.recommendationMode == "INCLUDE_EXPIRED" or item.expirationDate >= today)]
            matched_request = request.model_copy(update={"inventory": inventory})
            logger.info("recipe_stock_mapping input_count=%d explicit_key_count=%d reviewed_name_mapping_count=%d",
                        len(request.inventory), sum(item.ingredientKey is not None for item in inventory),
                        sum(item.ingredientKey is None and canonical(original_stock[item.ingredientId]) != item.name for item in inventory))
            original_names = [item.name for item in inventory]
            names = sorted({canonical(name) for name in original_names})
            query_names = query_variants(original_names)
            expiring = {canonical(item.name) for item in inventory if 0 <= (item.expirationDate - today).days <= 3}
            ranker = MealRanker(self.reasoner, self.settings.recipe_ai_timeout_seconds, self._ai_slots, self.settings.ollama_model, self.settings.recipe_ai_input_max_bytes)
            ranker.decisions, ranker.circuit = self._preference_decisions, self._ai_circuit
            context = {"today": today.isoformat(), "inventory": [item.model_dump(mode="json") for item in inventory],
                       "targetServings": request.targetServings, "strategies": request.strategies, "excludedIngredientNames": excluded,
                       "pantryIngredientNames": pantry, "limit": request.limit, "preferenceTags": request.preferenceTags,
                       "recommendationContext": recommendation_context(request.requestId), "seasoningPolicy": "IGNORE_FOR_SHOPPING_KEEP_SOURCE"}
            context["recommendationContext"]["personalizationMode"] = "PREFERENCE" if request.preferenceTags else "CONTEXT_RANDOM"
            context["expiryPolicy"] = {"urgentWithinDays": 3, "expiringIngredientIds": [item.ingredientId for item in inventory if 0 <= (item.expirationDate - today).days <= 3]}
            if inventory and not self.settings.recipe_ai_enabled:
                ranker.warnings.append("AI_DISABLED")
            if any(not is_excluded(name, excluded) and ingredient_role("", name)[0] not in {"SEASONING", "WATER"} for name in request.pantryIngredientNames):
                ranker.warnings.append("UNREVIEWED_PANTRY_INGREDIENTS_IGNORED")

            # 제외 재료 부분 문자열이 포함된 후보도 보수적으로 제외한다. 알레르기 안전 보증은 아니다.
            def usable(recipe: Recipe) -> bool:
                return allowed(recipe, query_names, excluded)

            lookup_started = monotonic()
            found = await self.repository.find(query_names) if names else []
            database_ms = (monotonic() - lookup_started) * 1000
            validation_started = monotonic()
            candidates = [recipe for recipe in found if usable(recipe)]
            searches = 1 if names else 0
            candidates = list({(r.source, r.sourceRecipeId): r for r in candidates}.values())
            search_names = list(dict.fromkeys(item.name for item in sorted(inventory, key=lambda item: (item.expirationDate, item.ingredientId))))
            source = "DATABASE"
            external_searched = False
            web_searches = 0
            # 원문 재료·조리법·분량을 보존한 채 동일한 기준으로 후보를 채점한다.
            def score_candidate(recipe: Recipe) -> dict:
                ingredients = {}
                for item in recipe.ingredients:
                    ingredients.setdefault(canonical(item.name), item.name)
                used = set(ingredients).intersection(names)
                urgent = used.intersection(expiring)
                recipe_pantry = [name for name in pantry if name not in {"마늘", "통마늘", "생강"} or ingredient_role(recipe.name, name)[0] == "SEASONING"]
                missing = set(ingredients) - used - set(recipe_pantry) - WATER_NAMES
                named = missing_named_ingredients(recipe.name, list(ingredients.values()), list(ingredients.values()))
                missing_named = missing_named_ingredients(recipe.name, list(ingredients.values()), [ingredients[name] for name in missing])
                breakdown = {"expiringIngredient": round(0.4 * len(urgent) / max(1, len(expiring)), 6),
                             "inventoryCoverage": round(0.6 * len(used) / len(ingredients), 6),
                             "missingIngredientPenalty": round(0.2 * len(missing) / len(ingredients) + 0.3 * len(missing_named) / max(1, len(named)), 6), "variety": 0.0}
                score = round(max(0, min(1, breakdown["expiringIngredient"] + breakdown["inventoryCoverage"] - breakdown["missingIngredientPenalty"])), 6)
                data = recipe.model_dump(mode="json", exclude={"status"})
                data.update(score=score, scoreBreakdown=breakdown, usesInventory=[ingredients[name] for name in sorted(used)],
                            usesExpiringIngredients=[ingredients[name] for name in sorted(urgent)], missingIngredients=[ingredients[name] for name in sorted(missing)],
                            excludedIngredientCheck="PASSED", reason=f"보유 재료 {len(used)}종 사용, 3일 내 소비기한 재료 {len(urgent)}종, 추가 재료 {len(missing)}종입니다.")
                if missing_named:
                    data["reason"] += f" 제목에 명시된 미보유 재료: {', '.join(missing_named)}."
                data.update(assess_quantities(recipe, inventory, request.targetServings, recipe_pantry))
                for usage in data["inventoryUsage"]:
                    usage["name"] = original_stock[usage["ingredientId"]]
                data["_suitability"] = assess_suitability(data, original_names)
                return data
            ranked = [score_candidate(recipe) for recipe in candidates]
            ranked.sort(key=lambda item: (-item["score"], item["source"], item["sourceRecipeId"]))
            baseline = build_baseline(ranked, matched_request)
            validation_ms = (monotonic() - validation_started) * 1000
            # 개수를 채우지 않는다. 아직 일반 추천의 주재료로 활용하지 못한 임박 이름을 찾는다.
            def uncovered(rows: list[dict]) -> list[str]:
                covered = {canonical(name) for row in rows for name in row["usesExpiringMainIngredients"]}
                return list(dict.fromkeys(item.name for item in sorted(inventory, key=lambda item: (item.expirationDate, item.ingredientId))
                                         if canonical(item.name) in expiring - covered))

            focus = uncovered(baseline) if "USE_EXPIRING" in request.strategies else []
            needs_more = not baseline or bool(focus)
            if names and needs_more and not external_searched:
                if self.settings.recipe_food_safety_api_key and self.settings.recipe_food_safety_storage_approved:
                    external_searched = True
                    try:
                        async with asyncio.timeout(max(0.1, min(10, external_deadline - monotonic()))):
                            found = await self.provider.search(focus or search_names)
                    except (AppError, TimeoutError):
                        ranker.warnings.append("EXTERNAL_REPLENISHMENT_UNAVAILABLE")
                    else:
                        merged = {(row["source"], row["sourceRecipeId"]): row for row in ranked}
                        unique_found = {(recipe.source, recipe.sourceRecipeId): (recipe, raw) for recipe, raw in found}
                        for recipe, raw in unique_found.values():
                            await self.repository.save(recipe, raw, "operator-approved:COOKRCP01")
                            identity = (recipe.source, recipe.sourceRecipeId)
                            if usable(recipe):
                                merged[identity] = score_candidate(recipe)
                        ranked = list(merged.values())
                        baseline = build_baseline(ranked, matched_request)
                        source = "DATABASE_AND_FOOD_SAFETY_KOREA"
                else:
                    ranker.warnings.append("EXTERNAL_REPLENISHMENT_NOT_CONFIGURED")
            focus = uncovered(baseline) if "USE_EXPIRING" in request.strategies else []
            # 명시적 취향 근거가 없는 임박 재료는 정렬 전에 보충해 RANK 두 번을 피한다.
            focus = list(dict.fromkeys([*preference_search_focus(inventory, baseline, request.preferenceTags), *focus]))
            if names and (focus or not baseline):
                if self.settings.recipe_tavily_api_key:
                    report = await self.web_provider.search(focus or search_names[:1], search_names, timeout_seconds=max(0.1, external_deadline - monotonic()),
                                                            preference_tags=request.preferenceTags)
                    web_searches = report.searches
                    web_reports.append(report)
                    if report.unavailable:
                        ranker.warnings.append("WEB_SEARCH_UNAVAILABLE")
                    merged = {(row["source"], row["sourceRecipeId"]): row for row in ranked}
                    for recipe, raw in report.recipes:
                        # 어댑터 이후에도 ACTIVE·제외 조건을 다시 검사한다.
                        if (recipe.source == "TAVILY_WEB" and recipe.sourceUrl and raw.get("url") == recipe.sourceUrl
                                and usable(recipe)):
                            if urlsplit(recipe.sourceUrl).hostname in self.settings.recipe_tavily_approved_domains:
                                await self.repository.save(recipe, raw, "operator-approved:TAVILY_WEB")
                            else:
                                ranker.warnings.append("WEB_SOURCE_NOT_REGISTERED")
                            merged[(recipe.source, recipe.sourceRecipeId)] = score_candidate(recipe)
                    ranked = list(merged.values())
                    baseline = build_baseline(ranked, matched_request)
                    source += "_AND_TAVILY_WEB"
                    if web_searches and not report.recipes:
                        ranker.warnings.append("WEB_SEARCH_NO_VERIFIED_RECIPE")
                else:
                    ranker.warnings.append("WEB_SEARCH_NOT_CONFIGURED")
            confirmation_rows = build_baseline(ranked, matched_request, confirmation_only=True)
            confirmations = confirmation_rows[:3]
            if confirmations:
                ranker.warnings.append("CONFIRMATION_CANDIDATES_SEPARATED")
            focus = uncovered(baseline) if "USE_EXPIRING" in request.strategies else []
            missing_main_count = sum(bool(row["_suitability"]["missingMain"]) for row in ranked)
            unresolved_count = sum(bool(row["_suitability"]["unresolvedMissing"]) for row in ranked)
            if missing_main_count:
                ranker.warnings.append("MISSING_INGREDIENTS_REQUIRE_SHOPPING")
            if unresolved_count:
                ranker.warnings.append("UNREVIEWED_MISSING_INGREDIENTS_NEED_CHECK")
            shortlist = preference_shortlist(baseline, request.preferenceTags, self.settings.recipe_ai_max_candidates)
            if not request.preferenceTags:
                baseline = vary_baseline(baseline, context["recommendationContext"]["variationSeed"])
                shortlist = baseline[:self.settings.recipe_ai_max_candidates]
            ranker.timeout = max(0.1, min(self.settings.recipe_ai_rank_timeout_seconds, self.settings.recipe_ai_timeout_seconds - (monotonic() - started)))
            can_call_ai = self.settings.recipe_ai_enabled and bool(shortlist) and (len(shortlist) > 1 or bool(request.preferenceTags))
            selected = await ranker.rank(context, shortlist) if can_call_ai else None
            # 웹 보충 예산에 DB 후보의 LLM 정렬 시간을 중복 차감하지 않는다. 전체 AI 예산은 별도로 유지한다.
            external_deadline += ranker.duration_ms / 1000
            preference_focus = preference_search_focus(inventory, baseline, request.preferenceTags, ranker.preference_matches)
            # 다른 재료의 취향 일치만으로 임박 주재료의 추가 탐색을 중단하지 않는다.
            if request.preferenceTags and (preference_focus or not ranker.preference_matches):
                if self.settings.recipe_tavily_api_key and not web_reports:
                    report = await self.web_provider.search(preference_focus or search_names[:2], search_names,
                                                            timeout_seconds=max(0.1, external_deadline - monotonic()),
                                                            preference_tags=request.preferenceTags)
                    web_searches += report.searches
                    web_reports.append(report)
                    merged = {(row["source"], row["sourceRecipeId"]): row for row in ranked}
                    added = False
                    for recipe, raw in report.recipes:
                        if (recipe.source == "TAVILY_WEB" and recipe.sourceUrl and raw.get("url") == recipe.sourceUrl
                                and usable(recipe)):
                            if urlsplit(recipe.sourceUrl).hostname in self.settings.recipe_tavily_approved_domains:
                                await self.repository.save(recipe, raw, "operator-approved:TAVILY_WEB")
                            else:
                                ranker.warnings.append("WEB_SOURCE_NOT_REGISTERED")
                            merged[(recipe.source, recipe.sourceRecipeId)] = score_candidate(recipe)
                            added = True
                    if added:
                        ranked = list(merged.values())
                        baseline = build_baseline(ranked, matched_request)
                        shortlist = preference_shortlist(baseline, request.preferenceTags, self.settings.recipe_ai_max_candidates)
                        # 새 웹 후보도 AI 평가 기회를 갖도록 앞쪽 후보를 일부 교체한다. 최종 임박 우선은 유지한다.
                        web_rows = [row for row in baseline if row["source"] == "TAVILY_WEB"]
                        shortlist = (web_rows[:5] + [row for row in shortlist if row["source"] != "TAVILY_WEB"])[:min(self.settings.recipe_ai_max_candidates, 12)]
                        ranker.timeout = max(0.1, min(self.settings.recipe_ai_rank_timeout_seconds, self.settings.recipe_ai_timeout_seconds - (monotonic() - started)))
                        selected = await ranker.rank(context, shortlist) if self.settings.recipe_ai_enabled and shortlist else None
                        source += "_AND_TAVILY_WEB"
                    if report.unavailable:
                        ranker.warnings.append("WEB_SEARCH_UNAVAILABLE")
                elif not self.settings.recipe_tavily_api_key:
                    ranker.warnings.append("PREFERENCE_WEB_SEARCH_NOT_CONFIGURED")
            evaluated = ranker.evaluated_identities
            for row in [*baseline, *confirmations]:
                identity = (row["source"], row["sourceRecipeId"])
                row["preferenceMatch"] = ("NOT_REQUESTED" if not request.preferenceTags else "MATCHED" if identity in ranker.preference_matches
                                          else "NOT_MATCHED" if identity in evaluated and identity not in ranker.rejected_preference_identities else "UNKNOWN")
                tag_evidence = ranker.preference_tag_evidence.get(identity, {})
                row["preferenceAssessments"] = [{"tag": tag, "status": "MATCHED" if tag in tag_evidence else
                                                  "NOT_MATCHED" if identity in evaluated and identity not in ranker.rejected_preference_identities
                                                  and tag not in ranker.rejected_preference_tags.get(identity, set()) else "UNKNOWN",
                                                  "evidence": tag_evidence.get(tag)} for tag in request.preferenceTags]
                if row["preferenceMatch"] == "MATCHED":
                    row["preferenceEvidence"] = ranker.preference_evidence[identity]
                    row["reason"] += f" AI가 요청 취향 중 {', '.join(tag_evidence)}에 대한 원문 근거를 확인했습니다."
                elif row["preferenceMatch"] == "NOT_MATCHED":
                    row["reason"] += " 요청 취향에 맞는 원문 근거가 확인되지 않아 같은 임박 활용 그룹의 취향 일치 후보보다 후순위입니다."
            if request.preferenceTags and len(baseline) > len(evaluated):
                ranker.warnings.append("PREFERENCE_EVALUATED_CANDIDATE_WINDOW")
            if request.preferenceTags and not ranker.preference_matches:
                ranker.warnings.append("NO_VERIFIED_PREFERENCE_MATCH")
            if self.settings.recipe_ai_enabled and len(shortlist) == 1 and not request.preferenceTags:
                ranker.warnings.append("AI_NOT_NEEDED_SINGLE_CANDIDATE")
            engine = "AI" if selected is not None else "RULE_FALLBACK" if baseline else "EMPTY"
            if engine == "RULE_FALLBACK":
                ranker.warnings.append("RULE_RESULTS_NOT_AI_JUDGEMENT")
            if ranked and not baseline:
                ranker.warnings.append("NO_ELIGIBLE_RECIPES")
            if len(baseline) < request.limit:
                ranker.warnings.append("CANDIDATE_COUNT_BELOW_REQUEST_LIMIT")
            if request.recommendationMode == "INCLUDE_EXPIRED" and any(item.expirationDate < today for item in inventory):
                ranker.warnings.append("EXPIRED_INVENTORY_INCLUDED_NOT_SAFETY_APPROVAL")
            # AI가 정렬한 앞부분 뒤에 검증된 기본 후보를 보존한다. 범위를 경고로 명시한다.
            chosen_ids = {(row["source"], row["sourceRecipeId"]) for row in selected or []}
            ordered = (selected + [row for row in baseline if (row["source"], row["sourceRecipeId"]) not in chosen_ids]) if selected is not None else baseline
            result = cover_expiring(diversify(ordered, request.recentRecipeIds), matched_request)[:request.limit]
            if any(row["baseServings"] is None for row in result):
                ranker.warnings.append("SOURCE_SERVINGS_UNAVAILABLE")
            if any(usage["allocationStatus"] == "UNRESOLVED" for row in result for usage in row["inventoryUsage"]):
                ranker.warnings.append("SOURCE_QUANTITIES_UNRESOLVED")
            # 외부 검색용 전체 후보 판정과 달리 응답은 최종 반환한 목록의 활용만 보고한다.
            focus = uncovered(result) if "USE_EXPIRING" in request.strategies else []
            if focus:
                ranker.warnings.append("UNCOVERED_EXPIRING_INGREDIENTS")
            ai_identities = evaluated
            if selected is not None and any((row["source"], row["sourceRecipeId"]) not in ai_identities for row in result):
                ranker.warnings.append("AI_RANKED_PREFIX_ONLY")
            if set(request.strategies) - {row["strategy"] for row in result} and result:
                ranker.warnings.append("PARTIAL_STRATEGY_COVERAGE")
            if selected is not None:
                logger.info("recipe_rank_comparison metrics=%s", json.dumps(compare_rankings(baseline, result, request.limit), separators=(",", ":")))
            logger.info("recipe_recommendation_completed engine=%s source=%s ai_calls=%d search_count=%d candidate_count=%d returned_count=%d",
                        engine, source, ranker.calls, searches, len(ranked), len(result))
            logger.info("recipe_candidate_filter missing_main_count=%d unreviewed_missing_count=%d eligible_count=%d external_search_count=%d", missing_main_count, unresolved_count, len(baseline), int(external_searched))
            logger.info("recipe_candidate_diagnostics metrics=%s",
                        json.dumps({"requestId": request.requestId, **candidate_diagnostics(ranked, len(baseline), len(confirmation_rows))},
                                   ensure_ascii=False, separators=(",", ":")))
            timings = {"databaseSearchMs": round(database_ms, 3), "initialCandidateEvaluationMs": round(validation_ms, 3),
                       "webExtractionMs": round(sum(report.extraction_ms for report in web_reports), 3),
                       "aiRankMs": round(ranker.duration_ms, 3), "recommendationMs": round((monotonic() - started) * 1000, 3)}
            web_failures = {}
            for report in web_reports:
                for code, count in report.failure_reasons.items():
                    web_failures[code] = web_failures.get(code, 0) + count
            extraction_calls = sum(report.extraction_calls for report in web_reports)
            logger.info("recipe_recommendation_timing request_id=%s timings_ms=%s ai_input_bytes=%d ai_candidate_count=%d", request.requestId,
                        json.dumps(timings, separators=(",", ":")), ranker.input_bytes, ranker.candidate_count)
            return RecommendationResponse(recommendationId=f"rec_{uuid4().hex}", refrigeratorId=request.refrigeratorId,
                                          inventoryFingerprint=request.inventoryFingerprint, recommendationMode=request.recommendationMode,
                                          recommendations=[{key: value for key, value in row.items() if not key.startswith("_")} for row in result],
                                          confirmationCandidates=[{key: value for key, value in row.items() if not key.startswith("_")} for row in confirmations],
                                          uncoveredExpiringIngredients=focus,
                                          generatedAt=datetime.now(UTC), recommendationEngine=engine,
                                          mealPlan={}, warnings=list(dict.fromkeys(ranker.warnings)), preferenceTags=request.preferenceTags,
                                          context=context["recommendationContext"],
                                          trace={"policyVersion": POLICY_VERSION, "model": ranker.model, "promptVersion": PROMPT_VERSION if ranker.calls or extraction_calls or ranker.preference_cache_hits else None,
                                                 "preferenceDecisionCacheHits": ranker.preference_cache_hits,
                                                 "aiCalls": ranker.calls + extraction_calls, "searchCount": searches, "candidateCount": len(ranked),
                                                  "externalSearchCount": int(external_searched), "webSearchCount": web_searches,
                                                  "webDiscoveredCount": sum(report.discovered for report in web_reports),
                                                  "webExtractionCalls": extraction_calls, "webParseFailureCounts": web_failures,
                                                  "webSearchCacheHits": sum(report.search_cache_hits for report in web_reports),
                                                  "webExtractionCacheHits": sum(report.extraction_cache_hits for report in web_reports),
                                                  "aiCandidateCount": ranker.candidate_count, "aiInputBytes": ranker.input_bytes, "timingsMs": timings})

        result = await self.cache.get_or_load(key, load)
        if {"AI_UNAVAILABLE_OR_INVALID", "AI_CIRCUIT_OPEN", "EXTERNAL_REPLENISHMENT_UNAVAILABLE", "WEB_SEARCH_UNAVAILABLE"}.intersection(result.warnings):
            self.cache.forget(key)
        return result.model_copy(deep=True)

    # 앱 종료 시 레시피 전용 DB pool을 해제한다.
    async def close(self) -> None:
        await self.repository.close()
        await self.reasoner.close()
