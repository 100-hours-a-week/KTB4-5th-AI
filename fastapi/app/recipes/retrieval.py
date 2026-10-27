"""추천 재료 별칭과 제외 조건. 벡터 병합은 비교 실험에서만 사용한다."""

import re

from app.recipes.schemas import Recipe, normalize

# 명시적 동의어만 사용한다. 순두부/두부, 우유/두유처럼 대체 가능성을 추측하지 않는다.
ALIASES = {
    "달걀": "계란", "쇠고기": "소고기", "우육": "소고기", "돈육": "돼지고기", "무우": "무", "브로컬리": "브로콜리",
    "양송이": "양송이버섯", "모짜렐라치즈": "모차렐라치즈", "올리브오일": "올리브유",
    "빨강파프리카": "홍파프리카", "빨간파프리카": "홍파프리카", "노랑파프리카": "황파프리카", "노란파프리카": "황파프리카",
    "양파다진것": "다진양파", "당근다진것": "다진당근", "파다진것": "다진파",
    "청피망다진것": "다진청피망", "홍피망다진것": "다진홍피망", "마늘다진것": "다진마늘",
}
MATCH_VERSION = "recipe-ingredient-v3"
# 수집 원문의 용도 표제만 제거한다. 부위·가열·냉동·품종을 나타내는 괄호는 유지한다.
SECTION_LABELS = ("반죽재료", "속재료", "소스", "토핑재료", "토핑", "비스킷재료", "아몬드우유")
SECTION_PREFIX = re.compile(r"^\((?:" + "|".join(SECTION_LABELS) + r")\)(?=.)")

# 제목의 주재료 누락 경고에만 쓰는 검토된 표기 관계다. 재고 매칭·단위 환산·대체 가능성에는 쓰지 않는다.
TITLE_INGREDIENT_TERMS = {"통오징어": "오징어", "무청시래기": "시래기", "모차렐라치즈": "치즈", "플레인요거트": "요거트"}
TITLE_MATCH_VERSION = "title-ingredient-v3"

# 계열은 제목 연결·확인 필요 안내 전용이다. 정확 보유 매칭·분량 배분에 사용하지 않는다.
INGREDIENT_FAMILIES = {
    "소고기": {"소고기", "쇠고기", "우육", "소고기안심", "쇠고기안심", "소고기등심", "쇠고기등심", "다진소고기", "다진쇠고기", "소고기다짐육", "우삼겹", "다진쇠고기등심"},
    "돼지고기": {"돼지고기", "돈육", "돼지고기안심", "돼지고기등심", "다진돼지고기", "돼지고기다짐육", "삼겹살", "목살"},
    "닭고기": {"닭고기", "닭가슴살", "닭다리살", "닭안심", "닭고기(가슴살)", "닭고기(안심)", "다진닭고기", "닭날개", "닭봉", "닭윙"},
    "오징어": {"오징어", "통오징어", "오징어몸통", "오징어다리"},
    "새우": {"새우", "생새우", "새우살", "칵테일새우", "깐새우"},
    "계란": {"계란", "달걀", "계란흰자", "달걀흰자", "계란노른자", "달걀노른자"},
    "두부": {"두부", "순두부", "연두부"},
    "치즈": {"치즈", "모차렐라치즈", "모짜렐라치즈", "리코타치즈", "파마산치즈"},
    "요거트": {"요거트", "플레인요거트", "그릭요거트"},
    "시래기": {"시래기", "무청시래기", "무청시래기(삶은것)"},
    "버섯": {"버섯", "표고버섯", "팽이버섯", "느타리버섯", "새송이버섯", "양송이버섯", "송이버섯"},
    "김치": {"김치", "배추김치", "포기김치"},
    "마늘": {"마늘", "통마늘", "다진마늘", "마늘다진것"},
}
FAMILY_BY_NAME = {normalize(name): family for family, names in INGREDIENT_FAMILIES.items() for name in names}


# 검토된 계열만 반환한다. 모르는 이름의 부분 문자열·조리 상태를 추측하지 않는다.
def ingredient_family(name: str) -> str | None:
    return FAMILY_BY_NAME.get(canonical(name))


# 공백·유니코드와 검토된 용도 표제를 정규화한 뒤 같은 재료의 별칭만 치환한다.
def canonical(name: str) -> str:
    normalized = SECTION_PREFIX.sub("", normalize(name), count=1)
    return ALIASES.get(normalized, normalized)


# 제목에 실제로 명시된 원문 재료 중 없는 재료만 찾는다. 모든 주재료를 식별한다는 뜻은 아니다.
def missing_named_ingredients(name: str, ingredient_names: list[str], missing_names: list[str]) -> list[str]:
    title = canonical(name)
    missing = {canonical(value) for value in missing_names}
    # '무'→'무침'처럼 한 글자 부분 문자열이 다른 단어에 걸리는 오탐을 피한다.
    result = []
    for value in ingredient_names:
        if canonical(value) not in missing:
            continue
        terms = set(variants([value]))
        # 괄호의 조리 상태는 원문·분량 계산에 보존하고, 제목 연결에만 접두 명칭을 사용한다.
        base = canonical(value).split("(", 1)[0]
        terms.add(base)
        if base in TITLE_INGREDIENT_TERMS:
            terms.add(TITLE_INGREDIENT_TERMS[base])
        family = ingredient_family(value)
        if family:
            terms.update(variants([family]))
        if any(len(term) >= 2 and term in title for term in terms):
            result.append(value)
    return result


# DB 문자열 검색용 이름 집합을 정확 이름과 검토된 별칭으로 확장한다.
def variants(names: list[str]) -> list[str]:
    wanted = {canonical(name) for name in names}
    return sorted(wanted | {normalize(name) for name in names} | {name for name, target in ALIASES.items() if target in wanted})


# 검색 범위만 계열로 넓힌다. 보유·사용·분량 계산은 canonical 정확 매칭을 유지한다.
def query_variants(names: list[str]) -> list[str]:
    families = {ingredient_family(name) for name in names} - {None}
    values = set(variants(names)) | {normalize(name) for family in families for name in INGREDIENT_FAMILIES[family]}
    # 기존 DB의 ingredient_names는 원문 이름을 유지한다. 원문을 덮어쓰지 않고 조회 표현을 확장한다.
    return sorted(values | {f"({label}){value}" for value in values for label in SECTION_LABELS})


# 원문·별칭의 부분 문자열을 함께 검사한다. 계란 제외가 달걀노른자를 놓치지 않도록 한다.
def is_excluded(name: str, excluded: list[str]) -> bool:
    exclusions = variants([value for value in excluded if normalize(value)])
    for value in excluded:
        family = ingredient_family(value)
        if family and canonical(value) == canonical(family):
            exclusions.extend(normalize(member) for member in INGREDIENT_FAMILIES[family])
    return any(exclusion == candidate or (len(exclusion) >= 2 and exclusion in candidate)
               for candidate in {normalize(name), canonical(name)} for exclusion in exclusions)


# 후보가 요청 재료를 실제로 포함하고 제외 재료를 포함하지 않는지 문자열·별칭으로 재검증한다.
def allowed(recipe: Recipe, inventory: list[str], excluded: list[str]) -> bool:
    ingredients = {canonical(item.name) for item in recipe.ingredients}
    return recipe.status == "ACTIVE" and bool(ingredients.intersection(canonical(name) for name in inventory)) and not any(is_excluded(item.name, excluded) for item in recipe.ingredients)


# 이름 매칭 기반 부족 재료 감점을 계산한다. 수량 충족·알레르기 안전을 보증하지 않는다.
def coverage_score(recipe: Recipe, inventory: list[str], use_aliases: bool = False) -> float:
    transform = canonical if use_aliases else normalize
    names = {transform(item.name) for item in recipe.ingredients}
    used = names.intersection(transform(name) for name in inventory)
    return max(0, 0.6 * len(used) / max(1, len(names)) - 0.2 * len(names - used) / max(1, len(names)))


# 두 순위의 RRF를 구한다. 벡터 점수를 현재 추천 점수나 confidence로 노출하지 않는다.
def reciprocal_rank_fusion(lexical: list[Recipe], semantic: list[Recipe], inventory: list[str], excluded: list[str], limit: int) -> list[Recipe]:
    scores, recipes = {}, {}
    for ranked in (lexical, semantic):
        for rank, recipe in enumerate(ranked, start=1):
            key = (recipe.source, recipe.sourceRecipeId)
            recipes[key] = recipe
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    eligible = [key for key, recipe in recipes.items() if allowed(recipe, inventory, excluded)]
    eligible.sort(key=lambda key: (-coverage_score(recipes[key], inventory, True), -scores[key], key))
    return [recipes[key] for key in eligible[:limit]]
