from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.adapters._cache import LazyPool
from app.config import Settings
from app.errors import DependencyUnavailable
from app.schemas import CallbackPayload

_SAVE_QUERY = """
    INSERT INTO receipt_ai.analysis_callback_outbox (analysis_id, payload)
    VALUES ($1, $2::jsonb)
    ON CONFLICT (analysis_id) DO NOTHING
"""

# 선점은 next_attempt_at을 선점 만료 시각으로 미루는 것으로 대신한다. 보내던 프로세스가 죽어도
# 만료 뒤에 다시 집히고, SKIP LOCKED라 AI가 여러 대여도 같은 행을 동시에 보내지 않는다.
# 시각은 모두 DB의 now()로 계산해 서버 시계 차이에 영향받지 않게 한다.
_CLAIM_QUERY = """
    UPDATE receipt_ai.analysis_callback_outbox AS outbox
    SET next_attempt_at = now() + make_interval(secs => $2),
        attempt_count = outbox.attempt_count + 1
    FROM (
        SELECT analysis_id
        FROM receipt_ai.analysis_callback_outbox
        WHERE status = 'PENDING' AND next_attempt_at <= now()
        ORDER BY next_attempt_at
        LIMIT $1
        FOR UPDATE SKIP LOCKED
    ) AS due
    WHERE outbox.analysis_id = due.analysis_id
    RETURNING outbox.analysis_id,
              outbox.payload::text AS payload,
              outbox.attempt_count,
              outbox.created_at < now() - make_interval(secs => $3) AS expired
"""

_FINISH_QUERY = """
    UPDATE receipt_ai.analysis_callback_outbox
    SET status = $2, finished_at = now()
    WHERE analysis_id = $1 AND status = 'PENDING'
"""

_SCHEDULE_RETRY_QUERY = """
    UPDATE receipt_ai.analysis_callback_outbox
    SET next_attempt_at = now() + make_interval(secs => $2)
    WHERE analysis_id = $1 AND status = 'PENDING'
"""

_DELETE_FINISHED_QUERY = """
    DELETE FROM receipt_ai.analysis_callback_outbox
    WHERE status <> 'PENDING' AND finished_at < now() - make_interval(secs => $1)
"""


class CallbackOutboxStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(slots=True, frozen=True)
class CallbackOutboxEntry:
    analysis_id: str
    payload: CallbackPayload
    attempt_count: int
    expired: bool


class PostgresCallbackOutbox:
    # DB 접속 정보가 담긴 설정을 받아 콜백 outbox를 구성한다. 연결 pool은 처음 쓸 때 만든다.
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool = LazyPool(self._create_pool)

    # 완료 콜백 본문을 보낼 차례로 저장한다. 같은 분석 ID가 이미 있으면 그대로 둔다.
    async def save(self, payload: CallbackPayload) -> None:
        pool = await self._pool.get()
        await pool.execute(_SAVE_QUERY, payload.analysis_id, payload.model_dump_json(by_alias=True))

    # 보낼 시각이 된 콜백을 최대 limit개 선점해 반환한다. 선점은 lease_seconds 뒤 풀린다.
    async def claim_due(self, limit: int, lease_seconds: float, expires_seconds: float) -> list[CallbackOutboxEntry]:
        pool = await self._pool.get()
        rows = await pool.fetch(_CLAIM_QUERY, limit, lease_seconds, expires_seconds)
        return [
            CallbackOutboxEntry(
                analysis_id=row["analysis_id"],
                payload=CallbackPayload.model_validate_json(row["payload"]),
                attempt_count=row["attempt_count"],
                expired=row["expired"],
            )
            for row in rows
        ]

    # 보낼 차례인 콜백을 전달 완료·거절·기한 만료 중 하나로 끝낸다.
    async def finish(self, analysis_id: str, status: CallbackOutboxStatus) -> None:
        pool = await self._pool.get()
        await pool.execute(_FINISH_QUERY, analysis_id, status.value)

    # 보내지 못한 콜백의 다음 전송 시각을 delay_seconds 뒤로 정한다.
    async def schedule_retry(self, analysis_id: str, delay_seconds: float) -> None:
        pool = await self._pool.get()
        await pool.execute(_SCHEDULE_RETRY_QUERY, analysis_id, delay_seconds)

    # 끝난 지 retention_seconds가 지난 콜백을 지우고 지운 수를 반환한다.
    async def delete_finished(self, retention_seconds: float) -> int:
        pool = await self._pool.get()
        result = await pool.execute(_DELETE_FINISHED_QUERY, retention_seconds)
        return int(result.split()[-1])

    # 열려 있는 연결 pool이 있으면 닫는다.
    async def close(self) -> None:
        await self._pool.close()

    # DATABASE_URL로 asyncpg 연결 pool을 새로 생성해 반환한다.
    async def _create_pool(self) -> Any:
        try:
            import asyncpg
        except ImportError as exc:
            raise DependencyUnavailable("콜백 outbox를 사용하려면 asyncpg 의존성을 설치해야 합니다.") from exc
        return await asyncpg.create_pool(
            self._settings.database_url,
            min_size=1,
            max_size=self._settings.database_pool_max_size,
        )
