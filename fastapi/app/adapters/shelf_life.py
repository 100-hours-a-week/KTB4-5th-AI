from typing import Any

from app.adapters._cache import BoundedAsyncCache, LazyPool
from app.config import Settings
from app.domain.models import ShelfLifeEstimate
from app.errors import DependencyUnavailable

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
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool = LazyPool(self._create_pool)
        self._cache: BoundedAsyncCache[ShelfLifeEstimate | None] = BoundedAsyncCache(settings.reference_cache_max_size)

    # 정규화 식재료명과 보관 방식으로 검수된 보관기간 규칙을 조회해 반환한다.
    # normalized_ingredient_name은 정확히 일치하지 않아도, shelf_life_rules에 저장된
    # 일반 식재료명(예: "우유")이 조회 이름(예: "서울우유")에 부분 문자열로 포함되면 매칭한다.
    # 여러 규칙이 동시에 포함되면 더 긴(더 구체적인) 이름을 우선한다.
    # 결과(없음 포함)는 프로세스 메모리에 캐싱해 동일 식재료·보관방식 재조회 시 DB를 다시 안 부른다.
    async def find(self, normalized_ingredient_name: str, storage_type: str, fresh_item: bool = True) -> ShelfLifeEstimate | None:
        cache_key = f"{normalized_ingredient_name}\x1f{storage_type}\x1f{fresh_item}"
        return await self._cache.get_or_load(cache_key, lambda: self._query(normalized_ingredient_name, storage_type, fresh_item))

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
    async def close(self) -> None:
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
