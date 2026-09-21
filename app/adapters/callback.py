import asyncio
import logging

import httpx

from app.config import Settings
from app.schemas import CallbackPayload

logger = logging.getLogger(__name__)


class CallbackDelivery:
    # 콜백 설정과 선택 HTTP 클라이언트를 받아 제한 재시도 전달기를 구성한다.
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    # 내부에서 생성한 HTTP 클라이언트가 있으면 닫고 반환한다.
    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    # 완료 payload를 백엔드에 제한 재시도하고 최종 전달 성공 여부를 반환한다.
    async def try_send(self, payload: CallbackPayload) -> bool:
        if not self._settings.callback_url:
            logger.info("callback.skipped analysis_id=%s reason=no_url", payload.analysis_id)
            return False
        headers: dict[str, str] = {}
        if self._settings.callback_api_key:
            headers["X-Internal-API-Key"] = self._settings.callback_api_key
        client = self._get_client()
        for attempt in range(1, self._settings.callback_max_attempts + 1):
            logger.info(
                "callback.attempt analysis_id=%s attempt=%s",
                payload.analysis_id,
                attempt,
            )
            try:
                response = await client.post(
                    self._settings.callback_url,
                    json=payload.model_dump(by_alias=True, mode="json"),
                    headers=headers,
                )
                if 200 <= response.status_code < 300:
                    logger.info(
                        "callback.succeeded analysis_id=%s attempt=%s status=%s",
                        payload.analysis_id,
                        attempt,
                        response.status_code,
                    )
                    return True
                if response.status_code not in {429, 500, 502, 503, 504}:
                    logger.warning(
                        "callback.rejected analysis_id=%s status=%s",
                        payload.analysis_id,
                        response.status_code,
                    )
                    return False
            except httpx.HTTPError:
                logger.warning(
                    "callback.request_failed analysis_id=%s attempt=%s",
                    payload.analysis_id,
                    attempt,
                )
            if attempt < self._settings.callback_max_attempts:
                await asyncio.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
        return False

    # 기존 HTTP 클라이언트를 반환하거나 설정 timeout으로 새 클라이언트를 생성해 반환한다.
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.callback_timeout_seconds)
        return self._client
