"""취향 / 임박 활용 / 구매 부담 / 다양성을 한 번의 최종 선정 과정에서 비교한다."""

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


# 후보별 정적 우선순위다. 취향 불일치는 임박 재료를 쓴다는 이유로 보호하지 않는다.
def candidate_priority(row: dict, covered: set[str] | None = None) -> tuple[int, ...]:
    preference = preference_priority(row)
    urgent = row.get("_expiryPriority", ())
    main = {canonical(name) for name in row.get("usesExpiringMainIngredients", [])}
    all_urgent = {canonical(name) for name in row.get("usesExpiringIngredients", [])}
    marginal = (-len(main - covered), -len(all_urgent - covered)) if covered is not None else ()
    coverage = (-len(main), -len(all_urgent))
    quantity = 0 if row.get("quantityStatus") == "SUFFICIENT" else 1 if row.get("baseServings") is not None else 2
    return (*preference[:1], *urgent, *preference[1:], *marginal, *coverage, shopping_band(row), len(row["missingIngredients"]),
            quantity, row["_suitability"]["tier"], *row.get("_expiryDetail", ()))


# 한 추천 목록을 확정한다. 검증된 취향 판정을 사용하며 새 재료 / 분량은 생성하지 않는다.
def select_recommendations(rows: list[dict], request: RecommendationRequest, seed: str = "") -> list[dict]:
    pending, selected, covered, seen_main = list(rows), [], set(), set()
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
        selected.append(row)
        covered.update(canonical(name) for name in row.get("usesExpiringIngredients", []))
        seen_main.add(signature)
        pending.remove(original)
    return selected
