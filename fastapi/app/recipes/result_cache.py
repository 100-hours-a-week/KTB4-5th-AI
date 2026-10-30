"""요청 멱등키와 추천 내용의 캐시 키를 분리하고 같은 스냅샷의 동시 생성을 합친다."""

import asyncio
import hashlib
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from app.config import Settings
from app.recipes.ai_contract import PROMPT_VERSION
from app.recipes.context import recommendation_context
from app.recipes.retrieval import MATCH_VERSION, TITLE_MATCH_VERSION
from app.recipes.schemas import RecommendationRequest, RecommendationResponse


# userId / refrigeratorId가 포함된 본문으로 사용자별 캐시를 분리한다. 새 requestId는 취향 캐시를 무효화하지 않는다.
def recommendation_key(request: RecommendationRequest, settings: Settings, policy: str) -> str:
    body = request.model_dump(mode="json", exclude={"requestId"})
    body["preferenceTags"] = sorted(request.preferenceTags)
    body["recentRecipeIds"] = sorted(request.recentRecipeIds)
    context = recommendation_context(request.requestId)
    if request.preferenceTags:
        context.pop("variationSeed")
    config = [policy, MATCH_VERSION, TITLE_MATCH_VERSION, PROMPT_VERSION, settings.ollama_model, settings.recipe_ai_enabled,
              settings.recipe_ai_max_candidates, settings.recipe_ai_input_max_bytes, settings.recipe_ai_rank_timeout_seconds,
              settings.recipe_supply_timeout_seconds, bool(settings.recipe_tavily_api_key), sorted(settings.recipe_tavily_approved_domains),
              bool(settings.recipe_food_safety_api_key), settings.recipe_food_safety_storage_approved]
    return hashlib.sha256(json.dumps([body, context, config], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass
class ResultFlight:
    task: asyncio.Task[RecommendationResponse]
    waiters: int = 0


class RecipeResultCache:
    # 완료 결과만 TTL 동안 유지한다. 장애 fallback은 고정하지 않으며 다른 replica와 공유하지 않는다.
    def __init__(self, max_size: int, ttl_seconds: float) -> None:
        self.max_size, self.ttl = max_size, ttl_seconds
        self.entries: OrderedDict[str, tuple[float, RecommendationResponse]] = OrderedDict()
        self.flights: dict[str, ResultFlight] = {}

    # 로더는 키당 한 번 실행한다. 모든 호출자가 취소되면 생성 중인 추천도 취소한다.
    async def get_or_load(self, key: str, loader: Callable[[], Awaitable[RecommendationResponse]]) -> tuple[RecommendationResponse, bool]:
        entry = self.entries.get(key)
        if entry and monotonic() < entry[0]:
            self.entries.move_to_end(key)
            return entry[1].model_copy(deep=True), True
        self.entries.pop(key, None)
        flight = self.flights.get(key)
        reused = flight is not None
        if flight is None:
            flight = ResultFlight(asyncio.create_task(self._load(key, loader)))
            self.flights[key] = flight
        flight.waiters += 1
        try:
            result = await asyncio.shield(flight.task)
            return result.model_copy(deep=True), reused
        finally:
            flight.waiters -= 1
            if flight.waiters == 0:
                if not flight.task.done():
                    flight.task.cancel()
                    try:
                        await flight.task
                    except asyncio.CancelledError:
                        pass
                self.flights.pop(key, None)

    # 웹 보충 실패가 있어도 검증된 AI 추천은 고정한다. 생성된 목록을 나중에 조용히 덮어쓰지 않는다.
    async def _load(self, key: str, loader: Callable[[], Awaitable[RecommendationResponse]]) -> RecommendationResponse:
        result = await loader()
        failed_rank = {"AI_UNAVAILABLE_OR_INVALID", "AI_CIRCUIT_OPEN", "AI_PREFERENCE_EVIDENCE_REJECTED"}.intersection(result.warnings)
        failed_fallback = result.recommendationEngine != "AI" and {"EXTERNAL_REPLENISHMENT_UNAVAILABLE", "WEB_SEARCH_UNAVAILABLE",
                                                                  "RECIPE_SUPPLY_BUDGET_EXHAUSTED"}.intersection(result.warnings)
        if result.recommendations and not failed_rank and not failed_fallback:
            self.entries[key] = (monotonic() + self.ttl, result.model_copy(deep=True))
            self.entries.move_to_end(key)
            while len(self.entries) > self.max_size:
                self.entries.popitem(last=False)
        return result
