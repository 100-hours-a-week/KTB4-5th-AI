from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response

from app.dependencies import authenticate_service, get_runtime
from app.monitoring.metrics import render_metrics
from app.runtime import AnalysisRuntime

router = APIRouter(tags=["monitoring"])


_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}


# 같은 Task·호스트의 CloudWatch Agent는 커스텀 헤더를 보낼 수 없으므로 루프백 요청만 키 검사를 생략한다.
async def authenticate_metrics_scraper(
    request: Request,
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-API-Key"),
) -> None:
    if request.client is not None and request.client.host in _LOOPBACK_HOSTS:
        return
    await authenticate_service(request, x_internal_api_key)


# 내부 인증을 통과한 수집기에 Prometheus 형식의 서비스 지표를 반환한다.
@router.get("/metrics", include_in_schema=False, dependencies=[Depends(authenticate_metrics_scraper)])
async def metrics(runtime: Annotated[AnalysisRuntime, Depends(get_runtime)]) -> Response:
    runtime.refresh_metrics()
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
