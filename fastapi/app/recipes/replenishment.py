"""부족한 원천의 보충만 담당한다. 후보 선정 / 취향 평가와 원천 저장 권한을 분리한다."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from urllib.parse import urlsplit

from app.config import Settings
from app.errors import AppError
from app.recipes.food_safety import FoodSafetyProvider
from app.recipes.preferences import explicit_preference_signal
from app.recipes.repository import RecipeRepository
from app.recipes.retrieval import canonical
from app.recipes.schemas import Recipe
from app.recipes.suitability import ingredient_role
from app.recipes.web_provider import WebRecipeProvider, WebSearchResult


@dataclass
class SupplyResult:
    recipes: list[Recipe] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    web_reports: list[WebSearchResult] = field(default_factory=list)
    food_safety_searched: bool = False


class RecipeSupply:
    # 기존 원천 어댑터를 주입한다. 별도 모델이나 DB 큐를 만들지 않는다.
    def __init__(self, settings: Settings, repository: RecipeRepository, provider: FoodSafetyProvider, web_provider: WebRecipeProvider) -> None:
        self.settings, self.repository, self.provider, self.web_provider = settings, repository, provider, web_provider

    # 하나의 절대 마감 안에서 공식 원천과 웹 원천을 순서대로 보충한다. 시간 초과는 기존 추천을 실패시키지 않는다.
    async def replenish(self, focus: list[str], companions: list[str], tags: list[str], deadline: float, allow_food_safety: bool = True, usable: Callable[[Recipe], bool] | None = None) -> SupplyResult:
        result = SupplyResult()
        if not focus:
            return result
        try:
            async with asyncio.timeout(max(0.001, deadline - monotonic())):
                if allow_food_safety and self.settings.recipe_food_safety_api_key and self.settings.recipe_food_safety_storage_approved:
                    result.food_safety_searched = True
                    try:
                        async with asyncio.timeout(max(0.001, min(3, deadline - monotonic()))):
                            found = await self.provider.search(focus)
                            for recipe, raw in found:
                                await self.repository.save(recipe, raw, "operator-approved:COOKRCP01")
                                result.recipes.append(recipe)
                    except (AppError, TimeoutError):
                        result.warnings.append("EXTERNAL_REPLENISHMENT_UNAVAILABLE")
                elif allow_food_safety:
                    result.warnings.append("EXTERNAL_REPLENISHMENT_NOT_CONFIGURED")
                covered = {canonical(item.name) for recipe in result.recipes for item in recipe.ingredients
                           if (usable is None or usable(recipe)) and ingredient_role(recipe.name, item.name)[0] == "MAIN"
                           and (not tags or explicit_preference_signal(recipe.model_dump(), tags))}
                if set(map(canonical, focus)) <= covered:
                    return result
                if self.settings.recipe_tavily_api_key and deadline - monotonic() >= 1:
                    report = await self.web_provider.search(focus, companions, timeout_seconds=deadline-monotonic(), preference_tags=tags)
                    result.web_reports.append(report)
                    if report.unavailable:
                        result.warnings.append("WEB_SEARCH_UNAVAILABLE")
                    if report.searches and not report.recipes:
                        result.warnings.append("WEB_SEARCH_NO_VERIFIED_RECIPE")
                    for recipe, raw in report.recipes:
                        if recipe.source != "TAVILY_WEB" or not recipe.sourceUrl or raw.get("url") != recipe.sourceUrl:
                            continue
                        if urlsplit(recipe.sourceUrl).hostname in self.settings.recipe_tavily_approved_domains:
                            await self.repository.save(recipe, raw, "operator-approved:TAVILY_WEB")
                        else:
                            result.warnings.append("WEB_SOURCE_NOT_REGISTERED")
                        result.recipes.append(recipe)
                elif not self.settings.recipe_tavily_api_key:
                    result.warnings.append("WEB_SEARCH_NOT_CONFIGURED")
        except TimeoutError:
            result.warnings.append("RECIPE_SUPPLY_BUDGET_EXHAUSTED")
        except AppError:
            result.warnings.append("WEB_SEARCH_UNAVAILABLE")
        return result
