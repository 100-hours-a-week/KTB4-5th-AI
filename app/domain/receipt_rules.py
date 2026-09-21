import re
from datetime import date

from app.domain.models import OcrLine, ReceiptProductRow
from app.schemas import Category

SENSITIVE_PATTERNS = (
    re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),
    re.compile(r"(?<!\d)01[016789][- ]?\d{3,4}[- ]?\d{4}(?!\d)"),
    re.compile(r"(?<!\d)\d{2,3}[- ]\d{3,4}[- ]\d{4}(?!\d)"),
)

NON_PRODUCT_KEYWORDS = {
    "합계",
    "총액",
    "부가세",
    "과세",
    "면세",
    "승인",
    "카드",
    "현금",
    "거스름돈",
    "영수증",
    "사업자",
    "대표자",
    "전화",
    "주소",
    "일시",
    "결제",
    "할인",
    "포인트",
    "수량",
    "단가",
    "금액",
}

# 상품번호 없는 영수증에서 "같은 행"으로 볼 y좌표 오차(px). GS25 실영수증에서 같은 행의
# 상품명·수량·금액 y중심이 최대 5px 정도 차이났던 걸 기준으로 여유를 뒀다.
_ROW_Y_TOLERANCE = 20

KNOWN_FOOD_CATEGORY_RULES = (
    (re.compile(r"우유|밀크", re.I), Category.DAIRY),
    (re.compile(r"바리스타|커피|음료|주스|콜라|샘물|생수|워터|소주|처음처럼|맥주|와인|수라즈|쉬라즈|까베르네|조니워커|하이네켄|삿포로|아사히|칭타오", re.I), Category.BEVERAGE),
    (re.compile(r"젤리|꼬깔콘|포스틱|짜파게|라면|피자|콤비네이션", re.I), Category.PROCESSED),
    (re.compile(r"드레싱|솔트|소금|소스", re.I), Category.SEASONING),
    (re.compile(r"딸기", re.I), Category.FRUIT),
    (re.compile(r"아스파라거|애호박|호박|장아찌", re.I), Category.VEGETABLE),
    (re.compile(r"호주곡물|오이스터블", re.I), Category.MEAT),
)


# OCR 문자열을 받아 카드·전화번호 패턴을 REDACTED 표기로 바꿔 반환한다.
def redact_sensitive_text(text: str) -> str:
    redacted = text
    for pattern in SENSITIVE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


# OCR 줄 tuple을 받아 빈 줄 제거와 민감정보 마스킹이 적용된 새 줄 목록을 반환한다.
def sanitize_lines(lines: tuple[OcrLine, ...]) -> list[OcrLine]:
    return [
        OcrLine(
            line_no=line.line_no,
            text=redact_sensitive_text(line.text).strip(),
            confidence=line.confidence,
            box=line.box,
        )
        for line in lines
        if line.text.strip()
    ]


# 마스킹된 OCR 줄을 받아 영수증 키워드·가격·날짜 기반 0~1 신뢰도를 반환한다.
def receipt_likelihood(lines: list[OcrLine]) -> float:
    if not lines:
        return 0.0
    text = "\n".join(line.text for line in lines)
    keyword_hits = sum(keyword in text for keyword in ("합계", "금액", "카드", "수량", "영수증"))
    price_hits = len(re.findall(r"(?<!\d)\d{1,3}(?:,\d{3})+(?!\d)", text))
    date_hits = len(re.findall(r"20\d{2}[./-]\d{1,2}[./-]\d{1,2}", text))
    score = 0.15 + min(keyword_hits * 0.12, 0.48) + min(price_hits * 0.04, 0.24)
    score += min(date_hits * 0.08, 0.08)
    return max(0.0, min(score, 0.99))


# 전체 OCR 줄을 받아 합계·결제·숫자 중심 줄을 제외한 식품 후보 줄을 반환한다.
def build_food_candidate_lines(lines: list[OcrLine]) -> list[OcrLine]:
    candidates: list[OcrLine] = []
    for line in lines:
        compact = re.sub(r"\s+", " ", line.text).strip()
        if len(compact) < 2 or compact == "[REDACTED]":
            continue
        if any(keyword in compact for keyword in NON_PRODUCT_KEYWORDS):
            continue
        if not re.search(r"[가-힣A-Za-z]", compact):
            continue
        digit_ratio = sum(ch.isdigit() for ch in compact) / max(len(compact), 1)
        if digit_ratio > 0.7:
            continue
        candidates.append(OcrLine(line_no=line.line_no, text=compact, confidence=line.confidence, box=line.box))
    return candidates


# 상품명 문자열을 받아 명확한 키워드 규칙에 해당하는 식품 카테고리 또는 None을 반환한다.
def known_food_category(text: str) -> Category | None:
    for pattern, category in KNOWN_FOOD_CATEGORY_RULES:
        if pattern.search(text):
            return category
    return None


# 모델 제안 상품명을 받아 용량·가격·불필요 문자를 제거한 표시 이름을 반환한다.
def normalize_product_name(value: str) -> str:
    value = re.sub(r"\b\d+(?:[.,]\d+)?\s*(?:kg|g|ml|l|개입|개)\b", "", value, flags=re.I)
    value = re.sub(r"(?<!\d)\d{1,3}(?:,\d{3})+(?!\d).*$", "", value)
    value = re.sub(r"[^0-9A-Za-z가-힣%+&()\- ]", " ", value)
    return re.sub(r"\s+", " ", value).strip(" -")


# OCR 상품행을 받아 측정 유형·수량·중량·단위·근거 문자열 tuple을 반환한다.
def parse_measurement(text: str, quantity_hint: int | None = None) -> tuple[str, int | None, float | None, str, str | None]:
    weight_match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(kg|g|ml|l)\b", text, re.I)
    count_match = re.search(r"(?<!\d)(\d{1,3})\s*(?:개입|개|EA)\b", text, re.I)

    if weight_match:
        value = float(weight_match.group(1))
        raw_unit = weight_match.group(2).lower()
        if raw_unit == "kg":
            value *= 1000
            unit = "G"
        elif raw_unit == "l":
            value *= 1000
            unit = "ML"
        elif raw_unit == "g":
            unit = "G"
        else:
            unit = "ML"
        return "WEIGHT", None, value, unit, weight_match.group(0)

    if count_match:
        quantity = int(count_match.group(1))
        if 1 <= quantity <= 100:
            return "COUNT", quantity, None, "NONE", count_match.group(0)

    if quantity_hint is not None and 1 <= quantity_hint <= 100:
        return "COUNT", quantity_hint, None, "NONE", f"영수증 수량 열: {quantity_hint}"

    return "COUNT", 1, None, "NONE", None


# OCR 박스 좌표와 이미지 너비를 받아 상품명·단가·수량·금액이 연결된 영수증 상품 행을 반환한다.
# 상품번호(001/002...) 앞자리가 있는 영수증이 우선이고, 없으면(예: GS25) 좌측 텍스트 줄 중
# 같은 행에 수량·금액이 모두 붙어 있는 줄만 상품행으로 인정하는 대체 방식으로 넘어간다.
def build_receipt_product_rows(lines: list[OcrLine], image_width: int) -> list[ReceiptProductRow]:
    positioned = [(line, _box_center(line)) for line in lines if line.box]
    unit_price_x, quantity_x, amount_x = _receipt_column_centers(positioned, image_width)
    unit_quantity_boundary = (unit_price_x + quantity_x) / 2
    quantity_amount_boundary = (quantity_x + amount_x) / 2

    anchors = _numbered_anchors(positioned, image_width)
    if len(anchors) < 2:
        # "단가/수량/금액" 헤더가 없는 영수증(예: GS25)은 _receipt_column_centers가 전역 비율
        # 기본값으로 빠지는데, 실제 열 위치와 크게 어긋날 수 있다(GS25는 수량·금액 열이
        # 기본값보다 훨씬 왼쪽에 있었다). 실제 존재하는 숫자 토큰들의 위치로 다시 추정한다.
        empirical = _empirical_column_centers(positioned)
        if empirical is not None:
            unit_price_x, quantity_x, amount_x = empirical
            unit_quantity_boundary = (unit_price_x + quantity_x) / 2
            quantity_amount_boundary = (quantity_x + amount_x) / 2
        anchors = _name_anchors(positioned, image_width, unit_quantity_boundary, quantity_amount_boundary)
    if len(anchors) < 2:
        return []
    anchors.sort(key=lambda item: item[1][1])

    gaps = [anchors[index + 1][1][1] - anchors[index][1][1] for index in range(len(anchors) - 1)]
    typical_gap = sorted(gaps)[len(gaps) // 2]
    rows: list[ReceiptProductRow] = []

    for index, (anchor, (_, anchor_y), anchor_name, parsed_row_number) in enumerate(anchors):
        upper = (anchors[index - 1][1][1] + anchor_y) / 2 if index > 0 else anchor_y - typical_gap / 2
        lower = (anchor_y + anchors[index + 1][1][1]) / 2 if index + 1 < len(anchors) else anchor_y + typical_gap / 2
        tokens = [(line, center) for line, center in positioned if line is not anchor and upper <= center[1] < lower]
        product_tokens = [
            (line, center)
            for line, center in tokens
            if image_width * 0.12 <= center[0] < image_width * 0.85 and re.search(r"[가-힣A-Za-z]", line.text) and not any(keyword in line.text for keyword in ("할인", "REDACTED"))
        ]
        product_tokens.sort(key=lambda item: item[1][0])
        if anchor_name:
            product_tokens.insert(0, (OcrLine(line_no=anchor.line_no, text=anchor_name, confidence=anchor.confidence, box=anchor.box), _box_center(anchor)))
        if not product_tokens:
            continue

        product_text = " ".join(line.text.strip() for line, _ in product_tokens)
        product_line = OcrLine(line_no=product_tokens[0][0].line_no, text=product_text, confidence=min(line.confidence for line, _ in product_tokens), box=product_tokens[0][0].box)
        unit_price = _column_integer(tokens, image_width * 0.35, unit_quantity_boundary, anchor_y)
        quantity = _column_integer(tokens, unit_quantity_boundary, quantity_amount_boundary, anchor_y)
        amount = _column_integer(tokens, quantity_amount_boundary, image_width * 1.01, anchor_y)
        expected_row_number = index + 1
        row_number = parsed_row_number if parsed_row_number == expected_row_number else expected_row_number
        rows.append(ReceiptProductRow(row_number=row_number, product_line=product_line, unit_price=unit_price, quantity=quantity, amount=amount))

    return rows


# 상품번호(001/002...) 접두어가 붙은 줄만 앵커로 인정해 (줄, 좌표, 상품명, 파싱된 행번호) 목록을 반환한다.
def _numbered_anchors(
    positioned: list[tuple[OcrLine, tuple[float, float]]], image_width: int
) -> list[tuple[OcrLine, tuple[float, float], str, int]]:
    result: list[tuple[OcrLine, tuple[float, float], str, int]] = []
    for line, center in positioned:
        if _box_left(line) > image_width * 0.16:
            continue
        match = _product_anchor_match(line.text)
        if match is None:
            continue
        name = _strip_product_type_code(match.group("name").strip())
        result.append((line, center, name, int(match.group("number").rstrip("*"))))
    return result


# 같은 행에 수량·금액 열 값이 모두 있는 좌측 텍스트 줄만 앵커로 인정한다(GS25처럼 상품번호가 없는 영수증용).
# 수량과 금액을 동시에 요구해 매장명·주소·정책 안내문 같은 비상품 줄과, 수량 없이 금액만 있는
# 소계/부가세 같은 합계 줄을 자연스럽게 걸러낸다. 행번호가 없으므로 순번(index+1)을 그대로 쓴다.
def _name_anchors(
    positioned: list[tuple[OcrLine, tuple[float, float]]],
    image_width: int,
    unit_quantity_boundary: float,
    quantity_amount_boundary: float,
) -> list[tuple[OcrLine, tuple[float, float], str, None]]:
    result: list[tuple[OcrLine, tuple[float, float], str, None]] = []
    for line, center in positioned:
        if _box_left(line) > image_width * 0.16:
            continue
        if _product_anchor_match(line.text) is not None:
            continue
        text = line.text.strip()
        if not re.search(r"[가-힣A-Za-z]", text):
            continue
        if any(keyword in text for keyword in NON_PRODUCT_KEYWORDS) or "REDACTED" in text:
            continue
        nearby = [(l2, c2) for l2, c2 in positioned if l2 is not line and abs(c2[1] - center[1]) <= _ROW_Y_TOLERANCE]
        quantity = _column_integer(nearby, unit_quantity_boundary, quantity_amount_boundary, center[1])
        amount = _column_integer(nearby, quantity_amount_boundary, image_width * 1.01, center[1])
        if quantity is None or amount is None:
            continue
        result.append((line, center, text, None))
    return result


# 순수 숫자(수량형)·콤마 포함 숫자(금액형) 토큰들의 실제 x좌표 분포로 열 위치를 추정한다.
# "단가/수량/금액" 헤더가 없어 _receipt_column_centers가 전역 비율 기본값으로 빠지는
# 영수증(예: GS25)에서, 실제 열이 기본값과 크게 어긋나 있으면 이 값을 대신 쓴다.
# 단가 열은 실측할 수 없어(수량이 항상 1이면 값이 따로 없는 경우가 많다) 수량-금액 간격만큼
# 수량보다 왼쪽으로 뒀다 — 정확한 값이 필요해서가 아니라 열 경계 계산이 깨지지 않게 하기 위함이다.
def _empirical_column_centers(positioned: list[tuple[OcrLine, tuple[float, float]]]) -> tuple[float, float, float] | None:
    quantity_xs = sorted(center[0] for line, center in positioned if re.fullmatch(r"\d{1,2}", line.text.strip()))
    amount_xs = sorted(center[0] for line, center in positioned if re.fullmatch(r"\d{1,3}(?:,\d{3})+", line.text.strip()))
    if not quantity_xs or not amount_xs:
        return None
    quantity_x = quantity_xs[len(quantity_xs) // 2]
    amount_x = amount_xs[len(amount_xs) // 2]
    if amount_x <= quantity_x:
        return None
    unit_price_x = quantity_x - (amount_x - quantity_x)
    return unit_price_x, quantity_x, amount_x


# OCR 줄의 사각형 좌표를 받아 중심점 좌표를 반환한다.
def _box_center(line: OcrLine) -> tuple[float, float]:
    xs = [point[0] for point in line.box]
    ys = [point[1] for point in line.box]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


# OCR 줄의 사각형 좌표를 받아 가장 왼쪽 x 좌표를 반환한다.
def _box_left(line: OcrLine) -> float:
    return min(point[0] for point in line.box)


# OCR 문자열이 상품 번호 단독 또는 상품명과 합쳐진 번호 행인지 확인해 정규식 결과를 반환한다.
# 상품번호 뒤에 붙는 단일 문자 코드(예: P=포인트적립, #=영세, ≠=면세)를 상품명에서 제거한다.
# 일부 마트 영수증은 "품번 뒤 코드"를 상품명과 공백 없이 붙여 출력하는데, 이 코드가 남아
# 있으면 정규식 키워드 매칭과 Gemma 분류 둘 다에서 "양파"→"P양파"처럼 상품을 못 알아본다.
# 영문 대문자/코드 기호 1글자가 뒤이어 오는 한글과 공백 없이 붙어 있을 때만 제거해,
# 실제로 영문으로 시작하는 상품명(예: "Post시리얼")까지 잘못 깎아내지 않게 좁혀 뒀다.
_PRODUCT_TYPE_CODE_PREFIX = re.compile(r"^[A-Z#≠](?=[가-힣])")


# 상품명 앞의 코드 문자를 제거한 문자열을 반환한다.
def _strip_product_type_code(text: str) -> str:
    return _PRODUCT_TYPE_CODE_PREFIX.sub("", text, count=1)


def _product_anchor_match(text: str) -> re.Match[str] | None:
    compact = text.strip()
    standalone = re.fullmatch(r"(?P<number>\d{1,3}\*?)(?P<name>)", compact)
    if standalone:
        return standalone
    return re.match(r"^(?P<number>\d{3})(?:\s+|(?=[가-힣A-Za-z]))(?P<name>.+)$", compact)


# OCR 열 제목의 중심 좌표를 찾아 단가·수량·금액 열 중심을 반환하며 제목이 없으면 비율 기본값을 사용한다.
def _receipt_column_centers(positioned: list[tuple[OcrLine, tuple[float, float]]], image_width: int) -> tuple[float, float, float]:
    defaults = {"단가": image_width * 0.63, "수량": image_width * 0.76, "금액": image_width * 0.91}
    centers = defaults.copy()
    for line, (center_x, _) in positioned:
        compact = re.sub(r"\s+", "", line.text)
        for heading in centers:
            if compact == heading:
                centers[heading] = center_x
    return centers["단가"], centers["수량"], centers["금액"]


# OCR 토큰 목록과 열의 x 범위를 받아 해당 열의 첫 정수 값을 반환한다.
def _column_integer(tokens: list[tuple[OcrLine, tuple[float, float]]], minimum_x: float, maximum_x: float, anchor_y: float) -> int | None:
    for line, (center_x, _) in sorted(tokens, key=lambda item: abs(item[1][1] - anchor_y)):
        if minimum_x <= center_x < maximum_x and re.fullmatch(r"\d{1,3}(?:,\d{3})*", line.text.strip()):
            return int(line.text.replace(",", ""))
    return None


# OCR 상품행과 기준일을 받아 명시된 소비기한 날짜와 근거 문자열을 반환한다.
def parse_expiration(text: str, today: date) -> tuple[date | None, str | None]:
    keyword_match = re.search(
        r"(?:소비기한|유통기한|까지)\s*[:：]?\s*(20\d{2})[./-](\d{1,2})[./-](\d{1,2})",
        text,
    )
    if not keyword_match:
        return None, None
    try:
        parsed = date(*(int(part) for part in keyword_match.groups()))
    except ValueError:
        return None, keyword_match.group(0)
    if parsed.year < today.year - 1 or parsed.year > today.year + 10:
        return None, keyword_match.group(0)
    return parsed, keyword_match.group(0)


PURCHASE_DATE_KEYWORD_PATTERN = re.compile(
    r"(?:구매일시|거래일시|판매일시|영수증일자)\s*[:：]?\s*(20\d{2})[./-](\d{1,2})[./-](\d{1,2})"
)
PURCHASE_DATE_FALLBACK_PATTERN = re.compile(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})")


# 마스킹된 OCR 줄 전체를 받아 영수증에 찍힌 구매일을 찾아 반환한다.
def parse_purchase_date(lines: list[OcrLine], today: date) -> date | None:
    text = "\n".join(line.text for line in lines)
    match = PURCHASE_DATE_KEYWORD_PATTERN.search(text) or PURCHASE_DATE_FALLBACK_PATTERN.search(text)
    if not match:
        return None
    try:
        parsed = date(*(int(part) for part in match.groups()))
    except ValueError:
        return None
    if parsed > today or parsed.year < today.year - 1:
        return None
    return parsed


# 식재료 카테고리를 받아 기본 냉장 또는 냉동 보관 제안을 반환한다.
def storage_for_category(category: str) -> str:
    return "FROZEN" if category in {"MEAT", "SEAFOOD"} else "REFRIGERATED"
