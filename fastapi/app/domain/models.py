from dataclasses import dataclass, field
from typing import Protocol

from app.schemas import Category


@dataclass(frozen=True, slots=True)
class OcrLine:
    line_no: int
    text: str
    confidence: float
    box: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class OcrDocument:
    lines: tuple[OcrLine, ...]
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ReceiptProductRow:
    row_number: int
    product_line: OcrLine
    unit_price: int | None
    quantity: int | None
    amount: int | None


@dataclass(frozen=True, slots=True)
class ClassifiedFoodLine:
    line_no: int
    normalized_name: str
    category: Category
    confidence: float
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ShelfLifeEstimate:
    rule_id: int
    duration_min_days: int
    duration_max_days: int
    source_name: str
    source_url: str


@dataclass(frozen=True, slots=True)
class ProductCatalogMatch:
    product_id: int
    canonical_name: str
    category: Category
    suggested_storage_type: str | None
    matched_via: str = "exact"  # "exact" | "fuzzy" | "learned"(피드백 학습 지식)
    similarity: float | None = None


@dataclass(slots=True)
class PipelineDiagnostics:
    worker_queue_wait_ms: float | None = None
    download_ms: float | None = None
    input_classifier_ms: float | None = None
    ocr_queue_wait_ms: float | None = None
    ocr_ms: float | None = None
    product_detection_ms: float | None = None
    llm_queue_wait_ms: float | None = None
    llm_ms: float | None = None
    worker_total_ms: float | None = None
    input_classifier_active: bool | None = None
    llm_prompt_tokens: int | None = None
    llm_completion_tokens: int | None = None
    llm_prompt_duration_ns: int | None = None
    llm_eval_duration_ns: int | None = None
    llm_load_duration_ns: int | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class InputTypePrediction:
    document_type: str
    confidence: float
    margin: float
    accepted: bool


@dataclass(frozen=True, slots=True)
class RemoteInferenceBundle:
    document: OcrDocument
    input_prediction: InputTypePrediction | None
    model_candidates: tuple[ClassifiedFoodLine, ...]
    diagnostics: PipelineDiagnostics


class RemoteInferenceProvider(Protocol):
    # 객체 키·해시·입력 힌트를 원격 추론 작업자에 전달하고 한 번의 호출로 추론 묶음을 반환한다.
    async def infer(self, object_key: str, sha256: str, input_hint: str) -> RemoteInferenceBundle:
        ...

    # 원격 추론 작업자의 준비 여부를 반환한다.
    async def ready(self) -> bool:
        ...

    # 재사용 HTTP 연결을 정리한다.
    async def close(self) -> None:
        ...


class ShelfLifeLookup(Protocol):
    # 정규화 식재료명·보관 방식·신선식품 여부를 받아 검수된 보관기간 추정 규칙 또는 None을 반환한다.
    async def find(self, normalized_ingredient_name: str, storage_type: str, fresh_item: bool = True) -> ShelfLifeEstimate | None:
        ...


class ProductCatalogLookup(Protocol):
    # 정규화 상품명을 받아 별칭·표준명이 정확히 일치하는 카탈로그 항목 또는 None을 반환한다.
    async def find(self, normalized_text: str) -> ProductCatalogMatch | None:
        ...
