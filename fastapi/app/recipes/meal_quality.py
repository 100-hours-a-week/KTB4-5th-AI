"""원문의 조리 방식/표기 분량으로 취향 판단과 조리 가능성의 한계를 설명한다."""

import re

from app.recipes.retrieval import canonical

FRYING = re.compile(r"튀겨|튀긴|튀기|튀김|튀깁|튀길")


# 전체 단계를 검사한다. 부정/생략 표현은 튀김을 실제 수행한 근거로 사용하지 않는다.
def frying_steps(row: dict) -> list[str]:
    return [step["instruction"] for step in row["steps"] if FRYING.search(step["instruction"])
            and not re.search(r"튀기지|튀김\s*없이|튀길\s*필요.*없|에어프라이|기름\s*없이", step["instruction"])]


# 고추장만 소량 명시된 경우의 보수적 확인 신호다. 맛 강도나 영양 수치를 추정하지 않는다.
def weak_spicy_basis(row: dict) -> bool:
    spicy = [item for item in row["ingredients"] if any(word in canonical(item["name"]) for word in ("고추", "칠리", "마라"))]
    if not spicy or any("고추장" not in canonical(item["name"]) for item in spicy):
        return False
    if any(word in canonical(row["name"]) for word in ("매운", "매콤", "칼칼", "마라")):
        return False
    if any(item.get("unit") != "G" or item.get("amountValue") is None for item in spicy):
        return False
    grams = sum(item["amountValue"] for item in spicy)
    known_food = sum(item.get("amountValue") or 0 for item in row["ingredients"]
                     if item.get("unit") == "G" and item not in spicy)
    # 1%는 검증 전 초기 선별값이다. 낮은 비중에서는 매운 강도 확정을 보류한다.
    return 0 < grams <= 2 and known_food > 0 and grams / known_food < 0.01


# 모델에는 모든 단계를 훑은 조리 신호와 관련 원문 인용/양념 분량만 압축해 전달한다.
def taste_context(row: dict) -> dict:
    fried = frying_steps(row)
    evidence = fried or [step["instruction"] for step in row["steps"] if re.search(r"양념|소스|무쳐|버무|찐다|찜|삶|끓", step["instruction"])]
    seasonings = [[item["name"], item.get("amountText")] for item in row["ingredients"]
                  if any(word in canonical(item["name"]) for word in ("고추", "기름", "식용유", "쌈장", "마요네즈", "소금", "설탕"))]
    return {"deepFried": bool(fried), "weakSpicyBasis": weak_spicy_basis(row),
            "cookingEvidence": [text[:160] for text in evidence[:2]], "seasoningAmounts": seasonings[:8],
            "baseServings": row.get("baseServings"), "evidenceMayBeTruncated": any(len(text) > 160 for text in evidence) or len(evidence) > 2}


# 취향 일치 그룹 안에서만 사용할 구매 부담 단계. 큰 부담도 후보에서 삭제하지 않는다.
def shopping_band(row: dict) -> int:
    missing = len(row["missingIngredients"])
    return 0 if missing <= 5 else 1
