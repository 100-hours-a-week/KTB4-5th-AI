"""추천 재료 별칭과 제외 조건. 벡터 병합은 비교 실험에서만 사용한다."""

from app.recipes.schemas import Recipe, normalize

# 명시적 동의어만 사용한다. 순두부/두부, 우유/두유처럼 대체 가능성을 추측하지 않는다.
ALIASES = {"달걀": "계란", "쇠고기": "소고기", "우육": "소고기", "돈육": "돼지고기", "무우": "무", "브로컬리": "브로콜리"}

# 제목의 주재료 누락 경고에만 쓰는 검토된 표기 관계다. 재고 매칭·단위 환산·대체 가능성에는 쓰지 않는다.
TITLE_INGREDIENT_TERMS = {"통오징어": "오징어", "무청시래기": "시래기", "모차렐라치즈": "치즈", "플레인요거트": "요거트"}
TITLE_MATCH_VERSION = "title-ingredient-v2"


# 공백·유니코드 정규화 뒤 검토된 별칭만 치환한다.
def canonical(name: str) -> str:
    normalized = normalize(name)
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
        if any(len(term) >= 2 and term in title for term in terms):
            result.append(value)
    return result


# DB 문자열 검색용 이름 집합을 정확 이름과 검토된 별칭으로 확장한다.
def variants(names: list[str]) -> list[str]:
    wanted = {canonical(name) for name in names}
    return sorted(wanted | {normalize(name) for name in names} | {name for name, target in ALIASES.items() if target in wanted})


# 원문·별칭의 부분 문자열을 함께 검사한다. 계란 제외가 달걀노른자를 놓치지 않도록 한다.
def is_excluded(name: str, excluded: list[str]) -> bool:
    exclusions = variants([value for value in excluded if normalize(value)])
    return any(exclusion in candidate for candidate in {normalize(name), canonical(name)} for exclusion in exclusions)


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
