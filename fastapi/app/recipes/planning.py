"""검증된 레시피의 기본 추천을 구성하고 단일 AI 호출로 순서만 조정한다."""

import asyncio
import json
import logging
from time import monotonic

import httpx

from app.errors import AppError
from app.recipes.ai import RecipeReasoner
from app.recipes.ai_contract import PROMPT_VERSION, RankResult
from app.recipes.retrieval import canonical, missing_named_ingredients
from app.recipes.schemas import RecommendationRequest

logger = logging.getLogger(__name__)
AI_ERRORS = (AppError, httpx.HTTPError, TimeoutError, ValueError, TypeError)
POLICY_VERSION = "recipe-rerank-v4"


class MealRanker:
    # DB 검색·분량 검증 이후에만 생성하며 슬롯 대기를 포함한 모델 호출을 최대 한 번 수행한다.
    def __init__(self, reasoner: RecipeReasoner, timeout: float, slots: asyncio.Semaphore, expected_model: str) -> None:
        self.reasoner, self.timeout, self.slots = reasoner, timeout, slots
        self.expected_model = expected_model
        self.model: str | None = None
        self.calls = 0
        self.warnings: list[str] = []

    # 후보 전체의 ID 순열만 수용한다. 오류·누락·중복은 기본 추천을 지우지 않고 None으로 반환한다.
    async def rank(self, context: dict, shortlist: list[dict]) -> list[dict] | None:
        try:
            snapshots = compact_candidates(shortlist)
            payload = {**context, "candidates": snapshots}
            if len(json.dumps(payload, ensure_ascii=False).encode()) > 24000:
                payload["candidates"] = compact_candidates(shortlist, brief=True)
            # 입력 예산 때문에 후보를 제거하지 않는다. 초과하면 이미 검증한 기본 추천으로 돌아간다.
            if len(json.dumps(payload, ensure_ascii=False).encode()) > 24000:
                raise ValueError("RECIPE_CONTEXT_TOO_LARGE")
            self.calls += 1
            started = monotonic()
            async with asyncio.timeout(self.timeout), self.slots:
                payload["executionBudgetSeconds"] = max(0.1, min(60, self.timeout - (monotonic() - started) - 1))
                result, model = await self.reasoner.rank(payload)
            if not isinstance(result, RankResult) or model != self.expected_model:
                raise ValueError("RECIPE_MODEL_OR_SCHEMA_MISMATCH")
            wanted = set(range(1, len(shortlist) + 1))
            if len(result.candidateIds) != len(shortlist) or set(result.candidateIds) != wanted:
                raise ValueError("INVALID_RECIPE_RANK_PERMUTATION")
            ordered = [shortlist[identifier - 1] for identifier in result.candidateIds]
            # AI가 미보유 주재료나 수량 부족 후보를 검증된 상위 적합성 그룹 앞으로 올릴 수 없다.
            constrained = sorted(ordered, key=eligibility_tier)
            if constrained != ordered:
                self.warnings.append("AI_ORDER_CONSTRAINED_BY_ELIGIBILITY")
            self.model = model
            return constrained
        except AI_ERRORS as exc:
            allowed = {"RECIPE_CONTEXT_TOO_LARGE", "RECIPE_MODEL_OR_SCHEMA_MISMATCH", "INVALID_RECIPE_RANK_PERMUTATION"}
            code = str(exc) if type(exc) is ValueError and str(exc) in allowed else "TIME_BUDGET_EXCEEDED" if isinstance(exc, TimeoutError) else "REMOTE_OR_SCHEMA_ERROR"
            logger.warning("recipe_ai_fallback stage=RANK error_type=%s reason_code=%s prompt=%s", type(exc).__name__, code, PROMPT_VERSION)
            self.warnings.append("AI_UNAVAILABLE_OR_INVALID")
            return None


# 주재료 누락·분량 부족·추가 구매 여부를 AI가 뒤집지 못하도록 적합성 그룹을 반환한다.
def eligibility_tier(row: dict) -> tuple[bool, bool, bool]:
    missing_title = missing_named_ingredients(row["name"], [i["name"] for i in row["ingredients"]], row["missingIngredients"])
    return bool(missing_title), row["quantityStatus"] == "SHORTAGE", bool(row["missingIngredients"])


# 양수 점수의 중복 없는 후보에 전략·검증된 사유·구매 필요 여부를 부여한다. 수치·조리문은 보존한다.
def build_baseline(ranked: list[dict], request: RecommendationRequest) -> list[dict]:
    rows = []
    seen = set()
    for original in ranked:
        identity = (original["source"], original["sourceRecipeId"])
        if identity in seen or original["score"] <= 0:
            continue
        seen.add(identity)
        row = dict(original)
        urgent = row["usesExpiringIngredients"]
        if request.strategies == ["USE_EXPIRING"] and not urgent:
            continue
        strategy = "USE_EXPIRING" if urgent and "USE_EXPIRING" in request.strategies else "MINIMIZE_SHOPPING" if "MINIMIZE_SHOPPING" in request.strategies else "SIMPLE_COOKING"
        named = missing_named_ingredients(row["name"], [i["name"] for i in row["ingredients"]], [i["name"] for i in row["ingredients"]])
        missing = {canonical(name) for name in row["missingIngredients"]}
        essential = [i["ingredientNo"] for i in row["ingredients"] if i["name"] in named]
        row.update(strategy=strategy, essentialIngredientNos=essential,
                   missingEssentialIngredientNos=[i["ingredientNo"] for i in row["ingredients"] if i["ingredientNo"] in essential and canonical(i["name"]) in missing])
        row["feasibility"] = "NEEDS_SHOPPING" if missing or row["quantityStatus"] == "SHORTAGE" else "QUANTITY_VERIFIED" if row["quantityStatus"] == "SUFFICIENT" else "NEEDS_CHECK"
        urgent_text = f"3일 이내 소비기한 재료 {', '.join(urgent)}가 원문에 포함됩니다." if urgent else "이 레시피에 사용되는 임박 재료는 없습니다."
        missing_title = missing_named_ingredients(row["name"], [i["name"] for i in row["ingredients"]], row["missingIngredients"])
        warning = f" 제목에 명시된 미보유 재료: {', '.join(missing_title)}." if missing_title else ""
        row["reason"] = (f"보유 재료 {len(row['usesInventory'])}종 사용, 추가로 확인할 재료 {len(row['missingIngredients'])}종입니다. "
                         f"{urgent_text} 원문 조리 {len(row['steps'])}단계이며 분량 상태는 {row['quantityStatus']}입니다.{warning}")
        rows.append(row)
    rows.sort(key=lambda row: (*eligibility_tier(row), len(row["steps"]) if request.strategies == ["SIMPLE_COOKING"] else 0,
                              len(row["missingIngredients"]), -row["score"], row["source"], row["sourceRecipeId"]))
    return rows


# 순위 판단에 필요한 원문 근거만 압축한다. 실제 응답의 전체 원문 재료와 단계는 자르지 않는다.
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
