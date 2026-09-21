import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.dependencies import authenticate_service, get_runtime
from app.errors import InvalidRequest
from app.runtime import AnalysisRuntime
from app.schemas import (
    AnalysisAccepted,
    AnalysisCreateRequest,
    AnalysisView,
    ApiErrorResponse,
    InputHint,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai/v1/analyses", tags=["analyses"])


@router.post(
    "",
    response_model=AnalysisAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={400: {"model": ApiErrorResponse}, 429: {"model": ApiErrorResponse}},
)
# 영수증 분석 요청을 큐에 넣고 접수 ID와 현재 상태를 반환한다.
async def create_analysis(payload: AnalysisCreateRequest, _: Annotated[None, Depends(authenticate_service)], runtime: Annotated[AnalysisRuntime, Depends(get_runtime)]) -> AnalysisAccepted:
    if payload.input_hint == InputHint.PRODUCT:
        raise InvalidRequest("현재 구현은 영수증 분석만 지원합니다.")
    logger.info(
        "analysis.api.submit request_id=%s input_hint=%s", payload.request_id, payload.input_hint
    )
    return runtime.submit(payload)


@router.get(
    "/{analysis_id}",
    response_model=AnalysisView,
    responses={404: {"model": ApiErrorResponse}},
)
# 분석 ID를 받아 큐 처리 상태, 완료 결과 또는 실패 정보를 반환한다.
async def get_analysis(analysis_id: str, _: Annotated[None, Depends(authenticate_service)], runtime: Annotated[AnalysisRuntime, Depends(get_runtime)]) -> AnalysisView:
    logger.info("analysis.api.get analysis_id=%s", analysis_id)
    return runtime.store.get_view(analysis_id)