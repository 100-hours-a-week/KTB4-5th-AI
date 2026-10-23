"""원문에서 명시한 재료·단위만 복원하는 식품안전나라 재료 파서."""

import re
from fractions import Fraction
from functools import lru_cache

from app.recipes.schemas import Ingredient, normalize

FRACTIONS = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8"}
NUMBER = r"(?:\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d+(?:\.\d+)?[½⅓⅔¼¾⅛⅜⅝⅞]?|[½⅓⅔¼¾⅛⅜⅝⅞])"
UNIT = r"(?:kg|㎏|mg|g|그램|ml|mL|㎖|cc|CC|L|l|ℓ|리터|개|대|쪽|장|모|알|뿌리|마리|컵|큰\s?술|작은\s?술|T|t|봉지|봉|팩|줌|줄기|토막|송이|조각|꼬집|포기|스푼|방울|매|통|잎|캔)"
AMOUNT = re.compile(rf"(?P<number>{NUMBER})\s*(?P<unit>{UNIT})(?P<note>(?:\s*\([^()]*\))*)")
NAME = re.compile(r"[가-힣A-Za-z0-9·\- /()]+")
AMBIGUOUS_SECTION = re.compile(r"^(?:[가-힣]*(?:소스|육수|양념|드레싱|준비)|곁들임)\s+\S")
HEADINGS = {"고명", "소스", "양념", "양념장", "주재료", "부재료", "재료", "장식", "육수", "드레싱", "필수재료", "선택재료", "절임물", "곁들임채소"}
PREFIXES = ("재료", "육수", "소스", "양념", "양념장", "다시마육수", "고기완자양념", "완자")
BARE_INGREDIENTS = {"표고버섯 기둥", "파마산치즈", "도토리묵", "올방개묵"}


# 명시된 요리·구역 제목과 인분 표기만 제거한다. 해석되지 않은 일반 줄은 보존한다.
def clean_sections(raw: str, title: str) -> str:
    # 같은 줄의 명시적 '- 양념장 :' 경계만 분리한다. 괄호 속 설명은 변경하지 않는다.
    original = raw
    raw = re.sub(r"\s+-\s*[가-힣0-9 ]{1,30}\s*[:：]\s*",
                 lambda match: "\n" if original[:match.start()].count("(") == original[:match.start()].count(")") else match.group(), raw)
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^\[\s*\d+(?:\.\d+)?\s*인분\s*\]\s*", "", line)
        heading = normalize(line.lstrip("●•■▶·- ").rstrip(":："))
        if heading == normalize(title) or heading in HEADINGS:
            continue
        line = re.sub(r"^[●•■▶·-]?\s*[가-힣0-9 ]{1,30}\s*[:：]\s*", "", line)
        for prefix in PREFIXES:
            if line.startswith(prefix + " "):
                line = line[len(prefix):].strip()
                break
        if line:
            lines.append(line)
    return "\n".join(lines)


# 괄호 바깥의 쉼표·줄바꿈만 구분자로 사용한다. 닫히지 않은 괄호를 임의 복구하지 않는다.
def ingredient_parts(raw: str, title: str) -> list[str] | None:
    text = clean_sections(raw, title)
    parts, current = [], []
    depth = 0
    for character in text:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                return None
        if character in {",", "\n"} and depth == 0:
            if "".join(current).strip():
                parts.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if depth:
        return None
    if "".join(current).strip():
        parts.append("".join(current).strip())
    return parts


# 정수·소수·분수·혼합 분수를 수치로 바꾼다. 0 분모와 0 이하 분량은 거부한다.
def amount_number(text: str) -> float:
    for glyph, fraction in FRACTIONS.items():
        text = text.replace(glyph, " " + fraction)
    pieces = text.strip().split()
    if "/" in text:
        text = re.sub(r"\s*/\s*", "/", text).strip()
        pieces = text.split()
    value = float(sum(Fraction(piece) for piece in pieces))
    if value <= 0:
        raise ValueError("분량은 양수여야 합니다.")
    return value


# 한 재료의 원문 분량을 보존하고 g·ml·정수 개수만 수치로 확정한다.
def parse_ingredient(part: str, number: int) -> Ingredient | None:
    text = part.strip().rstrip(".").strip()
    if text in BARE_INGREDIENTS:
        return Ingredient(ingredientNo=number, name=text, amountText=part)
    wrapped_unknown = re.fullmatch(r"([가-힣A-Za-z· ]+)\((약간|적당량|조금|적량)\)", text)
    if wrapped_unknown:
        text = wrapped_unknown.group(1).strip() + " " + wrapped_unknown.group(2)
    unknown = re.fullmatch(r"(.+?)\s*(약간|적당량|조금|적량)(?:\s*\([^()]*\))?", text)
    if unknown and NAME.fullmatch(unknown.group(1)) and not re.search(r"[0-9]", unknown.group(1)) and not AMBIGUOUS_SECTION.search(unknown.group(1)):
        return Ingredient(ingredientNo=number, name=unknown.group(1), amountText=part)
    name, match = None, None
    # 돼지고기(50g), 닭고기(가슴살, 120g)처럼 분량이 괄호 안에 있는 원문.
    wrapped = re.fullmatch(r"([^()]+)\(([^()]*)\)", text)
    if wrapped and NAME.fullmatch(wrapped.group(1).strip()) and not AMOUNT.search(wrapped.group(1)):
        inside = wrapped.group(2).strip()
        match = AMOUNT.fullmatch(inside)
        name = wrapped.group(1).strip()
        if match is None and "," in inside:
            descriptor, amount = inside.rsplit(",", 1)
            if NAME.fullmatch(descriptor.strip()):
                match = AMOUNT.fullmatch(amount.strip())
                name += f"({descriptor.strip()})"
    if match is None:
        # 스팸(마일드) 10g, 호두 1/2알, 물 1½컵 등의 후행 분량.
        for candidate in AMOUNT.finditer(text):
            if candidate.end() == len(text):
                prefix = text[:candidate.start()].strip()
                if prefix and NAME.fullmatch(prefix) and not AMOUNT.search(prefix):
                    name, match = prefix, candidate
                    break
    if match is None or not name or not NAME.fullmatch(name) or AMOUNT.search(name) or name.endswith(("-", "/", "(")) or name.count("(") != name.count(")"):
        return None
    # 표제와 재료 사이 구분자가 없으면 임의로 어느 쪽도 삭제하거나 합쳐 확정하지 않는다.
    if AMBIGUOUS_SECTION.search(name) or re.search(r"\d+\s*인분", name):
        return None
    try:
        value = amount_number(match["number"])
    except (ValueError, ZeroDivisionError):
        return None
    unit = match["unit"]
    kind, output_unit, amount = "UNKNOWN", None, None
    if unit in {"kg", "㎏", "mg", "g", "그램"}:
        kind, output_unit, amount = "WEIGHT", "G", value * (1000 if unit in {"kg", "㎏"} else 0.001 if unit == "mg" else 1)
    elif unit in {"ml", "mL", "㎖", "cc", "CC", "L", "l", "ℓ", "리터"}:
        kind, output_unit, amount = "VOLUME", "ML", value * (1000 if unit in {"L", "l", "ℓ", "리터"} else 1)
    elif unit == "개" and value.is_integer():
        kind, output_unit, amount = "COUNT", "EA", value
    return Ingredient(ingredientNo=number, name=name, amountText=part, measureType=kind, amountValue=amount, unit=output_unit)


# 구분자가 빠졌어도 공백 양쪽이 완전한 재료·분량일 때만 나눈다. 미해석 잔여는 허용하지 않는다.
def parse_piece(part: str) -> list[Ingredient] | None:
    @lru_cache(maxsize=128)
    def visit(text: str) -> tuple[Ingredient, ...] | None:
        item = parse_ingredient(text, 1)
        if item:
            return (item,)
        composite = re.fullmatch(r"(물녹말|소금물)\((.*)\)", text)
        if composite:
            children = ingredient_parts(composite.group(2), "")
            items = [parse_ingredient(child, 1) for child in children or []]
            if len(items) >= 2 and all(items):
                return tuple(items)
        if len(text) > 1000:
            return None
        depth = 0
        for index, character in enumerate(text):
            depth += int(character == "(") - int(character == ")")
            if character.isspace() and depth == 0 and text[index + 1:].strip():
                first = parse_ingredient(text[:index].strip(), 1)
                if first:
                    rest = visit(text[index + 1:].strip())
                    if rest:
                        return (first, *rest)
        return None

    result = visit(part)
    return list(result) if result else None


# 전 재료 조각이 파싱된 경우에만 목록을 반환한다. 한 조각이라도 실패하면 전체를 검토 대상으로 둔다.
def parse_ingredients(raw: str, title: str) -> list[Ingredient] | None:
    parts = ingredient_parts(raw, title)
    if not parts:
        return None
    ingredients = []
    for part in parts:
        items = parse_piece(part)
        if items is None:
            return None
        for item in items:
            ingredients.append(item.model_copy(update={"ingredientNo": len(ingredients) + 1}))
    return ingredients
