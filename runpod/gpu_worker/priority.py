"""LLM 슬롯 우선순위. 사용자 요청(사진 분석 / 추천 판단)이 기다리는 동안 백그라운드 작업(웹 레시피 추출, 맛 평가)은 슬롯을 양보한다."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

# FastAPI가 X-Recipe-Priority: background 헤더로 표시한 호출만 백그라운드로 본다. 표시가 없으면 사용자 요청이다.
RECIPE_BACKGROUND: ContextVar[bool] = ContextVar("recipe_background", default=False)


class PriorityGate:
    # 이미 실행 중인 호출은 끊지 않는다. 다음 슬롯을 사용자 요청에 먼저 준다.
    def __init__(self, size: int) -> None:
        self._slots = asyncio.Semaphore(size)
        self._foreground_waiting = 0
        self._foreground_clear = asyncio.Event()
        self._foreground_clear.set()

    # 우선순위 없이 슬롯 하나를 직접 잡는다(사용자 요청과 같은 취급). 진단 / 시험에서 슬롯 점유를 흉내 낼 때 쓴다.
    async def acquire(self) -> None:
        await self._slots.acquire()

    def release(self) -> None:
        self._slots.release()

    @property
    def foreground_waiting(self) -> int:
        return self._foreground_waiting

    @asynccontextmanager
    async def slot(self, background: bool = False) -> AsyncIterator[None]:
        if background:
            while True:
                await self._foreground_clear.wait()
                await self._slots.acquire()
                if self._foreground_waiting == 0:
                    break
                # 슬롯을 얻는 사이 사용자 요청이 들어왔으면 돌려주고 다시 기다린다.
                self._slots.release()
                await asyncio.sleep(0)
        else:
            self._foreground_waiting += 1
            self._foreground_clear.clear()
            try:
                await self._slots.acquire()
            finally:
                self._foreground_waiting -= 1
                if self._foreground_waiting == 0:
                    self._foreground_clear.set()
        try:
            yield
        finally:
            self._slots.release()
