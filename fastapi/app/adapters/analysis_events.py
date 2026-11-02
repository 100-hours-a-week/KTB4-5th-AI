"""BE Kafka 계약과 기존 HTTP DTO/콜백 형식 사이의 변환만 담당한다."""

from datetime import UTC
from typing import Annotated, Generic, Literal, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from app.adapters.callback import build_callback_payload
from app.schemas import AnalysisCreateRequest, AnalysisView, FeedbackRequest


class EventCorrelation(BaseModel):
    event_type: str = Field(alias="eventType")
    occurred_at: AwareDatetime = Field(alias="occurredAt")
    request_id: UUID = Field(alias="requestId")
    user_id: Annotated[StrictInt, Field(gt=0)] = Field(alias="userId")


class RequestedPayload(EventCorrelation):
    event_type: Literal["AnalysisRequested"] = Field(alias="eventType")
    # 전환 전에 발행된 요청은 냉장고 ID 없이 카탈로그만 조회한다.
    refrigerator_id: Annotated[StrictInt, Field(gt=0)] | None = Field(default=None, alias="refrigeratorId")
    image: dict
    input_hint: str = Field(alias="inputHint")
    locale: str
    timezone: str

    def analysis_request(self) -> AnalysisCreateRequest:
        return AnalysisCreateRequest.model_validate({
            "requestId": str(self.request_id), "image": self.image,
            "refrigeratorId": self.refrigerator_id,
            "inputHint": self.input_hint, "locale": self.locale, "timezone": self.timezone,
        })


class FeedbackPayload(EventCorrelation, FeedbackRequest):
    event_type: Literal["AnalysisFeedbackSubmitted"] = Field(alias="eventType")
    analysis_id: str = Field(alias="analysisId", min_length=1, max_length=64, pattern=r"\S")

    def feedback_request(self) -> FeedbackRequest:
        return FeedbackRequest(
            refrigeratorId=self.refrigerator_id,
            consentForModelImprovement=self.model_improvement_consent,
            items=self.items,
        )


EventPayload = TypeVar("EventPayload", bound=EventCorrelation)


class EventEnvelope(BaseModel, Generic[EventPayload]):
    model_config = ConfigDict(extra="forbid")

    message_id: UUID = Field(alias="messageId")
    payload: EventPayload

    def validate_key(self, key: bytes | None) -> None:
        if key != str(self.payload.user_id).encode("utf-8"):
            raise ValueError("Kafka key와 payload.userId가 일치해야 합니다")


def completed_event(event: EventEnvelope[RequestedPayload], view: AnalysisView) -> dict:
    callback = build_callback_payload(view)
    payload = callback.model_dump(by_alias=True, mode="json")
    if not view.analysis_id.strip() or len(view.analysis_id) > 64:
        raise ValueError("analysisId는 비어 있지 않은 64자 이하여야 합니다")
    if callback.result is not None:
        if callback.result.document_type == "OTHER" and callback.result.items:
            raise ValueError("OTHER 결과의 items는 비어 있어야 합니다")
        if any(not item.item_id.strip() or len(item.item_id) > 64 for item in callback.result.items):
            raise ValueError("itemId는 비어 있지 않은 64자 이하여야 합니다")
    completed_at = callback.completed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    payload.update(
        eventType="AnalysisCompleted", occurredAt=completed_at, completedAt=completed_at,
        requestId=str(event.payload.request_id), userId=event.payload.user_id,
    )
    # 같은 입력과 저장된 결과의 재전송에서는 메시지 ID와 본문을 유지한다.
    message_id = uuid5(NAMESPACE_URL, f"{event.message_id}:{view.analysis_id}")
    return {"messageId": str(message_id), "payload": payload}
