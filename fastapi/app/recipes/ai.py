"""이미지 분석과 동일 Worker의 Gemma를 이용하는 단일 후보 순위 조정 어댑터."""

from app.adapters.remote_inference import RemoteInferenceClient
from app.config import Settings
from app.recipes.ai_contract import PROMPT_VERSION, ExtractedRecipe, RankResult, RecipeAiResponse


class RecipeReasoner:
    # 기존 Worker 인증·자동 주소 탐색을 재사용한다. 별도 모델·포트·DB는 만들지 않는다.
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = RemoteInferenceClient(settings)

    # 검증된 후보 전체의 순서만 받는다. 후보 제거·재료 변경·분량 추론은 허용하지 않는다.
    async def rank(self, context: dict) -> tuple[RankResult, str]:
        result = await self._call("RANK", context)
        if not isinstance(result.result, RankResult):
            raise ValueError("RECIPE_RANK_SCHEMA_MISMATCH")
        return result.result, result.model

    # 웹 본문을 원문 인용으로만 구조화한다. 외부 검색이나 DB 쓰기는 Worker에 위임하지 않는다.
    async def extract(self, context: dict) -> ExtractedRecipe:
        result = await self._call("EXTRACT", context)
        if not isinstance(result.result, ExtractedRecipe):
            raise ValueError("RECIPE_EXTRACT_SCHEMA_MISMATCH")
        return result.result

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
