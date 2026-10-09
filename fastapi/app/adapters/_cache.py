import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class BoundedAsyncCache(Generic[T]):
    """조회 결과(None 포함)를 제한된 크기로 캐싱하는 단순 LRU 캐시.

    shelf_life_rules·product_knowledge처럼 자주 안 바뀌는 소규모 참조 테이블 조회 앞에 둔다.
    동시에 같은 키를 처음 조회하는 요청이 여럿이면 DB 조회가 중복될 수 있지만(락은 캐시
    딕셔너리 접근만 보호), 현재 파이프라인은 분석을 한 번에 하나씩만 처리하므로 실질적으로
    발생하지 않는다. 더 강한 요청 합치기가 필요해지면 키별 락으로 교체한다.
    """

    def __init__(self, max_size: int) -> None:
        self._max_size = max_size
        self._store: OrderedDict[str, T] = OrderedDict()
        self._lock = asyncio.Lock()

    # 캐시에 있으면 반환하고, 없으면 loader를 실행해 결과를 캐싱한 뒤 반환한다.
    async def get_or_load(self, key: str, loader: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                return self._store[key]
        value = await loader()
        async with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)
        return value


class LazyPool(Generic[T]):
    """DATABASE_URL로 연결 pool을 지연 생성하되 동시 생성 경합을 락으로 막는다."""

    def __init__(self, factory: Callable[[], Awaitable[T]]) -> None:
        self._factory = factory
        self._pool: T | None = None
        self._lock = asyncio.Lock()

    # 이미 만든 pool이 있으면 반환하고, 없으면 락 안에서 한 번만 생성해 반환한다.
    async def get(self) -> T:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                self._pool = await self._factory()
        return self._pool

    # 만든 pool이 있으면 close 속성을 찾아 호출한다.
    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
