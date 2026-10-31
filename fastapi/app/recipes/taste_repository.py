"""승인된 원천 레시피의 버전별 맛 평가만 저장한다. 사용자 취향 / 원문 레시피는 변경하지 않는다."""

import logging

import asyncpg

from app.errors import DependencyUnavailable
from app.recipes.ai_contract import PROMPT_VERSION, TasteProfile
from app.recipes.repository import RecipeRepository
from app.recipes.schemas import Recipe
from app.recipes.taste_profiles import EVALUATION_VERSION, profile_key, validate_profile

logger = logging.getLogger(__name__)


class TasteProfileRepository:
    # 원천 저장소 pool을 공유하며 앱 시작 시 DDL을 실행하지 않는다.
    def __init__(self, repository: RecipeRepository) -> None:
        self.repository = repository

    # 모든 검색 후보를 한 번에 조회하고 현재 원문 / 모델과 일치하는 검증 결과만 반환한다.
    async def load_many(self, rows: list[dict], model: str) -> dict[tuple[str, str], TasteProfile]:
        wanted = {profile_key(row, model): row for row in rows}
        if not wanted:
            return {}
        try:
            pool = await self.repository.pool.get()
            records = await pool.fetch("SELECT profile_key,profile FROM recipe_ai.taste_profiles WHERE profile_key=ANY($1::text[])", list(wanted))
            result = {}
            for record in records:
                row = wanted.get(record["profile_key"])
                if row is not None:
                    try:
                        profile = validate_profile(row, TasteProfile.model_validate_json(record["profile"]))
                    except ValueError:
                        logger.warning("recipe_taste_profile_invalid_cached_evidence")
                        continue
                    result[(row["source"], row["sourceRecipeId"])] = profile
            return result
        except (asyncpg.PostgresError, OSError, TimeoutError, ValueError) as exc:
            raise DependencyUnavailable("레시피 맛 평가를 조회하지 못했습니다. 023 마이그레이션을 확인하세요.") from exc

    # 원천이 같은 버전일 때만 저장한다. 미등록 웹 원문이나 처리 도중 변경된 원천은 저장하지 않는다.
    async def save(self, recipe: Recipe, profile: TasteProfile, model: str) -> bool:
        row = recipe.model_dump(mode="json")
        verified = validate_profile(row, profile)
        key = profile_key(row, model)
        pool = await self.repository.pool.get()
        async with pool.acquire() as connection, connection.transaction():
            current = await connection.fetchrow("SELECT id,snapshot FROM recipe_ai.recipes WHERE source=$1 AND source_recipe_id=$2 AND status='ACTIVE' FOR SHARE",
                                                recipe.source, recipe.sourceRecipeId)
            if current is None or profile_key(Recipe.model_validate_json(current["snapshot"]).model_dump(mode="json"), model) != key:
                return False
            await connection.execute("""INSERT INTO recipe_ai.taste_profiles(recipe_id,profile_key,model_name,prompt_version,evaluation_version,profile)
                                     VALUES($1,$2,$3,$4,$5,$6::jsonb) ON CONFLICT(profile_key) DO UPDATE SET profile=EXCLUDED.profile,updated_at=now()""",
                                     current["id"], key, model, PROMPT_VERSION, EVALUATION_VERSION, verified.model_dump_json())
        return True
