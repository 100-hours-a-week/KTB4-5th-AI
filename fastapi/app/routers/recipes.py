from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.dependencies import authenticate_service
from app.recipes.schemas import ChangesResponse, RecommendationRequest, RecommendationResponse

router = APIRouter(prefix="/ai/v1", tags=["recipes"], dependencies=[Depends(authenticate_service)])


# 백엔드 확정 재고를 받아 DB·외부 공급자 후보를 순서대로 조회하고 추천 스냅샷을 반환한다.
@router.post("/recipe-recommendations", response_model=RecommendationResponse)
async def recommend(body: RecommendationRequest, request: Request) -> RecommendationResponse:
    return await request.app.state.recipes.recommend(body)


# 백엔드가 마지막 성공 cursor 이후의 레시피 변경을 읽어 자체 MySQL에 반영한다.
@router.get("/recipe-sync/changes", response_model=ChangesResponse)
async def changes(request: Request, cursor: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> ChangesResponse:
    return await request.app.state.recipes.repository.changes(cursor, limit)
