from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi

from app.adapters.callback import CallbackDelivery
from app.adapters.callback_outbox import PostgresCallbackOutbox
from app.adapters.product_catalog import PostgresProductCatalogLookup
from app.adapters.remote_inference import RemoteInferenceClient
from app.adapters.shelf_life import PostgresShelfLifeLookup
from app.config import Settings, get_settings
from app.error_handlers import handle_app_error, handle_validation_error
from app.errors import AppError
from app.logging import ApiAuditLogMiddleware, configure_logging, shutdown_logging
from app.pipeline import ReceiptAnalysisPipeline
from app.routers import analyses, health, status
from app.runtime import AnalysisRuntime
from app.store import InMemoryAnalysisStore


# 설정을 입력받아 저장소·원격 GPU Worker·DB 조회·콜백·콜백 outbox를 조립한 분석 런타임을 반환한다.
def build_runtime(settings: Settings) -> AnalysisRuntime:
    shelf_life_lookup = PostgresShelfLifeLookup(settings) if settings.database_url else None
    product_catalog_lookup = PostgresProductCatalogLookup(settings) if settings.database_url else None
    pipeline = ReceiptAnalysisPipeline(
        settings,
        RemoteInferenceClient(settings),
        shelf_life_lookup,
        product_catalog_lookup,
    )

    store = InMemoryAnalysisStore(
        ttl_seconds=settings.analysis_result_ttl_seconds,
        max_entries=settings.analysis_result_max_entries,
    )

    callback_outbox = PostgresCallbackOutbox(settings) if settings.callback_outbox_enabled else None
    return AnalysisRuntime(settings, store, pipeline, CallbackDelivery(settings), callback_outbox)


# 설정과 선택 런타임으로 미들웨어·예외 처리·router가 조립된 앱을 반환한다.
def create_app(settings: Settings | None = None, runtime: AnalysisRuntime | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(app_settings)

    app_runtime = runtime or build_runtime(app_settings)

    @asynccontextmanager
    # 앱 시작 시 런타임을 시작하고 종료 시 백그라운드 작업과 자원을 정리한다.
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await app_runtime.start()
        try:
            yield
        finally:
            await app_runtime.stop()
            shutdown_logging()

    application = FastAPI(
        title="KTB Image AI",
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/docs" if app_settings.app_env != "production" else None,
        redoc_url=None,
    )

    application.state.settings = app_settings
    application.state.runtime = app_runtime

    application.add_middleware(ApiAuditLogMiddleware)

    application.add_exception_handler(AppError, handle_app_error)
    application.add_exception_handler(RequestValidationError, handle_validation_error)

    application.include_router(health.router)
    application.include_router(analyses.router)
    application.include_router(status.router)

    # 요청 검증 오류는 공통 핸들러가 400 INVALID_REQUEST로 변환하므로 Swagger의 기본 422를 제거한다.
    def build_openapi_schema() -> dict:
        if application.openapi_schema is not None:
            return application.openapi_schema
        schema = get_openapi(
            title=application.title,
            version=application.version,
            routes=application.routes,
        )
        for path_item in schema.get("paths", {}).values():
            for operation in path_item.values():
                if isinstance(operation, dict):
                    operation.get("responses", {}).pop("422", None)
        application.openapi_schema = schema
        return schema

    application.openapi = build_openapi_schema

    return application


app = create_app()
