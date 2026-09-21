import hmac

from fastapi import Header, Request

from app.config import Settings
from app.errors import UnauthorizedService
from app.runtime import AnalysisRuntime


# FastAPI 앱 상태에서 현재 분석 런타임을 꺼내 반환한다.
def get_runtime(request: Request) -> AnalysisRuntime:
    return request.app.state.runtime


# FastAPI 앱 상태에서 검증이 끝난 실행 설정을 꺼내 반환한다.
def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


# 요청 헤더의 내부 API 키를 설정값과 상수 시간 비교하고 성공 시 반환한다.
async def authenticate_service(request: Request, x_internal_api_key: str | None = Header(default=None, alias="X-Internal-API-Key")) -> None:
    expected: str | None = request.app.state.settings.internal_api_key
    if expected and (
        not x_internal_api_key or not hmac.compare_digest(x_internal_api_key, expected)
    ):
        raise UnauthorizedService()
