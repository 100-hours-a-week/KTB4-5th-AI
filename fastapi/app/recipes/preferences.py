"""AI의 취향 적합 응답에 원문 근거를 요구한다. 알레르기/영양 판단은 하지 않는다."""

from app.recipes.candidate_pool import representative_candidates
from app.recipes.meal_quality import frying_steps, weak_spicy_basis
from app.recipes.recipe_attributes import attribute_tag
from app.recipes.retrieval import canonical
from app.recipes.suitability import ingredient_role
from app.recipes.taste_profiles import TAG_KINDS

SPICY_TAGS = {"매운거", "매운맛", "매운음식", "매콤한", "매콤한거", "칼칼한거", "칼칼한맛"}
SPICY_EVIDENCE = ("고춧가루", "고추가루", "청양고추", "고추장", "매콤", "매운", "칠리", "마라", "고추기름")
PLAIN_TAGS = {"담백한", "담백한거", "담백한맛", "담백한음식"}
MILD_TAGS = {*PLAIN_TAGS, "순한거", "안매운거", "맵지않은거"}


# 인용문은 실제 전달한 제목/재료/단계의 일부여야 한다. 알려진 매운맛 태그에는 명시적 매운 근거를 요구한다.
def valid_preference_evidence(row: dict, quote: str, tags: list[str]) -> bool:
    # 요리 종류 / 난이도 / 식사 목적 / 식감은 인용문 하나로 확인할 수 없다. 저장된 AI 속성 분류로만 판정한다.
    if any(attribute_tag(tag) for tag in tags):
        return False
    text = canonical(quote)
    sources = [row["name"], *(item["name"] for item in row["ingredients"]), *(step["instruction"] for step in row["steps"])]
    if len(text) < 2 or not any(text in canonical(source) for source in sources):
        return False
    if {canonical(tag) for tag in tags}.intersection(SPICY_TAGS) and not any(word in text for word in SPICY_EVIDENCE):
        return False
    if {canonical(tag) for tag in tags}.intersection(SPICY_TAGS) and weak_spicy_basis(row):
        return False
    if {canonical(tag) for tag in tags}.intersection(MILD_TAGS) and any(word in canonical(source) for source in sources for word in SPICY_EVIDENCE):
        return False
    if {canonical(tag) for tag in tags}.intersection(PLAIN_TAGS) and frying_steps(row):
        return False
    kinds = {TAG_KINDS.get(canonical(tag)) for tag in tags}
    if kinds.intersection({"SWEET", "SALTY", "MILD", "PEPPERY"}):
        # 조리 근거와 계량 정보가 없는 단일 재료명은 알려진 맛의 확정 근거로 사용하지 않는다.
        if not any(text in canonical(step["instruction"]) for step in row["steps"]):
            return False
        explicit = any(word in text for word in ("담백", "달콤", "달달", "짭짤", "칼칼"))
        # 맛은 재료 간 비율로 판단하므로 원문 인분 표기는 요구하지 않는다.
        complete = all(item.get("amountValue") is not None for item in row["ingredients"])
        if not explicit and not complete:
            return False
    return True


# 여러 태그의 판정을 하나로 합친다. 맛 등 인용 검증 태그는 모두 일치해야 하고, 속성 태그(요리 종류 / 난이도 등)는 정렬 가산 요소다.
# 속성 태그만 요청하면 하나 이상 일치하고 불일치가 없을 때 일치다.
def combine_preference_status(statuses: dict[str, str]) -> str:
    required = [status for tag, status in statuses.items() if not attribute_tag(tag)]
    optional = [status for tag, status in statuses.items() if attribute_tag(tag)]
    if "NOT_MATCHED" in required:
        return "NOT_MATCHED"
    if required:
        return "MATCHED" if all(status == "MATCHED" for status in required) else "UNKNOWN"
    if "MATCHED" in optional and "NOT_MATCHED" not in optional:
        return "MATCHED"
    if optional and all(status == "NOT_MATCHED" for status in optional):
        return "NOT_MATCHED"
    return "UNKNOWN"


# 규칙은 명시적 매운 근거의 존재만 선별한다. 최종 MATCHED는 여전히 모델/원문 검증으로 결정한다.
def explicit_preference_signal(row: dict, tags: list[str]) -> bool:
    if not {canonical(tag) for tag in tags}.intersection(SPICY_TAGS):
        return False
    return any(valid_preference_evidence(row, item["name"], tags) for item in row["ingredients"])


# 검색에 쓰는 동의 표현만 정리한다. 원래 사용자 태그는 모델 판단과 응답에 그대로 유지한다.
def search_preference_terms(tags: list[str]) -> list[str]:
    return list(dict.fromkeys("칼칼한맛" if canonical(tag) in {"칼칼한거", "칼칼한맛"} else "매운거" if canonical(tag) in SPICY_TAGS else tag for tag in tags))


# 전체 취향 후보 하나로 검색을 끝내지 않고, 날짜순 임박 식재료마다 주재료 활용/취향 근거를 확인한다.
def preference_search_focus(inventory: list, rows: list[dict], tags: list[str], matched: set[tuple[str, str]] | None = None) -> list[str]:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    if not tags or matched is None and not {canonical(tag) for tag in tags}.intersection(SPICY_TAGS):
        return []
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    supported = [row for row in rows if ((row["source"], row["sourceRecipeId"]) in matched if matched is not None else explicit_preference_signal(row, tags))]
    covered = {canonical(name) for row in supported for name in row["usesExpiringMainIngredients"]}
    return list(dict.fromkeys(item.name for item in sorted(inventory, key=lambda item: (item.expirationDate, item.ingredientId))
                              if 0 <= (item.expirationDate - today).days <= 3 and canonical(item.name) not in covered
                              and ingredient_role("", item.name)[0] not in {"SEASONING", "WATER"}))


# 취향이 있는 경우에만 평가 창을 줄인다. 명시적 근거/웹 후보를 앞에 두되 최종 원문 후보는 보존한다.
def preference_shortlist(rows: list[dict], tags: list[str], limit: int) -> list[dict]:
    if not tags:
        return rows[:limit]
    ordered = sorted(rows, key=lambda row: (row.get("_expiryPriority", ()),
                                            0 if row["source"] == "TAVILY_WEB" else 1,
                                            0 if explicit_preference_signal(row, tags) else 1))
    return representative_candidates(ordered, min(limit, 12))
