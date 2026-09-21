from typing import Any

from app.adapters._cache import BoundedAsyncCache, LazyPool
from app.config import Settings
from app.domain.models import ProductCatalogMatch
from app.errors import DependencyUnavailable
from app.schemas import Category

# 별칭 정확 일치를 표준명 정확 일치보다 우선한다(priority 오름차순, LIMIT 1).
# 바코드 매칭(조회 순서 1번)과 pg_trgm 유사도 매칭(3번)은 지금 파이프라인이 바코드를
# 추출하지 않고 실제 데이터도 적어 후보 랭킹의 의미가 없어 이번 범위에서 제외했다.
_LOOKUP_QUERY = """
    SELECT pk.product_id, pk.canonical_name, pk.category, pk.suggested_storage_type, 1 AS priority
    FROM receipt_ai.product_aliases pa
    JOIN receipt_ai.product_knowledge pk ON pk.product_id = pa.product_id
    WHERE pa.normalized_alias = $1 AND pa.is_active AND pk.is_active
    UNION ALL
    SELECT product_id, canonical_name, category, suggested_storage_type, 2 AS priority
    FROM receipt_ai.product_knowledge
    WHERE normalized_name = $1 AND is_active
    ORDER BY priority
    LIMIT 1
"""

# 정확 일치가 없을 때만 쓰는 pg_trgm 유사도 폴백. product_aliases는 아직 비어 있어(0건)
# 표준명(normalized_name)만 대상으로 한다 — 별칭이 쌓이면 그때 UNION으로 확장한다.
# 인덱스는 마이그레이션 001의 ix_product_knowledge_name_trgm(GIN, gin_trgm_ops)를 그대로 쓴다.
_FUZZY_LOOKUP_QUERY = """
    SELECT product_id, canonical_name, category, suggested_storage_type,
           similarity(normalized_name, $1) AS sim
    FROM receipt_ai.product_knowledge
    WHERE is_active AND similarity(normalized_name, $1) > $2
    ORDER BY sim DESC
    LIMIT 1
"""


class PostgresProductCatalogLookup:
    # DB 접속 정보가 담긴 설정을 받아 조회기를 구성한다. 연결 pool은 최초 조회 시 지연 생성한다.
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool = LazyPool(self._create_pool)
        self._cache: BoundedAsyncCache[ProductCatalogMatch | None] = BoundedAsyncCache(settings.reference_cache_max_size)

    # 정규화 상품명으로 별칭·표준명이 정확히 일치하는 카탈로그 항목을 조회해 반환한다.
    # 결과(없음 포함)는 프로세스 메모리에 캐싱해 동일 문자열 재조회 시 DB를 다시 안 부른다.
    async def find(self, normalized_text: str) -> ProductCatalogMatch | None:
        return await self._cache.get_or_load(normalized_text, lambda: self._query(normalized_text))

    async def _query(self, normalized_text: str) -> ProductCatalogMatch | None:
        try:
            pool = await self._pool.get()
            row = await pool.fetchrow(_LOOKUP_QUERY, normalized_text)
            if row is None:
                row = await pool.fetchrow(
                    _FUZZY_LOOKUP_QUERY,
                    normalized_text,
                    self._settings.catalog_fuzzy_similarity_threshold,
                )
                matched_via = "fuzzy"
            else:
                matched_via = "exact"
        except DependencyUnavailable:
            raise
        except Exception as exc:
            raise DependencyUnavailable("상품 카탈로그 조회에 실패했습니다.") from exc
        if row is None:
            return None
        return ProductCatalogMatch(
            product_id=row["product_id"],
            canonical_name=row["canonical_name"],
            category=Category(row["category"]),
            suggested_storage_type=row["suggested_storage_type"],
            matched_via=matched_via,
            similarity=row["sim"] if matched_via == "fuzzy" else None,
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
                "상품 카탈로그 조회를 사용하려면 asyncpg 의존성을 설치해야 합니다."
            ) from exc
        return await asyncpg.create_pool(
            self._settings.database_url,
            min_size=1,
            max_size=self._settings.database_pool_max_size,
        )
