import asyncio
import logging
import re
import time
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
        self._discovered_url: str | None = None
        self._discovered_at = 0.0
        self._discovery_lock = asyncio.Lock()

    # 객체 키를 서명 URL로 바꿔 Worker에 보내고 도메인 추론 묶음으로 변환한다.
    async def infer(self, object_key: str, sha256: str, input_hint: str) -> RemoteInferenceBundle:
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
            response = await self._post_inference(payload, headers)
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
            product_detection_ms=parsed.diagnostics.get("productDetectionMs"),
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
        try:
            worker_url = await self._worker_url()
            response = await self._client.get(
                f"{worker_url}/health/ready"
            )
            return response.status_code == 200 and bool(response.json().get("ready"))
        except (DependencyUnavailable, httpx.HTTPError, ValueError):
            return False

    async def _post_inference(self, payload: dict, headers: dict) -> httpx.Response:
        worker_url = await self._worker_url()
        try:
            response = await self._client.post(
                f"{worker_url}/internal/v1/infer",
                json=payload,
                headers=headers,
            )
        except httpx.RequestError:
            if self._settings.remote_inference_url:
                raise
            self._invalidate_discovery()
            worker_url = await self._worker_url(force=True)
            response = await self._client.post(
                f"{worker_url}/internal/v1/infer",
                json=payload,
                headers=headers,
            )
        if self._settings.remote_inference_url or response.status_code < 500:
            response.raise_for_status()
            return response

        self._invalidate_discovery()
        worker_url = await self._worker_url(force=True)
        response = await self._client.post(
            f"{worker_url}/internal/v1/infer",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        return response

    async def _worker_url(self, *, force: bool = False) -> str:
        if self._settings.remote_inference_url:
            return self._settings.remote_inference_url.rstrip("/")
        if not self._settings.runpod_api_key or not self._settings.runpod_pod_name_prefix:
            raise DependencyUnavailable("GPU Worker 주소 또는 Runpod 자동 탐색 설정이 필요합니다.")

        now = time.monotonic()
        if not force and self._discovered_url and now - self._discovered_at < self._settings.runpod_discovery_ttl_seconds:
            return self._discovered_url

        async with self._discovery_lock:
            now = time.monotonic()
            if not force and self._discovered_url and now - self._discovered_at < self._settings.runpod_discovery_ttl_seconds:
                return self._discovered_url
            return await self._discover_worker()

    async def _discover_worker(self) -> str:
        headers = {"Authorization": f"Bearer {self._settings.runpod_api_key}"}
        response = await self._client.get("https://api.runpod.io/v2/pods", params={"limit": 1000}, headers=headers)
        response.raise_for_status()
        pods = response.json().get("pods", [])
        pattern = re.compile(rf"^{re.escape(self._settings.runpod_pod_name_prefix)}-v(\d+)$")
        candidates: list[tuple[int, str, str]] = []
        for pod in pods:
            match = pattern.fullmatch(str(pod.get("name", "")))
            if not match or pod.get("status") != "RUNNING":
                continue
            if self._settings.runpod_template_id and pod.get("template") != self._settings.runpod_template_id:
                continue
            pod_id = str(pod.get("id", ""))
            if pod_id:
                candidates.append((int(match.group(1)), str(pod.get("startedAt") or ""), pod_id))

        candidates.sort(reverse=True)
        versions: dict[int, int] = {}
        for version, _, _ in candidates:
            versions[version] = versions.get(version, 0) + 1
        duplicate_versions = [version for version, count in versions.items() if count > 1]
        if duplicate_versions:
            joined = ", ".join(f"v{version}" for version in sorted(duplicate_versions, reverse=True))
            raise DependencyUnavailable(f"같은 버전의 Runpod Worker가 여러 개 실행 중입니다: {joined}")
        for version, _, pod_id in candidates:
            worker_url = f"https://{pod_id}-{self._settings.runpod_worker_port}.proxy.runpod.net"
            try:
                ready = await self._client.get(f"{worker_url}/health/ready")
                if ready.status_code == 200 and bool(ready.json().get("ready")):
                    self._discovered_url = worker_url
                    self._discovered_at = time.monotonic()
                    logger.info("remote_inference.discovered pod_id=%s version=%s", pod_id, version)
                    return worker_url
            except (httpx.HTTPError, ValueError):
                continue
        raise DependencyUnavailable("실행 중이며 준비가 완료된 Runpod GPU Worker를 찾지 못했습니다.")

    def _invalidate_discovery(self) -> None:
        self._discovered_url = None
        self._discovered_at = 0.0

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
