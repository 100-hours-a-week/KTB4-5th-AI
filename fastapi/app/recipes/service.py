import asyncio
import json
import logging
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.adapters._cache import BoundedAsyncCache
from app.config import Settings
from app.recipes.ai import RecipeReasoner
from app.recipes.ai_contract import PROMPT_VERSION
from app.recipes.evaluation import compare_rankings
from app.recipes.food_safety import FoodSafetyProvider
from app.recipes.planning import POLICY_VERSION, MealRanker, build_baseline
from app.recipes.quantities import assess_quantities
from app.recipes.repository import RecipeRepository
from app.recipes.retrieval import TITLE_MATCH_VERSION, allowed, canonical, is_excluded, missing_named_ingredients, variants
from app.recipes.schemas import Recipe, RecommendationRequest, RecommendationResponse, normalize
from app.recipes.suitability import WATER_NAMES, assess_suitability, ingredient_role

logger = logging.getLogger(__name__)


class RecipeService:
    # DB·원천·원격 Gemma를 분리한다. 사진 분석 큐와 백엔드 재고는 수정하지 않는다.
    def __init__(self, settings: Settings, repository: RecipeRepository | None = None, provider: FoodSafetyProvider | None = None, reasoner: RecipeReasoner | None = None) -> None:
        self.settings = settings
        self.reasoner = reasoner or RecipeReasoner(settings)
        self._ai_slots = asyncio.Semaphore(2)
        self.repository = repository or RecipeRepository(settings)
        self.provider = provider or FoodSafetyProvider(settings)
        self.cache = BoundedAsyncCache[RecommendationResponse](settings.reference_cache_max_size, settings.recipe_cache_ttl_seconds)

    # 재료 이름·제외 목록·날짜별 캐시를 검증하고 확정 재고 기반 추천을 반환한다.
    async def recommend(self, request: RecommendationRequest) -> RecommendationResponse:
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        excluded = sorted({normalize(name) for name in request.excludedIngredientNames if normalize(name)})
        requested_pantry = sorted({canonical(name) for name in request.pantryIngredientNames if not is_excluded(name, excluded)})
        pantry = sorted({canonical(name) for name in request.pantryIngredientNames
                         if not is_excluded(name, excluded) and ingredient_role("", name)[0] == "SEASONING"})
        key = json.dumps([POLICY_VERSION, TITLE_MATCH_VERSION, PROMPT_VERSION, self.settings.ollama_model, self.settings.recipe_ai_enabled, request.refrigeratorId,
                          request.inventoryFingerprint, excluded, request.recommendationMode, request.limit, request.targetServings,
                          request.strategies, requested_pantry, self.settings.recipe_ai_max_candidates, today.isoformat()], ensure_ascii=False)

        # 캐시 미스일 때 DB를 우선 조회하고 후보가 없을 때만 외부 검색·저장을 수행한다.
        async def load() -> RecommendationResponse:
            inventory = [item for item in request.inventory if not is_excluded(item.name, excluded)
                         and canonical(item.name) not in WATER_NAMES
                         and (request.recommendationMode == "INCLUDE_EXPIRED" or item.expirationDate >= today)]
            original_names = [item.name for item in inventory]
            names = sorted({canonical(name) for name in original_names})
            expiring = {canonical(item.name) for item in inventory if 0 <= (item.expirationDate - today).days <= 3}
            ranker = MealRanker(self.reasoner, self.settings.recipe_ai_timeout_seconds, self._ai_slots, self.settings.ollama_model)
            context = {"today": today.isoformat(), "inventory": [item.model_dump(mode="json") for item in inventory],
                       "targetServings": request.targetServings, "strategies": request.strategies, "excludedIngredientNames": excluded,
                       "pantryIngredientNames": pantry, "limit": request.limit}
            context["expiryPolicy"] = {"urgentWithinDays": 3, "expiringIngredientIds": [item.ingredientId for item in inventory if 0 <= (item.expirationDate - today).days <= 3]}
            if inventory and not self.settings.recipe_ai_enabled:
                ranker.warnings.append("AI_DISABLED")
            if any(not is_excluded(name, excluded) and ingredient_role("", name)[0] not in {"SEASONING", "WATER"} for name in request.pantryIngredientNames):
                ranker.warnings.append("UNREVIEWED_PANTRY_INGREDIENTS_IGNORED")

            # 제외 재료 부분 문자열이 포함된 후보도 보수적으로 제외한다. 알레르기 안전 보증은 아니다.
            def usable(recipe: Recipe) -> bool:
                return allowed(recipe, original_names, excluded)

            candidates = [recipe for recipe in await self.repository.find(variants(original_names)) if usable(recipe)] if names else []
            searches = 1 if names else 0
            candidates = list({(r.source, r.sourceRecipeId): r for r in candidates}.values())
            source = "DATABASE"
            if names and not candidates:
                source = "FOOD_SAFETY_KOREA"
                found = await self.provider.search([item.name for item in inventory])
                for recipe, raw in found:
                    await self.repository.save(recipe, raw, "operator-approved:COOKRCP01")
                candidates = [recipe for recipe, _ in found if usable(recipe)]
            ranked = []
            for recipe in candidates:
                ingredients = {}
                for item in recipe.ingredients:
                    ingredients.setdefault(canonical(item.name), item.name)
                used = set(ingredients).intersection(names)
                urgent = used.intersection(expiring)
                missing = set(ingredients) - used - set(pantry) - WATER_NAMES
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
                data.update(assess_quantities(recipe, inventory, request.targetServings, pantry))
                data["_suitability"] = assess_suitability(data, original_names)
                ranked.append(data)
            ranked.sort(key=lambda item: (-item["score"], item["source"], item["sourceRecipeId"]))
            baseline = build_baseline(ranked, request)
            missing_main_count = sum(bool(row["_suitability"]["missingMain"]) for row in ranked)
            unresolved_count = sum(bool(row["_suitability"]["unresolvedMissing"]) for row in ranked)
            if missing_main_count:
                ranker.warnings.append("MISSING_MAIN_INGREDIENT_RECIPES_EXCLUDED")
            if unresolved_count:
                ranker.warnings.append("UNREVIEWED_MISSING_INGREDIENT_RECIPES_EXCLUDED")
            shortlist = baseline[:self.settings.recipe_ai_max_candidates]
            selected = await ranker.rank(context, shortlist) if self.settings.recipe_ai_enabled and len(shortlist) > 1 else None
            if self.settings.recipe_ai_enabled and len(shortlist) == 1:
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
            result = ((selected + baseline[len(shortlist):]) if selected is not None else baseline)[:request.limit]
            if selected is not None and len(result) > len(shortlist):
                ranker.warnings.append("AI_RANKED_PREFIX_ONLY")
            if set(request.strategies) - {row["strategy"] for row in result} and result:
                ranker.warnings.append("PARTIAL_STRATEGY_COVERAGE")
            if selected is not None:
                logger.info("recipe_rank_comparison metrics=%s", json.dumps(compare_rankings(baseline, result, request.limit), separators=(",", ":")))
            logger.info("recipe_recommendation_completed engine=%s source=%s ai_calls=%d search_count=%d candidate_count=%d returned_count=%d",
                        engine, source, ranker.calls, searches, len(ranked), len(result))
            logger.info("recipe_candidate_filter missing_main_count=%d unreviewed_missing_count=%d eligible_count=%d", missing_main_count, unresolved_count, len(baseline))
            return RecommendationResponse(recommendationId=f"rec_{uuid4().hex}", refrigeratorId=request.refrigeratorId,
                                          inventoryFingerprint=request.inventoryFingerprint, recommendationMode=request.recommendationMode,
                                          recommendations=[{key: value for key, value in row.items() if not key.startswith("_")} for row in result],
                                          generatedAt=datetime.now(UTC), recommendationEngine=engine,
                                          mealPlan={}, warnings=list(dict.fromkeys(ranker.warnings)),
                                          trace={"policyVersion": POLICY_VERSION, "model": ranker.model, "promptVersion": PROMPT_VERSION if ranker.calls else None,
                                                 "aiCalls": ranker.calls, "searchCount": searches, "candidateCount": len(ranked)})

        result = await self.cache.get_or_load(key, load)
        if "AI_UNAVAILABLE_OR_INVALID" in result.warnings:
            self.cache.forget(key)
        return result.model_copy(deep=True)

    # 앱 종료 시 레시피 전용 DB pool을 해제한다.
    async def close(self) -> None:
        await self.repository.close()
        await self.reasoner.close()
