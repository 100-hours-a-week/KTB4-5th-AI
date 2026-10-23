import json
import logging
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.adapters._cache import BoundedAsyncCache
from app.config import Settings
from app.recipes.food_safety import FoodSafetyProvider
from app.recipes.repository import RecipeRepository
from app.recipes.retrieval import allowed, canonical, is_excluded, variants
from app.recipes.schemas import Recipe, RecommendationRequest, RecommendationResponse, normalize

logger = logging.getLogger(__name__)


class RecipeService:
    # CPU 전용 추천 서비스를 조립한다. 분석 큐나 GPU 추론을 사용하지 않는다.
    def __init__(self, settings: Settings, repository: RecipeRepository | None = None, provider: FoodSafetyProvider | None = None) -> None:
        self.repository = repository or RecipeRepository(settings)
        self.provider = provider or FoodSafetyProvider(settings)
        self.cache = BoundedAsyncCache[RecommendationResponse](settings.reference_cache_max_size, settings.recipe_cache_ttl_seconds)

    # 재료 이름·제외 목록·날짜별 캐시를 검증하고 확정 재고 기반 추천을 반환한다.
    async def recommend(self, request: RecommendationRequest) -> RecommendationResponse:
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        excluded = sorted({normalize(name) for name in request.excludedIngredientNames if normalize(name)})
        key = json.dumps(["recipe-alias-policy-v1", request.refrigeratorId, request.inventoryFingerprint, excluded, request.recommendationMode, request.limit, today.isoformat()], ensure_ascii=False)

        # 캐시 미스일 때 DB를 우선 조회하고 후보가 없을 때만 외부 검색·저장을 수행한다.
        async def load() -> RecommendationResponse:
            inventory = [item for item in request.inventory if not is_excluded(item.name, excluded)
                         and (request.recommendationMode == "INCLUDE_EXPIRED" or item.expirationDate >= today)]
            original_names = [item.name for item in inventory]
            names = sorted({canonical(name) for name in original_names})
            expiring = {canonical(item.name) for item in inventory if 0 <= (item.expirationDate - today).days <= 3}

            # 제외 재료 부분 문자열이 포함된 후보도 보수적으로 제외한다. 알레르기 안전 보증은 아니다.
            def usable(recipe: Recipe) -> bool:
                return allowed(recipe, original_names, excluded)

            candidates = [recipe for recipe in await self.repository.find(variants(original_names)) if usable(recipe)] if names else []
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
                missing = set(ingredients) - used
                breakdown = {"expiringIngredient": round(0.4 * len(urgent) / max(1, len(expiring)), 6),
                             "inventoryCoverage": round(0.6 * len(used) / len(ingredients), 6),
                             "missingIngredientPenalty": round(0.2 * len(missing) / len(ingredients), 6), "variety": 0.0}
                score = round(max(0, min(1, breakdown["expiringIngredient"] + breakdown["inventoryCoverage"] - breakdown["missingIngredientPenalty"])), 6)
                data = recipe.model_dump(mode="json", exclude={"status"})
                data.update(score=score, scoreBreakdown=breakdown, usesInventory=[ingredients[name] for name in sorted(used)],
                            usesExpiringIngredients=[ingredients[name] for name in sorted(urgent)], missingIngredients=[ingredients[name] for name in sorted(missing)],
                            excludedIngredientCheck="PASSED", reason=f"보유 재료 {len(used)}종 사용, 3일 내 소비기한 재료 {len(urgent)}종, 추가 재료 {len(missing)}종입니다.")
                ranked.append(data)
            ranked.sort(key=lambda item: (-item["score"], item["source"], item["sourceRecipeId"]))
            logger.info("recipe_recommendation_completed source=%s candidate_count=%d returned_count=%d", source, len(ranked), min(len(ranked), request.limit))
            return RecommendationResponse(recommendationId=f"rec_{uuid4().hex}", refrigeratorId=request.refrigeratorId,
                                          inventoryFingerprint=request.inventoryFingerprint, recommendationMode=request.recommendationMode,
                                          recommendations=ranked[:request.limit], generatedAt=datetime.now(UTC))

        return (await self.cache.get_or_load(key, load)).model_copy(deep=True)

    # 앱 종료 시 레시피 전용 DB pool을 해제한다.
    async def close(self) -> None:
        await self.repository.close()
