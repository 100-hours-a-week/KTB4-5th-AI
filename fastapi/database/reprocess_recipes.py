"""수집한 원본을 재호출 없이 정규화한다. 원본·이전 실행 결과·DB는 변경하지 않는다."""

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.recipes.food_safety import PARSER_VERSION, parse_recipe  # noqa: E402
from app.recipes.ingredients import ingredient_parts, parse_piece  # noqa: E402
from database.collect_recipes import write_line  # noqa: E402


# 원천의 남은 파싱 실패 조각을 기록한다. 원문이나 재료를 임의로 보정하지 않는다.
def rejection_details(row: dict) -> dict:
    parts = ingredient_parts(str(row.get("RCP_PARTS_DTLS") or ""), str(row.get("RCP_NM") or ""))
    if parts is None:
        return {"reason": "UNBALANCED_PARENTHESES", "failedParts": []}
    failed = []
    for part in parts:
        try:
            valid = parse_piece(part) is not None
        except (ValueError, TypeError):
            valid = False
        if not valid:
            failed.append(part)
    return {"reason": "UNPARSED_INGREDIENT" if failed else "MISSING_OR_INVALID_RECIPE_FIELD", "failedParts": failed}


# raw.jsonl을 새 디렉터리로 재처리하며 이전 통과본의 누락·재료 변경도 요약한다.
def reprocess(source: Path, output: Path, baseline: Path | None = None) -> dict:
    started = time.monotonic()
    source_bytes = source.read_bytes()
    rows = [json.loads(line) for line in source_bytes.decode("utf-8").splitlines() if line.strip()]
    old = {row["sourceRecipeId"]: row for row in (json.loads(line) for line in baseline.read_text(encoding="utf-8").splitlines())} if baseline else {}
    ready, review, seen = [], [], set()
    for row in rows:
        source_id = str(row["RCP_SEQ"])
        if source_id in seen:
            raise ValueError("원본 ID가 중복됩니다.")
        seen.add(source_id)
        try:
            recipe = parse_recipe(row)
        except (ValueError, TypeError, KeyError):
            recipe = None
        if recipe:
            ready.append(recipe.model_dump(mode="json"))
        else:
            review.append({"sourceRecipeId": source_id, "name": row.get("RCP_NM"), **rejection_details(row)})
    current = {row["sourceRecipeId"]: row for row in ready}
    regressions = sorted(set(old) - set(current))
    changed = sorted(key for key in set(old).intersection(current) if old[key]["ingredients"] != current[key]["ingredients"])
    summary = {"source": "FOOD_SAFETY_KOREA", "dataset": "COOKRCP01", "parserVersion": PARSER_VERSION, "status": "REPROCESSED",
               "rawSha256": hashlib.sha256(source_bytes).hexdigest(), "uniqueRows": len(rows), "validated": len(ready), "needsReview": len(review),
               "baselineValidated": len(old), "baselineRegressions": regressions, "baselineIngredientChanges": changed,
               "newlyValidated": len(set(current) - set(old)) if baseline else None,
               "databaseImported": False, "storageUsageApproved": False,
               "elapsedSeconds": round(time.monotonic() - started, 3), "finishedAt": datetime.now(UTC).isoformat()}
    output.mkdir(parents=True, exist_ok=False)
    (output / "raw.jsonl").write_bytes(source_bytes)
    for filename, records in (("validated.jsonl", ready), ("needs_review.jsonl", review)):
        with (output / filename).open("x", encoding="utf-8") as stream:
            for record in records:
                write_line(stream, record)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


# 재처리 대상과 비교 기준을 명시적으로 받는다. 인증키·외부 API·DB 연결을 사용하지 않는다.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "recipe_staging" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + PARSER_VERSION))
    args = parser.parse_args()
    summary = reprocess(args.input, args.output, args.baseline)
    print(json.dumps({**summary, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
