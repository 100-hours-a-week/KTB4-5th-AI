import logging
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from app.config import Settings
from app.domain.receipt_rules import correct_common_ocr_errors, normalize_product_name
from app.errors import AnalysisNotCompleted, AnalysisNotFound, AppError, DependencyUnavailable, InvalidRequest
from app.schemas import (
    AnalysisView,
    FeedbackAction,
    FeedbackItem,
    FeedbackRecorded,
    FeedbackRequest,
    PublicStatus,
    ReceiptItem,
    TrainingEligibility,
)
from app.store import AnalysisRecord, InMemoryAnalysisStore

logger = logging.getLogger(__name__)

# 정규화 후 이보다 짧은 OCR 원문은 학습 키로 쓰지 않는다. 한 글자 조각은 여러 상품에 공통으로
# 나타나 잘못된 지식으로 번질 위험이 크다.
_MIN_LEARNING_KEY_LENGTH = 2


# OCR 원문·상품명을 카탈로그 조회와 같은 규칙으로 정규화해 학습 키·비교 값으로 반환한다.
def normalize_learning_text(text: str | None) -> str:
    if not text:
        return ""
    return normalize_product_name(correct_common_ocr_errors(text))


@dataclass(slots=True)
class FeedbackEntry:
    """저장할 피드백 한 항목과 학습 판단 결과."""

    item_id: str | None
    action: FeedbackAction
    original_item: dict | None
    confirmed: dict | None
    changed_fields: list[str] = field(default_factory=list)
    learning_key: str | None = None
    target_name: str | None = None
    target_normalized_name: str | None = None
    target_category: str | None = None
    training_eligibility: TrainingEligibility = TrainingEligibility.NOT_ELIGIBLE


@dataclass(frozen=True, slots=True)
class LearningVote:
    """같은 학습 키에 대한 한 냉장고의 수정 의견."""

    refrigerator_id: int
    target_name: str
    target_normalized_name: str
    target_category: str


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    target_name: str
    target_normalized_name: str
    target_category: str
    support_count: int
    agreement_ratio: float


class FeedbackRepository(Protocol):
    # 완료·실패한 분석 결과를 영속 저장소에 남긴다.
    async def save_analysis(self, record: AnalysisRecord) -> None: ...

    # 영속 저장소에서 분석 결과 view를 찾아 반환하고 없으면 None을 반환한다.
    async def load_analysis(self, analysis_id: str) -> AnalysisView | None: ...

    # 분석에 이미 기록된 피드백이 있으면 반환한다.
    async def find_feedback(self, analysis_id: str) -> FeedbackRecorded | None: ...

    # 피드백을 저장하고 승격 판단을 반영한 결과와 승격된 학습 키 목록을 반환한다.
    async def record_feedback(
        self,
        feedback_id: str,
        analysis_id: str,
        request: FeedbackRequest,
        entries: list[FeedbackEntry],
        min_support: int,
        min_agreement: float,
    ) -> tuple[FeedbackRecorded, list[str]]: ...


class LearnedKnowledgeCache(Protocol):
    # 학습 지식이 바뀐 조회 키의 캐시를 비운다.
    def forget(self, normalized_text: str, refrigerator_id: int | None = None) -> None: ...


# 분석 원본 항목과 사용자 피드백을 비교해 저장할 항목과 학습 후보 여부를 반환한다.
# 학습 대상은 동의한 사용자가 수정(CORRECTED)한 항목의 상품명·카테고리뿐이다. 새로 추가하거나 삭제한
# 상품, 그대로 확정한 상품, 수량·중량·소비기한만 바꾼 상품은 기록만 하고 학습하지 않는다.
def evaluate_feedback_item(item: FeedbackItem, original: ReceiptItem | None, consent: bool) -> FeedbackEntry:
    entry = FeedbackEntry(
        item_id=item.item_id,
        action=item.action,
        original_item=original.model_dump(by_alias=True, mode="json") if original is not None else None,
        confirmed=item.confirmed.model_dump(by_alias=True, mode="json") if item.confirmed is not None else None,
    )
    if item.action != FeedbackAction.CORRECTED or original is None or item.confirmed is None:
        return entry

    confirmed = item.confirmed
    original_names = {normalize_learning_text(original.name.value), normalize_learning_text(original.name.normalized_value)}
    target_normalized = normalize_learning_text(confirmed.name)
    if target_normalized and target_normalized not in original_names:
        entry.changed_fields.append("name")
    if confirmed.category != original.category.value:
        entry.changed_fields.append("category")
    if confirmed.storage_type != original.storage_type:
        entry.changed_fields.append("storageType")
    if confirmed.measure_type != original.measure_type:
        entry.changed_fields.append("measureType")
    original_quantity = original.quantity.value if original.quantity is not None else None
    if confirmed.quantity != original_quantity:
        entry.changed_fields.append("quantity")
    if confirmed.weight_value != original.weight.value or confirmed.weight_unit != original.weight.unit:
        entry.changed_fields.append("weight")
    if confirmed.expiration_date != original.expiration.date:
        entry.changed_fields.append("expirationDate")

    learning_key = normalize_learning_text(original.name.evidence_text or original.name.value)
    learnable = {"name", "category"} & set(entry.changed_fields)
    if consent and learnable and len(learning_key.replace(" ", "")) >= _MIN_LEARNING_KEY_LENGTH and target_normalized:
        entry.learning_key = learning_key
        entry.target_name = confirmed.name
        entry.target_normalized_name = target_normalized
        entry.target_category = confirmed.category.value
        entry.training_eligibility = TrainingEligibility.PERSONAL_APPLIED
    return entry


# 같은 학습 키에 쌓인 수정 의견을 받아 승격할 값을 반환하고, 기준에 못 미치면 None을 반환한다.
# 서로 다른 냉장고 min_support곳 이상이 같은 값(정규화 이름·카테고리)으로 고쳤고, 의견을 낸 전체
# 냉장고 중 그 값의 비율이 min_agreement 이상일 때만 승격한다. 한 냉장고가 여러 번 보내도 한 표다.
def decide_promotion(votes: Iterable[LearningVote], min_support: int, min_agreement: float) -> PromotionDecision | None:
    # 저장소가 오래된 순서로 전달한 의견 중 냉장고별 마지막 의견만 사용한다. 같은 냉장고가
    # 서로 다른 분석에서 값을 다시 고친 경우 과거 값과 최신 값에 동시에 투표하는 것을 막는다.
    latest_by_refrigerator: dict[int, LearningVote] = {}
    for vote in votes:
        latest_by_refrigerator[vote.refrigerator_id] = vote

    supporters: dict[tuple[str, str], set[int]] = {}
    display_names: dict[tuple[str, str], str] = {}
    voters: set[int] = set()
    for vote in latest_by_refrigerator.values():
        target = (vote.target_normalized_name, vote.target_category)
        supporters.setdefault(target, set()).add(vote.refrigerator_id)
        display_names.setdefault(target, vote.target_name)
        voters.add(vote.refrigerator_id)
    if not supporters:
        return None
    target, fridges = max(supporters.items(), key=lambda item: (len(item[1]), item[0]))
    support = len(fridges)
    agreement = support / len(voters)
    if support < min_support or agreement < min_agreement:
        return None
    return PromotionDecision(
        target_name=display_names[target],
        target_normalized_name=target[0],
        target_category=target[1],
        support_count=support,
        agreement_ratio=round(agreement, 4),
    )


class FeedbackService:
    # 메모리 분석 저장소, 영속 저장소, 학습 지식 캐시와 승격 기준 설정으로 서비스를 구성한다.
    def __init__(
        self,
        settings: Settings,
        store: InMemoryAnalysisStore,
        repository: FeedbackRepository | None,
        knowledge_cache: LearnedKnowledgeCache | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._repository = repository
        self._knowledge_cache = knowledge_cache

    # 분석 ID와 피드백을 받아 기록 결과와 신규 기록 여부를 반환한다. 같은 분석에 다시 보내면 기존 기록을 돌려준다.
    async def submit(self, analysis_id: str, request: FeedbackRequest) -> tuple[FeedbackRecorded, bool]:
        try:
            return await self._submit(analysis_id, request)
        except AppError:
            raise
        except Exception as exc:
            # 테이블 누락(마이그레이션 전)·연결 오류 같은 저장소 장애는 재시도 가능한 503으로 알린다.
            logger.exception("feedback.repository.failed analysis_id=%s", analysis_id)
            raise DependencyUnavailable("피드백 저장소를 사용할 수 없습니다.") from exc

    async def _submit(self, analysis_id: str, request: FeedbackRequest) -> tuple[FeedbackRecorded, bool]:
        if self._repository is None:
            raise DependencyUnavailable("피드백 저장소(DATABASE_URL)가 설정되지 않아 피드백을 기록할 수 없습니다.")

        existing = await self._repository.find_feedback(analysis_id)
        if existing is not None:
            logger.info("feedback.duplicate analysis_id=%s feedback_id=%s", analysis_id, existing.feedback_id)
            return existing, False

        view = await self._find_view(analysis_id)
        if view.status != PublicStatus.COMPLETED or view.result is None:
            raise AnalysisNotCompleted()

        originals = {item.item_id: item for item in view.result.items}
        unknown = [item.item_id for item in request.items if item.item_id is not None and item.item_id not in originals]
        if unknown:
            raise InvalidRequest(f"분석 결과에 없는 itemId입니다: {', '.join(sorted(unknown))}")

        entries = [
            evaluate_feedback_item(item, originals.get(item.item_id) if item.item_id else None, request.model_improvement_consent)
            for item in request.items
        ]
        feedback_id = f"fbk_{secrets.token_hex(12)}"
        recorded, promoted_keys = await self._repository.record_feedback(
            feedback_id,
            analysis_id,
            request,
            entries,
            self._settings.feedback_promotion_min_support,
            self._settings.feedback_promotion_min_agreement,
        )
        if self._knowledge_cache is not None:
            personal_keys = {entry.learning_key for entry in entries if entry.learning_key}
            for key in personal_keys:
                self._knowledge_cache.forget(key, request.refrigerator_id)
            for key in promoted_keys:
                self._knowledge_cache.forget(key)
        logger.info(
            "feedback.recorded analysis_id=%s feedback_id=%s items=%s learning_candidates=%s promoted=%s",
            analysis_id,
            feedback_id,
            len(entries),
            sum(entry.training_eligibility != TrainingEligibility.NOT_ELIGIBLE for entry in entries),
            len(promoted_keys),
        )
        return recorded, True

    # 메모리에 남아 있는 분석을 먼저 찾고, 보관시간이 지났으면 영속 저장소에서 찾는다.
    async def _find_view(self, analysis_id: str) -> AnalysisView:
        try:
            return self._store.get_view(analysis_id)
        except AnalysisNotFound:
            pass
        view = await self._repository.load_analysis(analysis_id) if self._repository is not None else None
        if view is None:
            raise AnalysisNotFound()
        return view
