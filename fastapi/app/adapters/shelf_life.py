import asyncio
import logging
from typing import Any

from app.adapters._cache import BoundedAsyncCache, LazyPool
from app.adapters.product_shelf_life import FoodSafetyProductShelfLife, ProductShelfLifeUnavailable
from app.config import Settings
from app.domain.models import ShelfLifeEstimate
from app.errors import DependencyUnavailable

logger = logging.getLogger(__name__)

# 판매 허용 기한 마지막 날에 사도 남는 기간의 비율 (소비기한 − 평균 유통기한) / 소비기한.
# 식약처 참고값 17개 유형(010·025 원표)의 중앙값이다. 범위는 베이컨류 0.11 ~ 과채음료 0.45.
_DEFAULT_REMAINING_RATIO = 0.31

# fresh_only 규칙(양파·감자·돼지고기 같은 신선식품, 마이그레이션 012)은 상품이 채소·과일·육류·수산으로
# 분류됐을 때만 쓴다 — 이름에 "감자"가 들어간 과자(감자깡)나 "사과"가 들어간 주스에 신선식품 보관기간이
# 붙는 걸 막는다. 가공식품 기준(식약처 참고값: 두부·햄·소시지·우유 등)은 분류와 관계없이 쓴다.
#
# 1글자 규칙명("무", "햄")은 아무 위치에서나 포함되면 "단무지"·"무항생제 닭가슴살"·"햄버거"처럼
# 엉뚱한 상품에 걸린다. 그래서 1글자 이름은 상품명 끝에 올 때만 인정한다("무", "총각무",
# "슬라이스햄"은 매칭, "단무지"·"햄버거"는 제외). 2글자 이상은 기존처럼 부분 문자열 포함이다.
_LOOKUP_QUERY = """
    SELECT shelf_life_rule_id, duration_min_days, duration_max_days, source_name, source_url
    FROM receipt_ai.shelf_life_rules
    WHERE (
            (length(normalized_ingredient_name) >= 2 AND strpos($1, normalized_ingredient_name) > 0)
            OR (length(normalized_ingredient_name) = 1 AND right(rtrim($1), 1) = normalized_ingredient_name)
          )
      AND storage_type = $2
      AND is_active
      AND (NOT fresh_only OR $3)
    ORDER BY (package_state = 'UNOPENED') DESC, LENGTH(normalized_ingredient_name) DESC, duration_min_days ASC
    LIMIT 1
"""


class PostgresShelfLifeLookup:
    # DB 접속 정보가 담긴 설정을 받아 조회기를 구성한다. 연결 pool은 최초 조회 시 지연 생성한다.
    def __init__(self, settings: Settings, product_lookup: FoodSafetyProductShelfLife | None = None) -> None:
        self._settings = settings
        self._pool = LazyPool(self._create_pool)
        self._cache: BoundedAsyncCache[ShelfLifeEstimate | None] = BoundedAsyncCache(settings.reference_cache_max_size)
        self._product_lookup = product_lookup or FoodSafetyProductShelfLife(settings, self._pool.get)

    # 정규화 식재료명과 보관 방식으로 검수된 보관기간 규칙을 조회해 반환한다.
    # normalized_ingredient_name은 정확히 일치하지 않아도, shelf_life_rules에 저장된
    # 일반 식재료명(예: "우유")이 조회 이름(예: "서울우유")에 부분 문자열로 포함되면 매칭한다.
    # 여러 규칙이 동시에 포함되면 더 긴(더 구체적인) 이름을 우선한다.
    # 결과(없음 포함)는 프로세스 메모리에 캐싱해 동일 식재료·보관방식 재조회 시 DB를 다시 안 부른다.
    #
    # 가공식품(fresh_item=False)은 먼저 제조사 신고 소비기한(품목제조보고)을 찾는다. 유형 규칙보다 제품에
    # 가깝기 때문이다. 신고값은 제조일 기준이라 뒤 날짜만 정하고, 앞 날짜는 같은 상품의 유형 규칙 비율
    # (예: 과자 36/81)로 줄인다. 유형 규칙이 없으면 식약처 참고값 유형들의 중앙 비율(_DEFAULT_REMAINING_RATIO)을 쓴다.
    # 앞 날짜를 기준일 당일로 두면 앱 카드에 "오늘"이 기한처럼 보였다(dev 2026-11-12, 진비빔면).
    async def find(self, normalized_ingredient_name: str, storage_type: str, fresh_item: bool = True) -> ShelfLifeEstimate | None:
        cache_key = f"{normalized_ingredient_name}\x1f{storage_type}\x1f{fresh_item}"
        rule = await self._cache.get_or_load(cache_key, lambda: self._query(normalized_ingredient_name, storage_type, fresh_item))
        # 신선식품으로 분류된 상품(크랩마스터=수산, 소시지=육류)은 유형 규칙이 아예 없을 때만, 이름이 정확히
        # 맞는 신고값을 쓴다. 양파처럼 규칙이 있는 신선식품에 같은 이름의 가공품 신고값이 붙지 않게 한다.
        if not self._product_lookup.enabled or (fresh_item and rule is not None):
            return rule
        try:
            product = await self._product_lookup.find(normalized_ingredient_name)
        except ProductShelfLifeUnavailable as exc:
            logger.warning("product_shelf_life.unavailable reason=%s", exc)
            return rule
        if product.duration_days is None or (fresh_item and product.match_kind != "EXACT"):
            return rule
        days = product.duration_days
        if rule and rule.duration_max_days > 0:
            ratio, ratio_basis = rule.duration_min_days / rule.duration_max_days, "같은 유형 규칙의 비율"
        else:
            ratio, ratio_basis = _DEFAULT_REMAINING_RATIO, "식약처 참고값 17개 유형 비율의 중앙값"
        min_days = round(days * ratio)
        return ShelfLifeEstimate(
            rule_id=0,
            duration_min_days=min(min_days, days),
            duration_max_days=days,
            source_name=f"식품안전나라 품목제조보고(제조사 신고 소비기한 {product.raw_text})",
            source_url="https://www.foodsafetykorea.go.kr/api/openApiInfo.do?svc_no=I1250",
            evidence_id=f"food-safety-i1250:{product.matched_product_name}",
            assumption=(
                f"제조사 신고 소비기한은 제조일 기준이며 제조일을 알 수 없어 {product.matched_product_name}"
                f"({product.item_type}) 신고값 {days}일을 상한, {ratio_basis}({ratio:.2f})로 줄인 {min_days}일을 하한으로 사용"
            ),
        )

    async def _query(self, normalized_ingredient_name: str, storage_type: str, fresh_item: bool) -> ShelfLifeEstimate | None:
        try:
            pool = await self._pool.get()
            row = await pool.fetchrow(_LOOKUP_QUERY, normalized_ingredient_name, storage_type, fresh_item)
        except DependencyUnavailable:
            raise
        except Exception as exc:
            raise DependencyUnavailable("보관기간 조회에 실패했습니다.") from exc
        if row is None:
            return None
        return ShelfLifeEstimate(
            rule_id=row["shelf_life_rule_id"],
            duration_min_days=row["duration_min_days"],
            duration_max_days=row["duration_max_days"],
            source_name=row["source_name"],
            source_url=row["source_url"],
        )

    # 열려 있는 연결 pool이 있으면 닫는다.
    # 분석 한 건의 상품명을 미리 동시에 조회해 캐시를 채운다. 항목별 find()가 순서대로 API를 기다리지 않게 한다.
    async def prefetch(self, names: list[str]) -> None:
        if not self._product_lookup.enabled:
            return
        unique = list(dict.fromkeys(name for name in names if name))
        results = await asyncio.gather(*(self._product_lookup.find(name) for name in unique), return_exceptions=True)
        failed = sum(isinstance(result, Exception) for result in results)
        if failed:
            logger.warning("product_shelf_life.prefetch_failed count=%s total=%s", failed, len(unique))

    async def close(self) -> None:
        await self._product_lookup.close()
        await self._pool.close()

    # DATABASE_URL로 asyncpg 연결 pool을 새로 생성해 반환한다.
    async def _create_pool(self) -> Any:
        try:
            import asyncpg
        except ImportError as exc:
            raise DependencyUnavailable(
                "보관기간 조회를 사용하려면 asyncpg 의존성을 설치해야 합니다."
            ) from exc
        return await asyncpg.create_pool(
            self._settings.database_url,
            min_size=1,
            max_size=self._settings.database_pool_max_size,
        )
