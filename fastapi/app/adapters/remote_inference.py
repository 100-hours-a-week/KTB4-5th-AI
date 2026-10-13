import asyncio
import logging
from pathlib import PurePosixPath
from urllib.parse import quote, urljoin

import httpx

from app.config import Settings
from app.domain.models import (
    ClassifiedFoodLine,
    InputTypePrediction,
    OcrDocument,
    OcrLine,
    PipelineDiagnostics,
    RemoteInferenceBundle,
)
from app.errors import DependencyUnavailable, InvalidRequest
from app.schemas import RemoteInferenceResponse

logger = logging.getLogger(__name__)


class RemoteInferenceClient:
    # 설정을 받아 RunPod 등 원격 GPU Worker용 재사용 HTTP 클라이언트를 구성한다.
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=settings.remote_inference_timeout_seconds)

    # 객체 키를 서명 URL로 바꿔 Worker에 보내고 도메인 추론 묶음으로 변환한다.
    async def infer(self, object_key: str, sha256: str, input_hint: str) -> RemoteInferenceBundle:
        if not self._settings.remote_inference_url:
            raise DependencyUnavailable("REMOTE_INFERENCE_URL이 설정되지 않아 GPU Worker를 호출할 수 없습니다.")
        image_url = await self._resolve_image_url(object_key)
        payload = {
            "requestId": f"remote_{sha256[:16]}",
            "imageUrl": image_url,
            "sha256": sha256,
            "inputHint": input_hint,
        }
        headers = {}
        if self._settings.remote_inference_api_key:
            headers["X-Worker-API-Key"] = self._settings.remote_inference_api_key
        try:
            response = await self._client.post(
                f"{self._settings.remote_inference_url.rstrip('/')}/internal/v1/infer",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            parsed = RemoteInferenceResponse.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            error_code, error_message = self._worker_error(exc.response)
            logger.warning(
                "remote_inference.failed status_code=%s error_code=%s message=%s",
                exc.response.status_code,
                error_code,
                error_message,
            )
            if 400 <= exc.response.status_code < 500:
                raise InvalidRequest(f"원격 추론 요청이 거부되었습니다: {error_code}") from exc
            raise DependencyUnavailable(f"원격 GPU 추론 작업자 오류: {error_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("remote_inference.failed error_type=%s", type(exc).__name__)
            raise DependencyUnavailable("원격 GPU 추론 작업자를 호출하지 못했습니다.") from exc
        prediction = None
        if parsed.input_prediction is not None:
            prediction = InputTypePrediction(
                document_type=parsed.input_prediction.value,
                confidence=parsed.input_prediction.confidence,
                margin=parsed.input_prediction.margin,
                accepted=parsed.input_prediction.accepted,
            )
        document = OcrDocument(
            lines=tuple(
                OcrLine(
                    line_no=line.line_no,
                    text=line.text,
                    confidence=line.confidence,
                    box=tuple((point[0], point[1]) for point in line.box if len(point) >= 2),
                )
                for line in parsed.lines
            ),
            width=parsed.width,
            height=parsed.height,
        )
        candidates = tuple(
            ClassifiedFoodLine(
                line_no=item.line_no,
                normalized_name=item.normalized_name,
                category=item.category,
                confidence=item.confidence,
                reason=item.reason,
            )
            for item in parsed.model_candidates
        )
        diagnostics = PipelineDiagnostics(
            worker_queue_wait_ms=parsed.diagnostics.get("workerQueueWaitMs"),
            download_ms=parsed.diagnostics.get("downloadMs"),
            input_classifier_ms=parsed.diagnostics.get("inputClassifierMs"),
            ocr_queue_wait_ms=parsed.diagnostics.get("ocrQueueWaitMs"),
            ocr_ms=parsed.diagnostics.get("ocrMs"),
            llm_queue_wait_ms=parsed.diagnostics.get("llmQueueWaitMs"),
            llm_ms=parsed.diagnostics.get("llmMs"),
            worker_total_ms=parsed.diagnostics.get("workerTotalMs"),
            input_classifier_active=parsed.diagnostics.get("inputClassifierActive"),
            llm_prompt_tokens=parsed.diagnostics.get("llmPromptTokens"),
            llm_completion_tokens=parsed.diagnostics.get("llmCompletionTokens"),
            llm_prompt_duration_ns=parsed.diagnostics.get("llmPromptDurationNs"),
            llm_eval_duration_ns=parsed.diagnostics.get("llmEvalDurationNs"),
            llm_load_duration_ns=parsed.diagnostics.get("llmLoadDurationNs"),
            warnings=list(parsed.diagnostics.get("warnings", [])),
        )
        return RemoteInferenceBundle(document, prediction, candidates, diagnostics)

    # Worker의 readiness endpoint가 성공하는지 확인한다.
    async def ready(self) -> bool:
        if not self._settings.remote_inference_url:
            return False
        try:
            response = await self._client.get(
                f"{self._settings.remote_inference_url.rstrip('/')}/health/ready"
            )
            return response.status_code == 200 and bool(response.json().get("ready"))
        except (httpx.HTTPError, ValueError):
            return False

    # 이 어댑터가 생성한 HTTP 클라이언트만 종료한다.
    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    # Worker 오류 응답에서 공개 가능한 오류 코드와 메시지만 추출해 진단 로그에 사용한다.
    def _worker_error(response: httpx.Response) -> tuple[str, str]:
        try:
            error = response.json().get("error", {})
            return str(error.get("code", "WORKER_HTTP_ERROR")), str(error.get("message", ""))[:300]
        except (ValueError, AttributeError):
            return "WORKER_HTTP_ERROR", ""

    # S3 objectKey는 짧은 만료시간의 서명 URL로, 로컬 키는 테스트용 기준 URL로 변환한다.
    async def _resolve_image_url(self, object_key: str) -> str:
        key = PurePosixPath(object_key)
        if key.is_absolute() or ".." in key.parts:
            raise InvalidRequest("허용되지 않은 image.objectKey입니다.")
        if self._settings.image_source == "s3":
            if not object_key.startswith(self._settings.s3_allowed_prefix):
                raise InvalidRequest("허용되지 않은 S3 객체 경로입니다.")
            return await asyncio.to_thread(self._presign_s3, object_key)
        base_url = self._settings.remote_image_base_url.rstrip("/") + "/"
        return urljoin(base_url, quote(object_key, safe="/"))

    # 고정 버킷과 객체 키에 대해 10분짜리 S3 GET 서명 URL을 생성한다.
    def _presign_s3(self, object_key: str) -> str:
        try:
            import boto3

            region = self._settings.s3_region
            endpoint_url = f"https://s3.{region}.amazonaws.com" if region else None
            client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url)
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._settings.s3_bucket, "Key": object_key},
                ExpiresIn=600,
            )
        except Exception as exc:
            raise DependencyUnavailable("S3 이미지 서명 URL을 생성하지 못했습니다.") from exc
