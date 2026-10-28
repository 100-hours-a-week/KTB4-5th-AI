"""AI의 취향 적합 응답에 원문 근거를 요구한다. 알레르기/영양 판단은 하지 않는다."""

from app.recipes.retrieval import canonical

SPICY_TAGS = {"매운거", "매운맛", "매운음식", "매콤한거", "칼칼한거", "칼칼한맛"}
SPICY_EVIDENCE = ("고춧가루", "고추가루", "청양고추", "고추장", "매콤", "매운", "칠리", "마라", "고추기름")
MILD_TAGS = {"담백한거", "담백한맛", "담백한음식", "순한거", "안매운거", "맵지않은거"}


# 인용문은 실제 전달한 제목/재료/단계의 일부여야 한다. 알려진 매운맛 태그에는 명시적 매운 근거를 요구한다.
def valid_preference_evidence(row: dict, quote: str, tags: list[str]) -> bool:
    text = canonical(quote)
    sources = [row["name"], *(item["name"] for item in row["ingredients"]), *(step["instruction"][:80] for step in row["steps"][:2])]
    if len(text) < 2 or not any(text in canonical(source) for source in sources):
        return False
    if {canonical(tag) for tag in tags}.intersection(SPICY_TAGS) and not any(word in text for word in SPICY_EVIDENCE):
        return False
    if {canonical(tag) for tag in tags}.intersection(MILD_TAGS) and any(word in canonical(source) for source in sources for word in SPICY_EVIDENCE):
        return False
    return True
