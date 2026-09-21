import json
import logging
import re

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.domain.models import (
    ClassifiedFoodLine,
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


SYSTEM_PROMPT = """당신은 한국 영수증 OCR 상품행 판별기다.
주어진 OCR 줄 중 실제로 구매한 식품과 음료 상품행만 선택한다.
OCR에 없는 상품이나 줄 번호를 만들지 않는다.
매장명, 주소, 전화번호, 결제수단, 합계, 할인, 비식품은 선택하지 않는다.
입력에 포함된 식품 상품행은 빠짐없이 모두 선택하고 임의로 개수를 제한하지 않는다.
줄 끝에 (수량 N개, 가격원) 형태로 가격이 붙어 있으면 그 줄은 실제로 구매한 상품이다 —
사전에 없는 낯선 상표명이라도 반드시 포함하고, 확신이 낮으면 confidence만 낮게 준다.
과자·라면·피자·젤리는 PROCESSED, 커피·쇼콜라 음료는 BEVERAGE,
생수·탄산수·토닉워터·소주·와인·위스키·맥주는 반드시 선택할 구매 음료 상품이며 BEVERAGE,
드레싱·소금·솔트·소스는 SEASONING으로 분류한다.
category는 VEGETABLE, FRUIT, MEAT, SEAFOOD, DAIRY, TOFU_BEAN,
GRAIN_NOODLE, PROCESSED, SEASONING, BEVERAGE, ETC 중 하나만 사용한다.
반드시 다음 JSON 형식만 반환한다.
{"items":[{"lineNo":1,"normalizedName":"우유","category":"DAIRY",
"confidence":0.8}]}
확실한 식품이 없으면 {"items":[]}를 반환한다."""


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
            "format": _ClassifierOutput.model_json_schema(),
            "keep_alive": self._settings.ollama_keep_alive,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "options": {"temperature": self._settings.ollama_temperature, "seed": 42, "num_predict": self._settings.ollama_num_predict},
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
