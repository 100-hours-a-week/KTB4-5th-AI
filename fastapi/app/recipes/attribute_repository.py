"""승인된 원천 레시피의 버전별 AI 요리 성격 분류만 저장한다. 원문 레시피는 변경하지 않는다."""

import logging

import asyncpg

from app.errors import DependencyUnavailable
from app.recipes.ai_contract import ATTRIBUTE_PROMPT_VERSION, RecipeAttributes
from app.recipes.recipe_attributes import ATTRIBUTE_VERSION, attribute_key, validate_attributes
from app.recipes.repository import RecipeRepository
from app.recipes.schemas import Recipe

logger = logging.getLogger(__name__)


class RecipeAttributeRepository:
    # 원천 저장소 pool을 공유하며 앱 시작 시 DDL을 실행하지 않는다.
    def __init__(self, repository: RecipeRepository) -> None:
        self.repository = repository

    # 모든 후보를 한 번에 조회하고 현재 원문 / 모델과 일치하는 결과만 다시 검증해 반환한다.
    async def load_many(self, rows: list[dict], model: str) -> dict[tuple[str, str], RecipeAttributes]:
        wanted = {attribute_key(row, model): row for row in rows}
        if not wanted:
            return {}
        try:
            pool = await self.repository.pool.get()
            records = await pool.fetch("SELECT attribute_key,attributes FROM recipe_ai.recipe_attributes WHERE attribute_key=ANY($1::text[])", list(wanted))
            result = {}
            for record in records:
                row = wanted.get(record["attribute_key"])
                if row is None:
                    continue
                try:
                    result[(row["source"], row["sourceRecipeId"])] = validate_attributes(row, RecipeAttributes.model_validate_json(record["attributes"]))
                except ValueError:
                    logger.warning("recipe_attribute_invalid_cached_value")
            return result
        except (asyncpg.PostgresError, OSError, TimeoutError, ValueError) as exc:
            raise DependencyUnavailable("레시피 속성 분류를 조회하지 못했습니다. 024 마이그레이션을 확인하세요.") from exc

    # 원천이 같은 버전일 때만 저장한다. 처리 도중 변경된 원천은 저장하지 않는다.
    async def save(self, recipe: Recipe, attributes: RecipeAttributes, model: str) -> bool:
        row = recipe.model_dump(mode="json")
        verified = validate_attributes(row, attributes)
        key = attribute_key(row, model)
        pool = await self.repository.pool.get()
        async with pool.acquire() as connection, connection.transaction():
            current = await connection.fetchrow("SELECT id,snapshot FROM recipe_ai.recipes WHERE source=$1 AND source_recipe_id=$2 AND status='ACTIVE' FOR SHARE",
                                                recipe.source, recipe.sourceRecipeId)
            if current is None or attribute_key(Recipe.model_validate_json(current["snapshot"]).model_dump(mode="json"), model) != key:
                return False
            await connection.execute("""INSERT INTO recipe_ai.recipe_attributes(recipe_id,attribute_key,model_name,prompt_version,attribute_version,attributes)
                                     VALUES($1,$2,$3,$4,$5,$6::jsonb) ON CONFLICT(attribute_key) DO UPDATE SET attributes=EXCLUDED.attributes,updated_at=now()""",
                                     current["id"], key, model, ATTRIBUTE_PROMPT_VERSION, ATTRIBUTE_VERSION, verified.model_dump_json())
        return True
