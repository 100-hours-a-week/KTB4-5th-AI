import asyncio
import hashlib
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

from app.config import Settings
from app.errors import DependencyUnavailable
from app.recipes.ingredients import parse_ingredients
from app.recipes.retrieval import variants
from app.recipes.schemas import Recipe, Step

logger = logging.getLogger(__name__)
PARSER_VERSION = "food-safety-v3"


# 원문 재료 목록을 보수적으로 파싱한다. 구분이 모호한 원본은 추천·동기화하지 않는다.
def parse_recipe(row: dict) -> Recipe | None:
    raw = str(row.get("RCP_PARTS_DTLS", "")).strip()
    ingredients = parse_ingredients(raw, str(row.get("RCP_NM", "")))
    steps = [Step(stepNo=index, instruction=str(row[f"MANUAL{index:02d}"]).strip(), imageUrl=row.get(f"MANUAL_IMG{index:02d}") or None)
             for index in range(1, 21) if str(row.get(f"MANUAL{index:02d}") or "").strip()]
    if not ingredients or not steps or not row.get("RCP_SEQ") or not row.get("RCP_NM"):
        return None
    payload = {"name": row["RCP_NM"], "ingredients": [item.model_dump() for item in ingredients], "steps": [item.model_dump() for item in steps],
               "imageUrl": row.get("ATT_FILE_NO_MAIN") or None, "parser": PARSER_VERSION}
    revision = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return Recipe(source="FOOD_SAFETY_KOREA", sourceRecipeId=str(row["RCP_SEQ"]), sourceRevision=revision, name=payload["name"],
                  imageUrl=payload["imageUrl"], ingredients=ingredients, steps=steps)


class FoodSafetyProvider:
    # 키와 원문 저장 승인 설정을 받는다. 무료 sample 키를 운영 대체 경로로 사용하지 않는다.
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = asyncio.Lock()

    # URL에 키가 들어가는 서비스이므로 URL을 자동 로그로 남기지 않는 stdlib 클라이언트를 사용한다.
    def _fetch(self, ingredient: str | None, start: int = 1, end: int = 20) -> dict:
        key = quote(self.settings.recipe_food_safety_api_key or "", safe="")
        url = f"https://openapi.foodsafetykorea.go.kr/api/{key}/COOKRCP01/json/{start}/{end}"
        if ingredient is not None:
            url += f"/RCP_PARTS_DTLS={quote(ingredient, safe='')}"
        with urlopen(url, timeout=8) as response:
            data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("응답 크기 초과")
        return json.loads(data)

    # 최대 세 재료를 순차 검색하고 429·5xx만 한 번 재시도한다. 완전 파싱된 원본만 반환한다.
    async def search(self, names: list[str]) -> list[tuple[Recipe, dict]]:
        if not self.settings.recipe_food_safety_api_key or not self.settings.recipe_food_safety_storage_approved:
            raise DependencyUnavailable("DB에 추천 후보가 없습니다. 식품안전나라 키와 원문 저장 승인 설정이 필요합니다.")
        results = {}
        # 호출 상한은 유지하면서 입력 표기와 검토된 별칭을 함께 검색한다.
        queries = list(dict.fromkeys(term for name in names for term in [name, *variants([name])]))[:3]
        try:
            async with asyncio.timeout(30), self._lock:
                for name in queries:
                    for attempt in range(2):
                        try:
                            response = await asyncio.to_thread(self._fetch, name)
                            break
                        except HTTPError as exc:
                            if attempt or (exc.code != 429 and exc.code < 500):
                                raise
                            await asyncio.sleep(1)
                    if not isinstance(response, dict):
                        raise ValueError("원천 응답 형식 오류")
                    service = response.get("COOKRCP01", {})
                    if not isinstance(service, dict) or not isinstance(service.get("RESULT"), dict):
                        raise ValueError("원천 응답 형식 오류")
                    code = service.get("RESULT", {}).get("CODE")
                    if code == "INFO-200":
                        continue
                    if code != "INFO-000":
                        raise ValueError("원천 API 오류")
                    for row in service.get("row", []):
                        if not isinstance(row, dict):
                            raise ValueError("원천 레시피 형식 오류")
                        recipe = parse_recipe(row)
                        if recipe:
                            results[recipe.sourceRecipeId] = (recipe, row)
                    await asyncio.sleep(0.3)
        except (URLError, TimeoutError, ValueError, TypeError, KeyError) as exc:
            logger.warning("recipe_source_failed source=FOOD_SAFETY_KOREA error_type=%s", type(exc).__name__)
            raise DependencyUnavailable("식품안전나라 검색에 실패했습니다.") from None
        logger.info("recipe_source_completed source=FOOD_SAFETY_KOREA validated_count=%d", len(results))
        return list(results.values())
