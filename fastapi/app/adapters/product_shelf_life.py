"""상품명으로 제조사가 신고한 소비기한(식품안전나라 I1250 품목제조보고)을 찾는다.

I1250의 제품명 조회는 "이름 포함" 검색이라 굿모닝우유를 넣으면 굿모닝우유식빵이, 커스타드를 넣으면
커스타드 소스·빵·전분믹스가 함께 나온다. 그래서 다음 두 경우에만 값을 쓴다.

- EXACT: 제품명이 조회 이름과 같거나, 앞에 브랜드만 붙은 경우(오뚜기 진비빔면 → 진비빔면).
  여러 건이면 가장 짧은 기간을 쓴다.
- CONSENSUS: 정확한 이름은 없지만 이름이 포함된 제품 2건 이상이 대부분(60% 이상) 같은 기간일 때
  (생생감자칩 맛별 제품이 모두 6개월).

POG_DAYCNT는 자유 문자열이다("제조일로부터 6개월", "15일", "실온 6개월", "해당없음",
포장별 여러 값). 기간이 하나로 읽히는 경우만 일수로 바꾸고 나머지는 버린다.
값은 제조일 기준이므로 구매일·분석일에 그대로 더하면 실제보다 늦은 날짜가 된다. 범위 계산은 호출자가 한다.
"""

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.adapters._cache import BoundedAsyncCache
from app.config import Settings

logger = logging.getLogger(__name__)

_I1250_URL = "https://openapi.foodsafetykorea.go.kr/api/{key}/I1250/json/1/{size}/PRDLST_NM={name}"
_PAGE_SIZE = 100
# 브랜드명 앞붙임(오뚜기·농심 등) 한 단어만 허용한다. 더 길거나 여러 단어면 다른 제품명일 가능성이 크다
# ("날씬 떡볶이와 소시지", "알뜰 누들컵 우동맛").
_MAX_BRAND_PREFIX = 6
_CONSENSUS_MIN_ROWS = 2
_CONSENSUS_MIN_SHARE = 0.6
# 완제품이 아니라 원료·조미용으로 신고된 유형이다. 조회 이름 자체에 같은 말이 있으면(장조림소스) 제외하지 않는다.
_INGREDIENT_ITEM_TYPES = ("소스", "혼합제제", "향료", "첨가물", "착색료", "전분")
_PERIOD = re.compile(r"(\d+(?:\.\d+)?)\s*(개월|달|년|일|주)")
_UNIT_DAYS = {"일": 1, "주": 7, "개월": 30, "달": 30, "년": 365}


@dataclass(frozen=True, slots=True)
class ProductShelfLife:
    duration_days: int | None
    match_kind: str
    matched_product_name: str | None
    item_type: str | None
    raw_text: str | None
    candidate_count: int


class ProductShelfLifeUnavailable(Exception):
    """API 장애·설정 누락처럼 조회 자체를 못 한 경우. 결과로 저장하지 않는다."""


def compact_name(value: str) -> str:
    without_brackets = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", value)
    return "".join(char for char in without_brackets.lower() if char.isalnum())


# 소비기한 문자열에서 기간이 하나로 읽힐 때만 일수를 반환한다. "해당없음"·포장별 여러 기간은 None이다.
def parse_shelf_life_days(text: str | None) -> int | None:
    if not text:
        return None
    periods = {round(float(number) * _UNIT_DAYS[unit]) for number, unit in _PERIOD.findall(text)}
    if len(periods) != 1:
        return None
    days = periods.pop()
    return days if 1 <= days <= 3650 else None


def _is_ingredient_type(item_type: str, query: str) -> bool:
    return any(word in item_type and word not in query for word in _INGREDIENT_ITEM_TYPES)


# I1250 응답 행에서 조회 이름에 해당하는 제조사 신고 기간을 고른다.
def select_product_shelf_life(query: str, rows: list[dict[str, Any]]) -> ProductShelfLife:
    query_key = compact_name(query)
    if len(query_key) < 2:
        return ProductShelfLife(None, "NONE", None, None, None, len(rows))
    active = [row for row in rows if (row.get("PRODUCTION") or "").strip() != "예"] or rows
    candidates = []
    for row in active:
        name = row.get("PRDLST_NM") or ""
        name_key = compact_name(name)
        item_type = (row.get("PRDLST_DCNM") or "").strip()
        days = parse_shelf_life_days(row.get("POG_DAYCNT"))
        if query_key not in name_key or days is None or _is_ingredient_type(item_type, query):
            continue
        # 결과가 한 페이지를 꽉 채운 일반 이름(소시지·두부·참치)은 다른 제품이 섞이기 쉬워 이름이 완전히 같을 때만 인정한다.
        exact = name_key == query_key if len(rows) >= _PAGE_SIZE else _is_exact_name(name, query_key)
        candidates.append((exact, days, name, item_type, row.get("POG_DAYCNT")))

    exact_rows = [candidate for candidate in candidates if candidate[0]]
    # 같은 제품명이 여러 건이면(봉지 6개월·용기 7개월, 제조사별 37일·60일) 가장 짧은 기간을 쓴다.
    # 실제보다 늦은 날짜를 알려 주지 않는 쪽이다.
    if exact_rows:
        chosen = min(exact_rows, key=lambda candidate: candidate[1])
        return ProductShelfLife(chosen[1], "EXACT", chosen[2], chosen[3], chosen[4], len(rows))
    if len(candidates) >= _CONSENSUS_MIN_ROWS:
        chosen = _consensus_period(candidates)
        if chosen is not None:
            return ProductShelfLife(chosen[1], "CONSENSUS", chosen[2], chosen[3], chosen[4], len(rows))
    return ProductShelfLife(None, "NONE", None, None, None, len(rows))


# 제품명이 조회 이름과 같거나, 띄어쓰기로 구분된 앞말(브랜드)만 더 붙은 경우다. "오뚜기 진비빔면"은 맞고
# "감자간장조림"(장조림)·"CF어육소시지"(소시지)처럼 이름 안에 붙어 있는 다른 제품은 아니다.
def _is_exact_name(name: str, query_key: str) -> bool:
    without_brackets = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", name).split()
    if not without_brackets:
        return False
    if compact_name(name) == query_key:
        return True
    if len(without_brackets) != 2:
        return False
    brand, product = (compact_name(token) for token in without_brackets)
    return product == query_key and 1 <= len(brand) <= _MAX_BRAND_PREFIX


# 이름이 포함된 다른 제품들은 60% 이상이 정확히 같은 기간일 때만 그 기간을 쓴다(생생감자칩 맛별 6개월).
def _consensus_period(candidates: list[tuple]) -> tuple | None:
    days, count = Counter(candidate[1] for candidate in candidates).most_common(1)[0]
    if count / len(candidates) >= _CONSENSUS_MIN_SHARE:
        return next(candidate for candidate in candidates if candidate[1] == days)
    return None


class FoodSafetyProductShelfLife:
    """I1250 조회 + PostgreSQL 저장(026) + 프로세스 메모리 캐시."""

    def __init__(self, settings: Settings, pool_getter, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._pool_getter = pool_getter
        self._client = client
        self._owns_client = client is None
        self._cache: BoundedAsyncCache[ProductShelfLife] = BoundedAsyncCache(settings.reference_cache_max_size, ttl_seconds=86_400)
        self._store_available = True
        # 동시에 여러 건을 보내면 INFO-500·시간 초과가 크게 늘었다(8건 동시 → 6건 실패, 2026-11-12). 2건씩만 보낸다.
        self._slots = asyncio.Semaphore(2)

    @property
    def enabled(self) -> bool:
        return self._settings.product_shelf_life_lookup_enabled and bool(self._settings.recipe_food_safety_api_key)

    # 상품명으로 제조사 신고 기간을 찾는다. 조회하지 못하면 ProductShelfLifeUnavailable을 던진다.
    async def find(self, product_name: str) -> ProductShelfLife:
        query = " ".join(product_name.split())[:100]
        return await self._cache.get_or_load(compact_name(query), lambda: self._load(query))

    async def _load(self, query: str) -> ProductShelfLife:
        key = compact_name(query)
        stored = await self._read_store(key)
        if stored is not None:
            return stored
        rows = await self._fetch(query)
        result = select_product_shelf_life(query, rows)
        await self._write_store(key, result)
        logger.info(
            "product_shelf_life.lookup query=%s match=%s days=%s candidates=%s",
            query, result.match_kind, result.duration_days, result.candidate_count,
        )
        return result

    # 같은 요청도 시간 초과·INFO-500과 정상 응답이 번갈아 나온다(2026-11-12 실측). 한 번만 다시 시도한다.
    async def _fetch(self, query: str) -> list[dict[str, Any]]:
        try:
            return await self._fetch_once(query)
        except ProductShelfLifeUnavailable:
            if not self.enabled:
                raise
            return await self._fetch_once(query)

    async def _fetch_once(self, query: str) -> list[dict[str, Any]]:
        if not self.enabled:
            raise ProductShelfLifeUnavailable("품목제조보고 조회가 비활성화돼 있습니다.")
        url = _I1250_URL.format(key=self._settings.recipe_food_safety_api_key, size=_PAGE_SIZE, name=quote(query))
        try:
            async with self._slots:
                response = await self._get_client().get(url)
            response.raise_for_status()
            payload = response.json().get("I1250", {})
        except (httpx.HTTPError, ValueError) as exc:
            # URL에 키가 들어 있어 예외 문자열을 남기지 않는다.
            raise ProductShelfLifeUnavailable(f"품목제조보고 조회 실패: {type(exc).__name__}") from exc
        code = (payload.get("RESULT") or {}).get("CODE")
        if code == "INFO-200":  # 해당하는 데이터가 없습니다
            return []
        if code != "INFO-000":
            raise ProductShelfLifeUnavailable(f"품목제조보고 조회 실패: {code}")
        return list(payload.get("row") or [])

    async def _read_store(self, key: str) -> ProductShelfLife | None:
        if not self._store_available or self._pool_getter is None:
            return None
        try:
            pool = await self._pool_getter()
            row = await pool.fetchrow(
                """
                SELECT duration_days, match_kind, matched_product_name, item_type, raw_text, candidate_count
                FROM receipt_ai.product_shelf_life_lookups
                WHERE query_name = $1 AND fetched_at > now() - make_interval(days => $2)
                """,
                key,
                self._settings.product_shelf_life_refresh_days,
            )
        except Exception as exc:
            self._disable_store(exc)
            return None
        if row is None:
            return None
        return ProductShelfLife(
            row["duration_days"], row["match_kind"], row["matched_product_name"], row["item_type"], row["raw_text"], row["candidate_count"]
        )

    async def _write_store(self, key: str, result: ProductShelfLife) -> None:
        if not self._store_available or self._pool_getter is None or not 2 <= len(key) <= 100:
            return
        try:
            pool = await self._pool_getter()
            await pool.execute(
                """
                INSERT INTO receipt_ai.product_shelf_life_lookups
                    (query_name, duration_days, match_kind, matched_product_name, item_type, raw_text, candidate_count, fetched_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, now())
                ON CONFLICT (query_name) DO UPDATE SET
                    duration_days = EXCLUDED.duration_days, match_kind = EXCLUDED.match_kind,
                    matched_product_name = EXCLUDED.matched_product_name, item_type = EXCLUDED.item_type,
                    raw_text = EXCLUDED.raw_text, candidate_count = EXCLUDED.candidate_count, fetched_at = now()
                """,
                key, result.duration_days, result.match_kind, result.matched_product_name,
                result.item_type, (result.raw_text or "")[:500] or None, result.candidate_count,
            )
        except Exception as exc:
            self._disable_store(exc)

    # 026 미적용·권한 부족이면 저장 없이 계속 조회만 한다. 분석은 실패시키지 않는다.
    def _disable_store(self, exc: Exception) -> None:
        if self._store_available:
            logger.warning("product_shelf_life.store_unavailable error_type=%s", type(exc).__name__)
        self._store_available = False

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.product_shelf_life_timeout_seconds)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
