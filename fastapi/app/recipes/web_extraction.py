"""웹 본문의 표기만 정리하고, 모델이 인용한 원문과 분량을 검증한다."""

import re

from app.recipes.ai_contract import ExtractedRecipe
from app.recipes.ingredients import NAME, parse_piece
from app.recipes.schemas import Ingredient

INGREDIENT_LABELS = {"재료", "재료소개", "준비재료", "주재료", "ingredients"}
STEP_LABELS = {"조리법", "만드는법", "만드는 법", "조리방법", "조리 방법", "조리순서", "조리 순서", "instructions", "directions"}


# 이미지/링크 주소/장식만 제거한다. 숫자, 단위, 문장 순서는 바꾸지 않으며 이 결과가 인용 검증 기준이다.
def source_text(content: str) -> str:
    text = re.sub(r"!\[[^\]\n]*\]\([^\n]*?\)", "", content)
    text = re.sub(r"\[([^\]\n]+)\]\([^\n]*?\)", r"\1", text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# 재료와 별도 대량 양념/배수 조리가 섞인 원문은 한 끼 수량으로 확정하지 않는다.
def mixed_batch(content: str) -> bool:
    text = source_text(content)
    return bool(re.search(r"(?:양념|소스|고추기름)[^\n]{0,80}(?:\d+\s*(?:번|회)|소분|냉동)|\d+\s*배\s*분량", text)
                or re.search(r"(?:양념|소스|고추기름)[\s\S]{0,80}만드시면\s*\d+\s*번", text))


# 제목 줄의 Markdown 장식과 인분 표기를 분리한다. 임의 문장을 재료 제목으로 추측하지 않는다.
def section_label(line: str) -> str:
    value = re.sub(r"^[\s>#*\-]+|[\s*\-:：]+$", "", line)
    value = re.sub(r"^\[([^]]+)\]\s*(?:\d+\s*인(?:분|기준))?\s*$", r"\1", value)
    return value.strip().casefold()


# 원문에서 하나의 명시적 인분만 발견된 경우에만 사용한다. 서로 다른 인분이나 혼합 배합은 추측하지 않는다.
def source_servings(text: str) -> float | None:
    values = {float(value) for value in re.findall(r"(?<!\d)(\d+(?:\.\d+)?)\s*인(?:분|기준)", text)}
    return next(iter(values)) if len(values) == 1 and next(iter(values)) > 0 and not mixed_batch(text) else None


# 표기상 이름/분량 경계가 있는 미지원 범위·단위는 수치화하지 않고 원문 그대로 보존한다.
def unmeasured_source_ingredient(text: str, number: int) -> Ingredient | None:
    found = re.match(r"^([가-힣A-Za-z][가-힣A-Za-z ()/\-]*?)\s+(?=[0-9½⅓⅔¼¾⅛⅜⅝⅞])", text)
    if not found or len(text) > 500:
        return None
    name = found[1].strip()
    if len(name) > 100 or re.search(r"넣|썰|볶|끓|찝|찌|삶|하세요|합니다", name):
        return None
    return Ingredient(ingredientNo=number, name=name, amountText=text)


# 번호 단계의 한 문단씩 보존한다. 번호 밖의 추가 조리문이 있으면 누락 위험으로 규칙 추출을 중단한다.
def numbered_instructions(lines: list[str]) -> list[str] | None:
    starts = [(index, re.match(r"^\s*(?:>\s*)?(\d+)[.)]\s+(.+)$", line)) for index, line in enumerate(lines)]
    starts = [(index, match) for index, match in starts if match]
    if not starts:
        return None
    if [int(match[1]) for _, match in starts] != list(range(1, len(starts) + 1)):
        return []
    result = []
    for n, (index, match) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        paragraph = [match[2].strip()]
        rest = lines[index + 1:end]
        boundary = next((i for i, line in enumerate(rest) if not line.strip()), len(rest))
        paragraph.extend(line.strip() for line in rest[:boundary])
        tail = "\n".join(rest[boundary:])
        if re.search(r"넣어|볶아|끓여|썰어|찐다|찝니다|찌고|찌세요|삶아|익혀|뒤집|식혀|버무려", tail):
            return []
        result.append("\n".join(paragraph))
    return result


# 원문의 한 재료를 읽고 미지원 표기는 UNKNOWN으로 보존한다. 알려진 분량을 모델이 바꾸지 못하게 한다.
def quoted_ingredient(name: str, quote: str, number: int) -> Ingredient | None:
    parsed = parse_piece(quote)
    if parsed and len(parsed) == 1:
        item = parsed[0]
        if name not in quote:
            return None
        # 모델의 짧은 이름으로 교체하지 않고 원문 파서의 전체 이름/수량을 유지한다.
        return item.model_copy(update={"ingredientNo": number})
    if not NAME.fullmatch(name) or name not in quote or re.search(r"\d", name):
        return None
    return Ingredient(ingredientNo=number, name=name, amountText=quote)


# 모델 출력은 원문 인용의 부분 문자열과 순서를 모두 검증한다. 요약/새 수치/중복/혼합 배합은 거부한다.
def validate_extraction(result: ExtractedRecipe, text: str, title: str) -> dict | None:
    if result.status != "EXTRACTED" or mixed_batch(text) or result.title not in text and result.title != title:
        return None
    if not result.ingredients or not result.steps:
        return None
    ingredients, used_quotes = [], set()
    previous = -1
    for row in result.ingredients:
        position = text.find(row.quote)
        if position < previous or position < 0 or row.quote in used_quotes or row.name not in row.quote:
            return None
        item = quoted_ingredient(row.name, row.quote, len(ingredients) + 1)
        if item is None:
            return None
        ingredients.append(item)
        used_quotes.add(row.quote)
        previous = position
    # 명시적인 재료 구획에서 읽을 수 있는 항목을 모델이 누락했으면 수용하지 않는다.
    in_ingredients = False
    for line in text.splitlines():
        label = section_label(line)
        if label in INGREDIENT_LABELS:
            in_ingredients = True
            continue
        if label in STEP_LABELS or re.match(r"^#{1,6}\s", line):
            in_ingredients = False
        candidate = re.sub(r"^\s*(?:[-*+]\s+|>\s*)", "", line).strip().strip("*")
        if in_ingredients and parse_piece(candidate) and not any(candidate in row.quote for row in result.ingredients):
            return None
    previous = -1
    for step in result.steps:
        position = text.find(step, previous + 1)
        if position < 0 or len(step) > 10000:
            return None
        previous = position
    # 원문에 조리 단계 번호가 있으면 각 단계 문장을 빠뜨리지 않았는지 추가 확인한다.
    numbered = re.findall(r"(?m)^\s*(?:>\s*)?\d+[.)]\s+(.+)$", text)
    declared = [line for line in numbered if re.search(r"넣|썰|썹|볶|끓|찜|찌|굽|구워|데치|삶|섞|버무|헹|자르|덮|뒤집|식혀", line)]
    if any(not any(line in step for step in result.steps) for line in declared):
        return None
    # 번호 단계에 이어지는 같은 문단의 시간 / 분량 / 마무리 문장도 빠뜨리지 않는다.
    paragraphs = numbered_instructions(text.splitlines())
    if paragraphs and any(not any(paragraph in step for step in result.steps) for paragraph in paragraphs):
        return None
    return {"name": result.title, "ingredients": ingredients, "steps": result.steps}
