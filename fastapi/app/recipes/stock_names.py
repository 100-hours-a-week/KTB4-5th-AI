"""상품명과 요리용 식재료명을 분리한다. 검토되지 않은 상품의 성분을 추측하지 않는다."""

import re
from functools import lru_cache

from app.recipes.retrieval import canonical

PRODUCT_INGREDIENTS = {
    "풀무원국산콩부침두부": "두부", "풀무원국산콩찌개두부": "두부",
    "서울우유": "우유", "국산대파": "대파",
}
FRESH_NAMES = {"두부", "순두부", "연두부", "우유", "대파", "쪽파", "양파", "당근", "감자", "고구마", "배추", "양배추", "무", "계란", "사과", "배"}
PACK_SIZE = re.compile(r"(?:\d+(?:\.\d+)?(?:kg|g|ml|l|개입|입|개|팩|모|단))+$")


# 검토된 상품명 또는 원산지·포장량만 붙은 신선 재료를 변환한다. 결과는 프로세스 내 제한 캐시로 보관한다.
@lru_cache(maxsize=4096)
def stock_ingredient(name: str) -> str:
    original = canonical(name)
    key = PACK_SIZE.sub("", original)
    if key in PRODUCT_INGREDIENTS:
        return PRODUCT_INGREDIENTS[key]
    fresh = re.sub(r"^(?:국산|국내산)", "", key)
    return fresh if fresh in FRESH_NAMES else original
