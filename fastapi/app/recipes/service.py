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
from app.recipes.food_safety import FoodSafetyProvider
from app.recipes.planning import MealPlanner
from app.recipes.quantities import assess_quantities
from app.recipes.repository import RecipeRepository
from app.recipes.retrieval import allowed, canonical, is_excluded, variants
from app.recipes.schemas import Recipe, RecommendationRequest, RecommendationResponse, normalize

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
        pantry = sorted({canonical(name) for name in request.pantryIngredientNames if not is_excluded(name, excluded)})
        key = json.dumps(["recipe-meal-plan-v2", PROMPT_VERSION, self.settings.ollama_model, self.settings.recipe_ai_enabled, request.refrigeratorId,
                          request.inventoryFingerprint, excluded, request.recommendationMode, request.limit, request.targetServings,
                          request.strategies, pantry, self.settings.recipe_ai_max_candidates, today.isoformat()], ensure_ascii=False)

        # 캐시 미스일 때 DB를 우선 조회하고 후보가 없을 때만 외부 검색·저장을 수행한다.
        async def load() -> RecommendationResponse:
            inventory = [item for item in request.inventory if not is_excluded(item.name, excluded)
                         and (request.recommendationMode == "INCLUDE_EXPIRED" or item.expirationDate >= today)]
            original_names = [item.name for item in inventory]
            names = sorted({canonical(name) for name in original_names})
            expiring = {canonical(item.name) for item in inventory if 0 <= (item.expirationDate - today).days <= 3}
            planner = MealPlanner(self.reasoner, self.settings.recipe_ai_timeout_seconds, self._ai_slots)
            context = {"today": today.isoformat(), "inventory": [item.model_dump(mode="json") for item in inventory],
                       "targetServings": request.targetServings, "strategies": request.strategies, "excludedIngredientNames": excluded,
                       "pantryIngredientNames": pantry, "limit": request.limit}
            plan = await planner.prepare(context) if inventory and self.settings.recipe_ai_enabled else None
            if inventory and not self.settings.recipe_ai_enabled:
                planner.warnings.append("AI_DISABLED")

            # 제외 재료 부분 문자열이 포함된 후보도 보수적으로 제외한다. 알레르기 안전 보증은 아니다.
            def usable(recipe: Recipe) -> bool:
                return allowed(recipe, original_names, excluded)

            focus = [item.name for item in inventory if item.ingredientId in plan.focusIngredientIds] if plan else original_names
            candidates = [recipe for recipe in await self.repository.find(variants(focus)) if usable(recipe)] if names else []
            searches = 1 if names else 0
            if plan and {canonical(name) for name in focus} != set(names):
                candidates += [recipe for recipe in await self.repository.find(variants(original_names)) if usable(recipe)]
                searches += 1
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
                missing = set(ingredients) - used - set(pantry)
                breakdown = {"expiringIngredient": round(0.4 * len(urgent) / max(1, len(expiring)), 6),
                             "inventoryCoverage": round(0.6 * len(used) / len(ingredients), 6),
                             "missingIngredientPenalty": round(0.2 * len(missing) / len(ingredients), 6), "variety": 0.0}
                score = round(max(0, min(1, breakdown["expiringIngredient"] + breakdown["inventoryCoverage"] - breakdown["missingIngredientPenalty"])), 6)
                data = recipe.model_dump(mode="json", exclude={"status"})
                data.update(score=score, scoreBreakdown=breakdown, usesInventory=[ingredients[name] for name in sorted(used)],
                            usesExpiringIngredients=[ingredients[name] for name in sorted(urgent)], missingIngredients=[ingredients[name] for name in sorted(missing)],
                            excludedIngredientCheck="PASSED", reason=f"보유 재료 {len(used)}종 사용, 3일 내 소비기한 재료 {len(urgent)}종, 추가 재료 {len(missing)}종입니다.")
                data.update(assess_quantities(recipe, inventory, request.targetServings, pantry))
                ranked.append(data)
            ranked.sort(key=lambda item: (-item["score"], item["source"], item["sourceRecipeId"]))
            selected = await planner.select(context, ranked[:self.settings.recipe_ai_max_candidates], request) if plan and ranked else None
            engine = "AI" if selected is not None else "RULE_FALLBACK" if ranked else "EMPTY"
            if engine == "RULE_FALLBACK":
                planner.warnings.append("RULE_RESULTS_NOT_AI_JUDGEMENT")
            if selected == []:
                planner.warnings.append("NO_SUITABLE_AI_CANDIDATES")
            if request.recommendationMode == "INCLUDE_EXPIRED" and any(item.expirationDate < today for item in inventory):
                planner.warnings.append("EXPIRED_INVENTORY_INCLUDED_NOT_SAFETY_APPROVAL")
            result = selected if selected is not None else ranked[:request.limit]
            logger.info("recipe_recommendation_completed engine=%s source=%s ai_calls=%d search_count=%d candidate_count=%d returned_count=%d",
                        engine, source, planner.calls, searches, len(ranked), len(result))
            return RecommendationResponse(recommendationId=f"rec_{uuid4().hex}", refrigeratorId=request.refrigeratorId,
                                          inventoryFingerprint=request.inventoryFingerprint, recommendationMode=request.recommendationMode,
                                          recommendations=result, generatedAt=datetime.now(UTC), recommendationEngine=engine,
                                          mealPlan=plan.model_dump() if plan else {}, warnings=list(dict.fromkeys(planner.warnings)),
                                          trace={"model": planner.model, "promptVersion": PROMPT_VERSION if planner.calls else None,
                                                 "aiCalls": planner.calls, "searchCount": searches, "candidateCount": len(ranked)})

        result = await self.cache.get_or_load(key, load)
        if result.recommendationEngine == "RULE_FALLBACK" and self.settings.recipe_ai_enabled:
            self.cache.forget(key)
        return result.model_copy(deep=True)

    # 앱 종료 시 레시피 전용 DB pool을 해제한다.
    async def close(self) -> None:
        await self.repository.close()
        await self.reasoner.close()
