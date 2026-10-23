from __future__ import annotations

from datetime import UTC, datetime
from datetime import date as Date
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class InputHint(StrEnum):
    AUTO = "AUTO"
    RECEIPT = "RECEIPT"
    PRODUCT = "PRODUCT"


class PublicStatus(StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class InternalStage(StrEnum):
    QUEUED = "QUEUED"
    CLASSIFYING = "CLASSIFYING"
    EXTRACTING = "EXTRACTING"
    ENRICHING = "ENRICHING"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DisplayStatus(StrEnum):
    RECOGNIZED = "RECOGNIZED"
    UNRECOGNIZED = "UNRECOGNIZED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    AI_ESTIMATED = "AI_ESTIMATED"


class ReviewField(StrEnum):
    ITEM = "ITEM"
    NAME = "NAME"
    CATEGORY = "CATEGORY"
    STORAGE_TYPE = "STORAGE_TYPE"
    MEASURE_TYPE = "MEASURE_TYPE"
    QUANTITY = "QUANTITY"
    WEIGHT = "WEIGHT"
    EXPIRATION = "EXPIRATION"
    IMAGE = "IMAGE"


class ReviewReasonCode(StrEnum):
    ITEM_REVIEW_REQUIRED = "ITEM_REVIEW_REQUIRED"
    IMAGE_QUALITY_REVIEW_REQUIRED = "IMAGE_QUALITY_REVIEW_REQUIRED"
    EXPIRATION_NOT_FOUND = "EXPIRATION_NOT_FOUND"
    EXPIRATION_ESTIMATED = "EXPIRATION_ESTIMATED"
    EXPIRATION_BASIS_FALLBACK = "EXPIRATION_BASIS_FALLBACK"
    QUANTITY_DEFAULTED = "QUANTITY_DEFAULTED"
    CATEGORY_FUZZY_MATCHED = "CATEGORY_FUZZY_MATCHED"


class ReviewReason(BaseModel):
    field: ReviewField
    code: ReviewReasonCode
    messages: list[NonBlank] = Field(min_length=1)


class Category(StrEnum):
    VEGETABLE = "VEGETABLE"
    FRUIT = "FRUIT"
    MEAT = "MEAT"
    SEAFOOD = "SEAFOOD"
    DAIRY = "DAIRY"
    TOFU_BEAN = "TOFU_BEAN"
    GRAINS_NOODLE = "GRAINS_NOODLE"
    PROCESSED_FOOD = "PROCESSED_FOOD"
    SEASONING = "SEASONING"
    BEVERAGE = "BEVERAGE"
    OTHER = "OTHER"


class Source(StrEnum):
    OCR = "OCR"
    BARCODE = "BARCODE"
    VISION = "VISION"
    RECEIPT_LINE = "RECEIPT_LINE"
    CATALOG = "CATALOG"
    CATALOG_FUZZY = "CATALOG_FUZZY"
    MODEL_INFERENCE = "MODEL_INFERENCE"
    BUSINESS_RULE = "BUSINESS_RULE"
    USER_CONFIRMED = "USER_CONFIRMED"
    KNOWLEDGE_ESTIMATE = "KNOWLEDGE_ESTIMATE"


class ImageReference(BaseModel):
    object_key: NonBlank = Field(alias="objectKey", max_length=512)
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-fA-F0-9]{64}$")]

    model_config = ConfigDict(populate_by_name=True)


class AnalysisCreateRequest(BaseModel):
    request_id: NonBlank = Field(alias="requestId", max_length=100)
    refrigerator_id: int | None = Field(default=None, alias="refrigeratorId", gt=0)
    image: ImageReference
    input_hint: InputHint = Field(default=InputHint.AUTO, alias="inputHint")
    locale: Literal["ko-KR"] = "ko-KR"
    timezone: NonBlank = "Asia/Seoul"

    model_config = ConfigDict(populate_by_name=True)


class AnalysisAccepted(BaseModel):
    analysis_id: str = Field(alias="analysisId")
    status: PublicStatus
    submitted_at: datetime = Field(alias="submittedAt")
    poll_after_ms: int = Field(default=1000, alias="pollAfterMs")

    model_config = ConfigDict(populate_by_name=True)


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool
    retry_after_ms: int | None = Field(default=None, alias="retryAfterMs")

    model_config = ConfigDict(populate_by_name=True)


class DocumentTypeResult(BaseModel):
    value: Literal["RECEIPT", "PRODUCT", "OTHER"]
    confidence: float = Field(ge=0, le=1)


class ImageQuality(BaseModel):
    score: float = Field(ge=0, le=1)
    issues: list[str] = Field(default_factory=list)


class SourcedString(BaseModel):
    value: str
    normalized_value: str | None = Field(default=None, alias="normalizedValue")
    source: Source
    confidence: float = Field(ge=0, le=1)
    evidence_text: str | None = Field(default=None, alias="evidenceText")

    model_config = ConfigDict(populate_by_name=True)


class CategoryValue(BaseModel):
    value: Category
    source: Source
    confidence: float = Field(ge=0, le=1)


class QuantityValue(BaseModel):
    value: int = Field(ge=1, le=100)
    source: Source
    confidence: float = Field(ge=0, le=1)


class WeightValue(BaseModel):
    value: float | None
    unit: Literal["NONE", "G", "ML"]
    source: Source
    confidence: float = Field(ge=0, le=1)
    evidence_text: str | None = Field(default=None, alias="evidenceText")

    model_config = ConfigDict(populate_by_name=True)


class EstimatedDateRange(BaseModel):
    from_date: Date = Field(alias="from")
    to_date: Date = Field(alias="to")

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    # 예상 소비기한의 시작일이 종료일보다 늦지 않은지 검증해 반환한다.
    def validate_date_order(self) -> EstimatedDateRange:
        if self.from_date > self.to_date:
            raise ValueError("estimatedRange.from은 estimatedRange.to보다 늦을 수 없습니다")
        return self


class ExpirationValue(BaseModel):
    date: Date | None = None
    date_type: Literal["USE_BY", "UNKNOWN", "ESTIMATED_CONSUMPTION_WINDOW"] = Field(default="UNKNOWN", alias="dateType")
    source: Source | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence_text: str | None = Field(default=None, alias="evidenceText")
    estimated_range: EstimatedDateRange | None = Field(default=None, alias="estimatedRange")
    assumptions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list, alias="evidenceIds")

    model_config = ConfigDict(populate_by_name=True)


class ReceiptItem(BaseModel):
    item_id: str = Field(alias="itemId")
    display_status: DisplayStatus = Field(alias="displayStatus")
    name: SourcedString
    category: CategoryValue
    storage_type: Literal["REFRIGERATED", "FROZEN"] = Field(alias="storageType")
    measure_type: Literal["COUNT", "WEIGHT"] = Field(alias="measureType")
    quantity: QuantityValue | None
    weight: WeightValue
    expiration: ExpirationValue
    review_reasons: list[ReviewReason] = Field(default_factory=list, alias="reviewReasons")

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    # COUNT는 수량만, WEIGHT는 포장 중량과 선택적 구매 수량을 검증해 반환한다.
    def validate_measurement(self) -> ReceiptItem:
        if self.measure_type == "COUNT":
            if self.quantity is None or self.weight.value is not None or self.weight.unit != "NONE":
                raise ValueError("COUNT는 quantity만 가져야 합니다")
        elif self.weight.value is None or self.weight.unit == "NONE":
            raise ValueError("WEIGHT는 weight 값과 G 또는 ML 단위를 가져야 합니다")
        return self


class ModelTrace(BaseModel):
    pipeline_version: str = Field(alias="pipelineVersion")
    classifier_version: str = Field(alias="classifierVersion")
    ocr_version: str = Field(alias="ocrVersion")
    llm_model: str = Field(alias="llmModel")
    normalizer_version: str = Field(alias="normalizerVersion")
    policy_version: str = Field(alias="policyVersion")

    model_config = ConfigDict(populate_by_name=True)


class AnalysisResult(BaseModel):
    document_type: DocumentTypeResult = Field(alias="documentType")
    image_quality: ImageQuality = Field(alias="imageQuality")
    items: list[ReceiptItem]
    model_trace: ModelTrace = Field(alias="modelTrace")

    model_config = ConfigDict(populate_by_name=True)


class AnalysisView(BaseModel):
    analysis_id: str = Field(alias="analysisId")
    status: PublicStatus
    stage: InternalStage | None = None
    submitted_at: datetime = Field(alias="submittedAt")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")
    result: AnalysisResult | None = None
    error: ErrorBody | None = None

    model_config = ConfigDict(populate_by_name=True)


# 아래 Callback* 모델은 백엔드가 소유한 "재고 인식 결과 콜백 API 명세"
# (POST /api/v1/internal/image-analyses/callback)를 따른다. GET 조회용 AnalysisResult보다 단순한
# 평면 구조이며, 인식하지 못한 수량·중량은 0으로 보낸다.
class CallbackWeight(BaseModel):
    value: float = Field(ge=0)
    unit: Literal["G", "ML", "NONE"]


class CallbackExpiration(BaseModel):
    date: Date | None
    date_type: Literal["USE_BY", "SELL_BY", "UNKNOWN", "ESTIMATED_CONSUMPTION_WINDOW"] = Field(alias="dateType")
    estimated_range: EstimatedDateRange | None = Field(alias="estimatedRange")

    model_config = ConfigDict(populate_by_name=True)


class CallbackItem(BaseModel):
    item_id: str = Field(alias="itemId")
    display_status: DisplayStatus = Field(alias="displayStatus")
    name: str | None
    category: Category | None
    storage_type: Literal["REFRIGERATED", "FROZEN"] | None = Field(alias="storageType")
    measure_type: Literal["COUNT", "WEIGHT"] | None = Field(alias="measureType")
    quantity: int = Field(ge=0, le=100)
    weight: CallbackWeight
    expiration: CallbackExpiration
    review_reasons: list[ReviewReason] = Field(default_factory=list, alias="reviewReasons")

    model_config = ConfigDict(populate_by_name=True)


class CallbackResult(BaseModel):
    document_type: Literal["RECEIPT", "PRODUCT", "OTHER"] = Field(alias="documentType")
    items: list[CallbackItem]

    model_config = ConfigDict(populate_by_name=True)


class CallbackPayload(BaseModel):
    analysis_id: str = Field(alias="analysisId")
    status: Literal[PublicStatus.COMPLETED, PublicStatus.FAILED]
    completed_at: datetime = Field(alias="completedAt")
    result: CallbackResult | None
    error: ErrorBody | None

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("completed_at")
    @classmethod
    # 콜백 완료 시각이 timezone-aware인지 확인하고 백엔드 전송 기준인 UTC로 정규화한다.
    def normalize_completed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("completedAt에는 시간대 정보가 필요합니다")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    # 완료 상태에는 결과만, 실패 상태에는 오류만 포함됐는지 검증해 반환한다.
    def validate_status_body(self) -> CallbackPayload:
        if self.status == PublicStatus.COMPLETED and (self.result is None or self.error is not None):
            raise ValueError("COMPLETED 콜백은 result가 필요하고 error는 null이어야 합니다")
        if self.status == PublicStatus.FAILED and (self.result is not None or self.error is None):
            raise ValueError("FAILED 콜백은 result가 null이고 error가 필요합니다")
        return self


class ApiErrorResponse(BaseModel):
    error: ErrorBody


class ModelStatus(BaseModel):
    ready: bool
    queue_size: int = Field(alias="queueSize")
    queue_capacity: int = Field(alias="queueCapacity")
    # 컨슈머가 여러 개라 동시에 여러 건이 처리 중일 수 있다. active_analysis_id는 하위 호환용으로
    # 그중 하나(또는 없으면 null)만 담고, 전체 동시 처리 수는 active_count로 확인한다.
    active_analysis_id: str | None = Field(default=None, alias="activeAnalysisId")
    active_count: int = Field(default=0, alias="activeCount")
    models: dict[str, Any]

    model_config = ConfigDict(populate_by_name=True)


class RemoteInferenceRequest(BaseModel):
    request_id: NonBlank = Field(alias="requestId", max_length=100)
    image_url: NonBlank = Field(alias="imageUrl", max_length=2048)
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-fA-F0-9]{64}$")]
    input_hint: InputHint = Field(alias="inputHint")

    model_config = ConfigDict(populate_by_name=True)


class RemoteInputPrediction(BaseModel):
    value: Literal["RECEIPT", "PRODUCT", "OTHER"]
    confidence: float = Field(ge=0, le=1)
    margin: float = Field(ge=0, le=1)
    accepted: bool


class RemoteOcrLine(BaseModel):
    line_no: int = Field(alias="lineNo", ge=1)
    text: str
    confidence: float = Field(ge=0, le=1)
    box: list[list[float]] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class RemoteModelCandidate(BaseModel):
    line_no: int = Field(alias="lineNo", ge=1)
    normalized_name: str = Field(alias="normalizedName")
    category: Category
    confidence: float = Field(ge=0, le=1)
    reason: str = "model_inference"

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("category", mode="before")
    @classmethod
    # 이전 GPU Worker가 사용하던 카테고리 코드를 현재 백엔드 계약 코드로 변환한다.
    def normalize_legacy_category(cls, value: Any) -> Any:
        return {
            "GRAIN_NOODLE": "GRAINS_NOODLE",
            "PROCESSED": "PROCESSED_FOOD",
            "ETC": "OTHER",
        }.get(value, value)


class RemoteInferenceResponse(BaseModel):
    input_prediction: RemoteInputPrediction | None = Field(default=None, alias="inputPrediction")
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    lines: list[RemoteOcrLine]
    model_candidates: list[RemoteModelCandidate] = Field(default_factory=list, alias="modelCandidates")
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


# 아래 Feedback* 모델은 "AI‐1단계 API 명세" 6절 피드백 API를 따른다. 백엔드는 사용자가 분석 결과를
# 재고로 확정할 때 분석 한 건의 항목별 처리 결과를 한 번에 보낸다.
class FeedbackAction(StrEnum):
    CONFIRMED = "CONFIRMED"  # AI 결과를 그대로 확정
    CORRECTED = "CORRECTED"  # AI 결과의 일부 값을 고쳐서 확정
    ADDED = "ADDED"  # AI가 찾지 못한 상품을 사용자가 새로 추가
    DELETED = "DELETED"  # AI가 찾은 상품을 사용자가 삭제


class TrainingEligibility(StrEnum):
    NOT_ELIGIBLE = "NOT_ELIGIBLE"  # 학습하지 않음(수정 아님, 동의 없음, 학습 대상 필드 변경 없음)
    PERSONAL_APPLIED = "PERSONAL_APPLIED"  # 해당 냉장고의 개인 보정에만 즉시 적용
    PENDING_REVIEW = "PENDING_REVIEW"  # 학습 후보. 다른 사용자의 같은 수정이 쌓이기를 기다림
    APPLIED = "APPLIED"  # 과거 전역 승격 이력 호환용. 신규 피드백에서는 생성하지 않음
    REJECTED = "REJECTED"  # 검수자가 학습에서 제외함


class FeedbackConfirmedItem(BaseModel):
    name: NonBlank = Field(max_length=100)
    category: Category
    storage_type: Literal["REFRIGERATED", "FROZEN"] = Field(alias="storageType")
    measure_type: Literal["COUNT", "WEIGHT"] = Field(alias="measureType")
    quantity: int | None = None
    weight_value: float | None = Field(default=None, alias="weightValue")
    weight_unit: Literal["G", "ML", "NONE"] = Field(default="NONE", alias="weightUnit")
    expiration_date: Date | None = Field(default=None, alias="expirationDate")

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    # 백엔드 ingredients ERD의 CHECK 조건과 같은 측정값 조합만 허용하고 자신을 반환한다.
    def validate_erd_measurement(self) -> FeedbackConfirmedItem:
        if self.measure_type == "COUNT":
            if self.quantity is None or not 1 <= self.quantity <= 100 or self.weight_value is not None or self.weight_unit != "NONE":
                raise ValueError("COUNT는 1~100 quantity만 가지며 weightValue=null, weightUnit=NONE이어야 합니다")
        elif self.quantity is not None or self.weight_value is None or self.weight_value <= 0 or self.weight_unit == "NONE":
            raise ValueError("WEIGHT는 quantity=null, weightValue>0, weightUnit=G 또는 ML이어야 합니다")
        return self


class FeedbackItem(BaseModel):
    item_id: str | None = Field(default=None, alias="itemId", max_length=64)
    action: FeedbackAction
    confirmed: FeedbackConfirmedItem | None = None

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    # 동작별로 필요한 itemId·확정값 조합을 검증하고 자신을 반환한다.
    def validate_action_fields(self) -> FeedbackItem:
        if self.action == FeedbackAction.ADDED:
            if self.item_id is not None or self.confirmed is None:
                raise ValueError("ADDED는 itemId 없이 confirmed만 가져야 합니다")
        elif self.item_id is None:
            raise ValueError(f"{self.action.value}에는 itemId가 필요합니다")
        elif self.action == FeedbackAction.CORRECTED and self.confirmed is None:
            raise ValueError("CORRECTED에는 confirmed가 필요합니다")
        elif self.action in {FeedbackAction.CONFIRMED, FeedbackAction.DELETED} and self.confirmed is not None:
            raise ValueError(f"{self.action.value}에는 confirmed를 보내지 않습니다")
        return self


class FeedbackRequest(BaseModel):
    refrigerator_id: int = Field(alias="refrigeratorId", gt=0)
    model_improvement_consent: bool = Field(alias="consentForModelImprovement")
    items: list[FeedbackItem] = Field(min_length=1, max_length=100)

    model_config = ConfigDict(populate_by_name=True)

    @model_validator(mode="after")
    # 같은 분석 항목을 두 번 보내지 않았는지 확인하고 자신을 반환한다.
    def validate_unique_items(self) -> FeedbackRequest:
        item_ids = [item.item_id for item in self.items if item.item_id is not None]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("같은 itemId를 두 번 보낼 수 없습니다")
        return self


class FeedbackItemResult(BaseModel):
    item_id: str | None = Field(alias="itemId")
    action: FeedbackAction
    training_eligibility: TrainingEligibility = Field(alias="trainingEligibility")

    model_config = ConfigDict(populate_by_name=True)


class FeedbackRecorded(BaseModel):
    feedback_id: str = Field(alias="feedbackId")
    analysis_id: str = Field(alias="analysisId")
    status: Literal["RECORDED"] = "RECORDED"
    model_improvement_consent: bool = Field(alias="modelImprovementConsent")
    items: list[FeedbackItemResult]
    recorded_at: datetime = Field(alias="recordedAt")

    model_config = ConfigDict(populate_by_name=True)
