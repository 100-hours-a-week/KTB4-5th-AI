"""레시피 AI 실행 예산·검색 계획·후보 선택과 코드 검증을 관리한다."""

import asyncio
import logging
from time import monotonic

import httpx

from app.errors import AppError
from app.recipes.ai import RecipeReasoner
from app.recipes.ai_contract import PROMPT_VERSION, PlanResult, SelectionResult
from app.recipes.retrieval import canonical
from app.recipes.schemas import RecommendationRequest

logger = logging.getLogger(__name__)
AI_ERRORS = (AppError, httpx.HTTPError, TimeoutError, ValueError, TypeError)


class MealPlanner:
    # 요청 하나의 계획과 선택은 합산 시간 예산을 공유하며 모델 호출은 최대 두 번이다.
    def __init__(self, reasoner: RecipeReasoner, timeout: float, slots: asyncio.Semaphore) -> None:
        self.reasoner, self.slots = reasoner, slots
        self.deadline = monotonic() + timeout
        self.plan: PlanResult | None = None
        self.model: str | None = None
        self.calls = 0
        self.warnings: list[str] = []

    # 재고 ID만 고를 수 있는 계획을 검증한다. 새 재료·소비기한은 모델이 만들 수 없다.
    async def prepare(self, context: dict) -> PlanResult | None:
        try:
            self.calls += 1
            async with asyncio.timeout(max(0.001, self.deadline - monotonic())), self.slots:
                plan, model = await self.reasoner.plan(context)
            ids = {item["ingredientId"] for item in context["inventory"]}
            if not isinstance(plan, PlanResult) or len(set(plan.focusIngredientIds)) != len(plan.focusIngredientIds) or not set(plan.focusIngredientIds) <= ids:
                raise ValueError("UNKNOWN_PLAN_INGREDIENT_ID")
            self.plan, self.model = plan, model
        except AI_ERRORS as exc:
            self._fail("PLAN", exc)
        return self.plan

    # 제한된 실제 후보와 분량 검증 결과를 제시하고 기존 ID·원문 재료 번호만 수용한다.
    async def select(self, context: dict, shortlist: list[dict], request: RecommendationRequest) -> list[dict] | None:
        try:
            snapshots = [{"candidateId": index, "name": row["name"], "baseServings": row["baseServings"],
                          "ingredients": row["ingredients"], "quantityStatus": row["quantityStatus"], "ingredientRequirements": row["ingredientRequirements"],
                          "steps": [step["instruction"][:700] for step in row["steps"][:12]],
                          "stepsMayBeTruncated": len(row["steps"]) > 12 or any(len(step["instruction"]) > 700 for step in row["steps"])}
                         for index, row in enumerate(shortlist, 1)]
            self.calls += 1
            async with asyncio.timeout(max(0.001, self.deadline - monotonic())), self.slots:
                selected, model = await self.reasoner.select({**context, "plan": self.plan.model_dump(), "candidates": snapshots})
            if model != self.model or not isinstance(selected, SelectionResult):
                raise ValueError("RECIPE_MODEL_OR_SCHEMA_MISMATCH")
            return validate_selections(selected, shortlist, request)
        except AI_ERRORS as exc:
            self._fail("SELECT", exc)
            return None

    # 공개 결과에는 고정 경고 코드만, 로그에는 단계·오류 클래스만 남긴다.
    def _fail(self, stage: str, exc: Exception) -> None:
        logger.warning("recipe_ai_fallback stage=%s error_type=%s prompt=%s", stage, type(exc).__name__, PROMPT_VERSION)
        self.warnings.append("AI_UNAVAILABLE_OR_INVALID")


# 핵심 재료 판단은 AI 제안이며, 존재·수량·전략 순서와 부족 여부는 코드가 재검증한다.
def validate_selections(selected: SelectionResult, shortlist: list[dict], request: RecommendationRequest) -> list[dict]:
    rows, seen = [], set()
    for selection in selected.selections:
        key = (selection.strategy, selection.candidateId)
        if key in seen or selection.strategy not in request.strategies or selection.candidateId > len(shortlist):
            raise ValueError("UNKNOWN_OR_DUPLICATE_RECIPE_SELECTION")
        seen.add(key)
        row = dict(shortlist[selection.candidateId - 1])
        essential = selection.essentialIngredientNos
        if len(set(essential)) != len(essential) or not set(essential) <= {i["ingredientNo"] for i in row["ingredients"]}:
            raise ValueError("INVALID_ESSENTIAL_INGREDIENT")
        missing_names = {canonical(name) for name in row["missingIngredients"]}
        missing = [i["ingredientNo"] for i in row["ingredients"] if i["ingredientNo"] in essential and canonical(i["name"]) in missing_names]
        row.update(strategy=selection.strategy, reason=selection.reason, essentialIngredientNos=essential, missingEssentialIngredientNos=missing)
        row["feasibility"] = "NEEDS_SHOPPING" if missing or row["quantityStatus"] == "SHORTAGE" else "QUANTITY_VERIFIED" if row["quantityStatus"] == "SUFFICIENT" else "NEEDS_CHECK"
        rows.append(row)
    groups = {strategy: sorted([row for row in rows if row["strategy"] == strategy], key=lambda row: (bool(row["missingEssentialIngredientNos"]), row["quantityStatus"] == "SHORTAGE"))
              for strategy in request.strategies}
    result = []
    while any(groups.values()) and len(result) < request.limit:
        for strategy in request.strategies:
            if groups[strategy] and len(result) < request.limit:
                result.append(groups[strategy].pop(0))
    return result
