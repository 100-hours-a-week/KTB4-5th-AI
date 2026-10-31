"""취향 / 임박 활용 / 구매 부담 / 다양성을 한 번의 최종 선정 과정에서 비교한다."""

from collections import defaultdict

from app.recipes.context import variation_key
from app.recipes.meal_quality import shopping_band
from app.recipes.retrieval import canonical
from app.recipes.schemas import RecommendationRequest


# 같은 원문 근거에 여러 취향 태그가 연결돼도 근거 수를 중복 가산하지 않는다.
def preference_priority(row: dict) -> tuple[int, ...]:
    status = row.get("preferenceMatch")
    if status in {None, "NOT_REQUESTED"}:
        return ()
    evidence = {canonical(item.get("evidence") or item.get("tag") or "unspecified")
                for item in row.get("preferenceAssessments", []) if item["status"] == "MATCHED"}
    return ({"MATCHED": 0, "UNKNOWN": 1, "NOT_MATCHED": 2}[status], -len(evidence))


# 취향이 있으면 검증된 일치만 정식 추천한다. 미확인은 불일치로 바꾸지 않고 확인 후보로 분리한다.
def preference_candidates(rows: list[dict], request: RecommendationRequest) -> tuple[list[dict], list[dict]]:
    if not request.preferenceTags:
        return list(rows), []
    return ([row for row in rows if row.get("preferenceMatch") == "MATCHED"],
            [row for row in rows if row.get("preferenceMatch") == "UNKNOWN"])


# 제목상 주재료 / 일반 주재료 / 부재료 포함을 구분한다. MAIN 사전 등재만으로 요리의 중심이라고 단정하지 않는다.
def urgent_role_priority(row: dict) -> int:
    urgent = {canonical(name) for name in row.get("usesExpiringIngredients", [])}
    facts = [item for item in row.get("_suitability", {}).get("ingredients", []) if canonical(item["name"]) in urgent]
    if any(item.get("role") == "MAIN" and item.get("roleEvidence") == "TITLE_MATCH" for item in facts):
        return 0
    if row.get("usesExpiringMainIngredients"):
        return 1
    return 2 if urgent else 3


# 임박 재고의 확정 배분 비율만 비교한다. 서로 다른 단위의 절대량을 더하거나 미확정량을 0으로 평가하지 않는다.
def verified_consumption(row: dict) -> float | None:
    urgent = {canonical(name) for name in row.get("usesExpiringIngredients", [])}
    requirements = {item["ingredientNo"]: item for item in row.get("ingredientRequirements", [])}
    groups = defaultdict(lambda: [0.0, 0.0])
    for usage in row.get("inventoryUsage", []):
        # 같은 재료의 여유 재고까지 분모에 넣지 않는다. 임박 ID는 평가 단계에서 명시한다.
        name = row.get("_urgentInventoryNames", {}).get(usage["ingredientId"], usage["name"])
        if canonical(name) not in urgent or usage["ingredientId"] not in row.get("_urgentInventoryIds", []):
            continue
        if usage["allocationStatus"] == "UNRESOLVED":
            return None
        if usage["availableAmount"] <= 0:
            continue
        if any(requirements.get(item["ingredientNo"], {}).get("status") not in {"SUFFICIENT", "SHORTAGE"} for item in usage["allocations"]):
            return None
        group = groups[(canonical(name), usage["unit"])]
        group[0] += usage["usedAmount"]
        group[1] += usage["availableAmount"]
    if not groups:
        return None
    return sum(min(1.0, used / available) for used, available in groups.values()) / len(groups)


# 후보별 정적 우선순위다. 취향 불일치는 임박 재료를 쓴다는 이유로 보호하지 않는다.
def candidate_priority(row: dict, covered: set[str] | None = None) -> tuple[int | float, ...]:
    preference = preference_priority(row)
    urgent = row.get("_expiryPriority", ())
    main = {canonical(name) for name in row.get("usesExpiringMainIngredients", [])}
    all_urgent = {canonical(name) for name in row.get("usesExpiringIngredients", [])}
    marginal = (-len(main - covered), -len(all_urgent - covered)) if covered is not None else ()
    coverage = (-len(main), -len(all_urgent))
    consumption = verified_consumption(row)
    consumption_key = (0, -consumption) if consumption is not None else (1, 0.0)
    quantity = 0 if row.get("quantityStatus") == "SUFFICIENT" else 1 if row.get("baseServings") is not None else 2
    return (*preference[:1], *urgent, *preference[1:], urgent_role_priority(row), *marginal, *coverage, *consumption_key, shopping_band(row), len(row["missingIngredients"]),
            quantity, row["_suitability"]["tier"], *row.get("_expiryDetail", ()))


# 한 추천 목록을 확정한다. 검증된 취향 판정을 사용하며 새 재료 / 분량은 생성하지 않는다.
def select_recommendations(rows: list[dict], request: RecommendationRequest, seed: str = "") -> list[dict]:
    eligible, _ = preference_candidates(rows, request)
    pending, selected, covered, seen_main = eligible, [], set(), set()
    recent = set(request.recentRecipeIds)
    while pending and len(selected) < request.limit:
        def key(row: dict) -> tuple:
            roles = row.get("_suitability", {}).get("ingredients", [])
            signature = frozenset(canonical(item["name"]) for item in roles if item["role"] == "MAIN")
            identity = f"{row['source']}:{row['sourceRecipeId']}"
            tie = (row["source"], row["sourceRecipeId"]) if request.preferenceTags else (variation_key(row, seed),)
            return (*candidate_priority(row, covered if "USE_EXPIRING" in request.strategies else None),
                    int(identity in recent), int(bool(signature) and signature in seen_main), *tie)
        original = min(pending, key=key)
        row = dict(original)
        newly_used = {canonical(name) for name in row.get("usesExpiringMainIngredients", [])} - covered
        roles = row.get("_suitability", {}).get("ingredients", [])
        signature = frozenset(canonical(item["name"]) for item in roles if item["role"] == "MAIN")
        duplicate = bool(signature) and signature in seen_main
        repeated = f"{row['source']}:{row['sourceRecipeId']}" in recent
        row["scoreBreakdown"] = {**row.get("scoreBreakdown", {}), "variety": 0.0 if repeated or duplicate else 1.0}
        if newly_used and "USE_EXPIRING" in request.strategies:
            row["reason"] += f" 임박 재료 분산 활용: {', '.join(sorted(newly_used))}를 이 목록에서 추가로 활용합니다."
        if repeated or duplicate:
            row["reason"] += " 같은 추천 조건에서 최근 추천과 주재료 중복을 고려했습니다."
        if row.get("usesExpiringIngredients"):
            role = urgent_role_priority(row)
            row["reason"] += " 임박 활용 근거: " + ("제목에 명시된 주재료입니다." if role == 0 else "재료 역할 규칙상 주재료입니다." if role == 1 else "부재료 포함이며 주재료 소진과 구분합니다.")
            consumption = verified_consumption(row)
            row["reason"] += (f" 확정 배분 가능한 임박 재고의 재료 / 단위별 소모 비율 평균은 {consumption:.0%}입니다. 실제 차감은 아닙니다."
                              if consumption is not None else " 임박 재료의 소모량은 인분 / 분량 / 단위 확인 전에는 확정하지 않습니다.")
        selected.append(row)
        covered.update(canonical(name) for name in row.get("usesExpiringIngredients", []))
        seen_main.add(signature)
        pending.remove(original)
    return selected
