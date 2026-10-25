"""보수적인 재료 역할·주재료 보유 판정. 계열 관계를 대체·수량 충족의 근거로 쓰지 않는다."""

from functools import lru_cache

from app.recipes.retrieval import canonical, ingredient_family, missing_named_ingredients

# 물만 기본 가용으로 취급한다. 육수·탄산수·우유·소금물은 포함하지 않는다.
WATER_NAMES = {"물", "생수", "정수", "찬물", "따뜻한물", "뜨거운물", "끓는물", "냉수"}
SEASONING_NAMES = {
    "소금", "저염소금", "설탕", "후추", "후춧가루", "흑후추", "간장", "저염간장", "진간장", "국간장", "양조간장", "고추장", "된장", "식초", "사과식초",
    "맛술", "청주", "참기름", "들기름", "식용유", "식물성기름", "올리브유", "올리브오일", "발사믹식초", "고춧가루", "고추가루", "통깨", "깨", "깨소금",
    "다진마늘", "마늘다진것", "마늘", "생강", "다진생강", "꿀", "올리고당", "물엿", "알룰로스", "아가베시럽", "레몬즙", "굴소스", "케첩", "마요네즈",
    "피클링스파이스", "라임즙", "생강가루", "파슬리가루", "계피가루", "유자청", "매실액", "월계수잎", "통후추", "저염된장", "볶은소금", "포도씨유",
}
# 제목에 명시되면 MAIN이 우선한다. 양파가 원문에 있으면 적은 양이어도 임의 삭제하지 않는다.
SIDE_NAMES = {"양파", "적양파", "당근", "대파", "쪽파", "청양고추", "홍고추", "풋고추", "고추", "피망", "파프리카", "깻잎", "파슬리", "부추",
              "적파프리카", "홍파프리카", "청파프리카", "황파프리카", "청피망", "홍피망", "셀러리줄기", "다진땅콩", "새싹채소"}
MAIN_NAMES = {
    "두부", "순두부", "연두부", "계란", "우유", "두유", "치즈", "요거트", "고구마", "감자", "쌀", "흰밥", "밥", "현미", "메주콩", "콩", "검은콩", "병아리콩",
    "양배추", "배추", "배추김치", "김치", "브로콜리", "애호박", "오이", "무", "버섯", "표고버섯", "팽이버섯", "시금치", "토마토", "방울토마토",
    "사과", "배", "딸기", "바나나", "과일", "연어", "참치", "고등어", "대구", "명태", "홍합", "조개", "굴", "전복", "게", "소고기", "돼지고기", "닭고기", "오징어", "새우",
    "적양배추", "단호박", "밀가루", "강력분", "중력분", "박력분", "식빵", "쌀가루", "멥쌀", "한천", "호두", "아몬드", "포도", "생크림", "버터",
    "양상추", "블루베리", "메추리알", "홍시", "찹쌀", "들깨가루", "미역줄기", "햄", "베이컨", "소시지", "돈육", "통조림햄",
}
# 원문의 괄호·수식어 속에 명시된 핵심 식재료도 부재 여부를 검증한다. 보유 매칭을 확장하지 않는다.
MAIN_COMPONENTS = {name for name in MAIN_NAMES if len(name) >= 2} | {"새우", "우삼겹", "닭가슴살", "닭다리살"}


# 이름·제목별 정적 판정을 캐시한다. 미검토 재료를 부재료나 양념이라고 추측하지 않는다.
@lru_cache(maxsize=8192)
def ingredient_role(title: str, name: str) -> tuple[str, str]:
    key = canonical(name)
    if key in WATER_NAMES:
        return "WATER", "BASIC_WATER_POLICY"
    if missing_named_ingredients(title, [name], [name]):
        return "MAIN", "TITLE_MATCH"
    if key in SEASONING_NAMES:
        return "SEASONING", "REVIEWED_RULE"
    if key in SIDE_NAMES:
        return "SIDE", "REVIEWED_RULE"
    if key in MAIN_NAMES or ingredient_family(name):
        return "MAIN", "REVIEWED_RULE"
    if any(component in key for component in MAIN_COMPONENTS):
        return "MAIN", "REVIEWED_MAIN_COMPONENT"
    return "UNKNOWN", "UNREVIEWED"


# 같은 계열 재고는 확인 힌트만 반환한다. exact 사용 목록이나 분량 배분에 추가하지 않는다.
def related_stock(name: str, stock_names: list[str]) -> list[str]:
    family = ingredient_family(name)
    return sorted({value for value in stock_names if family and ingredient_family(value) == family and canonical(value) != canonical(name)})


# 원문·정확 매칭·분량 검사에서 역할 근거와 적합성 그룹을 만든다. public 원문 필드는 변경하지 않는다.
def assess_suitability(row: dict, stock_names: list[str]) -> dict:
    missing = {canonical(name) for name in row["missingIngredients"]}
    requirements = {item["ingredientNo"]: item for item in row["ingredientRequirements"]}
    facts = []
    for item in row["ingredients"]:
        role, evidence = ingredient_role(row["name"], item["name"])
        absent = canonical(item["name"]) in missing
        related = related_stock(item["name"], stock_names) if absent and role == "MAIN" else []
        facts.append({"ingredientNo": item["ingredientNo"], "name": item["name"], "role": role, "roleEvidence": evidence,
                      "missing": absent, "relatedStockNames": related, "quantityStatus": requirements[item["ingredientNo"]]["status"]})
    missing_main = [item for item in facts if item["role"] == "MAIN" and item["missing"] and not item["relatedStockNames"]]
    unresolved = [item for item in facts if item["role"] == "UNKNOWN" and item["missing"]]
    related_main = [item for item in facts if item["role"] == "MAIN" and item["relatedStockNames"]]
    main_shortage = any(item["role"] == "MAIN" and item["quantityStatus"] == "SHORTAGE" for item in facts)
    used_names = {canonical(name) for name in row["usesInventory"]}
    used_food = any(canonical(item["name"]) in used_names and item["role"] not in {"WATER", "SEASONING"} for item in facts)
    # 관련 계열뿐이거나 주재료 분량이 부족한 후보는 완전 보유·단위 확인 후보보다 뒤로 보낸다.
    tier = 3 if related_main or main_shortage else 2 if missing else 0 if row["quantityStatus"] == "SUFFICIENT" else 1
    return {"ingredients": facts, "missingMain": missing_main, "unresolvedMissing": unresolved, "relatedMain": related_main,
            "hasFoodMatch": used_food or bool(related_main), "tier": tier}
