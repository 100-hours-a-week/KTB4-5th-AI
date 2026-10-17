import asyncio
import logging
import secrets
from contextlib import suppress

from app.adapters.callback import CallbackDelivery, CallbackSendOutcome, build_callback_payload
from app.adapters.callback_outbox import CallbackOutboxEntry, CallbackOutboxStatus, PostgresCallbackOutbox
from app.analysis_graph import AnalysisGraph
from app.config import Settings
from app.errors import AnalysisQueueFull, AppError, ModelUnavailable
from app.pipeline import ReceiptAnalysisPipeline
from app.schemas import (
    AnalysisAccepted,
    AnalysisCreateRequest,
    AnalysisView,
    ErrorBody,
    InternalStage,
)
from app.store import InMemoryAnalysisStore

logger = logging.getLogger(__name__)


class AnalysisRuntime:
    # 설정·저장소·파이프라인·콜백과 선택 콜백 outbox를 받아 다중 소비자 분석 런타임을 구성한다.
    def __init__(self, settings: Settings, store: InMemoryAnalysisStore, pipeline: ReceiptAnalysisPipeline, callback: CallbackDelivery, callback_outbox: PostgresCallbackOutbox | None = None) -> None:
        self.settings = settings
        self.store = store
        self.pipeline = pipeline
        self.analysis_graph = AnalysisGraph(
            pipeline,
            retry_count=settings.analysis_retry_count,
            retry_delay_seconds=settings.analysis_retry_delay_seconds,
        )
        self.callback = callback
        self.callback_outbox = callback_outbox
        self.callback_relay_task: asyncio.Task[None] | None = None
        self.callback_relay_wakeup = asyncio.Event()
        self.callback_sending_tasks: set[asyncio.Task[None]] = set()
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.analysis_queue_maxsize)
        self.consumer_tasks: list[asyncio.Task[None]] = []
        self.janitor_task: asyncio.Task[None] | None = None
        self.active_analysis_ids: set[str] = set()
        self.accepting = False

    # 하위 호환용 — 여러 컨슈머 중 하나(또는 없으면 None)를 반환한다. 전체 목록은
    # active_analysis_ids를 본다.
    @property
    def active_analysis_id(self) -> str | None:
        return next(iter(self.active_analysis_ids), None)

    # 신규 접수를 활성화하고 분석 소비자 여러 개와 결과 정리 작업을 시작한다.
    async def start(self) -> None:
        self.accepting = True
        self.consumer_tasks = [
            asyncio.create_task(self._consume(), name=f"analysis-consumer-{i}")
            for i in range(self.settings.analysis_consumer_count)
        ]
        self.janitor_task = asyncio.create_task(self._janitor(), name="analysis-janitor")
        if self.callback_outbox is not None:
            self.callback_relay_task = asyncio.create_task(self._relay_callbacks(), name="callback-outbox-relay")
        logger.info(
            "runtime.started queue_capacity=%s consumers=%s",
            self.queue.maxsize,
            len(self.consumer_tasks),
        )

    # 신규 접수를 중단하고 백그라운드 작업·콜백 클라이언트·모델 자원을 닫는다.
    async def stop(self) -> None:
        logger.info(
            "runtime.stopping active_analysis_ids=%s queue_size=%s",
            sorted(self.active_analysis_ids),
            self.queue.qsize(),
        )
        self.accepting = False
        tasks = [*self.consumer_tasks, self.janitor_task, self.callback_relay_task, *self.callback_sending_tasks]
        for task in tasks:
            if task:
                task.cancel()
        for task in tasks:
            if task:
                with suppress(asyncio.CancelledError):
                    await task
        await self.callback.close()
        if self.callback_outbox is not None:
            await self.callback_outbox.close()
        await self.pipeline.close()
        logger.info("runtime.stopped")

    # 검증된 분석 요청을 받아 멱등 레코드를 만들고 큐 접수 결과를 반환한다.
    def submit(self, request: AnalysisCreateRequest) -> AnalysisAccepted:
        if not self.accepting:
            raise ModelUnavailable("분석 런타임이 요청을 받지 않는 상태입니다.")
        analysis_id = f"ana_{secrets.token_hex(12)}"
        record, is_new = self.store.create_or_get(analysis_id, request)
        if is_new:
            try:
                self.queue.put_nowait(record.analysis_id)
                logger.info(
                    "analysis.queued analysis_id=%s request_id=%s queue_size=%s",
                    record.analysis_id,
                    request.request_id,
                    self.queue.qsize(),
                )
            except asyncio.QueueFull as exc:
                self.store.remove(record.analysis_id)
                logger.warning("analysis.queue_full request_id=%s", request.request_id)
                raise AnalysisQueueFull() from exc
        else:
            logger.info(
                "analysis.idempotent_hit analysis_id=%s request_id=%s status=%s",
                record.analysis_id,
                request.request_id,
                record.status,
            )
        return self.store.accepted(record)

    # 파이프라인의 OCR과 LLM 상태를 점검해 모델별 준비 여부를 반환한다.
    async def models_ready(self) -> dict[str, bool]:
        return await self.pipeline.readiness()

    # 큐에서 분석 ID를 꺼내 파이프라인 실행, 결과 저장, 콜백 전달을 수행한다. 이 코루틴이
    # analysis_consumer_count개만큼 동시에 돌면서 같은 큐를 나눠 소비한다 — OCR 자체는
    # PaddleTextExtractor의 asyncio.Lock으로 여전히 1개씩만 처리되지만, 그 앞뒤(이미지 로드,
    # Gemma 분류 대기, DB 조회)는 컨슈머끼리 겹칠 수 있어 큐 대기시간이 줄어든다.
    async def _consume(self) -> None:
        while True:
            analysis_id = await self.queue.get()
            self.active_analysis_ids.add(analysis_id)
            logger.info(
                "analysis.dequeued analysis_id=%s queue_size=%s", analysis_id, self.queue.qsize()
            )
            try:
                self.store.mark_processing(analysis_id)

                # 현재 분석의 내부 단계를 저장하고 단계 변경 로그를 남긴다.
                async def set_stage(stage: InternalStage, current_analysis_id: str = analysis_id) -> None:
                    self.store.set_stage(current_analysis_id, stage)
                    logger.info(
                        "analysis.stage analysis_id=%s stage=%s", current_analysis_id, stage
                    )

                record = self.store.get_record(analysis_id)
                result = await self.analysis_graph.run(record, set_stage)
                view = self.store.complete(analysis_id, result)
                logger.info(
                    "analysis.completed analysis_id=%s request_id=%s item_count=%s",
                    analysis_id,
                    record.request.request_id,
                    len(result.items),
                )
            except AppError as exc:
                view = self.store.fail(analysis_id, self._error_body(exc))
                logger.warning(
                    "analysis.failed analysis_id=%s error_code=%s retryable=%s",
                    analysis_id,
                    exc.code,
                    exc.retryable,
                )
            except Exception:
                logger.exception("unexpected analysis failure analysis_id=%s", analysis_id)
                view = self.store.fail(
                    analysis_id,
                    ErrorBody(
                        code="INTERNAL_ERROR",
                        message="이미지 분석 중 내부 오류가 발생했습니다.",
                        retryable=True,
                        retryAfterMs=5000,
                    ),
                )
            finally:
                self.active_analysis_ids.discard(analysis_id)
                self.queue.task_done()

            if view.completed_at is not None:
                await self._deliver_callback(analysis_id, view)

    # 완료·실패된 분석의 콜백을 outbox에 저장하거나, outbox가 없으면 바로 보낸다.
    async def _deliver_callback(self, analysis_id: str, view: AnalysisView) -> None:
        # 콜백 변환·전송 오류가 소비자 코루틴까지 끝내면 이후 큐 작업이 영원히 처리되지 않는다.
        # 분석 결과는 이미 저장됐으므로 오류만 기록하고 다음 작업으로 넘어간다(GET으로 조회 가능).
        try:
            payload = build_callback_payload(view)
            if self.callback_outbox is not None:
                try:
                    await self.callback_outbox.save(payload)
                    self.callback_relay_wakeup.set()
                    logger.info("callback.outbox.saved analysis_id=%s status=%s", analysis_id, view.status)
                    return
                except Exception:
                    # 저장하지 못하면 재시작 뒤 다시 보낼 수는 없지만 콜백 자체는 기존처럼 바로 보낸다.
                    logger.exception("callback.outbox.save_failed analysis_id=%s", analysis_id)
            logger.info(
                "callback.delivery.start analysis_id=%s status=%s", analysis_id, view.status
            )
            delivered = await self.callback.try_send(payload)
            logger.info(
                "callback.delivery.finished analysis_id=%s delivered=%s",
                analysis_id,
                delivered,
            )
        except Exception:
            logger.exception("callback.delivery.error analysis_id=%s", analysis_id)

    # outbox에서 보낼 차례인 콜백을 꺼내 보낸다. 새 콜백이 저장되면 바로, 아니면 주기마다 확인한다.
    async def _relay_callbacks(self) -> None:
        while True:
            # 확인 전에 신호를 지워야 확인하는 사이 저장된 콜백의 신호를 놓치지 않는다.
            self.callback_relay_wakeup.clear()
            try:
                await self._start_due_callbacks()
            except Exception:
                logger.exception("callback.outbox.relay_failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self.callback_relay_wakeup.wait(),
                    self.settings.callback_outbox_poll_interval_seconds,
                )

    # 보내는 중인 콜백이 묶음 크기보다 적으면 빈 만큼 선점해 각각 따로 보낸다. 묶음 전체를 기다리면
    # 응답이 늦은 콜백 하나가 그 뒤에 저장된 콜백까지 붙잡는다.
    async def _start_due_callbacks(self) -> None:
        capacity = self.settings.callback_outbox_batch_size - len(self.callback_sending_tasks)
        if capacity <= 0:
            return
        # 보내는 동안 선점이 풀려 같은 콜백을 두 번 보내지 않도록 콜백 타임아웃보다 길게 잡는다.
        lease_seconds = self.settings.callback_timeout_seconds + 30
        entries = await self.callback_outbox.claim_due(
            capacity,
            lease_seconds,
            self.settings.callback_outbox_expires_seconds,
        )
        for entry in entries:
            task = asyncio.create_task(self._relay_callback(entry), name=f"callback-outbox-{entry.analysis_id}")
            self.callback_sending_tasks.add(task)
            task.add_done_callback(self._finish_callback_sending)

    # 끝난 전송을 보내는 중 목록에서 빼고, 빈자리에 밀린 콜백을 바로 확인하도록 깨운다.
    def _finish_callback_sending(self, task: asyncio.Task[None]) -> None:
        self.callback_sending_tasks.discard(task)
        self.callback_relay_wakeup.set()
        if not task.cancelled() and task.exception() is not None:
            logger.error("callback.outbox.entry_failed task=%s error=%r", task.get_name(), task.exception())

    # 선점한 콜백 하나를 보내고 결과에 따라 끝내거나 다음 시도를 예약한다.
    async def _relay_callback(self, entry: CallbackOutboxEntry) -> None:
        if entry.expired:
            await self.callback_outbox.finish(entry.analysis_id, CallbackOutboxStatus.EXPIRED)
            logger.error(
                "callback.outbox.expired analysis_id=%s attempts=%s",
                entry.analysis_id,
                entry.attempt_count - 1,
            )
            return
        outcome = await self.callback.send_once(entry.payload, entry.attempt_count)
        if outcome == CallbackSendOutcome.DELIVERED:
            await self.callback_outbox.finish(entry.analysis_id, CallbackOutboxStatus.DELIVERED)
            return
        if outcome == CallbackSendOutcome.REJECTED:
            await self.callback_outbox.finish(entry.analysis_id, CallbackOutboxStatus.REJECTED)
            return
        retry_delay_seconds = min(
            2.0 ** (entry.attempt_count - 1),
            self.settings.callback_outbox_max_retry_delay_seconds,
        )
        await self.callback_outbox.schedule_retry(entry.analysis_id, retry_delay_seconds)
        logger.warning(
            "callback.outbox.retry_scheduled analysis_id=%s attempt=%s retry_delay_seconds=%s",
            entry.analysis_id,
            entry.attempt_count,
            retry_delay_seconds,
        )

    # 설정된 TTL 주기에 맞춰 완료된 인메모리 분석 결과를 정리한다.
    async def _janitor(self) -> None:
        while True:
            await asyncio.sleep(min(max(self.settings.analysis_result_ttl_seconds / 4, 30), 300))
            self.store.prune()
            logger.debug("analysis.store.pruned")
            if self.callback_outbox is not None:
                try:
                    deleted_count = await self.callback_outbox.delete_finished(
                        self.settings.callback_outbox_retention_seconds
                    )
                    logger.debug("callback.outbox.pruned deleted_count=%s", deleted_count)
                except Exception:
                    logger.exception("callback.outbox.prune_failed")

    @staticmethod
    # 내부 AppError를 API와 콜백에 사용하는 ErrorBody로 변환해 반환한다.
    def _error_body(error: AppError) -> ErrorBody:
        return ErrorBody(
            code=error.code,
            message=error.message,
            retryable=error.retryable,
            retryAfterMs=error.retry_after_ms,
        )
