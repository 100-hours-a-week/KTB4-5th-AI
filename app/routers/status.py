from typing import Annotated

from fastapi import APIRouter, Depends

from app.config import Settings
from app.dependencies import authenticate_service, get_app_settings, get_runtime
from app.runtime import AnalysisRuntime
from app.schemas import ModelStatus

router = APIRouter(prefix="/ai/v1", tags=["operations"])


@router.get("/models/status", response_model=ModelStatus)
# 모델 준비 여부와 현재 큐 용량·대기 수·처리 중 분석 ID를 반환한다.
async def get_model_status(_: Annotated[None, Depends(authenticate_service)], runtime: Annotated[AnalysisRuntime, Depends(get_runtime)], settings: Annotated[Settings, Depends(get_app_settings)]) -> ModelStatus:
    models = await runtime.models_ready()
    return ModelStatus(
        ready=runtime.accepting and all(models.values()),
        queueSize=runtime.queue.qsize(),
        queueCapacity=settings.analysis_queue_maxsize,
        activeAnalysisId=runtime.active_analysis_id,
        activeCount=len(runtime.active_analysis_ids),
        models=models,
    )
