"""빈도가 낮은 임박 재료의 후보가 평가 전에 사라지지 않도록 후보 풀을 구성한다."""

from collections import Counter

from app.recipes.retrieval import canonical


# 취향 선별 전에 임박 재료별 대표성을 확보한다. 모든 재료를 담지 못하면 미평가로 남긴다.
def representative_candidates(rows: list[dict], limit: int) -> list[dict]:
    pending, result, covered = list(rows), [], set()
    targets = [{canonical(name) for name in row.get("usesExpiringMainIngredients", [])} for row in pending]
    counts = Counter(name for names in targets for name in names)
    positions = {(row["source"], row["sourceRecipeId"]): index for index, row in enumerate(rows)}
    while pending and len(result) < limit:
        uncovered = set(counts) - covered
        available = [name for name in uncovered if any(name in {canonical(n) for n in row.get("usesExpiringMainIngredients", [])} for row in pending)]
        if not available:
            # 적은 비용으로 후보 전체를 확인할 수 있는 재료는 잔여 슬롯으로 평가를 완성한다.
            for name in sorted(counts, key=lambda value: (counts[value], value)):
                remaining = [row for row in pending if name in {canonical(n) for n in row.get("usesExpiringMainIngredients", [])}]
                if len(remaining) <= limit - len(result):
                    result.extend(remaining)
                    pending = [row for row in pending if row not in remaining]
            result.extend(pending[:limit-len(result)])
            break
        rare = min(available, key=lambda name: (counts[name], name))
        candidates = [row for row in pending if rare in {canonical(n) for n in row.get("usesExpiringMainIngredients", [])}]
        def key(row: dict) -> tuple:
            new = {canonical(name) for name in row.get("usesExpiringMainIngredients", [])} - covered
            return (-sum(1 / counts[name] for name in new), positions[(row["source"], row["sourceRecipeId"])])
        selected = min(candidates, key=key)
        result.append(selected)
        covered.update(canonical(name) for name in selected.get("usesExpiringMainIngredients", []))
        pending.remove(selected)
    return result


# 후보가 실제로 어느 단계까지 도달했는지 집계한다. 0은 취향 불일치 판정과 다르다.
def coverage_counts(eligible: list[dict], shortlist: list[dict], evaluated: set[tuple[str, str]], selected: list[dict]) -> dict[str, dict[str, int]]:
    stages = {"eligible": eligible, "shortlisted": shortlist,
              "evaluated": [row for row in eligible if (row["source"], row["sourceRecipeId"]) in evaluated], "recommended": selected}
    names = sorted({canonical(name) for row in eligible for name in row.get("usesExpiringMainIngredients", [])})
    return {name: {stage: sum(name in {canonical(n) for n in row.get("usesExpiringMainIngredients", [])} for row in rows)
                   for stage, rows in stages.items()} for name in names}
