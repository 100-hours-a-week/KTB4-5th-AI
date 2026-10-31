"""같은 후보의 기본·AI 순서를 사실 지표로 비교한다. 취향·맛·안전성 점수는 생성하지 않는다."""

from app.recipes.selection import verified_consumption


# 반환 상위 목록의 주재료 확인 부담·추가 구매·분량 충족·임박 활용을 집계한다.
def recommendation_metrics(rows: list[dict]) -> dict:
    return {"returned": len(rows),
            "preferenceMatched": sum(row.get("preferenceMatch") == "MATCHED" for row in rows),
            "usesExpiringMainIngredients": len({name for row in rows for name in row.get("usesExpiringMainIngredients", [])}),
            "verifiedUrgentConsumptionRecipes": sum(verified_consumption(row) is not None for row in rows),
            "quantityVerified": sum(row["feasibility"] == "QUANTITY_VERIFIED" for row in rows),
            "needsShopping": sum(row["feasibility"] == "NEEDS_SHOPPING" for row in rows),
            "missingMainIngredients": sum(len(row["missingEssentialIngredientNos"]) for row in rows),
            "missingIngredients": sum(len(row["missingIngredients"]) for row in rows),
            "usesExpiringIngredients": len({name for row in rows for name in row["usesExpiringIngredients"]}),
            "top3MissingIngredients": sum(len(row["missingIngredients"]) for row in rows[:3]),
            "top3MissingMainIngredients": sum(len(row["missingEssentialIngredientNos"]) for row in rows[:3])}


# 동일 후보·동일 limit에서 순위 변경과 사실 지표만 비교한다. 이를 AI 우월성으로 해석하지 않는다.
def compare_rankings(baseline: list[dict], reordered: list[dict], limit: int) -> dict:
    before, after = baseline[:limit], reordered[:limit]
    return {"baseline": recommendation_metrics(before), "ranked": recommendation_metrics(after),
            "orderChanged": [(row["source"], row["sourceRecipeId"]) for row in before] != [(row["source"], row["sourceRecipeId"]) for row in after]}
