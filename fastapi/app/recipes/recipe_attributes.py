"""원천 레시피의 요리 성격(요리 종류 / 형태 / 온도 / 무게감 / 난이도 / 식사 목적 / 식감) AI 사전 분류와 검증.

판단은 모델이 한다. 규칙은 원문과 명백히 모순되는 값만 UNKNOWN으로 되돌리며 값을 새로 정하지 않는다.
"""
import hashlib
import json
import re

from app.recipes.ai_contract import ATTRIBUTE_PROMPT_VERSION, RecipeAttributes
from app.recipes.meal_quality import frying_steps
from app.recipes.retrieval import canonical
from app.recipes.taste_profiles import taste_signal

ATTRIBUTE_VERSION = "recipe-attribute-v1"
HEAT_WORDS = ("끓", "볶", "굽", "구워", "구운", "튀기", "튀겨", "튀김", "찌", "쪄", "데치", "데쳐", "졸이", "조려", "조린", "오븐",
              "에어프라이", "전자레인지", "익히", "익혀", "부치", "부쳐", "지지", "삶", "가열", "불에")
CRISPY_WORDS = ("튀기", "튀겨", "튀김", "굽", "구워", "구운", "오븐", "에어프라이", "바삭", "부치", "부쳐", "지지", "노릇")
COLD_WORDS = ("냉", "차갑", "차게", "얼음", "시원", "식혀", "식힌")
SOUP_PATTERN = re.compile(r"물(?:을|에|이|과|\s|\d)|육수|국물|스톡|밑국물")

# 앱이 보내는 맛 외 취향 태그다. 맛 태그(매콤한 등)는 taste_profiles가 담당한다.
CUISINE_TAGS = {"한식": "KOREAN", "양식": "WESTERN", "중식": "CHINESE", "일식": "JAPANESE"}
DIFFICULTY_TAGS = {"쉬움": "EASY", "중간": "MEDIUM", "어려움": "HARD"}
PURPOSE_TAGS = {"간식": "SNACK", "디저트": "DESSERT", "한끼": "MEAL", "간편한식사": "QUICK_MEAL"}
TEXTURE_TAGS = {"바삭": "CRISPY", "바삭한": "CRISPY", "쫄깃": "CHEWY", "쫄깃한": "CHEWY", "부드러운": "SOFT"}
# 요리 형태 / 온도 / 무게감은 저장된 분류(dishForm / servingTemperature / heaviness)로 판정한다.
# 이전에는 이 표가 없어 "국물요리"를 맛 태그처럼 조리문 인용으로만 판정했고 국물 요리가 아닌 후보가 앞에 섰다(dev 실측).
DISH_FORM_TAGS = {"국물요리": "SOUP_STEW", "국물": "SOUP_STEW", "국": "SOUP_STEW", "찌개": "SOUP_STEW", "탕": "SOUP_STEW", "국찌개": "SOUP_STEW",
                  "밥요리": "RICE", "밥": "RICE", "덮밥": "RICE", "면요리": "NOODLE", "면": "NOODLE", "국수": "NOODLE",
                  "반찬": "SIDE_DISH", "메인요리": "MAIN_DISH", "샐러드": "SALAD", "베이킹": "BAKED", "음료": "DRINK"}
TEMPERATURE_TAGS = {"따뜻한": "HOT", "따뜻한요리": "HOT", "뜨끈한": "HOT", "시원한": "COLD", "차가운": "COLD", "시원한요리": "COLD"}
HEAVINESS_TAGS = {"가벼운": "LIGHT", "가벼운요리": "LIGHT", "가볍게": "LIGHT"}
LABELS = {"KOREAN": "한식", "WESTERN": "양식", "CHINESE": "중식", "JAPANESE": "일식", "EASY": "쉬움", "MEDIUM": "중간", "HARD": "어려움",
          "SNACK": "간식", "DESSERT": "디저트", "MEAL": "한끼", "QUICK_MEAL": "간편한 식사", "CRISPY": "바삭", "CHEWY": "쫄깃", "SOFT": "부드러움",
          "SOUP_STEW": "국물 요리", "RICE": "밥 요리", "NOODLE": "면 요리", "SIDE_DISH": "반찬", "MAIN_DISH": "메인 요리", "SALAD": "샐러드",
          "BAKED": "베이킹", "DRINK": "음료", "HOT": "따뜻한 요리", "COLD": "시원한 요리", "LIGHT": "가벼운 요리"}



# 맛 외 태그인지 판별한다. 이 태그는 요청 시점 인용문으로 검증할 수 없어 저장된 속성으로만 일치를 판단한다.
def attribute_tag(tag: str) -> bool:
    key = canonical(tag)
    return any(key in table for table in (CUISINE_TAGS, DIFFICULTY_TAGS, PURPOSE_TAGS, TEXTURE_TAGS, DISH_FORM_TAGS, TEMPERATURE_TAGS, HEAVINESS_TAGS))


# 원천 / 모델 / 분류 버전이 같으면 같은 키다. 사용자 / 재고 / 날씨는 포함하지 않는다.
def attribute_key(row: dict, model: str) -> str:
    source = {k: row.get(k) for k in ("source", "sourceRecipeId", "sourceRevision", "name", "ingredients", "steps")}
    return hashlib.sha256(json.dumps([source, model, ATTRIBUTE_PROMPT_VERSION, ATTRIBUTE_VERSION], ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


# 모델에는 원문 제목 / 재료 / 조리 단계만 보낸다. 사용자 정보와 재고는 보내지 않는다.
def attribute_context(row: dict) -> dict:
    recipe = {"name": row["name"],
              "ingredients": [[item["name"], item.get("amountText")] for item in row["ingredients"]],
              "steps": [step["instruction"] for step in row["steps"]],
              "stepCount": len(row["steps"]), "ingredientCount": len(row["ingredients"])}
    payload = {"recipe": recipe, "attributeVersion": ATTRIBUTE_VERSION, "executionBudgetSeconds": 30}
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 24000:
        raise ValueError("RECIPE_ATTRIBUTE_SOURCE_TOO_LARGE")
    return payload


def _limit(result: RecipeAttributes, code: str) -> None:
    result.limitations = list(dict.fromkeys([code, *result.limitations]))[:6]


# 원문과 정면으로 어긋나는 값만 UNKNOWN / 제외로 되돌린다. 근거가 없다는 이유로 모델 판단을 뒤집지 않는다.
def validate_attributes(row: dict, attributes: RecipeAttributes) -> RecipeAttributes:
    result = attributes.model_copy(deep=True)
    steps = " ".join(step["instruction"] for step in row["steps"])
    title = row["name"]
    names = " ".join(canonical(item["name"]) for item in row["ingredients"])
    step_count, ingredient_count = len(row["steps"]), len(row["ingredients"])
    if result.servingTemperature == "HOT" and not any(word in steps or word in title for word in HEAT_WORDS):
        result.servingTemperature = "UNKNOWN"
        _limit(result, "HOT_WITHOUT_HEATING_STEP")
    if result.dishForm == "SOUP_STEW" and not (SOUP_PATTERN.search(steps) or "육수" in names or "물" in names.split()):
        result.dishForm = "UNKNOWN"
        _limit(result, "SOUP_WITHOUT_LIQUID")
    if result.dishForm == "SOUP_STEW" and result.servingTemperature == "COLD" and not any(word in title + steps for word in COLD_WORDS):
        result.servingTemperature = "UNKNOWN"
        _limit(result, "COLD_SOUP_WITHOUT_COLD_SIGNAL")
    if "CRISPY" in result.textures and not any(word in steps or word in title for word in CRISPY_WORDS):
        result.textures = [value for value in result.textures if value != "CRISPY"]
        _limit(result, "CRISPY_WITHOUT_DRY_HEAT")
    sweet = taste_signal("SWEET", f"{title} {names}")
    if result.dishForm == "DESSERT" and not sweet:
        result.dishForm = "UNKNOWN"
        _limit(result, "DESSERT_WITHOUT_SWEET_SIGNAL")
    if "DESSERT" in result.mealPurposes and not sweet:
        result.mealPurposes = [value for value in result.mealPurposes if value != "DESSERT"]
        _limit(result, "DESSERT_WITHOUT_SWEET_SIGNAL")
    if result.heaviness == "LIGHT" and frying_steps(row):
        result.heaviness = "UNKNOWN"
        _limit(result, "LIGHT_WITH_DEEP_FRYING")
    if result.difficulty == "EASY" and (step_count > 10 or ingredient_count > 18):
        result.difficulty = "UNKNOWN"
        _limit(result, "EASY_WITH_MANY_STEPS")
    if result.difficulty == "HARD" and step_count <= 3:
        result.difficulty = "UNKNOWN"
        _limit(result, "HARD_WITH_FEW_STEPS")
    if "QUICK_MEAL" in result.mealPurposes and step_count > 8:
        result.mealPurposes = [value for value in result.mealPurposes if value != "QUICK_MEAL"]
        _limit(result, "QUICK_MEAL_WITH_MANY_STEPS")
    result.mealPurposes = list(dict.fromkeys(result.mealPurposes))
    result.textures = list(dict.fromkeys(result.textures))
    return result


# 저장된 속성으로 맛 외 태그를 판정한다. 목록형 속성에서 빠졌다는 이유로 불일치를 확정하지 않는다.
def attribute_decisions(attributes: RecipeAttributes | dict, tags: list[str]) -> dict[str, tuple[str, str | None]]:
    value = attributes if isinstance(attributes, RecipeAttributes) else RecipeAttributes.model_validate(attributes)
    result = {}
    for tag in tags:
        key = canonical(tag)
        single = next(((table[key], getattr(value, field)) for table, field in ((CUISINE_TAGS, "cuisine"), (DIFFICULTY_TAGS, "difficulty"),
                       (DISH_FORM_TAGS, "dishForm"), (TEMPERATURE_TAGS, "servingTemperature"), (HEAVINESS_TAGS, "heaviness")) if key in table), None)
        if single is not None:
            wanted, actual = single
            status = "UNKNOWN" if actual == "UNKNOWN" else "MATCHED" if actual == wanted else "NOT_MATCHED"
        elif key in PURPOSE_TAGS:
            wanted = PURPOSE_TAGS[key]
            status = "MATCHED" if wanted in value.mealPurposes else "UNKNOWN"
        elif key in TEXTURE_TAGS:
            wanted = TEXTURE_TAGS[key]
            status = "MATCHED" if wanted in value.textures else "UNKNOWN"
        else:
            continue
        result[tag] = (status, f"AI 레시피 분류: {LABELS[wanted]}" if status == "MATCHED" else None)
    return result


# 순위 판단 입력용 축약 속성이다.
def compact_attributes(attributes: dict | None) -> dict | None:
    if not attributes:
        return None
    return {key: attributes[key] for key in ("cuisine", "dishForm", "servingTemperature", "heaviness", "difficulty", "mealPurposes", "textures")}
