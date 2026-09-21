from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.adapters.callback import CallbackDelivery
from app.adapters.image_loader import ConfiguredImageLoader
from app.adapters.ocr import PaddleTextExtractor, StubTextExtractor
from app.adapters.ollama import OllamaFoodClassifier
from app.adapters.product_catalog import PostgresProductCatalogLookup
from app.adapters.shelf_life import PostgresShelfLifeLookup
from app.config import Settings, get_settings
from app.error_handlers import handle_app_error, handle_validation_error
from app.errors import AppError
from app.logging import ApiAuditLogMiddleware, configure_logging, shutdown_logging
from app.pipeline import ReceiptAnalysisPipeline
from app.routers import analyses, health, status
from app.runtime import AnalysisRuntime
from app.store import InMemoryAnalysisStore


# 설정을 입력받아 저장소·OCR·LLM·콜백을 조립한 분석 런타임을 반환한다.
def build_runtime(settings: Settings) -> AnalysisRuntime:
    image_loader = ConfiguredImageLoader(settings)

    text_extractor = (
        PaddleTextExtractor(settings) if settings.ocr_backend == "paddle" else StubTextExtractor()
    )

    classifier = OllamaFoodClassifier(settings)
    shelf_life_lookup = PostgresShelfLifeLookup(settings) if settings.database_url else None
    product_catalog_lookup = PostgresProductCatalogLookup(settings) if settings.database_url else None
    pipeline = ReceiptAnalysisPipeline(
        settings, image_loader, text_extractor, classifier, shelf_life_lookup, product_catalog_lookup
    )

    store = InMemoryAnalysisStore(
        ttl_seconds=settings.analysis_result_ttl_seconds,
        max_entries=settings.analysis_result_max_entries,
    )

    return AnalysisRuntime(settings, store, pipeline, CallbackDelivery(settings))


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
        title="KTB Receipt AI",
        version="0.1.0",
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

    return application


app = create_app()
