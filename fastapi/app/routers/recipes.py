from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.dependencies import authenticate_service
from app.recipes.schemas import ChangesResponse, RecipeJobAccepted, RecipeJobView, RecommendationRequest

router = APIRouter(prefix="/ai/v1", tags=["recipes"], dependencies=[Depends(authenticate_service)])


# 확정 재고/취향 요청을 전용 큐에 접수하고 즉시 작업 ID를 반환한다.
@router.post("/recipe-recommendations", response_model=RecipeJobAccepted, status_code=202)
async def recommend(body: RecommendationRequest, request: Request) -> RecipeJobAccepted:
    return await request.app.state.recipe_jobs.submit(body)


# 작업 ID로 진행 상태 또는 최종 추천과 재고 ID별 사용량을 조회한다.
@router.get("/recipe-recommendations/{job_id}", response_model=RecipeJobView)
async def recommendation_result(job_id: str, request: Request) -> RecipeJobView:
    return await request.app.state.recipe_jobs.get(job_id)


# 백엔드가 마지막 성공 cursor 이후의 레시피 변경을 읽어 자체 MySQL에 반영한다.
@router.get("/recipe-sync/changes", response_model=ChangesResponse)
async def changes(request: Request, cursor: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> ChangesResponse:
    return await request.app.state.recipes.repository.changes(cursor, limit)
