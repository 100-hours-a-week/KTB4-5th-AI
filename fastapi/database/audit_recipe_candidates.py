"""기존 수집본에서 재검토 후보를 찾는다. 외부 호출·DB 반입·원문 수정은 하지 않는다."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.recipes.quality import review_reasons  # noqa: E402
from app.recipes.schemas import Recipe, normalize  # noqa: E402


# 정규화 수집본과 기존 승인 묶음을 비교해 추가 검토 후보와 보류 사유를 새 폴더에 저장한다.
def audit(source: Path, baseline: Path, output: Path, targets: list[str]) -> dict:
    approved = {(row["recipe"]["source"], row["recipe"]["sourceRecipeId"])
                for row in (json.loads(line) for line in baseline.read_text(encoding="utf-8").splitlines() if line.strip())}
    candidates, held, seen = [], [], set()
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        recipe = Recipe.model_validate_json(line)
        key = (recipe.source, recipe.sourceRecipeId)
        if key in seen:
            raise ValueError("수집본의 원천 ID가 중복됩니다.")
        seen.add(key)
        if key in approved:
            continue
        matches = [target for target in targets if any(normalize(target) == normalize(item.name) for item in recipe.ingredients)]
        row = {"recipe": recipe.model_dump(mode="json"), "targetIngredients": matches,
               "status": "PENDING_CONTENT_REVIEW", "databaseImported": False}
        reasons = review_reasons(recipe)
        if reasons:
            held.append({**row, "reasons": reasons})
        else:
            candidates.append(row)
    output.mkdir(parents=True, exist_ok=False)
    for name, rows in (("candidates.jsonl", candidates), ("held.jsonl", held)):
        (output / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    summary = {"additionalCandidates": len(candidates), "held": len(held), "databaseImported": False,
               "targetCandidates": {target: sum(target in row["targetIngredients"] for row in candidates) for target in targets}}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 레시피 후보 재검사", "", f"추가 검토 후보 {len(candidates)}건. DB 반입은 하지 않았다.",
             "분량·시간 최소 검사 통과는 원문 정확성 검증이나 현재 냉장고의 조리 가능성을 보장하지 않는다.",
             "명시된 계량 분량은 보존하고 환산 불가능한 단위·기준 인분은 미확정으로 유지한다.", "",
             "| 원천 ID | 요리 | 일치 재료 |", "| --- | --- | --- |"]
    for row in candidates:
        if row["targetIngredients"]:
            recipe = row["recipe"]
            lines.append(f"| {recipe['sourceRecipeId']} | {recipe['name']} | {', '.join(row['targetIngredients'])} |")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


# 원본·비교 기준·새 출력 폴더를 명시적으로 받아 재검사 요약만 출력한다.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", action="append", default=None)
    args = parser.parse_args()
    print(json.dumps(audit(args.input, args.baseline, args.output, args.target or ["두부", "브로콜리"]), ensure_ascii=False))


if __name__ == "__main__":
    main()
