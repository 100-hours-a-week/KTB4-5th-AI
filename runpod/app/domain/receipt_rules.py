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
    "SUBTOTAL",
    "TOTAL",
    "TAX",
    "CHANGE",
    "CASH",
    "CARD",
    "CHECK CLOSED",
    "CHECK OPENED",
    "BALANCE DUE",
}

# 상품번호 없는 영수증에서 "같은 행"으로 볼 y좌표 오차(px). GS25 실영수증에서 같은 행의
# 상품명·수량·금액 y중심이 최대 5px 정도 차이났던 걸 기준으로 여유를 뒀다.
_ROW_Y_TOLERANCE = 20

KNOWN_FOOD_CATEGORY_RULES = (
    (re.compile(r"우유|밀크", re.I), Category.DAIRY),
    # "콜라비"는 채소인데 음료 키워드 "콜라"가 먼저 매칭되면 BEVERAGE로 잘못 분류된다.
    # 구체적인 식재료 규칙을 더 넓은 음료 규칙보다 앞에 둔다.
    (re.compile(r"당근|콜라비", re.I), Category.VEGETABLE),
    (re.compile(r"바리스타|커피|coffee|coke|sprite|soda|음료|주스|콜라|샘물|생수|워터|water|소주|처음처럼|맥주|와인|수라즈|쉬라즈|까베르네|조니워커|하이네켄|삿포로|아사히|칭타오", re.I), Category.BEVERAGE),
    (
        re.compile(
            r"젤리|꼬깔콘|포스틱|감자칩|감자원|과자|카스타드|커스터드|custard|컵누들|비빔면|우동맛|짜파게|라면|피자|콤비네이션|호떡|"
            r"초코|쿠키|오레오|빼빼로|빼빼롯|마이쮸|마이쯤|츄러스|사탕|쫄병|오사쯔|"
            r"\b(?:rice|fried|fry|masala|biryani|curry|roll|buffet|dinner|lunch|chips|salsa|tostones)\b",
            re.I,
        ),
        Category.PROCESSED_FOOD,
    ),
    (re.compile(r"크랩마스터", re.I), Category.SEAFOOD),
    (re.compile(r"드레싱|솔트|소금|소스|미원", re.I), Category.SEASONING),
    (re.compile(r"딸기", re.I), Category.FRUIT),
    (re.compile(r"장아찌|와플|브라우니|케익|케이크", re.I), Category.PROCESSED_FOOD),
    (re.compile(r"아스파라거|애호박|호박|방울토마토|대추토마토|양파|깻잎|브로콜리|브로커리", re.I), Category.VEGETABLE),
    # '무'는 한 글자라서 무설탕·무첨가 같은 가공식품 문구에는 매칭하지 않고 독립 상품명만 인정한다.
    (re.compile(r"(?:^|[^가-힣])무(?:[^가-힣]|$)", re.I), Category.VEGETABLE),
    (re.compile(r"호주곡물|오이스터블", re.I), Category.MEAT),
)

_COMMON_OCR_CORRECTIONS = (
    (re.compile(r"챗잎|쳇잎"), "깻잎"),
    (re.compile(r"잉파"), "양파"),
    # 실제 하선정 영수증에서 "장조림"을 "장아씨/장아찌"로 반복 오독했다. 일반 장아찌를
    # 바꾸지 않고 OCR 원문에 브랜드·고유 문구·오독 문자열이 함께 있을 때만 복원한다.
    (re.compile(r"(하선정\s*바로\s*먹기\s*좋은)\s*장아씨", re.I), r"\1 장조림"),
    (re.compile(r"장아씨"), "장아찌"),
    (re.compile(r"((?:장아찌|장조림)\s*\d+)9\b"), r"\1g"),
    (re.compile(r"브로커리"), "브로콜리"),
    (re.compile(r"궁모닝우유"), "굿모닝우유"),
    (re.compile(r"성생감자칩"), "생생감자칩"),
    (re.compile(r"가루바감자원"), "가루비감자원"),
    (re.compile(r"컵누드"), "컵누들"),
    (re.compile(r"비방면|비범면"), "비빔면"),
    # 실제 영수증 반복 실측에서 같은 형태로 재현된 상품명 오독이다. 짧은 일반 단어를
    # 광범위하게 바꾸지 않고 브랜드·상품명이 함께 있는 문자열만 교정한다.
    (re.compile(r"해태\s*오사(?=[^가-힣]|$)"), "해태 오사쯔"),
    (re.compile(r"돌심\s*졸병"), "농심 쫄병"),
    (re.compile(r"괄별사탐"), "왕별사탕"),
    (re.compile(r"크라운\s*마이쯤\s*애를망고"), "크라운 마이쮸 애플망고"),
    (re.compile(r"(크라운\s*마이쮸\s*애플망고)\s*\(?44(?!\s*g)", re.I), r"\1 44g"),
    (re.compile(r"롯데\s*빼빼롯"), "롯데 빼빼로"),
    (re.compile(r"롯데\s*크런키\s*카카오쿠키\s*\(?58\b"), "롯데 크런키 카카오쿠키 58g"),
)


# 반복 실측으로 확인된 한글 OCR 오인식만 보수적으로 교정해 상품명·규칙 입력으로 반환한다.
def correct_common_ocr_errors(text: str) -> str:
    corrected = text
    for pattern, replacement in _COMMON_OCR_CORRECTIONS:
        corrected = pattern.sub(replacement, corrected)
    return corrected


# 먹을 수 없는 생활용품. 마트·백화점 영수증에 식품과 섞여 찍히는데, Gemma가 "가공식품"으로 포함시키는
# 경우가 있었다(실측: 롯데백화점 알러지케어티슈·롤티슈·5겹 화장지, 이전 배치의 극세사 걸레가 PROCESSED_FOOD).
# 분류 전에 규칙으로 빼서 Gemma에 보내지 않는다. "\d+겹"은 화장지 겹수 표기다("삼겹살"은 숫자가 아니라 제외됨).
NON_FOOD_PATTERN = re.compile(
    r"티슈|화장지|휴지|키친타월|물티슈|극세사|행주|수세미|종이봉투|쇼핑백|봉투|세제|섬유유연제|샴푸|린스|"
    r"바디워시|치약|칫솔|면도|건전지|호일|지퍼백|위생장갑|종이컵|위생컵|일회용컵|나무젓가락|"
    r"양복커버|종량제|지함|인형|스푼|티스푼|접시|식기|스텐(?:레스)?|충전기|어댑터|어답터|"
    r"지우개|비누|뷰티바|퍼퓸|향수|티라이트|패드|가글|세정제|냅킨|\d+겹",
    re.I,
)

# 식당 영수증에서 메뉴 아래에 붙는 구성 설명과 주문 마감 문구를 상품 분류 전에 제거한다.
RECEIPT_METADATA_PATTERN = re.compile(
    r"\b(?:served\s+with|comes\s+with|choice\s+of|check\s+(?:closed|opened)|balance\s+due)\b",
    re.I,
)


# 상품명이 생활용품(비식품) 규칙에 해당하는지 반환한다.
def is_non_food(text: str) -> bool:
    return NON_FOOD_PATTERN.search(text) is not None


# 영수증 문자열이 상품이 아닌 결제 메타데이터나 메뉴 구성 설명인지 반환한다.
def is_receipt_metadata(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text).strip()
    upper = compact.upper()
    return any(keyword.upper() in upper for keyword in NON_PRODUCT_KEYWORDS) or RECEIPT_METADATA_PATTERN.search(compact) is not None


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
# 영수증에만 흔히 찍히는 단어. 부가세·과세·면세·사업자는 거의 모든 국내 영수증 하단에 있다.
_RECEIPT_KEYWORDS = ("합계", "금액", "카드", "수량", "영수증", "부가세", "과세", "면세", "사업자")


def receipt_likelihood(lines: list[OcrLine]) -> float:
    if not lines:
        return 0.0
    text = "\n".join(line.text for line in lines)
    # 영수증은 "합   계"처럼 글자 사이를 띄워 인쇄하는 경우가 많고, OCR이 이를 "합"·"계" 두 줄로
    # 쪼개기도 한다. 공백·줄바꿈을 모두 지운 문자열에서 키워드를 찾는다.
    compact = re.sub(r"\s+", "", text)
    keyword_hits = sum(keyword in compact for keyword in _RECEIPT_KEYWORDS)
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
        if is_receipt_metadata(compact):
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
    text = correct_common_ocr_errors(text)
    for pattern, category in KNOWN_FOOD_CATEGORY_RULES:
        if pattern.search(text):
            return category
    return None


# 모델 제안 상품명을 받아 용량·가격·불필요 문자를 제거한 표시 이름을 반환한다.
def normalize_product_name(value: str) -> str:
    value = re.sub(r"^\s*\d{3}\*?\s*", "", value)
    value = re.sub(r"(?<!\d)\d+(?:[.,]\d+)?\s*(?:kg|g|ml|l|개입|개)\b", "", value, flags=re.I)
    value = re.sub(r"(?<!\d)\d{1,3}(?:,\d{3})+(?!\d).*$", "", value)
    value = re.sub(r"[^0-9A-Za-z가-힣%+&()\- ]", " ", value)
    return re.sub(r"\s+", " ", value).strip(" -()")


# OCR 상품행을 받아 측정 유형·수량·중량·단위·근거 문자열 tuple을 반환한다.
def parse_measurement(text: str, quantity_hint: int | None = None) -> tuple[str, int | None, float | None, str, str | None]:
    # "트랜스지방 0g"처럼 영양성분의 0 표기는 포장 용량이 아니므로 양수인 첫 용량만 쓴다.
    weight_match = next(
        (match for match in re.finditer(r"(?<!\d)(\d+(?:\.\d+)?)\s*(kg|g|ml|l)\b", text, re.I) if float(match.group(1)) > 0),
        None,
    )
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
    summary_start_y = _receipt_summary_start_y(positioned)
    unit_price_x, quantity_x, amount_x = _receipt_column_centers(positioned, image_width)
    unit_quantity_boundary = (unit_price_x + quantity_x) / 2
    quantity_amount_boundary = (quantity_x + amount_x) / 2

    anchors = _numbered_anchors(positioned, image_width)
    if anchors:
        anchors = _with_misread_numbered_anchors(anchors, positioned, image_width)
    if summary_start_y is not None:
        # 합계·과세 영역의 숫자와 글자를 상품번호로 잘못 복원하지 않는다. 특히 마지막 합계 아래의
        # "1제원" 같은 저신뢰 OCR 문자열이 가짜 상품행이 되어 직전 상품명에 결제 문구까지 붙는
        # 문제가 있었다.
        anchors = [anchor for anchor in anchors if anchor[1][1] < summary_start_y]
    if len(anchors) < 2:
        # "단가/수량/금액" 헤더가 없는 영수증(예: GS25)은 _receipt_column_centers가 전역 비율
        # 기본값으로 빠지는데, 실제 열 위치와 크게 어긋날 수 있다(GS25는 수량·금액 열이
        # 기본값보다 훨씬 왼쪽에 있었다). 실제 존재하는 숫자 토큰들의 위치로 다시 추정한다.
        empirical = _empirical_column_centers(positioned)
        if empirical is not None:
            unit_price_x, quantity_x, amount_x = empirical
            unit_quantity_boundary = (unit_price_x + quantity_x) / 2
            quantity_amount_boundary = (quantity_x + amount_x) / 2
        name_anchors = _name_anchors(positioned, image_width, unit_quantity_boundary, quantity_amount_boundary)
        # 번호형 앵커가 딱 1개면(상품 1개짜리 번호형 영수증) 그대로 쓰고, 이름형이 더 많이 잡히면 이름형을 쓴다.
        if len(name_anchors) > len(anchors):
            anchors = name_anchors
    # 상품이 1개뿐인 영수증도 행으로 복원한다(실측: 정육 1건·두유 1건 영수증이 행 복원 실패로 가격 힌트
    # 없이 Gemma에 넘어가 제외됐다). 앵커 자체의 조건(번호형: 줄 맨 앞 상품번호, 이름형: 같은 줄에
    # 금액 + 수량/단가)이 있어서 1개만으로도 상품행이라는 근거가 된다.
    if not anchors:
        return []
    anchors.sort(key=lambda item: item[1][1])

    gaps = [anchors[index + 1][1][1] - anchors[index][1][1] for index in range(len(anchors) - 1)]
    if gaps:
        typical_gap = sorted(gaps)[len(gaps) // 2]
    else:
        # 앵커가 하나면 간격을 잴 수 없어 그 줄 높이의 2배(상품명 줄 + 아래 숫자 줄)를 쓴다.
        ys = [point[1] for point in anchors[0][0].box]
        typical_gap = (max(ys) - min(ys)) * 2
    rows: list[ReceiptProductRow] = []

    # 상품번호형 영수증은 "001 상품명" 줄 아래 줄에 바코드·단가·수량·금액이 찍힌다. 두 상품명 사이
    # 중간을 경계로 자르면, 사진이 조금만 기울어도 숫자 줄이 다음 상품 쪽 구간에 들어가 수량·금액이
    # 한 칸씩 밀린다(실측: 장성농협 영수증에서 진라면의 "2개 7,160원"이 요구르트로 감). 그래서 번호형은
    # 구간을 "내 상품명 조금 위 ~ 다음 상품명 조금 위"로 잡아 아래 줄 숫자를 내 것으로 가져온다.
    # 번호 없는 영수증(GS25형)은 상품명과 숫자가 같은 줄이라 기존처럼 중간을 경계로 쓴다.
    numbered = anchors[0][3] is not None
    parsed_numbers = [item[3] for item in anchors]
    prefixes_are_row_sequence = parsed_numbers == list(range(1, len(anchors) + 1)) and any(re.match(r"^\s*\d{3}", item[0].text) for item in anchors)
    lead = typical_gap * 0.3

    for index, (anchor, (_, anchor_y), anchor_name, parsed_row_number) in enumerate(anchors):
        if numbered:
            upper = anchor_y - lead
            lower = anchors[index + 1][1][1] - lead if index + 1 < len(anchors) else anchor_y + typical_gap - lead
        else:
            upper = (anchors[index - 1][1][1] + anchor_y) / 2 if index > 0 else anchor_y - typical_gap / 2
            lower = (anchor_y + anchors[index + 1][1][1]) / 2 if index + 1 < len(anchors) else anchor_y + typical_gap / 2
        if summary_start_y is not None:
            lower = min(lower, summary_start_y)
        tokens = [(line, center) for line, center in positioned if line is not anchor and upper <= center[1] < lower]
        product_tokens = [
            (line, center)
            for line, center in tokens
            if image_width * 0.12 <= center[0] < image_width * 0.85
            and re.search(r"[가-힣A-Za-z]", line.text)
            and not re.fullmatch(r"[A-Za-z*]{1,2}", line.text.strip())
            and not any(keyword in line.text for keyword in _NOT_PRODUCT_NAME_TOKENS)
            and not is_receipt_metadata(line.text)
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
        if quantity is not None and not 1 <= quantity <= 100:
            quantity = None
        price_quantity = _quantity_from_price(unit_price, amount)
        if price_quantity is not None and quantity != price_quantity:
            quantity = price_quantity
        if quantity is None and any(re.fullmatch(r"\[?\d{7,13}\]?", line.text.strip()) for line, _ in tokens):
            quantity = 1
        if quantity is None and amount is not None:
            quantity = 1
        # 식당 영수증의 `2 Lunch`처럼 1~2자리 접두어는 행번호가 아니라 주문 수량이다.
        # 001/002... 형태의 순차적인 소매 영수증 번호는 기존 행번호로 유지한다.
        if quantity is None and not prefixes_are_row_sequence and parsed_row_number is not None and parsed_row_number <= 100:
            quantity = parsed_row_number
        expected_row_number = index + 1
        row_number = parsed_row_number if parsed_row_number == expected_row_number else expected_row_number
        rows.append(ReceiptProductRow(row_number=row_number, product_line=product_line, unit_price=unit_price, quantity=quantity, amount=amount))

    return rows


# OCR 좌표 목록에서 상품 영역이 끝나고 합계·세금 영역이 시작되는 최초 y좌표를 반환한다.
# "금액"은 열 헤더에도 있어 제외하고, 단독 "계"처럼 OCR이 쪼갠 글자는 오탐 가능성이 높아
# 두 글자 이상의 명확한 결제·세금 키워드만 사용한다.
def _receipt_summary_start_y(positioned: list[tuple[OcrLine, tuple[float, float]]]) -> float | None:
    summary_keywords = ("합계", "총액", "부가세", "과세", "면세", "결제대상", "받을금액", "SUBTOTAL", "ORDERTOTAL", "TOTAL", "TAX")
    candidates = [center_y for line, (_, center_y) in positioned if any(keyword in re.sub(r"\s+", "", line.text) for keyword in summary_keywords)]
    return min(candidates, default=None)


# 상품명 바로 아래에 찍히는 할인·포인트 줄. 같은 행 구간에 들어와도 상품명에 이어 붙이지 않는다
# (실측: 롯데마트 "소 척아이롤" + "[L.POINT 에누리]"가 한 이름으로 합쳐져 Gemma가 상품에서 뺐다).
_NOT_PRODUCT_NAME_TOKENS = ("할인", "REDACTED", "에누리", "POINT", "포인트", "쿠폰")

# 상품번호를 OCR이 잘못 읽은 줄("001" → "101"·"03"·"6"·"↓004")을 찾는 느슨한 패턴.
_MISREAD_NUMBER_ANCHOR = re.compile(r"^\W{0,2}(?P<number>\d{1,3})(?:\s+|(?=[가-힣A-Za-z]))(?P<name>.+)$")


# 정확한 상품번호 앵커가 하나라도 있는(=번호형으로 확인된) 영수증에서, 번호를 잘못 읽은 상품명 줄을 앵커로 더한다.
# 실측: 기울고 작은 롯데마트 영수증에서 001~006을 101·102·03·↓004·05·6으로 읽어 정확한 앵커는 2개뿐이었다.
# 느슨한 패턴은 "2 개" 같은 일반 문장에도 걸릴 수 있어, 줄 맨 왼쪽에 있고 바로 아래(줄 높이 2.5배 이내)
# 오른쪽 금액 열에 쉼표 금액이 있을 때만 인정한다.
def _with_misread_numbered_anchors(
    anchors: list[tuple[OcrLine, tuple[float, float], str, int]],
    positioned: list[tuple[OcrLine, tuple[float, float]]],
    image_width: int,
) -> list[tuple[OcrLine, tuple[float, float], str, int]]:
    known = {id(anchor[0]) for anchor in anchors}
    result = list(anchors)
    for line, center in positioned:
        if id(line) in known or _box_left(line) > image_width * 0.16:
            continue
        match = _MISREAD_NUMBER_ANCHOR.match(line.text.strip())
        if match is None or not re.search(r"[가-힣]", match.group("name")):
            continue
        ys = [point[1] for point in line.box]
        height = max(ys) - min(ys)
        has_amount_below = any(
            other_center[0] > image_width * 0.6 and center[1] - height * 0.5 <= other_center[1] <= center[1] + height * 2.5 and re.fullmatch(r"\d{1,3}(?:,\d{3})+", other.text.strip())
            for other, other_center in positioned
        )
        if has_amount_below:
            name = _strip_product_type_code(match.group("name").strip())
            result.append((line, center, name, int(match.group("number"))))
    return result


# 상품번호(001/002...) 접두어가 붙은 줄만 앵커로 인정해 (줄, 좌표, 상품명, 파싱된 행번호) 목록을 반환한다.
def _numbered_anchors(positioned: list[tuple[OcrLine, tuple[float, float]]], image_width: int) -> list[tuple[OcrLine, tuple[float, float], str, int]]:
    result: list[tuple[OcrLine, tuple[float, float], str, int]] = []
    for line, center in positioned:
        if _box_left(line) > image_width * 0.16:
            continue
        match = _product_anchor_match(line.text)
        if match is None:
            continue
        name = _strip_product_type_code(match.group("name").strip())
        if any(keyword in name for keyword in NON_PRODUCT_KEYWORDS):
            continue
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
        row_tolerance = max(_ROW_Y_TOLERANCE, _box_height(line) * 0.65)
        nearby = [(l2, c2) for l2, c2 in positioned if l2 is not line and abs(c2[1] - center[1]) <= row_tolerance]
        quantity = _column_integer(nearby, unit_quantity_boundary, quantity_amount_boundary, center[1])
        amount = _column_integer(nearby, quantity_amount_boundary, image_width * 1.01, center[1])
        # 수량 "1"은 글자가 작아 OCR이 자주 놓친다(실측: 울릉농협 10줄 중 6줄에서 누락). 수량이 없어도
        # 같은 줄에 단가가 있으면 "상품명 | 단가 | 금액" 구조로 보고 인정한다.
        unit_price = _column_integer(nearby, image_width * 0.35, unit_quantity_boundary, center[1]) if quantity is None else None
        has_barcode_below = any(c2[0] < image_width * 0.35 and center[1] < c2[1] <= center[1] + _box_height(line) * 1.5 and re.fullmatch(r"\[?\d{7,13}\]?", l2.text.strip()) for l2, c2 in positioned)
        if quantity is None and has_barcode_below:
            quantity = 1
        if amount is None or (quantity is None and unit_price is None and not has_barcode_below):
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
_PRODUCT_TYPE_CODE_PREFIX = re.compile(r"^[A-Za-z#≠](?=[가-힣])")


# 상품명 앞의 코드 문자를 제거한 문자열을 반환한다.
def _strip_product_type_code(text: str) -> str:
    return _PRODUCT_TYPE_CODE_PREFIX.sub("", text, count=1)


def _product_anchor_match(text: str) -> re.Match[str] | None:
    compact = text.strip()
    standalone = re.fullmatch(r"(?P<number>\d{1,3}\*?)(?P<name>)", compact)
    if standalone:
        return standalone
    # 식당·카페 영수증은 `1 Coffee`, `2 Lunch`처럼 1~2자리 수량/행번호가 상품명 앞에
    # 바로 찍힌다. 뒤에 글자가 있는 경우에만 1~3자리를 허용해 가격·전화번호와 구분한다.
    return re.match(r"^(?P<number>\d{1,3})(?:\s+|(?=[가-힣A-Za-z]))(?P<name>.*[가-힣A-Za-z].*)$", compact)


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
        if line.confidence >= 0.35 and minimum_x <= center_x < maximum_x and re.fullmatch(r"\d{1,3}(?:,\d{3})*", line.text.strip()):
            return int(line.text.replace(",", ""))
    return None


# 수량 열 OCR이 빠졌을 때 단가와 금액의 정확한 정수 비율로 1~100개 수량을 복원한다.
# 가격 할인이 적용된 행은 나누어떨어지지 않으므로 추정하지 않고 기존 기본값 처리에 맡긴다.
def _quantity_from_price(unit_price: int | None, amount: int | None) -> int | None:
    if unit_price is None or amount is None or unit_price <= 0 or amount <= 0:
        return None
    quantity, remainder = divmod(amount, unit_price)
    return quantity if remainder == 0 and 1 <= quantity <= 100 else None


# OCR 사각형의 평균 세로 길이를 반환해 원근이 있는 영수증의 행 허용 오차 계산에 사용한다.
def _box_height(line: OcrLine) -> float:
    if not line.box:
        return 0.0
    ys = [point[1] for point in line.box]
    return max(ys) - min(ys)


# OCR 상품행과 기준일을 받아 명시된 소비기한 날짜와 근거 문자열을 반환한다.
def parse_expiration(text: str, today: date) -> tuple[date | None, str | None]:
    # 과거 포장지의 "유통기한" 문구도 입력 호환성을 위해 인식하지만 API와 화면에는
    # 날짜 종류를 모두 소비기한(USE_BY)으로 정규화한다.
    keyword_match = re.search(
        r"(?:소비기한|유통기한|까지)\s*[:：]?\s*(20\d{2}|\d{2})[./-](\d{1,2})[./-](\d{1,2})",
        text,
    )
    if not keyword_match:
        return None, None
    try:
        year, month, day = (int(part) for part in keyword_match.groups())
        parsed = date(year + 2000 if year < 100 else year, month, day)
    except ValueError:
        return None, keyword_match.group(0)
    if parsed.year < today.year - 1 or parsed.year > today.year + 10:
        return None, keyword_match.group(0)
    return parsed, keyword_match.group(0)


PURCHASE_DATE_KEYWORD_PATTERN = re.compile(r"(?:구매일시|거래일시|판매일시|영수증일자)\s*[:：]?\s*(20\d{2})[./-](\d{1,2})[./-](\d{1,2})")
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
    # 미래 날짜와 365일보다 오래된 날짜는 믿지 않고 None을 반환한다. 전에는 연도만
    # 비교해서(today.year - 1) 2026-09-22 기준 2025-01-01까지 통과했다 — 1년 반 지난 영수증의 두부가
    # 이미 지난 소비기한(2025-04-09)으로 나왔다.
    if parsed > today or (today - parsed).days > 365:
        return None
    return parsed


# 식재료 카테고리를 받아 기본 냉장 또는 냉동 보관 제안을 반환한다.
def storage_for_category(category: str) -> str:
    return "FROZEN" if category in {"MEAT", "SEAFOOD"} else "REFRIGERATED"
