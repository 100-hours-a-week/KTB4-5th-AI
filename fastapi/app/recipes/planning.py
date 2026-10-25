"""검증된 레시피의 기본 추천을 구성하고 단일 AI 호출로 순서만 조정한다."""

import asyncio
import json
import logging
from datetime import datetime
from time import monotonic
from zoneinfo import ZoneInfo

import httpx

from app.errors import AppError
from app.recipes.ai import RecipeReasoner
from app.recipes.ai_contract import PROMPT_VERSION, RankResult
from app.recipes.retrieval import canonical, missing_named_ingredients
from app.recipes.schemas import RecommendationRequest
from app.recipes.suitability import assess_suitability

logger = logging.getLogger(__name__)
AI_ERRORS = (AppError, httpx.HTTPError, TimeoutError, ValueError, TypeError)
POLICY_VERSION = "recipe-rerank-v7"


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
                payload["ingredientColumns"] = ["ingredientNo", "name", "role", "quantityStatus", "relatedStockNames", "unit", "requiredAmount"]
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
            # AI가 실제 임박 활용·소비기한·적합성의 검증된 우선순위를 뒤집을 수 없다.
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


# 임박 활용을 요청한 경우 날짜 우선순위 뒤에 적합성 그룹을 붙여 보호할 정렬 키를 반환한다.
def eligibility_tier(row: dict) -> tuple[int, ...]:
    prefix = (*row.get("_expiryPriority", ()), row["_suitability"]["tier"])
    return (*prefix, len(row["missingIngredients"]), *row.get("_expiryDetail", ())) if "_expiryPriority" in row else prefix


# 미보유 주재료를 제외하고 미검토 누락은 확인 사유로 보존한다. 실제 임박 활용을 우선한다.
def build_baseline(ranked: list[dict], request: RecommendationRequest, confirmation_only: bool = False) -> list[dict]:
    rows = []
    seen = set()
    for original in ranked:
        identity = (original["source"], original["sourceRecipeId"])
        if identity in seen:
            continue
        seen.add(identity)
        row = dict(original)
        facts = row.get("_suitability") or assess_suitability(row, [item.name for item in request.inventory])
        row["_suitability"] = facts
        if facts["missingMain"] or not (facts["hasFoodMatch"] or facts.get("hasRelatedFood")):
            continue
        uncertain = bool(facts["relatedMain"] or facts["unresolvedMissing"])
        if uncertain != confirmation_only:
            continue
        if not confirmation_only and not facts["hasFoodMatch"]:
            continue
        urgent = row["usesExpiringIngredients"]
        urgent_main = [item["name"] for item in facts["ingredients"] if item["role"] == "MAIN" and canonical(item["name"]) in {canonical(name) for name in urgent}]
        row["usesExpiringMainIngredients"] = list(dict.fromkeys(urgent_main))
        if "USE_EXPIRING" in request.strategies:
            focus = urgent_main or urgent
            today = datetime.now(ZoneInfo("Asia/Seoul")).date()
            dates = [item.expirationDate for item in request.inventory if canonical(item.name) in {canonical(name) for name in focus}
                     and 0 <= (item.expirationDate - today).days <= 3]
            # 임박 주재료를 먼저 보되, 같은 핵심 활용 그룹에서는 조리 적합성이 날짜보다 앞선다.
            row["_expiryPriority"] = (0 if urgent_main else 1 if urgent else 2,)
            row["_expiryDetail"] = (min(dates).toordinal(), -len(set(urgent_main))) if dates else (0, 0)
        if request.strategies == ["USE_EXPIRING"] and not urgent:
            continue
        strategy = "USE_EXPIRING" if urgent and "USE_EXPIRING" in request.strategies else "MINIMIZE_SHOPPING" if "MINIMIZE_SHOPPING" in request.strategies else "SIMPLE_COOKING"
        essential = [item["ingredientNo"] for item in facts["ingredients"] if item["role"] == "MAIN"]
        row.update(strategy=strategy, essentialIngredientNos=essential,
                   missingEssentialIngredientNos=[item["ingredientNo"] for item in facts["ingredients"] if item["role"] == "MAIN" and item["missing"]])
        row["feasibility"] = "NEEDS_SHOPPING" if row["missingIngredients"] or row["quantityStatus"] == "SHORTAGE" else "QUANTITY_VERIFIED" if row["quantityStatus"] == "SUFFICIENT" else "NEEDS_CHECK"
        row["confirmationReasons"] = ([f"재료 종류·상태 확인: 보유 {', '.join(item['relatedStockNames'])} / 원문 필요 {item['name']}" for item in facts["relatedMain"]]
                                      + [f"역할·보유 여부 확인: {item['name']}" for item in facts["unresolvedMissing"]])
        if confirmation_only:
            row["feasibility"] = "NEEDS_CHECK"
        if row.get("baseServings") is None:
            row["confirmationReasons"].append("원문 인분이 없어 요청 인분의 필요량을 확정할 수 없습니다.")
        urgent_text = f"3일 이내 소비기한 재료 {', '.join(urgent)}가 원문에 포함됩니다." if urgent else "이 레시피에 사용되는 임박 재료는 없습니다."
        missing_title = missing_named_ingredients(row["name"], [i["name"] for i in row["ingredients"]], row["missingIngredients"])
        warning = f" 제목에 명시된 미보유 재료: {', '.join(missing_title)}." if missing_title else ""
        if facts["relatedMain"]:
            warning += " 같은 계열 재고가 있지만 부위·가공 상태가 같다고 확정할 수 없어 정확한 재료와 필요량을 확인해야 합니다."
        if facts["unresolvedMissing"]:
            warning += f" 역할·보유 여부 확인 필요: {', '.join(item['name'] for item in facts['unresolvedMissing'])}. 원문 재료를 생략해도 된다는 뜻은 아닙니다."
        row["reason"] = (f"보유 재료 {len(row['usesInventory'])}종 사용, 추가로 확인할 재료 {len(row['missingIngredients'])}종입니다. "
                         f"{urgent_text} 원문 조리 {len(row['steps'])}단계이며 분량 상태는 {row['quantityStatus']}입니다.{warning}")
        rows.append(row)
    rows.sort(key=lambda row: (*eligibility_tier(row), len(row["steps"]) if request.strategies == ["SIMPLE_COOKING"] else 0,
                              len(row["missingIngredients"]), *row.get("_expiryDetail", ()), -row["score"], row["source"], row["sourceRecipeId"]))
    return rows


# 순위 판단에 필요한 원문 근거만 압축한다. 실제 응답의 전체 원문 재료와 단계는 자르지 않는다.
def compact_candidates(shortlist: list[dict], brief: bool = False) -> list[dict]:
    result = []
    for index, row in enumerate(shortlist, 1):
        requirements = {r["ingredientNo"]: r for r in row["ingredientRequirements"]}
        roles = {item["ingredientNo"]: item for item in row["_suitability"]["ingredients"]}
        if brief:
            used = {canonical(name) for name in row["usesInventory"]}
            urgent = {canonical(name) for name in row["usesExpiringIngredients"]}
            result.append({"candidateId": index, "name": row["name"],
                           "ingredients": [[item["ingredientNo"], item["name"], roles[item["ingredientNo"]]["role"],
                                            requirements[item["ingredientNo"]]["status"], roles[item["ingredientNo"]]["relatedStockNames"],
                                            requirements[item["ingredientNo"]].get("unit"), requirements[item["ingredientNo"]].get("requiredAmount")]
                                           for item in row["ingredients"]],
                           "ownedIngredientNos": [item["ingredientNo"] for item in row["ingredients"] if canonical(item["name"]) in used],
                           "expiringIngredientNos": [item["ingredientNo"] for item in row["ingredients"] if canonical(item["name"]) in urgent],
                           "missingIngredientNos": [item["ingredientNo"] for item in roles.values() if item["missing"]],
                           "eligibilityTier": row["_suitability"]["tier"], "expiryPriority": list(row.get("_expiryPriority", ())),
                           "steps": [step["instruction"][:100] for step in row["steps"][:2]],
                           "stepCount": len(row["steps"]), "detailsReduced": True, "stepsMayBeTruncated": True})
            continue
        ingredients = []
        for item in row["ingredients"]:
            requirement = requirements.get(item["ingredientNo"], {})
            keys = ("status",) if brief else ("requiredAmount", "unit", "shortageAmount", "status")
            role = roles[item["ingredientNo"]]
            ingredients.append({"ingredientNo": item["ingredientNo"], "name": item["name"], "role": role["role"], "roleEvidence": role["roleEvidence"],
                                "relatedStockNames": role["relatedStockNames"],
                                **{key: requirement[key] for key in keys if requirement.get(key) is not None}})
        result.append({"candidateId": index, "name": row["name"], "ingredients": ingredients,
                       "usesInventory": row["usesInventory"], "usesExpiringIngredients": row["usesExpiringIngredients"],
                       "missingIngredients": row["missingIngredients"], "quantityStatus": row["quantityStatus"],
                       "missingNamedIngredients": missing_named_ingredients(row["name"], [i["name"] for i in row["ingredients"]], row["missingIngredients"]),
                       "eligibilityTier": row["_suitability"]["tier"],
                       "expiryPriority": list(row.get("_expiryPriority", ())),
                       "stepCount": len(row["steps"]), "steps": [] if brief else [step["instruction"][:100] for step in row["steps"][:2]],
                       "detailsReduced": brief, "stepsMayBeTruncated": brief or len(row["steps"]) > 2 or any(len(step["instruction"]) > 100 for step in row["steps"])})
    return result
