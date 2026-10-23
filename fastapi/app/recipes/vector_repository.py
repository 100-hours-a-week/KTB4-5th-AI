import hashlib
import json
import math

from app.recipes.embeddings import DIMENSIONS, MODEL_NAME, MODEL_REVISION, TEXT_VERSION, recipe_text
from app.recipes.repository import RecipeRepository
from app.recipes.schemas import Recipe


# 숫자형·차원·유한값·0 벡터 여부를 검사한다. 모델이 다른 벡터를 암묵적으로 혼용하지 않는다.
def vector_literal(vector: list[float]) -> str:
    if len(vector) != DIMENSIONS or not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in vector) or not any(vector):
        raise ValueError("유한한 384차원 non-zero 벡터가 필요합니다.")
    return json.dumps(vector, separators=(",", ":"))


class RecipeVectorRepository:
    # 기존 repository pool만 재사용한다. 기본 추천 서비스에는 자동 연결하지 않는다.
    def __init__(self, repository: RecipeRepository) -> None:
        self.repository = repository

    # 현재 원천 revision과 일치하는 벡터만 저장한다. 변경된 원문에 예전 벡터를 덮어쓰지 않는다.
    async def save(self, recipe: Recipe, vector: list[float]) -> None:
        payload = vector_literal(vector)
        pool = await self.repository.pool.get()
        result = await pool.fetchval(
            """INSERT INTO recipe_ai.recipe_embeddings(recipe_id,model_name,model_revision,text_version,source_revision,text_sha256,embedding)
               SELECT id,$3,$4,$5,$6,$7,$8::text::vector FROM recipe_ai.recipes
               WHERE source=$1 AND source_recipe_id=$2 AND snapshot->>'sourceRevision'=$6
               ON CONFLICT(recipe_id,model_name,model_revision,text_version) DO UPDATE SET
               source_revision=EXCLUDED.source_revision,text_sha256=EXCLUDED.text_sha256,embedding=EXCLUDED.embedding,updated_at=now()
               RETURNING recipe_id""",
            recipe.source, recipe.sourceRecipeId, MODEL_NAME, MODEL_REVISION, TEXT_VERSION, recipe.sourceRevision,
            hashlib.sha256(recipe_text(recipe).encode()).hexdigest(), payload,
        )
        if result is None:
            raise ValueError("현재 DB 레시피와 임베딩 원천 revision이 일치하지 않습니다.")

    # 활성·모델·원천 revision을 모두 검증한 후보를 cosine 거리순으로 읽는다. 재료 확정은 별도다.
    async def nearest(self, vector: list[float], limit: int = 50) -> list[tuple[Recipe, float]]:
        if not 1 <= limit <= 500:
            raise ValueError("벡터 검색 limit은 1~500입니다.")
        payload = vector_literal(vector)
        pool = await self.repository.pool.get()
        rows = await pool.fetch(
            """SELECT r.snapshot, 1-(e.embedding <=> $1::text::vector) AS similarity
               FROM recipe_ai.recipe_embeddings e JOIN recipe_ai.recipes r ON r.id=e.recipe_id
               WHERE r.status='ACTIVE' AND r.snapshot->>'sourceRevision'=e.source_revision
               AND e.model_name=$2 AND e.model_revision=$3 AND e.text_version=$4
               ORDER BY e.embedding <=> $1::text::vector, r.source, r.source_recipe_id LIMIT $5""",
            payload, MODEL_NAME, MODEL_REVISION, TEXT_VERSION, limit,
        )
        return [(Recipe.model_validate_json(row["snapshot"]), float(row["similarity"])) for row in rows]
