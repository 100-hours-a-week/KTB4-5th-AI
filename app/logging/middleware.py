import json
import logging
import re
import secrets
from typing import Any
from urllib.parse import parse_qsl

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("api.audit")

MAX_CAPTURE_BYTES = 1024 * 1024


class ApiAuditLogMiddleware:
    # 다음 ASGI 앱을 받아 요청·응답 안전 요약과 trace ID를 기록하는 미들웨어를 구성한다.
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    # ASGI scope와 채널을 받아 HTTP body를 제한 캡처하고 요청·응답 로그를 남긴다.
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope.get("path", "")
        trace_id = self._trace_id(scope)
        request_body = bytearray()
        response_body = bytearray()
        response_status = 500
        response_content_type = ""

        # 하위 앱 요청 메시지를 받아 제한 크기로 body를 복사한 뒤 원본 메시지를 반환한다.
        async def receive_with_capture() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                self._append_limited(request_body, message.get("body", b""))
            return message

        # 응답 메시지를 받아 상태·body를 캡처하고 X-Trace-ID를 추가해 상위 send로 전달한다.
        async def send_with_capture(message: Message) -> None:
            nonlocal response_status, response_content_type
            if message["type"] == "http.response.start":
                response_status = message["status"]
                headers = list(message.get("headers", []))
                response_content_type = self._header(headers, b"content-type")
                headers.append((b"x-trace-id", trace_id.encode("ascii")))
                message["headers"] = headers
            elif message["type"] == "http.response.body":
                self._append_limited(response_body, message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive_with_capture, send_with_capture)
        finally:
            api = f"{method} {path}"
            request_params = self._request_params(scope, bytes(request_body))
            response_params = self._response_params(
                path, response_status, response_content_type, bytes(response_body)
            )
            logger.info(
                "traceId=%s , api=%s , request_params=%s",
                trace_id,
                api,
                self._json(request_params),
            )
            logger.info(
                "traceId=%s , api=%s , response_params=%s",
                trace_id,
                api,
                self._json(response_params),
            )

    @staticmethod
    # ASGI scope에서 안전한 X-Request-ID를 반환하거나 새 trace ID를 생성해 반환한다.
    def _trace_id(scope: Scope) -> str:
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-request-id":
                candidate = value.decode("utf-8", errors="ignore").strip()
                if (
                    candidate
                    and len(candidate) <= 100
                    and re.fullmatch(r"[A-Za-z0-9._:-]+", candidate)
                ):
                    return candidate
        return f"trace_{secrets.token_hex(12)}"

    @staticmethod
    # 대상 bytearray와 새 bytes를 받아 최대 캡처 크기 안에서만 추가한다.
    def _append_limited(target: bytearray, value: bytes) -> None:
        remaining = MAX_CAPTURE_BYTES - len(target)
        if remaining > 0:
            target.extend(value[:remaining])

    @staticmethod
    # ASGI 헤더 목록과 소문자 이름을 받아 일치하는 첫 헤더 문자열 또는 빈 값을 반환한다.
    def _header(headers: list[tuple[bytes, bytes]], target: bytes) -> str:
        for name, value in headers:
            if name.lower() == target:
                return value.decode("latin-1")
        return ""

    # ASGI scope와 요청 body를 받아 비밀값이 제거된 query·JSON 파라미터를 반환한다.
    def _request_params(self, scope: Scope, body: bytes) -> dict[str, Any]:
        result: dict[str, Any] = {}
        query = dict(parse_qsl(scope.get("query_string", b"").decode("utf-8", errors="ignore")))
        if query:
            result["query"] = self._redact(query)
        parsed = self._parse_json(body)
        if isinstance(parsed, dict):
            result["body"] = self._redact(parsed)
        elif body:
            result["bodyBytes"] = len(body)
        return result

    # 경로·상태·형식·body를 받아 상세 상품이 제외된 안전한 응답 요약을 반환한다.
    def _response_params(self, path: str, status: int, content_type: str, body: bytes) -> dict[str, Any]:
        result: dict[str, Any] = {"httpStatus": status}
        parsed = self._parse_json(body) if "application/json" in content_type else None
        if not isinstance(parsed, dict):
            result["bodyBytes"] = len(body)
            return result

        if path.startswith("/ai/v1/analyses/") and "status" in parsed:
            analysis_result = parsed.get("result") or {}
            result.update(
                {
                    "analysisId": parsed.get("analysisId"),
                    "status": parsed.get("status"),
                    "stage": parsed.get("stage"),
                    "submittedAt": parsed.get("submittedAt"),
                    "startedAt": parsed.get("startedAt"),
                    "completedAt": parsed.get("completedAt"),
                    "documentType": analysis_result.get("documentType"),
                    "itemCount": len(analysis_result.get("items", [])),
                    "error": parsed.get("error"),
                }
            )
            return result

        result["body"] = self._redact(parsed)
        return result

    # 임의 JSON 값과 필드명을 받아 비밀·해시·OCR 근거가 마스킹된 복사본을 반환한다.
    def _redact(self, value: Any, key: str = "") -> Any:
        lowered = key.lower()
        if lowered in {"sha256", "evidencetext"} or any(
            token in lowered for token in ("password", "secret", "token", "apikey", "api_key")
        ):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {item_key: self._redact(item, item_key) for item_key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item, key) for item in value[:50]]
        if isinstance(value, str) and len(value) > 512:
            return value[:512] + "...[TRUNCATED]"
        return value

    @staticmethod
    # body bytes를 JSON으로 변환해 반환하며 비어 있거나 잘못됐으면 None을 반환한다.
    def _parse_json(body: bytes) -> Any:
        if not body:
            return None
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    @staticmethod
    # 로그용 값을 받아 한 줄 UTF-8 JSON 문자열로 직렬화해 반환한다.
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
