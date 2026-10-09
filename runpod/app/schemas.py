from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

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
    date_type: Literal["USE_BY", "UNKNOWN", "ESTIMATED_CONSUMPTION_WINDOW"] = Field(
        default="UNKNOWN", alias="dateType"
    )
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
    review_reasons: list[str] = Field(default_factory=list, alias="reviewReasons")

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
# 평면 구조이며, 인식하지 못한 값은 기본값 대신 null로 보낸다.
class CallbackWeight(BaseModel):
    value: float | None = Field(default=None, gt=0)
    unit: Literal["G", "ML", "NONE"] | None


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
    quantity: int | None = Field(ge=1, le=100)
    weight: CallbackWeight
    expiration: CallbackExpiration

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


class RemoteInferenceResponse(BaseModel):
    input_prediction: RemoteInputPrediction | None = Field(default=None, alias="inputPrediction")
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    lines: list[RemoteOcrLine]
    model_candidates: list[RemoteModelCandidate] = Field(default_factory=list, alias="modelCandidates")
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)
