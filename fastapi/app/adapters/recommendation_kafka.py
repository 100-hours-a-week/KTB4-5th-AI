"""기존 추천 큐와 완료 이력을 사용하는 Kafka 요청·완료 경계."""

import asyncio
import logging
from contextlib import AsyncExitStack

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import ConsumerRecord

from app.adapters.kafka import PartitionConsumer
from app.adapters.recipe_events import RecipeEvent, RecommendationRequested, completed_event
from app.config import Settings
from app.recipes.jobs import RecipeJobs

logger = logging.getLogger(__name__)


class RecommendationKafka:
    def __init__(self, settings: Settings, recipe_jobs: RecipeJobs) -> None:
        self.settings = settings
        self.recipe_jobs = recipe_jobs
        self.producer: AIOKafkaProducer | None = None
        self.resources: AsyncExitStack | None = None

    def topic(self, name: str) -> str:
        return f"{self.settings.kafka_env}.dameokja.recommendation.{name}.v1"

    async def start(self) -> None:
        if self.recipe_jobs.history is None:
            raise RuntimeError("Kafka 추천 결과 재전송을 위해 DATABASE_URL과 022 마이그레이션이 필요합니다")
        resources = AsyncExitStack()
        self.resources = resources
        try:
            self.producer = AIOKafkaProducer(
                bootstrap_servers=self.settings.kafka_bootstrap_servers,
                security_protocol="PLAINTEXT", acks="all", enable_idempotence=True,
            )
            resources.push_async_callback(self._stop_producer)
            await self.producer.start()
            consumer = AIOKafkaConsumer(
                bootstrap_servers=self.settings.kafka_bootstrap_servers,
                group_id=f"{self.settings.kafka_env}.dameokja.ai.recommendation-requested.v1",
                enable_auto_commit=False, auto_offset_reset="earliest", security_protocol="PLAINTEXT",
                max_poll_interval_ms=self.settings.kafka_max_poll_interval_ms,
            )
            request_consumer = PartitionConsumer(consumer, self.recommend, self.settings.kafka_retry_delay_seconds)
            resources.push_async_callback(request_consumer.stop)
            await request_consumer.start(self.topic("requested"), [self.topic("completed")])
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
                logger.error("recommendation.kafka.shutdown_timeout unacknowledged_requests_remain_uncommitted")

    async def recommend(self, record: ConsumerRecord) -> None:
        request_event = RecipeEvent[RecommendationRequested].model_validate_json(record.value)
        request_event.validate_key(record.key)
        if self.producer is None:
            raise RuntimeError("Kafka 추천 producer가 시작되지 않았습니다")
        completed_job = await self.recipe_jobs.submit_and_wait(request_event.payload.to_request())
        completion_event = completed_event(request_event, completed_job)
        await self.producer.send_and_wait(
            self.topic("completed"), completion_event.model_dump_json().encode("utf-8"),
            key=str(request_event.payload.userId).encode("utf-8"),
        )
