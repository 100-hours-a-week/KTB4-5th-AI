"""영수증 콜백과 다른 고정 URL로 레시피 작업 최종 결과를 전달한다."""
import asyncio
import logging

import httpx

from app.config import Settings
from app.recipes.job_metrics import CALLBACKS
from app.recipes.schemas import RecipeJobView

logger = logging.getLogger(__name__)


class RecipeCallback:
    # 서버 설정으로만 URL과 키를 받는다. 요청 본문에서 임의 콜백 주소를 수용하지 않는다.
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings, self.client = settings, client
        self.owns_client = client is None

    # 조회 API와 동일한 최종 snapshot을 제한 재시도 전송한다. 모든 2xx는 수신 성공이다.
    async def send(self, view: RecipeJobView) -> bool:
        if not self.settings.recipe_callback_url:
            CALLBACKS.labels("skipped").inc()
            logger.info("recipe.callback.skipped job_id=%s reason=no_url", view.jobId)
            return False
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=self.settings.callback_timeout_seconds)
        payload = view.model_dump(mode="json")
        logger.info("recipe.callback.payload job_id=%s payload=%s", view.jobId, view.model_dump_json())
        for attempt in range(1, self.settings.callback_max_attempts + 1):
            try:
                response = await self.client.post(self.settings.recipe_callback_url, json=payload,
                                                  headers={"X-Internal-API-Key": self.settings.recipe_callback_api_key})
                outcome = "success" if 200 <= response.status_code < 300 else "retry" if response.status_code in {429, 500, 502, 503, 504} else "rejected"
                CALLBACKS.labels(outcome).inc()
                logger.info("recipe.callback.response job_id=%s attempt=%d http_status=%d", view.jobId, attempt, response.status_code)
                if outcome != "retry":
                    return outcome == "success"
            except httpx.HTTPError:
                CALLBACKS.labels("request_failed").inc()
                logger.warning("recipe.callback.request_failed job_id=%s attempt=%d", view.jobId, attempt)
            if attempt < self.settings.callback_max_attempts:
                await asyncio.sleep(min(.25 * 2 ** (attempt - 1), 2))
        CALLBACKS.labels("exhausted").inc()
        return False

    # 직접 생성한 레시피 콜백 전용 HTTP 클라이언트만 정리한다.
    async def close(self) -> None:
        if self.owns_client and self.client is not None:
            await self.client.aclose()
