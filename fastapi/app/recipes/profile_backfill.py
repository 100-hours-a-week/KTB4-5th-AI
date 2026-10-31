"""승인 원천만 순차 평가한다. 실행 예: python -m app.recipes.profile_backfill --limit 20 --after-id 0"""
import argparse
import asyncio
import json

from app.config import Settings
from app.recipes.ai import RecipeReasoner
from app.recipes.repository import RecipeRepository
from app.recipes.schemas import Recipe
from app.recipes.taste_profiles import profile_context, validate_profile
from app.recipes.taste_repository import TasteProfileRepository


# 원천 ID로 페이지를 진행한다. 기존 평가를 재사용하고 오류는 집계하며 운영 추천 큐는 변경하지 않는다.
async def backfill(limit: int, after_id: int, timeout: float) -> dict:
    settings = Settings()
    repository, reasoner = RecipeRepository(settings), RecipeReasoner(settings)
    store = TasteProfileRepository(repository)
    counts = {"scanned":0, "reused":0, "saved":0, "failed":0, "sourceChanged":0, "nextAfterId":after_id}
    try:
        pool = await repository.pool.get()
        if not await pool.fetchval("SELECT to_regclass('recipe_ai.taste_profiles') IS NOT NULL"):
            raise SystemExit("DB 소유자 권한으로 023 마이그레이션을 먼저 적용하세요.")
        rows = await pool.fetch("SELECT id,snapshot FROM recipe_ai.recipes WHERE status='ACTIVE' AND id>$1 ORDER BY id LIMIT $2", after_id, limit)
        for record in rows:
            counts["scanned"] += 1
            counts["nextAfterId"] = record["id"]
            try:
                recipe = Recipe.model_validate_json(record["snapshot"])
                row = recipe.model_dump(mode="json")
                if await store.load_many([row], settings.ollama_model):
                    counts["reused"] += 1
                    continue
                context = profile_context(row)
                context["executionBudgetSeconds"] = max(0.1, min(30, timeout - 1))
                async with asyncio.timeout(timeout):
                    profile, model = await reasoner.profile(context)
                if model != settings.ollama_model:
                    raise ValueError("TASTE_PROFILE_MODEL_MISMATCH")
                verified = validate_profile(row, profile)
                saved = await store.save(recipe, verified, model)
                counts["saved" if saved else "sourceChanged"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                counts["failed"] += 1
        return counts
    finally:
        await repository.close()
        await reasoner.close()


# 일괄 실행은 명시한 범위만 처리한다. 결과 집계에 원문 / 비밀키 / 사용자 정보는 포함하지 않는다.
def main() -> None:
    parser = argparse.ArgumentParser(description="승인 레시피 맛 특성 사전 평가")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=35)
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000 or args.after_id < 0 or not 1 <= args.timeout <= 60:
        parser.error("limit=1~1000, after-id>=0, timeout=1~60 범위가 필요합니다.")
    result = asyncio.run(backfill(args.limit, args.after_id, args.timeout))
    print(json.dumps(result))
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
