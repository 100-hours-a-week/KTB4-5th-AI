import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.dependencies import authenticate_service, get_runtime
from app.errors import DependencyUnavailable
from app.runtime import AnalysisRuntime
from app.schemas import (
    AnalysisAccepted,
    AnalysisCreateRequest,
    AnalysisView,
    ApiErrorResponse,
    FeedbackRecorded,
    FeedbackRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai/v1/analyses", tags=["analyses"])


@router.post(
    "",
    response_model=AnalysisAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        400: {"model": ApiErrorResponse},
        401: {"model": ApiErrorResponse},
        409: {"model": ApiErrorResponse},
        429: {"model": ApiErrorResponse},
        503: {"model": ApiErrorResponse},
    },
)
# 영수증·실물 분석 요청을 큐에 넣고 접수 ID와 현재 상태를 반환한다.
async def create_analysis(payload: AnalysisCreateRequest, _: Annotated[None, Depends(authenticate_service)], runtime: Annotated[AnalysisRuntime, Depends(get_runtime)]) -> AnalysisAccepted:
    logger.info(
        "analysis.api.submit request_id=%s input_hint=%s", payload.request_id, payload.input_hint
    )
    return runtime.submit(payload)


@router.get(
    "/{analysis_id}",
    response_model=AnalysisView,
    responses={
        401: {"model": ApiErrorResponse},
        404: {"model": ApiErrorResponse},
    },
)
# 분석 ID를 받아 큐 처리 상태, 완료 결과 또는 실패 정보를 반환한다.
async def get_analysis(analysis_id: str, _: Annotated[None, Depends(authenticate_service)], runtime: Annotated[AnalysisRuntime, Depends(get_runtime)]) -> AnalysisView:
    logger.info("analysis.api.get analysis_id=%s", analysis_id)
    return await runtime.find_view(analysis_id)


@router.post(
    "/{analysis_id}/feedback",
    response_model=FeedbackRecorded,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": FeedbackRecorded, "description": "같은 분석에 이미 기록된 피드백"},
        400: {"model": ApiErrorResponse},
        401: {"model": ApiErrorResponse},
        404: {"model": ApiErrorResponse},
        409: {"model": ApiErrorResponse},
        503: {"model": ApiErrorResponse},
    },
)
# 사용자가 재고로 확정한 항목별 결과를 기록하고, 동의한 수정만 학습 후보로 반영한다.
# 같은 분석에 다시 보내면 새로 기록하지 않고 기존 기록을 200으로 반환한다.
async def submit_feedback(
    analysis_id: str,
    payload: FeedbackRequest,
    response: Response,
    _: Annotated[None, Depends(authenticate_service)],
    runtime: Annotated[AnalysisRuntime, Depends(get_runtime)],
) -> FeedbackRecorded:
    logger.info("feedback.api.submit analysis_id=%s item_count=%s", analysis_id, len(payload.items))
    if runtime.feedback is None:
        raise DependencyUnavailable("피드백 저장소(DATABASE_URL)가 설정되지 않아 피드백을 기록할 수 없습니다.")
    recorded, created = await runtime.feedback.submit(analysis_id, payload)
    if not created:
        response.status_code = status.HTTP_200_OK
    return recorded
