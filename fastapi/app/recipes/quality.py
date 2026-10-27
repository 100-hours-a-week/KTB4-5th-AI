"""추천 준비 데이터의 최소 완전성 검사. 의미 정확성·식품 안전 검증을 대신하지 않는다."""

import re

from app.recipes.ingredients import AMOUNT, parse_ingredient
from app.recipes.schemas import Ingredient, Recipe, normalize

_HEAT = re.compile(r"찌|찐다|쪄|삶|끓|굽|구워|볶|튀|오븐|가열|중탕")
_TIME = re.compile(r"\d+(?:\.\d+)?\s*(?:[~～–-]\s*\d+(?:\.\d+)?)?\s*(?:분|초|시간)")


# 원문에 명시된 계량 분량을 확인한다. 큰술·모·송이 등은 g·ml·EA로 환산하지 않는다.
def has_readable_amount(item: Ingredient) -> bool:
    text = item.amountText or ""
    if re.search(r"약간|조금|적당량|적당히|취향|기호", text):
        return True
    parsed = parse_ingredient(text, item.ingredientNo)
    return parsed is not None and normalize(parsed.name) == normalize(item.name) and AMOUNT.search(text) is not None


# 검증된 Recipe를 받아 손상 문자·분량 누락·가열 시간 미표기 사유 목록을 반환한다.
def review_reasons(recipe: Recipe) -> list[str]:
    reasons = []
    text = "\n".join([recipe.name, *(item.name for item in recipe.ingredients), *(step.instruction for step in recipe.steps)])
    if any(marker in text for marker in ("?", "�", "□", "???")):
        reasons.append("DAMAGED_OR_UNRESOLVED_TEXT")
    if any(not item.amountText or not item.amountText.strip() for item in recipe.ingredients):
        reasons.append("MISSING_AMOUNT_TEXT")
    if any(item.measureType == "UNKNOWN" and not has_readable_amount(item) for item in recipe.ingredients):
        reasons.append("UNRESOLVED_AMOUNT")
    instructions = "\n".join(step.instruction for step in recipe.steps)
    if _HEAT.search(instructions) and not _TIME.search(instructions):
        reasons.append("HEATING_WITHOUT_NUMERIC_TIME")
    if len(instructions.strip()) < 30:
        reasons.append("INSUFFICIENT_INSTRUCTIONS")
    return reasons
