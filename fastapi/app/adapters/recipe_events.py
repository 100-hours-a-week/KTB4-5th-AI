"""추천 Kafka 계약과 기존 추천 요청·완료 결과 변환."""

from typing import Annotated, Generic, Literal, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.recipes.schemas import Contract, RecipeJobView, RecommendationRequest, Text

PositiveId = Annotated[StrictInt, Field(gt=0)]


class RecipeCorrelation(BaseModel):
    occurredAt: AwareDatetime
    requestId: Text
    userId: PositiveId
    refrigeratorId: PositiveId


class RecommendationRequested(RecipeCorrelation, RecommendationRequest):
    def to_request(self) -> RecommendationRequest:
        return RecommendationRequest.model_validate(self.model_dump(exclude={"occurredAt"}))


class RecommendationError(Contract):
    code: str = Field(min_length=1)
    message: str
    retryable: bool = Field(strict=True)


class RecommendationCompleted(RecipeCorrelation, RecipeJobView):
    jobId: str = Field(min_length=1)
    status: Literal["COMPLETED", "FAILED"]
    submittedAt: AwareDatetime
    startedAt: AwareDatetime | None = None
    completedAt: AwareDatetime
    error: RecommendationError | None = None

    @model_validator(mode="after")
    def validate_terminal_result(self) -> "RecommendationCompleted":
        if (self.status == "COMPLETED" and (self.result is None or self.error is not None)
                or self.status == "FAILED" and (self.result is not None or self.error is None)):
            raise ValueError("완료 상태와 result/error가 일치하지 않습니다")
        if self.result is not None and (self.result.userId != self.userId or self.result.refrigeratorId != self.refrigeratorId):
            raise ValueError("추천 결과의 사용자·냉장고가 완료 이벤트와 다릅니다")
        return self


Payload = TypeVar("Payload", bound=RecipeCorrelation)


class RecipeEvent(BaseModel, Generic[Payload]):
    model_config = ConfigDict(extra="forbid")
    messageId: UUID
    payload: Payload

    def validate_key(self, key: bytes | None) -> None:
        if key != str(self.payload.userId).encode("utf-8"):
            raise ValueError("Kafka key는 payload.userId와 같아야 합니다")


def completed_event(request: RecipeEvent[RecommendationRequested], view: RecipeJobView) -> RecipeEvent[RecommendationCompleted]:
    if (view.requestId, view.userId, view.refrigeratorId) != (request.payload.requestId, request.payload.userId, request.payload.refrigeratorId):
        raise ValueError("완료 작업이 접수한 요청·사용자·냉장고와 다릅니다")
    payload = RecommendationCompleted.model_validate({**view.model_dump(mode="json"), "occurredAt": view.completedAt})
    if payload.result is not None and (payload.result.inventoryFingerprint != request.payload.inventoryFingerprint
                                      or payload.result.recommendationMode != request.payload.recommendationMode):
        raise ValueError("추천 결과의 재고 지문·모드가 요청과 다릅니다")
    return RecipeEvent[RecommendationCompleted](messageId=uuid5(NAMESPACE_URL, f"{request.messageId}:{view.jobId}"), payload=payload)
