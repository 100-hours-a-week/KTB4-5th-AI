from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.dependencies import authenticate_service, get_runtime
from app.runtime import AnalysisRuntime

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", include_in_schema=False)
# HTTP 프로세스가 요청을 받을 수 있음을 나타내는 생존 응답을 반환한다.
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", include_in_schema=False)
# 런타임과 OCR·LLM 상태를 검사해 준비 여부와 200 또는 503을 반환한다.
async def readiness(_: Annotated[None, Depends(authenticate_service)], runtime: Annotated[AnalysisRuntime, Depends(get_runtime)]) -> JSONResponse:
    models = await runtime.models_ready()
    ready = runtime.accepting and all(models.values())
    return JSONResponse(
        status_code=200 if ready else 503, content={"ready": ready, "models": models}
    )
