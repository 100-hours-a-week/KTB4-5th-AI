"""이미지 분석과 동일 Worker의 Gemma를 이용하는 재고 계획·후보 선택 어댑터."""

from app.adapters.remote_inference import RemoteInferenceClient
from app.config import Settings
from app.recipes.ai_contract import PROMPT_VERSION, PlanResult, RecipeAiResponse, SelectionResult


class RecipeReasoner:
    # 기존 Worker 인증·자동 주소 탐색을 재사용한다. 별도 모델·포트·DB는 만들지 않는다.
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = RemoteInferenceClient(settings)

    # 원문 재고만으로 우선 사용할 ingredientId와 요리 검색 방향을 구조화한다.
    async def plan(self, context: dict) -> tuple[PlanResult, str]:
        result = await self._call("PLAN", context)
        if not isinstance(result.result, PlanResult):
            raise ValueError("RECIPE_PLAN_SCHEMA_MISMATCH")
        return result.result, result.model

    # 서버가 제공한 후보 ID·재료 번호 안에서만 추천을 선택한다.
    async def select(self, context: dict) -> tuple[SelectionResult, str]:
        result = await self._call("SELECT", context)
        if not isinstance(result.result, SelectionResult):
            raise ValueError("RECIPE_SELECTION_SCHEMA_MISMATCH")
        return result.result, result.model

    # 모델 프롬프트 계약이 다른 구버전 Worker 응답은 정상 AI 결과로 수용하지 않는다.
    async def _call(self, stage: str, context: dict) -> RecipeAiResponse:
        payload = await self.client.reason_recipe({"stage": stage, "context": context}, self.settings.recipe_ai_timeout_seconds)
        result = RecipeAiResponse.model_validate(payload)
        if result.stage != stage or result.promptVersion != PROMPT_VERSION:
            raise ValueError("RECIPE_AI_PROTOCOL_MISMATCH")
        return result

    # 레시피 전용 HTTP 클라이언트를 정리한다.
    async def close(self) -> None:
        await self.client.close()
