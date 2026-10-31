"""레시피 지연 진단 컨텍스트. 원문 / 사용자 취향 / 인증키를 기록하지 않는다."""
import hashlib
import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

TRACE_ID: ContextVar[str] = ContextVar("recipe_trace_id", default="-")
CALL_ID: ContextVar[str] = ContextVar("recipe_call_id", default="-")
logger = logging.getLogger('uvicorn.error.recipe_latency')


# 안전한 식별자만 로그에 사용한다. 외부 문자열은 길이 / 문자 검증 후 필요하면 지문으로 바꾼다.
def safe_id(value: str | None) -> str:
    if value and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        return value
    return "hash_" + hashlib.sha256(value.encode()).hexdigest()[:24] if value else uuid4().hex


# 비동기 작업별 추적 ID를 격리하고 정상 / 실패 / 취소 모두 이전 컨텍스트로 복구한다.
@contextmanager
def recipe_trace(trace_id: str | None, call_id: str | None = None) -> Iterator[None]:
    trace_token = TRACE_ID.set(safe_id(trace_id))
    call_token = CALL_ID.set(safe_id(call_id) if call_id else "-")
    try:
        yield
    finally:
        CALL_ID.reset(call_token)
        TRACE_ID.reset(trace_token)


# 단계 시간 / 상태 등 코드에서 만든 진단값만 기록한다. 본문과 헤더 전체를 인자로 넘기지 않는다.
def latency(phase: str, **values: Any) -> None:
    logger.info("recipe.latency trace_id=%s call_id=%s phase=%s details=%s", TRACE_ID.get(), CALL_ID.get(), phase,
                json.dumps(values, ensure_ascii=False, separators=(",", ":")))
