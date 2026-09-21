import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import AppError, InvalidRequest
from app.schemas import ApiErrorResponse

logger = logging.getLogger(__name__)


# 도메인 예외를 공개 오류 스키마와 적절한 HTTP 상태의 JSON 응답으로 변환한다.
async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
    content = ApiErrorResponse(
        error={
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
            "retryAfterMs": exc.retry_after_ms,
        }
    ).model_dump(by_alias=True, mode="json", exclude_none=True)
    headers: dict[str, str] = {}
    if exc.retry_after_ms is not None:
        headers["Retry-After"] = str(max(1, exc.retry_after_ms // 1000))
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers)


# 요청 검증 오류의 필드 위치만 기록하고 표준 INVALID_REQUEST 응답을 반환한다.
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    logger.info("request.validation.failed fields=%s", [item.get("loc") for item in exc.errors()])
    return await handle_app_error(request, InvalidRequest("요청 형식이 올바르지 않습니다."))
