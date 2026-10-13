import asyncio
import logging

import httpx

from app.config import Settings
from app.schemas import (
    AnalysisResult,
    AnalysisView,
    CallbackExpiration,
    CallbackItem,
    CallbackPayload,
    CallbackResult,
    CallbackWeight,
    DisplayStatus,
    ErrorBody,
    PublicStatus,
    ReceiptItem,
    Source,
)

logger = logging.getLogger(__name__)


# 완료·실패된 분석 view를 백엔드 콜백 명세 형식의 payload로 변환해 반환한다.
def build_callback_payload(view: AnalysisView) -> CallbackPayload:
    if view.result is None:
        error = view.error or ErrorBody(
            code="INTERNAL_ERROR",
            message="분석 결과가 생성되지 않았습니다.",
            retryable=True,
            retryAfterMs=5000,
        )
        return CallbackPayload(
            analysisId=view.analysis_id,
            status=PublicStatus.FAILED,
            completedAt=view.completed_at,
            result=None,
            error=error,
        )

    return CallbackPayload(
        analysisId=view.analysis_id,
        status=PublicStatus.COMPLETED,
        completedAt=view.completed_at,
        result=_callback_result(view.result),
        error=None,
    )


def _callback_result(result: AnalysisResult) -> CallbackResult:
    return CallbackResult(
        documentType=result.document_type.value,
        items=[_callback_item(item) for item in result.items],
    )


# 백엔드 명세는 인식하지 못한 값을 기본값으로 채우지 않고 null로 받는다. 영수증·포장에서 읽지 못해
# 규칙상 기본값(수량 1)으로 제안한 수량은 null로 보낸다. WEIGHT 항목은 명세상 quantity를 쓰지 않으므로
# 항상 null이다.
def _callback_item(item: ReceiptItem) -> CallbackItem:
    quantity = None
    if item.measure_type == "COUNT" and item.quantity is not None and item.quantity.source != Source.BUSINESS_RULE:
        quantity = item.quantity.value
    if item.measure_type == "COUNT":
        weight = CallbackWeight(value=None, unit="NONE")
    else:
        # 백엔드 명세상 중량은 양수만 유효하다. 0 이하는 인식하지 못한 값으로 보고 값·단위를 null로 보낸다.
        recognized = item.weight.value is not None and item.weight.value > 0
        weight = CallbackWeight(value=item.weight.value if recognized else None, unit=item.weight.unit if recognized else None)
    # 측정 방식에 필요한 값(COUNT의 수량, WEIGHT의 중량)이 없으면 등록에 필요한 값이 빠진 것이므로
    # 백엔드 명세에 따라 NEEDS_REVIEW로 보낸다.
    display_status = item.display_status
    measurement_missing = quantity is None if item.measure_type == "COUNT" else weight.value is None
    if measurement_missing and display_status in {DisplayStatus.RECOGNIZED, DisplayStatus.AI_ESTIMATED}:
        display_status = DisplayStatus.NEEDS_REVIEW
    return CallbackItem(
        itemId=item.item_id,
        displayStatus=display_status,
        name=item.name.value or item.name.normalized_value or None,
        category=item.category.value,
        storageType=item.storage_type,
        measureType=item.measure_type,
        quantity=quantity,
        weight=weight,
        expiration=CallbackExpiration(
            date=item.expiration.date,
            dateType=item.expiration.date_type,
            estimatedRange=item.expiration.estimated_range,
        ),
        reviewReasons=item.review_reasons,
    )


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
                if response.status_code == 200:
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
