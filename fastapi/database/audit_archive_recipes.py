"""확보한 한식진흥원 원본의 연결·순서·분량을 오프라인 검증한다. DB·네트워크 쓰기 없음."""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.recipes.ingredients import parse_ingredient  # noqa: E402

TIME = re.compile(r"(?:\d+(?:\.\d+)?\s*(?:~\s*\d+)?\s*(?:분|초|시간)|하루|이틀|사흘)")
HEAT = re.compile(r"찌|찐다|쪄|삶|끓|굽|구워|볶|튀|오븐|가열|지진|지지고")
KNOWN_CATEGORIES = {"recipecon.prepare", "recipecon.cooking", "recipecon.tip"}


# API 원문 목록에서 단계·재료를 구분하고 원천 ID별 검증 근거를 반환한다. 제목·인분·누락 단계는 생성하지 않는다.
def audit(parts: list[dict]) -> dict:
    steps, ingredients = [], []
    for part in parts:
        for row in part["rows"]:
            if "카테고리" in row:
                steps.append(row)
            else:
                ingredients.append(row)
    stage_groups, ingredient_groups = defaultdict(list), defaultdict(list)
    for row in steps:
        stage_groups[str(row["레시피ID"])].append(row)
    for row in ingredients:
        ingredient_groups[str(row["레시피ID"])].append(row)
    records, amount_counts = [], Counter()
    for identity in sorted(set(stage_groups) | set(ingredient_groups), key=int):
        stage_rows, ingredient_rows = stage_groups.get(identity, []), ingredient_groups.get(identity, [])
        reasons, categories = ["MISSING_OFFICIAL_TITLE", "MISSING_BASE_SERVINGS"], {}
        for category in sorted({str(row["카테고리"]) for row in stage_rows}):
            rows = [row for row in stage_rows if row["카테고리"] == category]
            numbers = [str(row["명칭"]) for row in rows]
            sequential = all(number.isdigit() for number in numbers) and sorted(map(int, numbers)) == list(range(1, len(rows) + 1))
            if not sequential:
                reasons.append("INVALID_STAGE_SEQUENCE")
            if category not in KNOWN_CATEGORIES:
                reasons.append("UNKNOWN_STAGE_CATEGORY")
            if any(str(row["구분"]) != str(row["명칭"]) for row in rows):
                reasons.append("CONFLICTING_STAGE_NUMBER")
            categories[category] = {"sequential": sequential, "rows": sorted(rows, key=lambda row: int(row["명칭"]) if str(row["명칭"]).isdigit() else int(row["정보ID"]))}
        cooking = categories.get("recipecon.cooking", {}).get("rows", [])
        # 완성·상차림이 1번에 나오는 것은 검토 힌트다. 원문 번호를 자동 반전하지 않는다.
        if len(cooking) > 1 and re.search(r"낸다|먹는다|곁들여", str(cooking[0]["내용"])):
            reasons.append("POSSIBLE_REVERSED_COOKING_ORDER")
        if not categories.get("recipecon.cooking"):
            reasons.append("MISSING_COOKING")
        if not ingredient_rows:
            reasons.append("MISSING_INGREDIENTS")
        parsed_rows = []
        for row in sorted(ingredient_rows, key=lambda row: (int(row["분류"]), int(row["재료ID"]))):
            raw_amount = str(row.get("내용") or "").strip()
            parsed = parse_ingredient(f"{row['명칭']} {raw_amount}", len(parsed_rows) + 1) if raw_amount else None
            state = "MISSING" if not raw_amount else "PARSED_NUMERIC" if parsed and parsed.amountValue is not None else "PARSED_TEXT" if parsed else "UNRESOLVED"
            amount_counts[state] += 1
            if state in {"MISSING", "UNRESOLVED"}:
                reasons.append("MISSING_OR_UNRESOLVED_AMOUNT")
            hint = "SECTION_LABEL_OR_MISSING_AMOUNT" if not raw_amount else None
            if state == "UNRESOLVED" and re.fullmatch(r"\d+(?:\.\d+)?Kg", raw_amount):
                hint = "UNIT_CASE_NORMALIZATION_CANDIDATE"
            elif state == "UNRESOLVED" and re.search(r"\d.*(?:개|알|잎|g).*?[가-힣].*\d", raw_amount):
                hint = "POSSIBLE_MERGED_INGREDIENTS"
            parsed_rows.append({"raw": row, "amountState": state, "reviewHint": hint, "parsed": parsed.model_dump(mode="json") if parsed else None})
        texts = [str(row.get("내용") or "") for row in stage_rows]
        if any(not text.strip() for text in texts):
            reasons.append("EMPTY_STAGE_TEXT")
        if any(any(marker in text for marker in ("�", "□", "?")) for text in texts + [str(row["명칭"]) for row in ingredient_rows]):
            reasons.append("TEXT_REQUIRES_REVIEW")
        has_heat = any(HEAT.search(text) for text in texts)
        has_time = any(TIME.search(text) for text in texts)
        if has_heat and not has_time:
            reasons.append("HEATING_WITHOUT_EXPLICIT_TIME")
        records.append({"recipeId": identity, "reviewReasons": sorted(set(reasons)), "ingredients": parsed_rows,
                        "stageCategories": categories, "hasHeatMention": has_heat, "hasTimeMention": has_time,
                        "readyForService": False})
    row_id_counts = {"stageDuplicateIds": sum(count - 1 for count in Counter(row["정보ID"] for row in steps).values()),
                     "ingredientDuplicateIds": sum(count - 1 for count in Counter(row["재료ID"] for row in ingredients).values())}
    return {"summary": {"stepRows": len(steps), "ingredientRows": len(ingredients), "stepRecipeIds": len(stage_groups),
                        "ingredientRecipeIds": len(ingredient_groups), "joinedRecipeIds": len(set(stage_groups) & set(ingredient_groups)),
                        "unionRecipeIds": len(records), "amountStates": dict(amount_counts), **row_id_counts,
                        "reasonCounts": dict(Counter(reason for row in records for reason in row["reviewReasons"])),
                        "serviceReadyRecipes": 0, "serviceDatabaseImported": 0,
                        "limits": ["시간 표현 존재는 모든 가열 단계의 충분한 시간 명시를 보장하지 않음",
                                   "정상 순서는 누락 없는 조리법임을 보장하지 않음", "분량 파싱 실패는 원문 자체 오류와 구분",
                                   "이용허락 확인과 조리 안전성 검토를 대신하지 않음"]},
            "recipes": records}


# 명시한 로컬 JSON만 읽어 새 감사 파일을 만든다. 기존 결과 덮어쓰기와 원본 변경은 거부한다.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    result = audit(json.loads(raw))
    result["inputSha256"] = hashlib.sha256(raw).hexdigest()
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
