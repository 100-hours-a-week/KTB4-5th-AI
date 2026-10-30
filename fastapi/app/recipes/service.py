import asyncio
import json
import logging
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.config import Settings
from app.recipes.ai import RecipeReasoner
from app.recipes.ai_contract import PROMPT_VERSION
from app.recipes.candidate_pool import coverage_counts, representative_candidates
from app.recipes.context import recommendation_context
from app.recipes.diagnostics import candidate_diagnostics
from app.recipes.evaluation import compare_rankings
from app.recipes.food_safety import FoodSafetyProvider
from app.recipes.planning import POLICY_VERSION, MealRanker, build_baseline, vary_baseline
from app.recipes.preferences import preference_search_focus, preference_shortlist
from app.recipes.quantities import assess_quantities
from app.recipes.reliability import PreferenceDecisionCache, RecipeCircuit
from app.recipes.replenishment import RecipeSupply
from app.recipes.repository import RecipeRepository
from app.recipes.result_cache import RecipeResultCache, recommendation_key
from app.recipes.retrieval import allowed, canonical, is_excluded, missing_named_ingredients, query_variants
from app.recipes.schemas import Recipe, RecommendationRequest, RecommendationResponse, normalize
from app.recipes.selection import select_recommendations
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
        self.cache = RecipeResultCache(settings.reference_cache_max_size, settings.recipe_cache_ttl_seconds)

    # 재료 이름·제외 목록·날짜별 캐시를 검증하고 확정 재고 기반 추천을 반환한다.
    async def recommend(self, request: RecommendationRequest) -> RecommendationResponse:
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        excluded = sorted({normalize(name) for name in request.excludedIngredientNames if normalize(name)})
        pantry = sorted({canonical(name) for name in [*DEFAULT_PANTRY_NAMES, *request.pantryIngredientNames]
                         if not is_excluded(name, excluded) and ingredient_role("", name)[0] == "SEASONING"})
        key = recommendation_key(request, self.settings, POLICY_VERSION)
        cache_started = monotonic()

        # 캐시 미스 시 DB를 우선 조회하고 부족하면 승인된 원천을 최대 한 번 보충한다.
        async def load() -> RecommendationResponse:
            started = monotonic()
            web_reports = []
            deadline = started + self.settings.recipe_ai_timeout_seconds
            supply_used = False
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
                       "recommendationContext": recommendation_context(request.requestId if not request.preferenceTags else key), "seasoningPolicy": "IGNORE_FOR_SHOPPING_KEEP_SOURCE"}
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

            # 공급은 별도 책임으로 위임하고 같은 추천 작업에서는 한 번만 실행한다.
            async def supplement(focus_names: list[str], allow_food_safety: bool = True) -> bool:
                nonlocal supply_used, ranked, baseline, source, external_searched, web_searches
                if supply_used or not focus_names or deadline - monotonic() < 1:
                    return False
                supply_used = True
                supplier = RecipeSupply(self.settings, self.repository, self.provider, self.web_provider)
                supplied = await supplier.replenish(focus_names, search_names, request.preferenceTags,
                                                     min(deadline, monotonic() + self.settings.recipe_supply_timeout_seconds), allow_food_safety, usable=usable)
                ranker.warnings.extend(supplied.warnings)
                external_searched = supplied.food_safety_searched
                web_reports.extend(supplied.web_reports)
                web_searches += sum(report.searches for report in supplied.web_reports)
                merged = {(row["source"], row["sourceRecipeId"]): row for row in ranked}
                added = False
                for recipe in supplied.recipes:
                    if usable(recipe):
                        merged[(recipe.source, recipe.sourceRecipeId)] = score_candidate(recipe)
                        added = True
                ranked = list(merged.values())
                baseline = build_baseline(ranked, matched_request)
                if supplied.food_safety_searched:
                    source += "_AND_FOOD_SAFETY_KOREA"
                if supplied.web_reports:
                    source += "_AND_TAVILY_WEB"
                return added

            # 미평가를 부재로 취급하지 않는다. 원천 자체에 후보가 없는 임박 재료만 우선 보충한다.
            focus = uncovered(baseline) if "USE_EXPIRING" in request.strategies else []
            if names and (not baseline or focus):
                await supplement(focus or search_names[:2])
            shortlist = preference_shortlist(baseline, request.preferenceTags, self.settings.recipe_ai_max_candidates)
            if not request.preferenceTags:
                baseline = vary_baseline(baseline, context["recommendationContext"]["variationSeed"])
                shortlist = representative_candidates(baseline, min(12, self.settings.recipe_ai_max_candidates))
            ranker.timeout = max(0.1, min(self.settings.recipe_ai_rank_timeout_seconds, self.settings.recipe_ai_timeout_seconds - (monotonic() - started)))
            can_call_ai = self.settings.recipe_ai_enabled and bool(shortlist) and (len(shortlist) > 1 or bool(request.preferenceTags))
            selected = await ranker.rank(context, shortlist) if can_call_ai else None
            if selected is not None:
                ranker.hydrate(context, baseline)
            # 같은 재료의 후보를 모두 정상 평가한 뒤에도 취향 일치가 없을 때만 웹 보충한다.
            supported = ranker.preference_matches
            valid_evaluated = ranker.evaluated_identities - ranker.rejected_preference_identities - set(ranker.rejected_preference_tags)
            preference_focus = preference_search_focus(inventory, baseline, request.preferenceTags, supported)
            proven_focus = []
            for name in preference_focus:
                relevant = {(row["source"], row["sourceRecipeId"]) for row in baseline
                            if canonical(name) in {canonical(value) for value in row["usesExpiringMainIngredients"]}}
                if relevant and relevant <= valid_evaluated:
                    proven_focus.append(name)
            if request.preferenceTags and proven_focus and await supplement(proven_focus, allow_food_safety=False):
                shortlist = preference_shortlist(baseline, request.preferenceTags, self.settings.recipe_ai_max_candidates)
                ranker.timeout = max(0.1, min(self.settings.recipe_ai_rank_timeout_seconds, deadline - monotonic()))
                selected = await ranker.rank(context, shortlist) if self.settings.recipe_ai_enabled and deadline - monotonic() >= 1 else selected
            if selected is not None:
                ranker.hydrate(context, baseline)
            confirmation_rows = build_baseline(ranked, matched_request, confirmation_only=True)
            confirmations = confirmation_rows[:3]
            if confirmations:
                ranker.warnings.append("CONFIRMATION_CANDIDATES_SEPARATED")
            missing_main_count = sum(bool(row["_suitability"]["missingMain"]) for row in ranked)
            unresolved_count = sum(bool(row["_suitability"]["unresolvedMissing"]) for row in ranked)
            if missing_main_count:
                ranker.warnings.append("MISSING_INGREDIENTS_REQUIRE_SHOPPING")
            if unresolved_count:
                ranker.warnings.append("UNREVIEWED_MISSING_INGREDIENTS_NEED_CHECK")
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
                    row["reason"] += " 요청 취향에 맞는 원문 근거가 확인되지 않아 취향 일치 후보보다 후순위입니다."
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
            # 최종 순위는 현재 원천 / 판정 스냅샷만으로 한 번 선정한다. 오래된 모델 순열의 행 참조를 재사용하지 않는다.
            ordered = baseline
            result = select_recommendations(ordered, matched_request, context["recommendationContext"]["variationSeed"])
            if any(row["missingIngredients"] for row in result):
                ranker.warnings.append("SHOPPING_COST_UNVERIFIED")
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
            return RecommendationResponse(recommendationId=f"rec_{uuid4().hex}", userId=request.userId, refrigeratorId=request.refrigeratorId,
                                          inventoryFingerprint=request.inventoryFingerprint, recommendationMode=request.recommendationMode,
                                          recommendations=[{key: value for key, value in row.items() if not key.startswith("_")} for row in result],
                                          confirmationCandidates=[{key: value for key, value in row.items() if not key.startswith("_")} for row in confirmations],
                                          uncoveredExpiringIngredients=focus,
                                          generatedAt=datetime.now(UTC), recommendationEngine=engine,
                                          warnings=list(dict.fromkeys(ranker.warnings)), preferenceTags=request.preferenceTags,
                                          context=context["recommendationContext"],
                                          trace={"policyVersion": POLICY_VERSION, "model": ranker.model, "promptVersion": PROMPT_VERSION if ranker.calls or extraction_calls or ranker.preference_cache_hits else None,
                                                 "preferenceDecisionCacheHits": ranker.preference_cache_hits,
                                                 "aiCalls": ranker.calls + extraction_calls, "searchCount": searches, "candidateCount": len(ranked),
                                                  "externalSearchCount": int(external_searched), "webSearchCount": web_searches,
                                                  "webDiscoveredCount": sum(report.discovered for report in web_reports),
                                                  "webExtractionCalls": extraction_calls, "webParseFailureCounts": web_failures,
                                                  "webSearchCacheHits": sum(report.search_cache_hits for report in web_reports),
                                                  "webExtractionCacheHits": sum(report.extraction_cache_hits for report in web_reports),
                                                  "candidateCoverage": coverage_counts(baseline, shortlist, evaluated, result),
                                                  "aiCandidateCount": ranker.candidate_count, "aiInputBytes": ranker.input_bytes, "timingsMs": timings})

        result, cached = await self.cache.get_or_load(key, load)
        # 캐시는 추천 내용만 재사용한다. 현재 작업이 모델 / 검색을 다시 실행한 것처럼 표시하지 않는다.
        if cached:
            result.trace.recommendationCacheHit = True
            for field in ("aiCalls", "searchCount", "externalSearchCount", "webSearchCount", "webExtractionCalls", "webDiscoveredCount",
                          "aiCandidateCount", "aiInputBytes", "preferenceDecisionCacheHits", "webSearchCacheHits", "webExtractionCacheHits"):
                setattr(result.trace, field, 0)
            result.trace.timingsMs = {"recommendationMs": round((monotonic() - cache_started) * 1000, 3)}
            result.warnings = list(dict.fromkeys([*result.warnings, "RECOMMENDATION_SNAPSHOT_REUSED"]))
        result.preferenceTags = list(request.preferenceTags)
        for row in [*result.recommendations, *result.confirmationCandidates]:
            by_tag = {item.tag: item for item in row.preferenceAssessments}
            row.preferenceAssessments = [by_tag[tag] for tag in request.preferenceTags if tag in by_tag]
        logger.info("recipe_snapshot_resolved request_id=%s cache_hit=%s", request.requestId, cached)
        return result

    # 앱 종료 시 레시피 전용 DB pool을 해제한다.
    async def close(self) -> None:
        await self.repository.close()
        await self.reasoner.close()
