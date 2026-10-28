"""사진 분석과 분리된 제한 인메모리 레시피 큐와 멱등 작업 상태 관리."""

import asyncio
import hashlib
import logging
from contextlib import suppress
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from app.config import Settings
from app.errors import AppError, IdempotencyConflict
from app.recipes.job_callback import RecipeCallback
from app.recipes.job_metrics import ACTIVE, DURATION, QUEUE, REQUESTS, RESULTS, WAIT
from app.recipes.job_repository import RecipeJobRepository
from app.recipes.schemas import RecipeJobAccepted, RecipeJobView, RecommendationRequest
from app.recipes.service import RecipeService

logger = logging.getLogger(__name__)


class RecipeJobs:
    # 전용 큐/소비자를 구성한다. 완료 이력만 선택적으로 DB에 저장하며 실제 재고는 차감하지 않는다.
    def __init__(self, settings: Settings, service: RecipeService, history: RecipeJobRepository | None = None, callback: RecipeCallback | None = None) -> None:
        self.settings, self.service, self.history = settings, service, history
        self.callback = callback or RecipeCallback(settings)
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.recipe_queue_maxsize)
        self.records: dict[str, tuple[str, RecommendationRequest, RecipeJobView]] = {}
        self.by_request: dict[str, str] = {}
        self.tasks: list[asyncio.Task] = []
        self.lock = asyncio.Lock()
        self.accepting = False

    # 앱 수명 동안 지정된 수의 레시피 소비자만 실행한다.
    async def start(self) -> None:
        self.accepting = True
        self.tasks = [asyncio.create_task(self._consume(), name=f"recipe-consumer-{i}") for i in range(self.settings.recipe_consumer_count)]
        QUEUE.set(0)
        ACTIVE.set(0)

    # 신규 요청을 막고 실행을 취소한다. 정상 종료에서 남은 작업은 실패 이력으로 보존한다.
    async def stop(self) -> None:
        self.accepting = False
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        interrupted = []
        for job_id, (digest, _, view) in list(self.records.items()):
            if view.status in {"QUEUED", "PROCESSING"}:
                view.status, view.completedAt = "FAILED", datetime.now(UTC)
                view.error = {"code": "RECIPE_WORKER_STOPPED", "message": "서버 종료로 추천 작업이 중단되었습니다. 새 requestId로 다시 요청하세요.", "retryable": True}
                interrupted.append((digest, view))
                logger.warning("recipe.job.interrupted job_id=%s", job_id)
        slots = asyncio.Semaphore(2)
        async def finish_interrupted(digest: str, view: RecipeJobView) -> None:
            async with slots:
                await self._finish(digest, view)
        try:
            async with asyncio.timeout(10):
                await asyncio.gather(*(finish_interrupted(digest, view) for digest, view in interrupted))
        except TimeoutError:
            logger.warning("recipe.jobs.shutdown_delivery_timeout interrupted_count=%d", len(interrupted))
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        await self.callback.close()
        QUEUE.set(0)
        ACTIVE.set(0)

    # requestId/본문 해시로 멱등 접수한다. 큐 100건 초과나 저장소 포화 시 429를 반환한다.
    async def submit(self, body: RecommendationRequest) -> RecipeJobAccepted:
        digest = hashlib.sha256(body.model_dump_json(exclude={"requestId"}).encode()).hexdigest()
        async with self.lock:
            if not self.accepting:
                raise AppError("RECIPE_NOT_READY", "레시피 작업을 접수할 수 없는 상태입니다.", 503, True, 5000)
            self._prune()
            previous = self.records.get(self.by_request.get(body.requestId, ""))
            saved = (previous[0], previous[2]) if previous else await self.history.by_request(body.requestId) if self.history else None
            if saved:
                if saved[0] != digest:
                    raise IdempotencyConflict()
                REQUESTS.labels("idempotent").inc()
                return self._accepted(saved[1])
            if self.queue.full() or len(self.records) >= self.settings.recipe_result_max_entries:
                REQUESTS.labels("queue_full").inc()
                raise AppError("RECIPE_QUEUE_FULL", "현재 레시피 추천 작업량이 너무 많습니다. 잠시 후 다시 시도해 주세요.", 429, True, 3000)
            view = RecipeJobView(jobId=f"rjob_{uuid4().hex}", requestId=body.requestId, refrigeratorId=body.refrigeratorId,
                                 status="QUEUED", submittedAt=datetime.now(UTC))
            self.records[view.jobId] = (digest, body.model_copy(deep=True), view)
            self.by_request[body.requestId] = view.jobId
            self.queue.put_nowait(view.jobId)
            QUEUE.set(self.queue.qsize())
            REQUESTS.labels("accepted").inc()
            logger.info("recipe.job.queued job_id=%s request_id=%s refrigerator_id=%d queue_size=%d", view.jobId, body.requestId, body.refrigeratorId, self.queue.qsize())
            return self._accepted(view)

    # 메모리 또는 영속 완료 이력에서 결과를 반환한다. 없어졌다면 레시피 전용 404다.
    async def get(self, job_id: str) -> RecipeJobView:
        self._prune()
        if job_id in self.records:
            return self.records[job_id][2].model_copy(deep=True)
        saved = await self.history.by_id(job_id) if self.history else None
        if saved:
            return saved[1]
        raise AppError("RECIPE_JOB_NOT_FOUND", "레시피 추천 작업을 찾을 수 없습니다.", 404)

    # 대기시간과 모델/검색 처리시간을 분리하고 성공/실패를 저장한 뒤 최종 콜백을 보낸다.
    async def _consume(self) -> None:
        while True:
            job_id = await self.queue.get()
            digest, body, view = self.records[job_id]
            view.status, view.startedAt = "PROCESSING", datetime.now(UTC)
            WAIT.observe((view.startedAt - view.submittedAt).total_seconds())
            QUEUE.set(self.queue.qsize())
            ACTIVE.inc()
            started = monotonic()
            logger.info("recipe.job.started job_id=%s request_id=%s", job_id, body.requestId)
            try:
                async with asyncio.timeout(self.settings.recipe_job_timeout_seconds):
                    view.result = await self.service.recommend(body)
                view.status = "COMPLETED"
            except asyncio.CancelledError:
                raise
            except AppError as exc:
                view.status = "FAILED"
                view.error = {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
            except Exception as exc:
                view.status = "FAILED"
                view.error = {"code": "RECIPE_TIMEOUT" if isinstance(exc, TimeoutError) else "RECIPE_INTERNAL_ERROR",
                              "message": "레시피 추천을 완료하지 못했습니다.", "retryable": True}
                logger.exception("recipe.job.failed job_id=%s", job_id)
            finally:
                ACTIVE.dec()
                self.queue.task_done()
            view.completedAt = datetime.now(UTC)
            DURATION.labels(view.status.lower()).observe(monotonic() - started)
            RESULTS.labels(view.status.lower()).inc()
            await self._finish(digest, view)

    # 저장/콜백 오류는 결과 자체를 실패로 뒤집거나 다음 작업 소비를 중단시키지 않는다.
    async def _finish(self, digest: str, view: RecipeJobView) -> None:
        logger.info("recipe.job.result job_id=%s status=%s error_code=%s", view.jobId, view.status, (view.error or {}).get("code"))
        if self.history:
            try:
                await self.history.save(digest, view)
            except Exception:
                logger.exception("recipe.job.persist_failed job_id=%s", view.jobId)
        try:
            delivered = await self.callback.send(view)
            logger.info("recipe.job.finished job_id=%s status=%s delivered=%s", view.jobId, view.status, delivered)
        except Exception:
            logger.exception("recipe.job.callback_failed job_id=%s", view.jobId)

    # 완료 결과만 TTL/최대 항목 제한으로 정리한다. 실행 중 작업은 제거하지 않는다.
    def _prune(self) -> None:
        now = datetime.now(UTC)
        for job_id, (_, _, view) in list(self.records.items()):
            if view.completedAt and (now - view.completedAt).total_seconds() >= self.settings.recipe_result_ttl_seconds:
                self.records.pop(job_id)
                self.by_request.pop(view.requestId, None)
        if len(self.records) >= self.settings.recipe_result_max_entries:
            terminal = sorted((view.completedAt, job_id) for job_id, (_, _, view) in self.records.items() if view.completedAt)
            if terminal:
                _, job_id = terminal[0]
                self.by_request.pop(self.records[job_id][2].requestId, None)
                self.records.pop(job_id)

    @staticmethod
    # 결과 본문을 포함하지 않는 202 접수 스냅샷을 반환한다.
    def _accepted(view: RecipeJobView) -> RecipeJobAccepted:
        return RecipeJobAccepted.model_validate(view.model_dump(include={"jobId", "requestId", "status", "submittedAt"}))
