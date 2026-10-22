import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.adapters._cache import LazyPool
from app.config import Settings
from app.errors import DependencyUnavailable
from app.feedback import FeedbackEntry, LearningVote, decide_promotion
from app.schemas import (
    AnalysisView,
    FeedbackAction,
    FeedbackItemResult,
    FeedbackRecorded,
    FeedbackRequest,
    TrainingEligibility,
)
from app.store import AnalysisRecord

logger = logging.getLogger(__name__)

# requestId 멱등성은 프로세스 메모리 범위라, 재시작 뒤 같은 requestId로 다시 접수하면 새 analysisId가
# 생긴다. 그때는 이전 분석 이력을 새 분석으로 대체한다(이전 분석의 피드백은 원본 항목을 복사해 두므로 남는다).
_REPLACE_SUPERSEDED_ANALYSIS = """
    DELETE FROM receipt_ai.receipt_analyses
    WHERE request_id = $1 AND analysis_id <> $2
"""

# 같은 analysis_id를 다시 저장하면 첫 기록을 유지한다.
_SAVE_ANALYSIS = """
    INSERT INTO receipt_ai.receipt_analyses (
        analysis_id, request_id, image_object_key, image_sha256, input_hint, locale, timezone,
        status, stage, document_type, document_confidence, image_quality_score, image_quality_issues,
        pipeline_version, ocr_version, llm_model, normalizer_version, policy_version, final_payload,
        error_code, error_message, error_retryable, submitted_at, started_at, completed_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14, $15, $16, $17, $18,
            $19::jsonb, $20, $21, $22, $23, $24, $25)
    ON CONFLICT (analysis_id) DO NOTHING
"""

_LOAD_ANALYSIS = """
    SELECT final_payload::text AS payload
    FROM receipt_ai.receipt_analyses
    WHERE analysis_id = $1 AND status IN ('COMPLETED', 'FAILED')
"""

_FIND_FEEDBACK = """
    SELECT feedback_id, item_id, action, training_eligibility, model_improvement_consent, created_at
    FROM receipt_ai.receipt_item_feedback
    WHERE analysis_id = $1
    ORDER BY feedback_pk
"""

_INSERT_FEEDBACK = """
    INSERT INTO receipt_ai.receipt_item_feedback (
        feedback_id, analysis_id, refrigerator_id, item_id, action, original_item, confirmed,
        changed_fields, learning_key, target_name, target_normalized_name, target_category,
        model_improvement_consent, training_eligibility
    )
    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10, $11, $12, $13, $14)
"""

_LEARNING_VOTES = """
    SELECT refrigerator_id, target_name, target_normalized_name, target_category
    FROM receipt_ai.receipt_item_feedback
    WHERE learning_key = $1 AND training_eligibility IN ('PERSONAL_APPLIED', 'PENDING_REVIEW', 'APPLIED')
    ORDER BY created_at, feedback_pk
"""

_ACTIVE_CORRECTION = """
    SELECT correction_id, normalized_name, category
    FROM receipt_ai.learned_corrections
    WHERE learning_key = $1 AND scope = 'GLOBAL' AND is_active
"""

_ACTIVE_PERSONAL_CORRECTION = """
    SELECT correction_id
    FROM receipt_ai.learned_corrections
    WHERE learning_key = $1 AND scope = 'PERSONAL' AND refrigerator_id = $2 AND is_active
"""

_UPDATE_PERSONAL_CORRECTION = """
    UPDATE receipt_ai.learned_corrections
    SET corrected_name = $2, normalized_name = $3, category = $4,
        support_count = 1, agreement_ratio = 1, review_status = 'APPROVED'
    WHERE correction_id = $1
"""

_INSERT_PERSONAL_CORRECTION = """
    INSERT INTO receipt_ai.learned_corrections (
        learning_key, corrected_name, normalized_name, category, support_count, agreement_ratio,
        scope, refrigerator_id, review_status
    )
    VALUES ($1, $2, $3, $4, 1, 1, 'PERSONAL', $5, 'APPROVED')
"""

_UPDATE_CORRECTION_SUPPORT = """
    UPDATE receipt_ai.learned_corrections
    SET corrected_name = $2, support_count = $3, agreement_ratio = $4
    WHERE correction_id = $1
"""

_DEACTIVATE_CORRECTION = """
    UPDATE receipt_ai.learned_corrections
    SET is_active = FALSE, deactivated_at = CURRENT_TIMESTAMP
    WHERE correction_id = $1
"""

_INSERT_CORRECTION = """
    INSERT INTO receipt_ai.learned_corrections (
        learning_key, corrected_name, normalized_name, category, support_count, agreement_ratio,
        scope, refrigerator_id, review_status
    )
    VALUES ($1, $2, $3, $4, $5, $6, 'GLOBAL', NULL, 'APPROVED')
"""

# 승격된 값과 같은 수정은 APPLIED로, 다른 값으로 고쳤던 기존 APPLIED 수정은 다시 후보로 되돌린다.
_MARK_APPLIED = """
    UPDATE receipt_ai.receipt_item_feedback
    SET training_eligibility = CASE
        WHEN target_normalized_name = $2 AND target_category = $3 THEN 'APPLIED'
        ELSE 'PERSONAL_APPLIED'
    END
    WHERE learning_key = $1 AND training_eligibility IN ('PERSONAL_APPLIED', 'PENDING_REVIEW', 'APPLIED')
"""


class PostgresFeedbackRepository:
    """분석 결과 이력, 사용자 피드백과 피드백에서 승격한 학습 지식을 PostgreSQL에 저장한다."""

    # DB 접속 설정을 받아 저장소를 구성한다. 연결 pool은 최초 사용 시 지연 생성한다.
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool = LazyPool(self._create_pool)

    # 완료·실패한 분석 결과를 receipt_analyses에 저장한다. 피드백이 1시간 메모리 보관 이후에도 원본을 찾게 한다.
    async def save_analysis(self, record: AnalysisRecord) -> None:
        view = record.to_view()
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(_REPLACE_SUPERSEDED_ANALYSIS, record.request.request_id, record.analysis_id)
            await self._insert_analysis(connection, record, view)

    async def _insert_analysis(self, connection: Any, record: AnalysisRecord, view: AnalysisView) -> None:
        result = view.result
        error = view.error
        await connection.execute(
            _SAVE_ANALYSIS,
            record.analysis_id,
            record.request.request_id,
            record.request.image.object_key,
            record.request.image.sha256,
            record.request.input_hint.value,
            record.request.locale,
            record.request.timezone,
            view.status.value,
            view.stage.value if view.stage is not None else None,
            result.document_type.value if result is not None else None,
            result.document_type.confidence if result is not None else None,
            result.image_quality.score if result is not None else None,
            json.dumps(result.image_quality.issues if result is not None else []),
            result.model_trace.pipeline_version if result is not None else self._settings.pipeline_version,
            result.model_trace.ocr_version if result is not None else self._settings.ocr_version,
            result.model_trace.llm_model if result is not None else self._settings.ollama_model,
            result.model_trace.normalizer_version if result is not None else self._settings.normalizer_version,
            result.model_trace.policy_version if result is not None else self._settings.policy_version,
            view.model_dump_json(by_alias=True),
            error.code if error is not None else None,
            error.message[:500] if error is not None else None,
            error.retryable if error is not None else None,
            view.submitted_at,
            view.started_at,
            view.completed_at,
        )

    # 영속 저장소에서 완료·실패한 분석 view를 찾아 반환하고 없으면 None을 반환한다.
    async def load_analysis(self, analysis_id: str) -> AnalysisView | None:
        pool = await self._get_pool()
        row = await pool.fetchrow(_LOAD_ANALYSIS, analysis_id)
        if row is None:
            return None
        return AnalysisView.model_validate_json(row["payload"])

    # 분석에 이미 기록된 피드백이 있으면 기록 결과로 반환하고 없으면 None을 반환한다.
    async def find_feedback(self, analysis_id: str) -> FeedbackRecorded | None:
        pool = await self._get_pool()
        rows = await pool.fetch(_FIND_FEEDBACK, analysis_id)
        return self._recorded(analysis_id, rows) if rows else None

    # 피드백 항목을 한 트랜잭션으로 저장하고, 학습 후보가 생긴 키마다 승격 여부를 판단해 반영한다.
    async def record_feedback(
        self,
        feedback_id: str,
        analysis_id: str,
        request: FeedbackRequest,
        entries: list[FeedbackEntry],
        min_support: int,
        min_agreement: float,
    ) -> tuple[FeedbackRecorded, list[str]]:
        import asyncpg

        pool = await self._get_pool()
        promoted: list[str] = []
        try:
            async with pool.acquire() as connection, connection.transaction():
                # 서비스 계층의 사전 조회만으로는 동시 재전송을 막을 수 없다. 분석 ID 단위로
                # 트랜잭션을 직렬화한 뒤 다시 조회해 ADDED-only 요청도 한 번만 저장한다.
                await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", analysis_id)
                existing_rows = await connection.fetch(_FIND_FEEDBACK, analysis_id)
                if existing_rows:
                    return self._recorded(analysis_id, existing_rows), []
                for entry in entries:
                    await connection.execute(
                        _INSERT_FEEDBACK,
                        feedback_id,
                        analysis_id,
                        request.refrigerator_id,
                        entry.item_id,
                        entry.action.value,
                        json.dumps(entry.original_item, ensure_ascii=False) if entry.original_item is not None else None,
                        json.dumps(entry.confirmed, ensure_ascii=False) if entry.confirmed is not None else None,
                        json.dumps(entry.changed_fields),
                        entry.learning_key,
                        entry.target_name,
                        entry.target_normalized_name,
                        entry.target_category,
                        request.model_improvement_consent,
                        entry.training_eligibility.value,
                    )
                learning_keys = sorted(
                    {
                        entry.learning_key
                        for entry in entries
                        if entry.learning_key and entry.training_eligibility == TrainingEligibility.PERSONAL_APPLIED
                    }
                )
                # 서로 다른 분석이 같은 키를 동시에 수정해도 개인 지식 삽입과 전역 집계를 직렬화한다.
                for key in learning_keys:
                    await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)
                for entry in entries:
                    if entry.learning_key and entry.training_eligibility == TrainingEligibility.PERSONAL_APPLIED:
                        await self._upsert_personal(connection, request.refrigerator_id, entry)
                for key in learning_keys:
                    if await self._promote(connection, key, min_support, min_agreement):
                        promoted.append(key)
                rows = await connection.fetch(_FIND_FEEDBACK, analysis_id)
        except asyncpg.UniqueViolationError:
            # 같은 분석의 피드백이 동시에 두 번 들어와 다른 요청이 먼저 기록한 경우다.
            existing = await self.find_feedback(analysis_id)
            if existing is None:
                raise
            return existing, []
        return self._recorded(analysis_id, rows), promoted

    # 사용자 수정 한 건을 해당 냉장고 전용 보정 지식으로 즉시 저장하거나 최신 값으로 교체한다.
    async def _upsert_personal(self, connection: Any, refrigerator_id: int, entry: FeedbackEntry) -> None:
        current = await connection.fetchrow(_ACTIVE_PERSONAL_CORRECTION, entry.learning_key, refrigerator_id)
        if current is None:
            await connection.execute(
                _INSERT_PERSONAL_CORRECTION,
                entry.learning_key,
                entry.target_name,
                entry.target_normalized_name,
                entry.target_category,
                refrigerator_id,
            )
            return
        await connection.execute(
            _UPDATE_PERSONAL_CORRECTION,
            current["correction_id"],
            entry.target_name,
            entry.target_normalized_name,
            entry.target_category,
        )

    # 학습 키 하나의 수정 의견을 모아 승격 기준을 넘으면 learned_corrections를 갱신하고 변경 여부를 반환한다.
    async def _promote(self, connection: Any, key: str, min_support: int, min_agreement: float) -> bool:
        # 같은 키를 동시에 승격하면 활성 지식이 두 개 생길 수 있으므로 키 단위로 직렬화한다.
        await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)
        votes = [
            LearningVote(
                refrigerator_id=row["refrigerator_id"],
                target_name=row["target_name"],
                target_normalized_name=row["target_normalized_name"],
                target_category=row["target_category"],
            )
            for row in await connection.fetch(_LEARNING_VOTES, key)
        ]
        decision = decide_promotion(votes, min_support, min_agreement)
        if decision is None:
            return False

        current = await connection.fetchrow(_ACTIVE_CORRECTION, key)
        if current is not None and (current["normalized_name"], current["category"]) == (
            decision.target_normalized_name,
            decision.target_category,
        ):
            await connection.execute(
                _UPDATE_CORRECTION_SUPPORT,
                current["correction_id"],
                decision.target_name,
                decision.support_count,
                decision.agreement_ratio,
            )
        else:
            if current is not None:
                await connection.execute(_DEACTIVATE_CORRECTION, current["correction_id"])
            await connection.execute(
                _INSERT_CORRECTION,
                key,
                decision.target_name,
                decision.target_normalized_name,
                decision.target_category,
                decision.support_count,
                decision.agreement_ratio,
            )
            logger.info(
                "feedback.learning.promoted learning_key_length=%s category=%s support=%s agreement=%.2f",
                len(key),
                decision.target_category,
                decision.support_count,
                decision.agreement_ratio,
            )
        await connection.execute(_MARK_APPLIED, key, decision.target_normalized_name, decision.target_category)
        return True

    # 열려 있는 연결 pool이 있으면 닫는다.
    async def close(self) -> None:
        await self._pool.close()

    async def _get_pool(self) -> Any:
        try:
            return await self._pool.get()
        except DependencyUnavailable:
            raise
        except Exception as exc:
            raise DependencyUnavailable("피드백 저장소에 연결하지 못했습니다.") from exc

    # DATABASE_URL로 asyncpg 연결 pool을 새로 생성해 반환한다.
    async def _create_pool(self) -> Any:
        try:
            import asyncpg
        except ImportError as exc:
            raise DependencyUnavailable("피드백 저장소를 사용하려면 asyncpg 의존성을 설치해야 합니다.") from exc
        return await asyncpg.create_pool(
            self._settings.database_url,
            min_size=1,
            max_size=self._settings.database_pool_max_size,
        )

    @staticmethod
    def _recorded(analysis_id: str, rows: list[Any]) -> FeedbackRecorded:
        return FeedbackRecorded(
            feedbackId=rows[0]["feedback_id"],
            analysisId=analysis_id,
            modelImprovementConsent=rows[0]["model_improvement_consent"],
            items=[
                FeedbackItemResult(
                    itemId=row["item_id"],
                    action=FeedbackAction(row["action"]),
                    trainingEligibility=TrainingEligibility(row["training_eligibility"]),
                )
                for row in rows
            ],
            recordedAt=rows[0]["created_at"] or datetime.now(UTC),
        )
