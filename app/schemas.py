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
    GRAIN_NOODLE = "GRAIN_NOODLE"
    PROCESSED = "PROCESSED"
    SEASONING = "SEASONING"
    BEVERAGE = "BEVERAGE"
    ETC = "ETC"


class Source(StrEnum):
    OCR = "OCR"
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
    value: Literal["RECEIPT", "OTHER"]
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


class ExpirationValue(BaseModel):
    date: Date | None = None
    date_type: Literal["USE_BY", "SELL_BY", "UNKNOWN", "ESTIMATED_CONSUMPTION_WINDOW"] = Field(
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
    # measureType에 따른 quantity·weight 조합을 검증하고 유효한 항목 자신을 반환한다.
    def validate_measurement(self) -> ReceiptItem:
        if self.measure_type == "COUNT":
            if self.quantity is None or self.weight.value is not None or self.weight.unit != "NONE":
                raise ValueError("COUNT는 quantity만 가져야 합니다")
        elif self.quantity is not None or self.weight.value is None or self.weight.unit == "NONE":
            raise ValueError("WEIGHT는 weight 값과 G 또는 ML 단위만 가져야 합니다")
        return self


class ModelTrace(BaseModel):
    pipeline_version: str = Field(alias="pipelineVersion")
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


class CallbackPayload(BaseModel):
    analysis_id: str = Field(alias="analysisId")
    status: Literal[PublicStatus.COMPLETED, PublicStatus.FAILED]
    result: AnalysisResult | None
    error: ErrorBody | None
    completed_at: datetime = Field(alias="completedAt")

    model_config = ConfigDict(populate_by_name=True)


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
