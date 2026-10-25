"""레시피 AI 실행 예산·검색 계획·후보 선택과 코드 검증을 관리한다."""

import asyncio
import json
import logging
from time import monotonic

import httpx

from app.errors import AppError
from app.recipes.ai import RecipeReasoner
from app.recipes.ai_contract import PROMPT_VERSION, PlanResult, SelectionResult
from app.recipes.retrieval import canonical, missing_named_ingredients
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
            snapshots = compact_candidates(shortlist)
            payload = {**context, "plan": self.plan.model_dump(), "candidates": snapshots}
            # 개수를 먼저 줄이지 않고 세부 수치·조리문부터 압축하여 10개 후보 공간을 확보한다.
            if len(json.dumps(payload, ensure_ascii=False).encode()) > 24000:
                snapshots = compact_candidates(shortlist, brief=True)
                payload["candidates"] = snapshots
            while len(json.dumps(payload, ensure_ascii=False).encode()) > 24000 and len(snapshots) > 1:
                snapshots.pop()
            if len(snapshots) < len(shortlist):
                self.warnings.append("CANDIDATES_TRUNCATED_TO_BUDGET")
            if len(shortlist) < request.limit:
                self.warnings.append("CANDIDATE_COUNT_BELOW_REQUEST_LIMIT")
            if len(json.dumps(payload, ensure_ascii=False).encode()) > 24000:
                raise ValueError("RECIPE_CONTEXT_TOO_LARGE")
            self.calls += 1
            async with asyncio.timeout(max(0.001, self.deadline - monotonic())), self.slots:
                payload["executionBudgetSeconds"] = max(0.1, self.deadline - monotonic() - 1)
                selected, model = await self.reasoner.select(payload)
            if model != self.model or not isinstance(selected, SelectionResult):
                raise ValueError("RECIPE_MODEL_OR_SCHEMA_MISMATCH")
            result = validate_selections(selected, shortlist[:len(snapshots)], request)
            if snapshots and snapshots[0]["detailsReduced"]:
                result = [row for row in result if row["strategy"] != "SIMPLE_COOKING"]
            if set(request.strategies) - {row["strategy"] for row in result}:
                self.warnings.append("PARTIAL_STRATEGY_COVERAGE")
            return result
        except AI_ERRORS as exc:
            self._fail("SELECT", exc)
            return None

    # 공개 결과에는 고정 경고만, 로그에는 단계·클래스·허용된 내부 원인 코드만 남긴다.
    def _fail(self, stage: str, exc: Exception) -> None:
        allowed_codes = {"UNKNOWN_PLAN_INGREDIENT_ID", "RECIPE_MODEL_OR_SCHEMA_MISMATCH", "UNKNOWN_OR_DUPLICATE_RECIPE_SELECTION", "INVALID_ESSENTIAL_INGREDIENT", "RECIPE_CONTEXT_TOO_LARGE"}
        code = str(exc) if type(exc) is ValueError and str(exc) in allowed_codes else "TIME_BUDGET_EXCEEDED" if isinstance(exc, TimeoutError) else "REMOTE_OR_SCHEMA_ERROR"
        logger.warning("recipe_ai_fallback stage=%s error_type=%s reason_code=%s prompt=%s", stage, type(exc).__name__, code, PROMPT_VERSION)
        self.warnings.append("AI_UNAVAILABLE_OR_INVALID")


# 재료 번호·분량 판정은 한 번만 전달한다. 원본 응답의 재료·조리문은 변경하지 않는다.
def compact_candidates(shortlist: list[dict], brief: bool = False) -> list[dict]:
    result = []
    for index, row in enumerate(shortlist, 1):
        requirements = {r["ingredientNo"]: r for r in row["ingredientRequirements"]}
        ingredients = []
        for item in row["ingredients"]:
            requirement = requirements.get(item["ingredientNo"], {})
            keys = ("status",) if brief else ("requiredAmount", "unit", "shortageAmount", "status")
            ingredients.append({"ingredientNo": item["ingredientNo"], "name": item["name"],
                                **{key: requirement[key] for key in keys if requirement.get(key) is not None}})
        result.append({"candidateId": index, "name": row["name"], "ingredients": ingredients,
                       "usesInventory": row["usesInventory"], "usesExpiringIngredients": row["usesExpiringIngredients"],
                       "missingIngredients": row["missingIngredients"], "quantityStatus": row["quantityStatus"],
                       "missingNamedIngredients": missing_named_ingredients(row["name"], [i["name"] for i in row["ingredients"]], row["missingIngredients"]),
                       "stepCount": len(row["steps"]), "steps": [] if brief else [step["instruction"][:100] for step in row["steps"][:2]],
                       "detailsReduced": brief, "stepsMayBeTruncated": brief or len(row["steps"]) > 2 or any(len(step["instruction"]) > 100 for step in row["steps"])})
    return result


# 핵심 재료 판단은 AI 제안이며, 존재·수량·전략 순서와 부족 여부는 코드가 재검증한다.
def validate_selections(selected: SelectionResult, shortlist: list[dict], request: RecommendationRequest) -> list[dict]:
    rows, seen = [], set()
    for selection in selected.selections:
        key = (selection.strategy, selection.candidateId)
        if key in seen or selection.strategy not in request.strategies or selection.candidateId > len(shortlist):
            raise ValueError("UNKNOWN_OR_DUPLICATE_RECIPE_SELECTION")
        seen.add(key)
        row = dict(shortlist[selection.candidateId - 1])
        essential = list(dict.fromkeys(selection.essentialIngredientNos))
        if not set(essential) <= {i["ingredientNo"] for i in row["ingredients"]}:
            raise ValueError("INVALID_ESSENTIAL_INGREDIENT")
        missing_names = {canonical(name) for name in row["missingIngredients"]}
        missing = [i["ingredientNo"] for i in row["ingredients"] if i["ingredientNo"] in essential and canonical(i["name"]) in missing_names]
        row.update(strategy=selection.strategy, reason=selection.reason, essentialIngredientNos=essential, missingEssentialIngredientNos=missing)
        if selection.strategy == "USE_EXPIRING" and not row["usesExpiringIngredients"]:
            continue
        # 후보 선택은 AI가 하지만 표시 문장은 검증된 사실로만 만든다. 자유 설명의 재료·요리명 환각을 차단한다.
        urgent_text = f"3일 이내 소비기한 재료 {', '.join(row['usesExpiringIngredients'])}가 원문에 포함됩니다." if row["usesExpiringIngredients"] else "이 레시피에 사용되는 임박 재료는 없습니다."
        row["reason"] = (f"보유 재료 {len(row['usesInventory'])}종 사용, 추가로 확인할 재료 {len(row['missingIngredients'])}종입니다. "
                         f"{urgent_text} 원문 조리 {len(row['steps'])}단계이며 분량 상태는 {row['quantityStatus']}입니다.")
        row["feasibility"] = "NEEDS_SHOPPING" if row["missingIngredients"] or missing or row["quantityStatus"] == "SHORTAGE" else "QUANTITY_VERIFIED" if row["quantityStatus"] == "SUFFICIENT" else "NEEDS_CHECK"
        rows.append(row)
    groups = {strategy: sorted([row for row in rows if row["strategy"] == strategy],
                              key=lambda row: (len(missing_named_ingredients(row["name"], [i["name"] for i in row["ingredients"]], row["missingIngredients"])), len(row["missingEssentialIngredientNos"]),
                                               len(row["missingIngredients"]) if strategy == "MINIMIZE_SHOPPING" else 0, row["quantityStatus"] == "SHORTAGE"))
              for strategy in request.strategies}
    result = []
    emitted = set()
    while any(groups.values()) and len(result) < request.limit:
        for strategy in request.strategies:
            if groups[strategy] and len(result) < request.limit:
                row = groups[strategy].pop(0)
                identity = (row["source"], row["sourceRecipeId"])
                if identity not in emitted:
                    emitted.add(identity)
                    result.append(row)
    return result
