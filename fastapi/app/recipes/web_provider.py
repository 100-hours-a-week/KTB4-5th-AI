"""Tavily 전체 웹 검색에서 명시적인 재료/조리 구획을 검증한다. 원천 DB 저장 승인은 별도로 판단한다."""

import asyncio
import logging
from dataclasses import dataclass, field
from time import monotonic

from app.adapters._cache import BoundedAsyncCache
from app.config import Settings
from app.errors import AppError
from app.recipes.ai import RecipeReasoner
from app.recipes.preferences import explicit_preference_signal, search_preference_terms
from app.recipes.retrieval import canonical
from app.recipes.schemas import Recipe
from app.recipes.source_parsing import parse_web_recipe as parse_web_recipe
from app.recipes.source_processing import RecipeSourceProcessor
from app.recipes.tavily import DiscoveryHit, TavilyDiscovery

logger = logging.getLogger(__name__)


@dataclass
class WebSearchResult:
    recipes: list[tuple[Recipe, dict]] = field(default_factory=list)
    searches: int = 0
    unavailable: bool = False
    discovered: int = 0
    failure_reasons: dict[str, int] = field(default_factory=dict)
    extraction_calls: int = 0
    extraction_ms: float = 0
    search_cache_hits: int = 0
    extraction_cache_hits: int = 0


class WebRecipeProvider:
    # 전체 웹 검색과 원문 구획 검증을 담당한다. 원천 DB 등록 승인은 서비스 계층에서 별도로 확인한다.
    def __init__(self, settings: Settings, discovery: TavilyDiscovery | None = None, reasoner: RecipeReasoner | None = None) -> None:
        self.settings = settings
        self.discovery = discovery or TavilyDiscovery(settings)
        self.reasoner = reasoner
        self._search_cache = BoundedAsyncCache[list[DiscoveryHit]](16, min(settings.recipe_cache_ttl_seconds, 120))
        self.source_processor = RecipeSourceProcessor(settings, reasoner)

    # 활용되지 않은 임박 재료를 최대 2개 검색한다. 서명 URL·냉장고 ID·재고량은 전달하지 않는다.
    async def search(self, focus: list[str], companions: list[str], timeout_seconds: float = 30, preference_tags: list[str] | None = None) -> WebSearchResult:
        report = WebSearchResult()
        results = {}
        deadline = monotonic() + timeout_seconds
        try:
            async with asyncio.timeout(timeout_seconds):
                for name in list(dict.fromkeys(focus))[:2]:
                    # 재고 전체를 검색 필수 조건처럼 묶지 않는다. 취향 검색은 핵심 임박 재료에 집중한다.
                    companion_terms = [] if preference_tags else [value for value in companions if value != name][:2]
                    query = " ".join([name, *companion_terms, *search_preference_terms(preference_tags or [])[:4], "레시피 재료 분량 조리법"])
                    searched = False
                    async def search_hits(search_query: str = query[:1000]) -> list[DiscoveryHit]:
                        nonlocal searched
                        searched = True
                        report.searches += 1
                        return await self.discovery.search(search_query, 5, fetch_raw_content=True)
                    hits = await self._search_cache.get_or_load(query[:1000], search_hits)
                    if not searched:
                        report.search_cache_hits += 1
                    if not any(hit.rawContent for hit in hits):
                        self._search_cache.forget(query[:1000])
                    report.discovered += len(hits)
                    for hit in hits:
                        outcome = await self.source_processor.process(hit, report.extraction_calls < 2, max(0.0, deadline - monotonic()))
                        recipe, failure = outcome.recipe, outcome.failure
                        report.extraction_calls += outcome.calls
                        report.extraction_ms += outcome.duration_ms
                        report.unavailable = report.unavailable or outcome.unavailable
                        if outcome.reused and recipe:
                            report.extraction_cache_hits += 1
                        if outcome.deferred:
                            logger.info("recipe_source_retry_deferred reason=%s", failure)
                        if recipe:
                            results[(recipe.source, recipe.sourceRecipeId)] = (recipe, {"url": hit.url, "raw_content": hit.rawContent})
                            # 이미 이 재료를 쓰는 취향 후보를 확보했으면 다른 페이지 추출은 생략한다.
                            relevant = any(canonical(item.name) == canonical(name) for item in recipe.ingredients)
                            if relevant and (not preference_tags or explicit_preference_signal(recipe.model_dump(), preference_tags)):
                                break
                        elif failure:
                            report.failure_reasons[failure] = report.failure_reasons.get(failure, 0) + 1
                            logger.info("recipe_web_parse_rejected reason=%s", failure)
        except (AppError, TimeoutError):
            report.unavailable = True
            report.failure_reasons["SEARCH_OR_EXTRACTION_UNAVAILABLE"] = report.failure_reasons.get("SEARCH_OR_EXTRACTION_UNAVAILABLE", 0) + 1
        report.recipes = list(results.values())
        logger.info("recipe_web_source_completed searches=%d discovered_count=%d validated_count=%d unavailable=%s", report.searches, report.discovered, len(results), report.unavailable)
        return report
