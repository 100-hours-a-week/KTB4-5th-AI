"""파티션별 한 건 처리, 지속 polling, 명시적 offset commit을 사용하는 Kafka 경계."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AsyncExitStack

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, ConsumerRebalanceListener
from aiokafka.structs import ConsumerRecord, TopicPartition

from app.adapters.analysis_events import EventEnvelope, FeedbackPayload, RequestedPayload, completed_event
from app.config import Settings
from app.runtime import AnalysisRuntime

logger = logging.getLogger(__name__)


class PartitionConsumer(ConsumerRebalanceListener):
    def __init__(self, consumer: AIOKafkaConsumer, handler: Callable[[ConsumerRecord], Awaitable[None]], retry_delay: float) -> None:
        self.consumer = consumer
        self.handler = handler
        self.retry_delay = retry_delay
        self.workers: dict[TopicPartition, asyncio.Task[None]] = {}
        self.poll_task: asyncio.Task[None] | None = None

    async def start(self, topic: str, required_topics: Iterable[str] = ()) -> None:
        await self.consumer.start()
        # 전체 토픽 metadata로 존재 여부부터 확인한다. 없는 토픽을 구독/발행해 자동 생성하지 않는다.
        missing = {topic, *required_topics} - await self.consumer.topics()
        if missing:
            raise RuntimeError(f"Kafka 토픽을 먼저 준비해야 합니다: {sorted(missing)}")
        self.consumer.subscribe([topic], listener=self)
        self.poll_task = asyncio.create_task(self._poll(), name=f"kafka-poll-{topic}")

    async def stop(self) -> None:
        if self.poll_task is not None:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
        await self.on_partitions_revoked(list(self.workers))
        await self.consumer.stop()

    async def on_partitions_revoked(self, revoked: Iterable[TopicPartition]) -> None:
        tasks = [self.workers.pop(partition) for partition in revoked if partition in self.workers]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def on_partitions_assigned(self, assigned: Iterable[TopicPartition]) -> None:
        pass

    async def _poll(self) -> None:
        while True:
            try:
                batches = await self.consumer.getmany(timeout_ms=1000, max_records=1)
                for partition, records in batches.items():
                    if records:
                        self.consumer.pause(partition)
                        self.workers[partition] = asyncio.create_task(self._process(partition, records[0]))
            except Exception:
                logger.exception("kafka.poll.failed")
                await asyncio.sleep(self.retry_delay)

    async def _process(self, partition: TopicPartition, record: ConsumerRecord) -> None:
        # pause + max_records=1로 같은 파티션의 뒤 offset이 먼저 완료될 수 없게 한다.
        while True:
            try:
                await self.handler(record)
                await self.consumer.commit({partition: record.offset + 1})
            except Exception:
                logger.exception("kafka.record.failed topic=%s partition=%s offset=%s", record.topic, record.partition, record.offset)
                # 실패한 원본을 유지한다. 다음 offset을 읽거나 임의 DLT로 건너뛰지 않는다.
                await asyncio.sleep(self.retry_delay)
            else:
                self.workers.pop(partition, None)
                self.consumer.resume(partition)
                return


class AnalysisKafka:
    def __init__(self, settings: Settings, runtime: AnalysisRuntime) -> None:
        self.settings = settings
        self.runtime = runtime
        self.producer: AIOKafkaProducer | None = None
        self.resources: AsyncExitStack | None = None

    def topic(self, name: str) -> str:
        return f"{self.settings.kafka_env}.dameokja.analysis.{name}.v1"

    async def start(self) -> None:
        resources = AsyncExitStack()
        self.resources = resources
        try:
            self.producer = AIOKafkaProducer(
                bootstrap_servers=self.settings.kafka_bootstrap_servers,
                security_protocol="PLAINTEXT", acks="all", enable_idempotence=True,
            )
            resources.push_async_callback(self._stop_producer)
            await self.producer.start()
            for name, handler in (("requested", self.analyze), ("feedback", self.feedback)):
                consumer = AIOKafkaConsumer(
                    bootstrap_servers=self.settings.kafka_bootstrap_servers,
                    group_id=f"{self.settings.kafka_env}.dameokja.ai.analysis-{name}.v1",
                    enable_auto_commit=False, auto_offset_reset="earliest",
                    security_protocol="PLAINTEXT",
                    max_poll_interval_ms=self.settings.kafka_max_poll_interval_ms,
                )
                runner = PartitionConsumer(consumer, handler, self.settings.kafka_retry_delay_seconds)
                resources.push_async_callback(runner.stop)
                await runner.start(self.topic(name), [self.topic("completed")])
        except BaseException:
            await resources.aclose()
            raise

    async def stop(self) -> None:
        if self.resources is not None:
            await self.resources.aclose()

    async def _stop_producer(self) -> None:
        if self.producer is not None:
            try:
                async with asyncio.timeout(self.settings.kafka_shutdown_timeout_seconds):
                    await self.producer.stop()
            except TimeoutError:
                # idempotent producer의 flush는 브로커 장애 때 무한 대기할 수 있다.
                logger.error("kafka.producer.shutdown_timeout unacknowledged_requests_remain_uncommitted")

    async def analyze(self, record: ConsumerRecord) -> None:
        event = EventEnvelope[RequestedPayload].model_validate_json(record.value)
        event.validate_key(record.key)
        view = await self.runtime.submit_and_wait(event.payload.analysis_request())
        outgoing = completed_event(event, view)
        if self.producer is None:
            raise RuntimeError("Kafka producer가 시작되지 않았습니다")
        await self.producer.send_and_wait(
            self.topic("completed"),
            json.dumps(outgoing, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            key=str(event.payload.user_id).encode("utf-8"),
        )

    async def feedback(self, record: ConsumerRecord) -> None:
        event = EventEnvelope[FeedbackPayload].model_validate_json(record.value)
        event.validate_key(record.key)
        if self.runtime.feedback is None:
            raise RuntimeError("피드백 서비스가 설정되지 않았습니다")
        await self.runtime.feedback.submit(event.payload.analysis_id, event.payload.feedback_request())
