"""미반입 수집본을 자동 검사로 분류한다. 외부 호출 / DB 반입 / 원문 수정은 하지 않는다.

가열 시간 숫자 미표기는 보류 사유로 쓰지 않는다(가정 레시피 대부분이 해당하고, 앱은 원문 확인 안내를 이미 표시한다).
대신 재료표와 조리문 대응, 날 단백질의 가열 단계, 제목 주재료, 손상 문자, 이름 중복을 검사한다.
실행 예: python database/review_recipe_batch.py --input results/recipe_collection_20261105/validated.jsonl \
  --baseline results/recipe_dev_seed_20261023_v2/approved.jsonl --baseline results/recipe_expansion_20261026/approved.jsonl \
  --output results/recipe_review_20261108
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.recipes.quality import review_reasons  # noqa: E402
from app.recipes.schemas import Recipe, normalize  # noqa: E402
from app.recipes.suitability import ingredient_role  # noqa: E402

HEAT = re.compile(r"찌|찐|찜|쪄|삶|끓|굽|구워|구운|볶|튀|오븐|가열|중탕|익히|익혀|익을|데치|데쳐|부치|부쳐|지지|졸이|졸여|조리|전자레인지|에어프라이|팬에|불에|불을|뜸")
RAW_PROTEIN = re.compile(r"고기|돼지|소고기|쇠고기|닭|오리|삼겹|목살|안심|등심|갈비|다짐육|햄|베이컨|소시지|새우|오징어|문어|낙지|주꾸미|조개|바지락|홍합|굴|전복|게|꽃게|대게|"
                         r"생선|연어|고등어|대구|가자미|광어|명태|동태|황태|북어|멸치|참치|장어|달걀|계란|메추리알|알$")
SAFE_RAW = re.compile(r"훈제|통조림|캔|가쓰오|육포|건새우|마른|말린|건멸치|액젓|젓갈|소스|가루|육수|다시|맛살|어묵|삶은|익힌|구운|조미|참치캔|김$")
TITLE_FOODS = ("단호박", "애호박", "두부", "감자", "고구마", "버섯", "닭", "소고기", "쇠고기", "돼지", "새우", "연어", "고등어", "오징어", "가지", "토마토", "브로콜리", "연근", "우엉", "김치", "달걀", "계란")
TITLE_STOP = {"소스", "샐러드", "볶음", "구이", "조림", "무침", "찜", "국", "탕", "전", "밥", "죽", "면", "롤", "말이", "쌈", "튀김", "스프", "수프"}


def mentioned(name: str, text: str) -> bool:
    key = normalize(name)
    if not key:
        return True
    if key in text:
        return True
    # 원문은 "다진 마늘" / "마늘" / "통마늘"처럼 수식어를 붙이거나 뗀다. 2자 이상 핵심어로 다시 본다.
    core = re.sub(r"^(다진|채썬|불린|삶은|데친|냉동|생|건|마른|통|깐|간)", "", key)
    return len(core) >= 2 and (core in text or core[:2] in text and len(core) <= 3)


def check(recipe: Recipe, known_names: set[str], vocabulary: set[str]) -> list[str]:
    reasons = [reason for reason in review_reasons(recipe) if reason != "HEATING_WITHOUT_NUMERIC_TIME"]
    steps = normalize(" ".join(step.instruction for step in recipe.steps))
    raw_steps = " ".join(step.instruction for step in recipe.steps)
    if len(recipe.steps) < 2:
        reasons.append("TOO_FEW_STEPS")
    foods = [item for item in recipe.ingredients if ingredient_role(recipe.name, item.name)[0] not in {"SEASONING", "WATER"}]
    unmentioned = [item.name for item in foods if not mentioned(item.name, steps)]
    collective = re.search(r"재료(로|를|들|와|에)|양념(장)? 재료|소스 재료|준비(한|된) (재료|채소)", raw_steps)
    if foods and len(unmentioned) / len(foods) > 0.5 and not (collective and len(unmentioned) / len(foods) <= 0.75):
        reasons.append("INGREDIENTS_NOT_IN_STEPS:" + ",".join(unmentioned[:6]))
    raw = [item.name for item in recipe.ingredients if RAW_PROTEIN.search(item.name) and not SAFE_RAW.search(item.name)]
    if raw and not HEAT.search(raw_steps):
        reasons.append("RAW_PROTEIN_WITHOUT_HEATING:" + ",".join(raw[:4]))
    names = [normalize(item.name) for item in recipe.ingredients]
    # 제목의 대표 식재료(두부 / 닭 등)가 재료표에 하나도 없으면 다른 요리의 재료표일 수 있다.
    missing_title = [food for food in TITLE_FOODS if food in recipe.name and not any(food in name or (food == "계란" and "달걀" in name) or (food == "달걀" and "계란" in name) for name in names)]
    if missing_title:
        reasons.append("TITLE_INGREDIENT_NOT_IN_LIST:" + ",".join(missing_title))
    # 반대 방향: 조리문에 나오는 식재료가 재료표에 없으면 재료표 / 조리문 불일치다(예: 재료표가 다른 요리의 것).
    extra = sorted({word for word in vocabulary if word in steps and not any(word in name or name in word for name in names)})
    extra = [word for word in extra if not any(word != other and word in other for other in extra)]
    if len(extra) >= 3:
        reasons.append("STEP_FOODS_NOT_IN_LIST:" + ",".join(extra[:6]))
    if normalize(recipe.name) in known_names:
        reasons.append("DUPLICATE_NAME_WITH_EXISTING")
    return reasons


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    known, known_names = set(), set()
    for path in args.baseline:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                recipe = json.loads(line)["recipe"]
                known.add((recipe["source"], recipe["sourceRecipeId"]))
                known_names.add(normalize(recipe["name"]))
    passed, held = [], []
    recipes = [Recipe.model_validate_json(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    # 수집본 전체 재료명 중 명확한 식재료(양념 / 물 제외, 2~4자)를 어휘로 쓴다.
    vocabulary = {normalize(item.name) for recipe in recipes for item in recipe.ingredients
                  if 2 <= len(normalize(item.name)) <= 4 and ingredient_role("", item.name)[0] not in {"SEASONING", "WATER", "UNKNOWN"}}
    for recipe in recipes:
        if (recipe.source, recipe.sourceRecipeId) in known:
            continue
        reasons = check(recipe, known_names, vocabulary)
        (held if reasons else passed).append({"recipe": recipe.model_dump(mode="json"), "reasons": reasons})
    args.output.mkdir(parents=True, exist_ok=True)
    for name, rows in (("auto_passed.jsonl", passed), ("auto_held.jsonl", held)):
        (args.output / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    counts: dict[str, int] = {}
    for row in held:
        for reason in row["reasons"]:
            counts[reason.split(":")[0]] = counts.get(reason.split(":")[0], 0) + 1
    print(json.dumps({"passed": len(passed), "held": len(held), "reasons": counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
