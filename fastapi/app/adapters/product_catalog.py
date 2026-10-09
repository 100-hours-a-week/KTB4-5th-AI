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

# 정확 일치가 없을 때만 쓰는 pg_trgm 유사도 폴백. product_aliases는 2건뿐이라
# 표준명(normalized_name)만 대상으로 한다 — 별칭이 더 쌓이면 그때 UNION으로 확장한다.
# 인덱스는 마이그레이션 001의 ix_product_knowledge_name_trgm(GIN, gin_trgm_ops)를 쓴다.
#
# WHERE에 similarity(...) > $2처럼 함수 호출을 직접 비교하면 GIN 인덱스를 못 쓰고 238,000여
# 행 전체를 순차 스캔한다(실측 508ms/건 — 영수증 하나에 미해결 줄이 여러 개면 그만큼 곱해져
# 파이프라인 지연시간의 대부분을 이 쿼리가 차지했다). `%` 연산자(트라이그램 인덱스가 직접
# 지원)로 바꾸면 같은 결과를 인덱스로 걸러낸 뒤에만 similarity()를 계산해 0.2ms대로 끝난다
# (실측 660배 개선, 매칭 결과는 동일함을 EXPLAIN ANALYZE로 확인). `%`의 임계값은 별도
# 파라미터가 아니라 세션 GUC(pg_trgm.similarity_threshold)를 따르므로, 커넥션 pool 생성 시
# server_settings로 한 번 고정한다(_create_pool 참고) — settings.catalog_fuzzy_similarity_threshold
# 와 항상 같은 값을 쓴다.
#
# length(normalized_name) >= 3: 2글자 이하 카탈로그 이름은 트라이그램이 3~4개뿐이라 유사도
# 계산이 원래 불안정하다. 실측 사례 — 결제단말기 브랜드 문구 "키스-제크"가 2글자짜리 정상
# 상품 "제크"(과자)와 유사도 0.5로 매칭돼 영수증 9장 중 9장에서 반복 오탐이 났다(2026-09-21
# 실험 기록 참고). 정확 일치(_LOOKUP_QUERY)는 길이와 무관하게 그대로 동작하므로, OCR이
# 짧은 이름을 정확히 읽은 경우는 영향받지 않는다 — 잃는 건 "짧은 이름을 심하게 오독했을 때"
# 뿐이고 얻는 건 이런 우연한 오탐 차단이다. 활성 행 238,817건 중 길이<=2는 694건(0.3%).
_FUZZY_LOOKUP_QUERY = """
    SELECT product_id, canonical_name, category, suggested_storage_type,
           similarity(normalized_name, $1) AS sim
    FROM receipt_ai.product_knowledge
    WHERE is_active AND normalized_name % $1 AND length(normalized_name) >= 3
    ORDER BY sim DESC
    LIMIT 1
"""

# 질의문(정규화한 OCR 텍스트, 공백 제외)이 이 길이보다 짧으면 유사도 폴백을 건너뛴다.
# 위 카탈로그 쪽 길이 필터의 대칭이다 — 1~3글자 질의문은 트라이그램이 몇 개 안 돼서 "퓨어 수박"
# (BEVERAGE)·"성주 참외"(PROCESSED_FOOD)·"복숭아 잼"(PROCESSED_FOOD)처럼 그 단어를 이름에 포함한
# 가공식품이 끌려오거나, "비고"·"대신"·"↑ID"·"C."·"ab" 같은 표 머리글·OCR 깨짐이 우연히 걸린다.
# 113장+58장 배치의 CATALOG_FUZZY 중 질의문이 3글자 이하였던 건은 사실상 전부 오탐이었다.
# 정확 일치는 길이와 무관하게 그대로라 "무"·"두부"·"상추"처럼 카탈로그에 정확히 있는 짧은
# 이름은 영향이 없다. 건너뛴 줄은 Gemma로 넘어가는데, 가격 힌트가 붙은 짧은 식재료명(수박·
# 배·귤·파·쌀·콩 등 13개)은 전부 정확히 분류했고 힌트 없는 깨진 조각은 제외했다(실측).
_FUZZY_MIN_QUERY_LENGTH = 4


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
                if len(normalized_text.replace(" ", "")) < _FUZZY_MIN_QUERY_LENGTH:
                    return None
                row = await pool.fetchrow(_FUZZY_LOOKUP_QUERY, normalized_text)
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
            # `%` 연산자가 유사도 임계값으로 쓰는 세션 GUC. 커넥션마다 한 번만 설정해서
            # _FUZZY_LOOKUP_QUERY가 매 쿼리에 SET을 추가로 보내지 않게 한다.
            server_settings={
                "pg_trgm.similarity_threshold": str(self._settings.catalog_fuzzy_similarity_threshold)
            },
        )
