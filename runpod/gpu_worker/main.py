import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Protocol

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError

from app.config import Settings, get_settings
from app.error_handlers import handle_app_error, handle_validation_error
from app.errors import AppError, UnauthorizedService
from app.monitoring.metrics import WORKER_INFLIGHT, WORKER_REQUESTS, observe_diagnostics, render_metrics
from app.schemas import RemoteInferenceRequest, RemoteInferenceResponse
from gpu_worker.engine import GpuInferenceEngine
from gpu_worker.recipe_contract import RecipeAiRequest, RecipeAiResponse

logger = logging.getLogger(__name__)


class WorkerEngine(Protocol):
    # 원천 후보에 근거한 레시피 계획·선택 결과를 반환한다.
    async def reason_recipe(self, request: RecipeAiRequest) -> RecipeAiResponse:
        ...

    # 원격 추론 요청을 처리해 구조화 결과를 반환한다.
    async def infer(self, request: RemoteInferenceRequest) -> RemoteInferenceResponse:
        ...

    # 모델별 준비 상태를 반환한다.
    async def readiness(self) -> dict[str, bool]:
        ...

    # Worker 자원을 정리한다.
    async def close(self) -> None:
        ...


# 앱 상태에서 Worker 엔진을 반환한다.
def get_engine(request: Request) -> WorkerEngine:
    return request.app.state.engine


# X-Worker-API-Key가 설정값과 일치하는지 검증한다.
def authenticate_worker(request: Request, x_worker_api_key: Annotated[str | None, Header()] = None) -> None:
    expected = request.app.state.settings.worker_api_key
    if expected and (x_worker_api_key is None or not secrets.compare_digest(x_worker_api_key, expected)):
        raise UnauthorizedService()


# 설정과 선택 엔진으로 RunPod·Colab 공용 Worker FastAPI 앱을 생성한다.
def create_worker_app(settings: Settings | None = None, engine: WorkerEngine | None = None) -> FastAPI:
    worker_settings = settings or get_settings()
    if worker_settings.app_env == "production" and not worker_settings.worker_api_key:
        raise ValueError("production GPU Worker에는 WORKER_API_KEY가 필요합니다")
    if worker_settings.app_env == "production" and not worker_settings.worker_allowed_image_hosts.strip():
        raise ValueError("production GPU Worker에는 WORKER_ALLOWED_IMAGE_HOSTS가 필요합니다")
    worker_engine = engine or GpuInferenceEngine(worker_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await worker_engine.close()

    application = FastAPI(title="KTB GPU Inference Worker", version="0.1.0", lifespan=lifespan)
    application.state.settings = worker_settings
    application.state.engine = worker_engine
    application.add_exception_handler(AppError, handle_app_error)
    application.add_exception_handler(RequestValidationError, handle_validation_error)

    @application.get("/health/live")
    async def live() -> dict[str, bool]:
        return {"live": True}

    @application.get("/health/ready")
    async def ready(current_engine: Annotated[WorkerEngine, Depends(get_engine)]) -> dict:
        models = await current_engine.readiness()
        return {"ready": bool(models) and all(models.values()), "models": models}

    @application.post("/internal/v1/infer", response_model=RemoteInferenceResponse, dependencies=[Depends(authenticate_worker)])
    async def infer(payload: RemoteInferenceRequest, current_engine: Annotated[WorkerEngine, Depends(get_engine)]) -> RemoteInferenceResponse:
        WORKER_INFLIGHT.inc()
        try:
            result = await current_engine.infer(payload)
            try:
                observe_diagnostics(result.diagnostics)
            except Exception:
                logger.exception("worker.metrics.observe_failed")
            WORKER_REQUESTS.labels(status="success").inc()
            return result
        except Exception:
            WORKER_REQUESTS.labels(status="failed").inc()
            raise
        finally:
            WORKER_INFLIGHT.dec()

    @application.post("/internal/v1/recipes/reason", response_model=RecipeAiResponse, dependencies=[Depends(authenticate_worker)])
    async def reason_recipe(payload: RecipeAiRequest, current_engine: Annotated[WorkerEngine, Depends(get_engine)]) -> RecipeAiResponse:
        return await current_engine.reason_recipe(payload)

    @application.get("/metrics", include_in_schema=False, dependencies=[Depends(authenticate_worker)])
    async def metrics() -> Response:
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)

    return application


app = create_worker_app()
