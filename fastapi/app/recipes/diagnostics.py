"""추천 후보 탈락 원인을 집계한다. 원문·재고·추천 순위는 변경하지 않는다."""

from collections import Counter

from app.recipes.retrieval import MATCH_VERSION, canonical


# 후보별 적합성 근거를 받아 중복 가능한 원인 수와 상위 부족 재료를 반환한다.
def candidate_diagnostics(ranked: list[dict], eligible_count: int, confirmation_count: int) -> dict:
    facts = [row["_suitability"] for row in ranked]

    # 같은 요리에서 한 재료가 여러 번 등장해도 원인별 요리 수는 한 번만 센다.
    def ingredients(reason: str) -> list[dict]:
        counts = Counter(name for row in facts for name in {canonical(item["name"]) for item in row[reason]})
        return [{"name": name, "recipeCount": count} for name, count in sorted(counts.items(), key=lambda row: (-row[1], row[0]))[:10]]

    return {"matchVersion": MATCH_VERSION, "candidateCount": len(ranked), "eligibleBeforeLimit": eligible_count,
            "confirmationBeforeLimit": confirmation_count, "missingMainRecipeCount": sum(bool(row["missingMain"]) for row in facts),
            "unreviewedMissingRecipeCount": sum(bool(row["unresolvedMissing"]) for row in facts),
            "relatedIngredientRecipeCount": sum(bool(row["relatedMain"]) for row in facts),
            "topMissingMain": ingredients("missingMain"), "topUnreviewedMissing": ingredients("unresolvedMissing"),
            "topRelatedIngredients": ingredients("relatedMain"), "reasonCountsMayOverlap": True}
