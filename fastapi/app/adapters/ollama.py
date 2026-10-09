import base64
import io
import json
import logging
import re

import httpx
from PIL import Image, ImageOps
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.domain.models import (
    ClassifiedFoodLine,
    LoadedImage,
    OcrLine,
    PipelineDiagnostics,
)
from app.errors import ModelUnavailable
from app.schemas import Category

logger = logging.getLogger(__name__)


class _SelectedLine(BaseModel):
    line_no: int = Field(alias="lineNo")
    normalized_name: str = Field(alias="normalizedName", min_length=1, max_length=100)
    category: Category
    confidence: float = Field(ge=0, le=1)


class _ClassifierOutput(BaseModel):
    items: list[_SelectedLine] = Field(max_length=20)


class _VisionClassifierOutput(BaseModel):
    document_type: str = Field(alias="documentType", pattern=r"^(RECEIPT|PRODUCT|OTHER)$")
    document_confidence: float = Field(alias="documentConfidence", ge=0, le=1)
    package_count: int | None = Field(default=None, alias="packageCount", ge=0, le=20)
    items: list[_SelectedLine] = Field(max_length=30)


class _ProductVisionOutput(BaseModel):
    package_count: int = Field(alias="packageCount", ge=0, le=20)
    items: list[_SelectedLine] = Field(max_length=20)


SYSTEM_PROMPT = """영수증 OCR에서 구매한 식품·음료 줄만 JSON items로 반환한다.
OCR에 없는 줄·상품은 만들지 말고 매장정보·합계·할인·결제·비식품은 제외한다.
가격 힌트가 붙은 낯선 상품도 포함하며 불확실하면 confidence를 낮춘다.
category: VEGETABLE,FRUIT,MEAT,SEAFOOD,DAIRY,TOFU_BEAN,GRAINS_NOODLE,
PROCESSED_FOOD,SEASONING,BEVERAGE,OTHER. 과자·면·조리식품=PROCESSED_FOOD, 물·커피·주류=BEVERAGE,
소금·소스=SEASONING. 형식: {"items":[{"lineNo":1,"normalizedName":"우유",
"category":"DAIRY","confidence":0.8}]}. 없으면 {"items":[]}."""

_CLASSIFIER_FORMAT = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "lineNo": {"type": "integer"},
                    "normalizedName": {"type": "string"},
                    "category": {"type": "string", "enum": [item.value for item in Category]},
                    "confidence": {"type": "number"},
                },
                "required": ["lineNo", "normalizedName", "category", "confidence"],
            },
        }
    },
    "required": ["items"],
}


VISION_SYSTEM_PROMPT = """당신은 한국 식품 이미지 분석기다. OCR 텍스트와 원본 이미지를 함께 본다.
문서 유형은 영수증 RECEIPT, 한 개 또는 소수의 실물 식품 PRODUCT, 그 외 OTHER로 판정한다.
RECEIPT이면 실제로 구매한 식품과 음료 상품행을 빠짐없이 선택한다. 매장명, 주소, 전화번호,
합계, 할인, 결제수단, 비식품은 제외한다. 낯선 상표라도 상품번호와 가격·수량 구조가 있으면
구매 상품으로 포함한다. lineNo는 해당 상품명이 있는 OCR 줄 번호를 사용한다. normalizedName은
깨진 OCR 문자열을 그대로 복사하지 말고 원본 이미지의 같은 행을 다시 읽어 사람이 이해할 수 있는
상품명으로 교정한다. 다만 이미지에서 확인할 수 없는 글자를 추측해 상표나 상품명을 만들지는 않는다.
상품명 앞의 P·@·# 같은 과세·적립 코드는 상품명에서 제외하고, 용량·묶음 수는 상품명과 분리한다.
PRODUCT이면 이미지 중심의 식품을 판별한다. 과일·채소·고기·생선 같은 비포장 식재료와
과자·라면·음료 같은 포장식품을 포함한다. 대표 상품명 OCR 줄이 있으면 그 lineNo를 쓰고,
글자가 없거나 적절한 줄이 없으면 lineNo 0을 쓴다. 실제로 여러 상품이 함께 보일 때만
여러 개를 반환한다. 반드시 눈에 보이는 실물 또는 포장 하나당 items 하나만 반환한다.
브랜드명만 있는 글자, 맛·용량·개수·홍보문구, 같은 포장의 OCR 조각을 별도 상품으로 만들지 않는다.
같은 포장에 한글 상품명과 영문 번역·부제가 함께 있으면 한글 상품명 하나만 반환한다.
normalizedName은 포장 전면에서 읽은 완전한 상품명 또는 시각적으로 확인한 일반 식재료명이어야 한다.
PRODUCT에서는 OCR 줄을 상품 목록으로 취급하지 말고 먼저 사진 속 서로 다른 실물·포장의 개수를 센다.
그 개수를 packageCount에 기록한다. items 개수는 packageCount와 정확히 같아야 하며 모든 item의
lineNo는 0으로 쓴다. 한 포장에 "어묵탕"과 "어묵"처럼 전체 상품명과 식품 유형명이 함께 보여도
더 구체적인 전체 상품명 하나만 반환한다.
식품이 아니거나 확신할 수 없으면 items를 비운다.
category는 VEGETABLE, FRUIT, MEAT, SEAFOOD, DAIRY, TOFU_BEAN, GRAINS_NOODLE,
PROCESSED_FOOD, SEASONING, BEVERAGE, OTHER 중 하나다. 과자·라면·젤리는 PROCESSED_FOOD,
물·커피·주류는 BEVERAGE, 소스·소금은 SEASONING이다. OCR에 없는 상표를 지어내지 말고,
시각적으로 식별한 일반 식재료명은 허용한다. 지정된 JSON 스키마만 반환한다."""


PRODUCT_VISION_SYSTEM_PROMPT = """식품 실물 사진을 분석한다.
눈에 보이는 서로 다른 식재료나 포장식품 하나당 item 하나만 반환한다.
normalizedName은 포장 전면의 전체 상품명 또는 일반 식재료명으로 쓰고 lineNo는 항상 0이다.
브랜드·맛·용량·홍보문구와 같은 포장의 OCR 조각을 별도 상품으로 만들지 않는다.
상품명이 안 보이면 용기 형태·내용물·식품 그림을 함께 보고 우유, 사과처럼 일반 식품명으로 식별한다.
Tetra Pak 같은 포장재 제조사명이나 용기 종류를 식품명으로 반환하지 않는다.
packageCount는 실제 식재료·포장 개수이며 items 개수와 같아야 한다.
category는 VEGETABLE, FRUIT, MEAT, SEAFOOD, DAIRY, TOFU_BEAN, GRAINS_NOODLE,
PROCESSED_FOOD, SEASONING, BEVERAGE, OTHER 중 하나다. 식품이 아니면 packageCount 0, items []로 반환한다.
지정된 JSON 스키마만 반환한다."""


class OllamaFoodClassifier:
    # Ollama 모델 설정과 선택 HTTP 클라이언트를 받아 식품행 분류기를 구성한다.
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    # 분류기가 직접 만든 HTTP 클라이언트가 있으면 닫고 반환한다.
    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    # Ollama 태그 목록에서 설정 모델이 로드 가능한지 확인해 boolean으로 반환한다.
    async def ready(self) -> bool:
        client = self._get_client()
        try:
            response = await client.get("/api/tags")
            response.raise_for_status()
            names = {item.get("name") for item in response.json().get("models", [])}
            return self._settings.ollama_model in names
        except (httpx.HTTPError, ValueError):
            return False

    # 마스킹된 OCR 줄을 모델에 전달하고 원문 검증된 식품행과 토큰 진단을 반환한다.
    async def classify(self, lines: list[OcrLine]) -> tuple[list[ClassifiedFoodLine], PipelineDiagnostics]:
        if not lines:
            return [], PipelineDiagnostics()
        allowed = {index: line for index, line in enumerate(lines, start=1)}
        user_prompt = "\n".join(f"{index}: {line.text}" for index, line in enumerate(lines, start=1))
        payload = {
            "model": self._settings.ollama_model,
            "stream": False,
            "think": False,
            "format": _CLASSIFIER_FORMAT,
            "keep_alive": self._settings.ollama_keep_alive,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "options": {
                "temperature": self._settings.ollama_temperature,
                "seed": 42,
                "num_predict": min(self._settings.ollama_num_predict, 64 + len(lines) * 24),
            },
        }
        try:
            response = await self._get_client().post("/api/chat", json=payload)
            response.raise_for_status()
            body = response.json()
            parsed = self._parse_content(body["message"]["content"])
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as exc:
            logger.exception("ollama.classification.failed error_type=%s", type(exc).__name__)
            raise ModelUnavailable("Ollama 상품행 판별에 실패했습니다.") from exc

        selected: list[ClassifiedFoodLine] = []
        seen: set[int] = set()
        for item in parsed.items:
            if item.line_no not in allowed or item.line_no in seen:
                continue
            original_line = allowed[item.line_no]
            original = original_line.text
            normalized = item.normalized_name.strip()
            if not normalized or not self._has_text_overlap(original, normalized):
                continue
            seen.add(item.line_no)
            selected.append(
                ClassifiedFoodLine(
                    line_no=original_line.line_no,
                    normalized_name=normalized,
                    category=item.category,
                    confidence=min(item.confidence, original_line.confidence),
                    reason="",
                )
            )
        diagnostics = PipelineDiagnostics(
            llm_prompt_tokens=body.get("prompt_eval_count"),
            llm_completion_tokens=body.get("eval_count"),
            llm_prompt_duration_ns=body.get("prompt_eval_duration"),
            llm_eval_duration_ns=body.get("eval_duration"),
            llm_load_duration_ns=body.get("load_duration"),
        )
        return selected, diagnostics

    # 원본 이미지와 OCR 줄을 함께 Gemma 비전 모델에 전달해 문서 유형·상품 후보·보정 OCR 줄을 반환한다.
    async def classify_image(
        self,
        image: LoadedImage,
        lines: list[OcrLine],
        input_hint: str,
    ) -> tuple[str, float, list[OcrLine], list[ClassifiedFoodLine], PipelineDiagnostics]:
        ocr_text = "\n".join(f"{line.line_no}: {line.text}" for line in lines)
        user_content = (
            "입력 힌트: PRODUCT\nOCR 글자 조각을 상품 수로 세지 말고, 원본 이미지에서 서로 다른 실물·포장만 세어 판별하세요."
            if input_hint == "PRODUCT"
            else f"입력 힌트: {input_hint}\nOCR 줄 목록:\n{ocr_text or '(OCR 텍스트 없음)'}"
        )
        payload = {
            "model": self._settings.ollama_model,
            "stream": False,
            "think": False,
            "format": _VisionClassifierOutput.model_json_schema(),
            "keep_alive": self._settings.ollama_keep_alive,
            "messages": [
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": user_content,
                    "images": [base64.b64encode(image.content).decode("ascii")],
                },
            ],
            "options": {
                "temperature": self._settings.ollama_temperature,
                "seed": 42,
                # 여러 실물 상품을 구조화하면 512~1024토큰에서도 JSON이 중간에 잘릴 수 있다.
                # 일반 텍스트 분류 설정은 유지하되 비전 응답은 설정 상한인 2048토큰을 확보한다.
                "num_predict": 2048,
            },
        }
        try:
            response = await self._get_client().post("/api/chat", json=payload)
            response.raise_for_status()
            body = response.json()
            parsed = _VisionClassifierOutput.model_validate_json(body["message"]["content"])
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as exc:
            logger.exception("ollama.vision_classification.failed error_type=%s", type(exc).__name__)
            raise ModelUnavailable("Ollama 이미지 상품 판별에 실패했습니다.") from exc

        document_type = input_hint if input_hint in {"RECEIPT", "PRODUCT"} else parsed.document_type
        output_lines = list(lines)
        valid_line_numbers = {line.line_no for line in output_lines}
        receipt_anchors = {line.line_no for line in output_lines if re.match(r"^\s*\d{3}(?:\D|$)", line.text)}
        parsed_items = self._deduplicate_product_items(parsed.items, parsed.package_count) if document_type == "PRODUCT" else parsed.items
        selected: list[ClassifiedFoodLine] = []
        seen_line_numbers: set[int] = set()
        for item in parsed_items:
            line_no = item.line_no
            if document_type == "RECEIPT" and line_no not in receipt_anchors:
                nearby = [anchor for anchor in receipt_anchors if 0 <= line_no - anchor <= 3]
                if nearby:
                    line_no = max(nearby)
            if document_type == "PRODUCT" and line_no not in valid_line_numbers:
                line_no = max(valid_line_numbers, default=0) + 1
                output_lines.append(
                    OcrLine(
                        line_no=line_no,
                        text=item.normalized_name,
                        confidence=item.confidence,
                    )
                )
                valid_line_numbers.add(line_no)
            if line_no not in valid_line_numbers or line_no in seen_line_numbers:
                continue
            seen_line_numbers.add(line_no)
            selected.append(
                ClassifiedFoodLine(
                    line_no=line_no,
                    normalized_name=item.normalized_name.strip(),
                    category=item.category,
                    confidence=item.confidence,
                    reason="vision_model" if document_type == "PRODUCT" else "model_inference",
                )
            )

        diagnostics = PipelineDiagnostics(
            llm_prompt_tokens=body.get("prompt_eval_count"),
            llm_completion_tokens=body.get("eval_count"),
            llm_prompt_duration_ns=body.get("prompt_eval_duration"),
            llm_eval_duration_ns=body.get("eval_duration"),
            llm_load_duration_ns=body.get("load_duration"),
        )
        return document_type, parsed.document_confidence, output_lines, selected, diagnostics

    # MobileNet이 PRODUCT로 확정한 이미지를 짧은 전용 프롬프트와 축소 이미지로 분석한다.
    async def classify_product_image(
        self,
        image: LoadedImage,
        lines: list[OcrLine],
    ) -> tuple[list[OcrLine], list[ClassifiedFoodLine], PipelineDiagnostics]:
        payload = {
            "model": self._settings.ollama_model,
            "stream": False,
            "think": False,
            "format": _ProductVisionOutput.model_json_schema(),
            "keep_alive": self._settings.ollama_keep_alive,
            "messages": [
                {"role": "system", "content": PRODUCT_VISION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "사진 속 식품을 식별하세요.",
                    "images": [self._prepare_vision_image(image)],
                },
            ],
            "options": {"temperature": self._settings.ollama_temperature, "seed": 42, "num_predict": 512},
        }
        try:
            response = await self._get_client().post("/api/chat", json=payload)
            response.raise_for_status()
            body = response.json()
            parsed = _ProductVisionOutput.model_validate_json(body["message"]["content"])
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as exc:
            logger.exception("ollama.product_vision.failed error_type=%s", type(exc).__name__)
            raise ModelUnavailable("Ollama 실물 상품 판별에 실패했습니다.") from exc

        output_lines = list(lines)
        selected: list[ClassifiedFoodLine] = []
        for item in self._deduplicate_product_items(parsed.items, parsed.package_count):
            line_no = max((line.line_no for line in output_lines), default=0) + 1
            output_lines.append(OcrLine(line_no=line_no, text=item.normalized_name, confidence=item.confidence))
            selected.append(ClassifiedFoodLine(line_no=line_no, normalized_name=item.normalized_name.strip(), category=item.category, confidence=item.confidence, reason="vision_model"))
        diagnostics = PipelineDiagnostics(
            llm_prompt_tokens=body.get("prompt_eval_count"),
            llm_completion_tokens=body.get("eval_count"),
            llm_prompt_duration_ns=body.get("prompt_eval_duration"),
            llm_eval_duration_ns=body.get("eval_duration"),
            llm_load_duration_ns=body.get("load_duration"),
        )
        return output_lines, selected, diagnostics

    @staticmethod
    # 비전 모델 입력의 긴 변을 768px로 제한하고 JPEG로 압축해 전송·이미지 인코딩 비용을 줄인다.
    def _prepare_vision_image(image: LoadedImage) -> str:
        with Image.open(io.BytesIO(image.content)) as source:
            prepared = ImageOps.exif_transpose(source).convert("RGB")
            prepared.thumbnail((768, 768), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            prepared.save(buffer, format="JPEG", quality=85, optimize=True)
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    @staticmethod
    # 실물 사진에서 한 포장의 전체 상품명과 짧은 식품 유형명이 중복 반환되면 구체적인 후보만 남긴다.
    def _deduplicate_product_items(items: list[_SelectedLine], package_count: int | None) -> list[_SelectedLine]:
        unique: list[_SelectedLine] = []
        for item in sorted(items, key=lambda value: len(re.sub(r"\W", "", value.normalized_name)), reverse=True):
            key = re.sub(r"[^0-9A-Za-z가-힣]", "", item.normalized_name).casefold()
            if not key:
                continue
            if any(item.category == kept.category and key in re.sub(r"[^0-9A-Za-z가-힣]", "", kept.normalized_name).casefold() for kept in unique):
                continue
            unique.append(item)
        if package_count is not None:
            return unique[:package_count]
        return unique

    # 기존 Ollama 클라이언트를 반환하거나 base URL과 timeout으로 생성해 반환한다.
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._settings.ollama_base_url,
                timeout=self._settings.ollama_timeout_seconds,
            )
        return self._client

    @staticmethod
    # 모델의 JSON 또는 JSON code fence 문자열을 검증된 분류 출력 객체로 변환한다.
    def _parse_content(content: str) -> _ClassifierOutput:
        cleaned = content.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.S)
        if fenced:
            cleaned = fenced.group(1)
        return _ClassifierOutput.model_validate(json.loads(cleaned))

    @staticmethod
    # OCR 원문과 정규화 이름을 받아 공통 토큰 또는 포함 관계 존재 여부를 반환한다.
    def _has_text_overlap(original: str, normalized: str) -> bool:

        # 문자열에서 두 글자 이상의 한글·영문 토큰 집합을 반환한다.
        def tokens(value: str) -> set[str]:
            return set(re.findall(r"[가-힣A-Za-z]{2,}", value.lower()))

        original_tokens = tokens(original)
        normalized_tokens = tokens(normalized)
        return bool(original_tokens & normalized_tokens) or normalized.replace(" ", "") in original.replace(" ", "")
