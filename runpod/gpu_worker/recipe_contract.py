"""FastAPI와 GPU Worker 사이의 제한된 레시피 계획·선택 계약. 두 배포 경계에서 동일하게 유지한다."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PROMPT_VERSION = "recipe-grounded-v3"
Strategy = Literal["USE_EXPIRING", "MINIMIZE_SHOPPING", "SIMPLE_COOKING"]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PlanResult(WireModel):
    focusIngredientIds: list[int] = Field(min_length=1, max_length=12)
    ideas: list[str] = Field(min_length=1, max_length=3)


class Selection(WireModel):
    candidateId: int = Field(ge=1)
    strategy: Strategy
    essentialIngredientNos: list[int] = Field(min_length=1, max_length=30)
    reason: str = Field(min_length=1, max_length=500)


class SelectionResult(WireModel):
    selections: list[Selection] = Field(max_length=20)


class RankResult(WireModel):
    candidateIds: list[int] = Field(min_length=1, max_length=20)


class RecipeAiRequest(WireModel):
    stage: Literal["PLAN", "SELECT", "RANK"]
    # 값 내부도 FastAPI에서 만든 재고·원천 스냅샷이다. Worker는 도구 실행·DB 접근을 하지 않는다.
    context: dict


class RecipeAiResponse(WireModel):
    stage: Literal["PLAN", "SELECT", "RANK"]
    result: PlanResult | SelectionResult | RankResult
    model: str
    promptVersion: str = PROMPT_VERSION
