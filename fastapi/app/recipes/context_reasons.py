"""AI가 고른 '오늘 이 레시피' 이유 코드를 날씨 / 시간대 / 계절 / 저장 속성과 대조해 검증하고 사용자 문구로 바꾼다.

AI의 자유 문장은 사용자에게 그대로 보이지 않는다. 검증된 코드만 서버 문구로 표시해 사실과 다른 이유를 막는다.
"""
import re

from app.recipes.meal_quality import frying_steps

SITUATION_FACTORS = {"WARM_FOR_COLD", "SOUP_FOR_RAIN", "LIGHT_FOR_HEAT", "MEAL_TIME_FIT", "SEASON_FIT"}
HEAT_PATTERN = re.compile(r"끓|볶|굽|구워|튀기|튀겨|찌|쪄|졸이|조려|오븐|에어프라이|전자레인지|익히|익혀|부치|부쳐|삶|가열")
SOUP_TITLE = re.compile(r"국$|국\b|찌개|탕$|탕\b|전골|수프|스프|스튜|국밥|짬뽕|우동|라면|칼국수|수제비|떡국|만둣국")
COLD_TITLE = re.compile(r"냉|샐러드|무침|빙수|화채|아이스|콩국수|냉채|겉절이|생채")


# 저장 속성이 없으면 원문 제목 / 조리 단계로만 보수적으로 확인한다. 확인이 안 되면 False다.
def _hot(row: dict, attributes: dict | None) -> bool:
    if attributes and attributes.get("servingTemperature") != "UNKNOWN":
        return attributes["servingTemperature"] == "HOT"
    steps = " ".join(step["instruction"] for step in row["steps"])
    return bool(HEAT_PATTERN.search(steps)) and not COLD_TITLE.search(row["name"])


def _soup(row: dict, attributes: dict | None) -> bool:
    if attributes and attributes.get("dishForm") != "UNKNOWN":
        return attributes["dishForm"] == "SOUP_STEW"
    return bool(SOUP_TITLE.search(row["name"]))


def _light(row: dict, attributes: dict | None) -> bool:
    if attributes and attributes.get("heaviness") != "UNKNOWN":
        return attributes["heaviness"] == "LIGHT" or attributes.get("servingTemperature") == "COLD"
    return bool(COLD_TITLE.search(row["name"])) and not frying_steps(row)


def _purposes(attributes: dict | None) -> set[str]:
    return set(attributes.get("mealPurposes", [])) if attributes else set()


# 코드 하나가 현재 상황과 레시피에 맞는지 확인한다.
def factor_holds(factor: str, row: dict, context: dict) -> bool:
    weather = context.get("weather") or {}
    band = weather.get("temperatureBand")
    attributes = row.get("recipeAttributes")
    if factor == "WARM_FOR_COLD":
        return band in {"COLD", "CHILLY"} and _hot(row, attributes)
    if factor == "SOUP_FOR_RAIN":
        return weather.get("precipitation") in {"RAIN", "SLEET", "SNOW"} and _soup(row, attributes) and _hot(row, attributes)
    if factor == "LIGHT_FOR_HEAT":
        return band in {"WARM", "HOT"} and _light(row, attributes)
    if factor == "MEAL_TIME_FIT":
        meal = context.get("mealTime")
        purposes = _purposes(attributes)
        heaviness = (attributes or {}).get("heaviness")
        if meal == "BREAKFAST":
            return heaviness == "LIGHT" or "QUICK_MEAL" in purposes
        if meal in {"LUNCH", "DINNER"}:
            return bool(purposes & {"MEAL", "QUICK_MEAL"})
        return meal == "LATE_NIGHT" and ("SNACK" in purposes or heaviness == "LIGHT")
    if factor == "SEASON_FIT":
        season = context.get("season")
        return season == "WINTER" and _hot(row, attributes) or season == "SUMMER" and _light(row, attributes)
    if factor == "PREFERENCE_FIT":
        return row.get("preferenceMatch") == "MATCHED"
    if factor == "USES_EXPIRING":
        return bool(row.get("usesExpiringIngredients"))
    if factor == "LOW_SHOPPING":
        return len(row.get("missingIngredients", [])) <= 2
    return False


# 맞는 코드만 남긴다. 반환값은 (검증된 코드, 기각된 코드)다.
def verify_factors(row: dict, factors: list[str], context: dict) -> tuple[list[str], list[str]]:
    verified, rejected = [], []
    for factor in dict.fromkeys(factors):
        (verified if factor_holds(factor, row, context) else rejected).append(factor)
    return verified, rejected


# 검증된 상황 코드만 문장으로 만든다. 취향 / 임박 / 구매 이유는 기존 문구가 이미 설명하므로 반복하지 않는다.
def situation_sentence(factors: list[str], context: dict) -> str | None:
    weather = context.get("weather") or {}
    parts = []
    if "WARM_FOR_COLD" in factors:
        parts.append(f"체감 {weather.get('feelsLikeC')}°C로 쌀쌀해 따뜻하게 먹는 요리")
    if "SOUP_FOR_RAIN" in factors:
        parts.append({"SNOW": "눈", "SLEET": "진눈깨비"}.get(weather.get("precipitation"), "비") + " 오는 날 어울리는 국물 요리")
    if "LIGHT_FOR_HEAT" in factors:
        parts.append(f"기온 {weather.get('temperatureC')}°C로 더워 가볍거나 시원한 요리")
    if "MEAL_TIME_FIT" in factors:
        parts.append({"BREAKFAST": "아침에 부담이 적은 요리", "LUNCH": "점심 한 끼로 알맞은 요리", "DINNER": "저녁 한 끼로 알맞은 요리",
                      "LATE_NIGHT": "늦은 시간에 가볍게 먹기 좋은 요리"}[context["mealTime"]])
    if "SEASON_FIT" in factors and not {"WARM_FOR_COLD", "LIGHT_FOR_HEAT"} & set(factors):
        parts.append("겨울철에 어울리는 따뜻한 요리" if context.get("season") == "WINTER" else "여름철에 어울리는 가벼운 요리")
    if not parts:
        return None
    return "AI 상황 판단: " + ", ".join(parts) + "입니다."


# 상황 코드가 검증된 후보를 같은 우선순위 안에서 앞에 둔다. 임박 활용 / 취향 우선순위는 바꾸지 않는다.
def situation_priority(factors: list[str]) -> tuple[int, int]:
    count = len(SITUATION_FACTORS.intersection(factors))
    return (0 if count else 1, -count)
