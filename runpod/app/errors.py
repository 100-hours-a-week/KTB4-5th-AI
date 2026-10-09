from dataclasses import dataclass


@dataclass(slots=True)
class AppError(Exception):
    code: str
    message: str
    status_code: int
    retryable: bool = False
    retry_after_ms: int | None = None


class InvalidRequest(AppError):
    # 사용자에게 공개할 요청 오류 메시지를 받아 HTTP 400 예외를 구성한다.
    def __init__(self, message: str) -> None:
        super().__init__("INVALID_REQUEST", message, 400)


class UnauthorizedService(AppError):
    # 내부 서비스 인증 실패를 나타내는 HTTP 401 예외를 구성한다.
    def __init__(self) -> None:
        super().__init__("UNAUTHORIZED_SERVICE", "내부 서비스 인증에 실패했습니다.", 401)


class AnalysisNotFound(AppError):
    # 분석 ID가 저장소에 없음을 나타내는 HTTP 404 예외를 구성한다.
    def __init__(self) -> None:
        super().__init__("ANALYSIS_NOT_FOUND", "분석 작업을 찾을 수 없습니다.", 404)


class IdempotencyConflict(AppError):
    # 동일 requestId의 내용 충돌을 나타내는 HTTP 409 예외를 구성한다.
    def __init__(self) -> None:
        super().__init__(
            "IDEMPOTENCY_CONFLICT",
            "같은 requestId에 다른 요청 내용이 사용되었습니다.",
            409,
        )


class ImageTooLarge(AppError):
    # 이미지 bytes 또는 픽셀 제한 초과를 나타내는 HTTP 413 예외를 구성한다.
    def __init__(self) -> None:
        super().__init__("IMAGE_TOO_LARGE", "이미지 크기 또는 해상도가 제한을 넘었습니다.", 413)


class UnsupportedImageType(AppError):
    # 지원하지 않는 이미지 형식을 나타내는 HTTP 415 예외를 구성한다.
    def __init__(self) -> None:
        super().__init__("UNSUPPORTED_IMAGE_TYPE", "JPEG, PNG, WebP만 지원합니다.", 415)


class AnalysisQueueFull(AppError):
    # 인메모리 분석 큐 포화를 나타내는 재시도 가능 HTTP 429 예외를 구성한다.
    def __init__(self) -> None:
        super().__init__(
            "AI_QUEUE_FULL",
            "분석 대기열이 가득 찼습니다. 잠시 후 다시 시도해 주세요.",
            429,
            retryable=True,
            retry_after_ms=3000,
        )


class DependencyUnavailable(AppError):
    # 외부 저장소 등의 오류 메시지를 받아 재시도 가능 HTTP 503 예외를 구성한다.
    def __init__(self, message: str) -> None:
        super().__init__(
            "DEPENDENCY_UNAVAILABLE", message, 503, retryable=True, retry_after_ms=5000
        )


class ModelUnavailable(AppError):
    # OCR 또는 LLM 오류 메시지를 받아 재시도 가능 HTTP 503 예외를 구성한다.
    def __init__(self, message: str) -> None:
        super().__init__("MODEL_UNAVAILABLE", message, 503, retryable=True, retry_after_ms=5000)
