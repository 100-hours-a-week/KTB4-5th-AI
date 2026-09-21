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
    matched_via: str = "exact"  # "exact" | "fuzzy"
    similarity: float | None = None


@dataclass(frozen=True, slots=True)
class LoadedImage:
    content: bytes
    media_type: str
    width: int
    height: int
    sha256: str


@dataclass(slots=True)
class PipelineDiagnostics:
    llm_prompt_tokens: int | None = None
    llm_completion_tokens: int | None = None
    llm_prompt_duration_ns: int | None = None
    llm_eval_duration_ns: int | None = None
    llm_load_duration_ns: int | None = None
    warnings: list[str] = field(default_factory=list)


class ImageLoader(Protocol):
    # 객체 키와 해시를 받아 검증된 이미지를 반환한다.
    async def load(self, object_key: str, expected_sha256: str) -> LoadedImage:
        ...


class TextExtractor(Protocol):
    # 이미지를 받아 OCR 문서를 반환한다.
    async def extract(self, image: LoadedImage) -> OcrDocument:
        ...

    # OCR 준비 여부를 반환한다.
    async def ready(self) -> bool:
        ...


class FoodCandidateClassifier(Protocol):
    # OCR 줄을 받아 식품 후보와 진단 정보를 반환한다.
    async def classify(self, lines: list[OcrLine]) -> tuple[list[ClassifiedFoodLine], PipelineDiagnostics]:
        ...

    # 분류 모델 준비 여부를 반환한다.
    async def ready(self) -> bool:
        ...


class ShelfLifeLookup(Protocol):
    # 정규화 식재료명과 보관 방식을 받아 검수된 보관기간 추정 규칙 또는 None을 반환한다.
    async def find(self, normalized_ingredient_name: str, storage_type: str) -> ShelfLifeEstimate | None:
        ...


class ProductCatalogLookup(Protocol):
    # 정규화 상품명을 받아 별칭·표준명이 정확히 일치하는 카탈로그 항목 또는 None을 반환한다.
    async def find(self, normalized_text: str) -> ProductCatalogMatch | None:
        ...
