from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.dependencies import authenticate_service, get_runtime
from app.monitoring.metrics import render_metrics
from app.runtime import AnalysisRuntime

router = APIRouter(tags=["monitoring"])


# 내부 인증을 통과한 수집기에 Prometheus 형식의 서비스 지표를 반환한다.
@router.get("/metrics", include_in_schema=False, dependencies=[Depends(authenticate_service)])
async def metrics(runtime: Annotated[AnalysisRuntime, Depends(get_runtime)]) -> Response:
    runtime.refresh_metrics()
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
