import asyncio
import json
import logging
from time import perf_counter

import httpx

from app.config import Settings
from app.monitoring.metrics import CALLBACK_ATTEMPTS, CALLBACK_DURATION
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
)

logger = logging.getLogger(__name__)


# 콜백 payload에서 이미지 위치·인증 정보 없이 분석 결과 추적에 필요한 필드만 요약해 반환한다.
def _callback_log_summary(payload: CallbackPayload) -> str:
    summary: dict[str, object] = {
        "analysisId": payload.analysis_id,
        "status": payload.status,
        "completedAt": payload.completed_at.isoformat(),
    }
    if payload.result is not None:
        summary["documentType"] = payload.result.document_type
        summary["itemCount"] = len(payload.result.items)
        summary["items"] = [
            {
                "itemId": item.item_id,
                "displayStatus": item.display_status,
                "name": item.name,
                "category": item.category,
                "storageType": item.storage_type,
                "measureType": item.measure_type,
                "quantity": item.quantity,
                "weight": item.weight.model_dump(mode="json"),
                "expiration": item.expiration.model_dump(by_alias=True, mode="json"),
                "reviewReasons": [
                    reason.model_dump(by_alias=True, mode="json") for reason in item.review_reasons
                ],
            }
            for item in payload.result.items
        ]
    if payload.error is not None:
        summary["error"] = payload.error.model_dump(by_alias=True, mode="json")
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


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


# 내부 분석 결과에 수량과 중량이 함께 있으면 콜백에서도 둘 다 보존한다. 포장 중량이 있는 상품도
# 여러 개 구매할 수 있으므로 WEIGHT 항목의 quantity를 제거하지 않으며, 규칙으로 보완한 수량도
# 사용자가 검토할 후보값으로 전달한다. 파이프라인에 수량 객체가 없을 때만 0으로 보낸다.
# BUSINESS_RULE 출처라도 영수증의 단가·금액 또는 기본 후보로 복원한 값이므로 콜백에서 다시
# 0으로 덮어쓰지 않는다. 출처와 확인 필요 여부는 reviewReasons가 설명한다.
def _callback_item(item: ReceiptItem) -> CallbackItem:
    quantity = item.quantity.value if item.quantity is not None else 0
    if item.measure_type == "COUNT":
        weight = CallbackWeight(value=0, unit="NONE")
    else:
        # 0 이하는 인식하지 못한 값으로 보고 값 0과 NONE 단위로 정규화한다.
        recognized = item.weight.value is not None and item.weight.value > 0
        weight = CallbackWeight(value=item.weight.value if recognized else 0, unit=item.weight.unit if recognized else "NONE")
    # 측정 방식에 필요한 값(COUNT의 수량, WEIGHT의 중량)이 없으면 등록에 필요한 값이 빠진 것이므로
    # 백엔드 명세에 따라 NEEDS_REVIEW로 보낸다.
    display_status = item.display_status
    measurement_missing = quantity <= 0 if item.measure_type == "COUNT" else weight.value <= 0
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
            CALLBACK_ATTEMPTS.labels(outcome="skipped").inc()
            logger.info("callback.skipped analysis_id=%s reason=no_url", payload.analysis_id)
            return False
        headers: dict[str, str] = {}
        if self._settings.callback_api_key:
            headers["X-Internal-API-Key"] = self._settings.callback_api_key
        client = self._get_client()
        logger.info(
            "callback.payload.ready analysis_id=%s payload_summary=%s",
            payload.analysis_id,
            _callback_log_summary(payload),
        )
        for attempt in range(1, self._settings.callback_max_attempts + 1):
            logger.info(
                "callback.attempt analysis_id=%s attempt=%s",
                payload.analysis_id,
                attempt,
            )
            try:
                started_at = perf_counter()
                response = await client.post(
                    self._settings.callback_url,
                    json=payload.model_dump(by_alias=True, mode="json"),
                    headers=headers,
                )
                if response.status_code == 200:
                    CALLBACK_ATTEMPTS.labels(outcome="success").inc()
                    CALLBACK_DURATION.labels(outcome="success").observe(perf_counter() - started_at)
                    logger.info(
                        "callback.succeeded analysis_id=%s attempt=%s http_status=%s payload_summary=%s",
                        payload.analysis_id,
                        attempt,
                        response.status_code,
                        _callback_log_summary(payload),
                    )
                    return True
                if response.status_code not in {429, 500, 502, 503, 504}:
                    CALLBACK_ATTEMPTS.labels(outcome="rejected").inc()
                    CALLBACK_DURATION.labels(outcome="rejected").observe(perf_counter() - started_at)
                    logger.warning(
                        "callback.rejected analysis_id=%s http_status=%s response_body=%s payload_summary=%s",
                        payload.analysis_id,
                        response.status_code,
                        response.text.replace("\n", " ")[:2000],
                        _callback_log_summary(payload),
                    )
                    return False
                CALLBACK_ATTEMPTS.labels(outcome="retryable_status").inc()
                CALLBACK_DURATION.labels(outcome="retryable_status").observe(perf_counter() - started_at)
            except httpx.HTTPError:
                CALLBACK_ATTEMPTS.labels(outcome="request_failed").inc()
                CALLBACK_DURATION.labels(outcome="request_failed").observe(perf_counter() - started_at)
                logger.warning(
                    "callback.request_failed analysis_id=%s attempt=%s",
                    payload.analysis_id,
                    attempt,
                )
            if attempt < self._settings.callback_max_attempts:
                await asyncio.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
        logger.error(
            "callback.exhausted analysis_id=%s attempts=%s payload_summary=%s",
            payload.analysis_id,
            self._settings.callback_max_attempts,
            _callback_log_summary(payload),
        )
        CALLBACK_ATTEMPTS.labels(outcome="exhausted").inc()
        return False

    # 기존 HTTP 클라이언트를 반환하거나 설정 timeout으로 새 클라이언트를 생성해 반환한다.
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.callback_timeout_seconds)
        return self._client
