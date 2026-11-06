"""승인 원천의 요리 성격을 순차 분류한다. 실행 예: python -m app.recipes.attribute_backfill --limit 100 --after-id 0

표본 검수용 CSV: python -m app.recipes.attribute_backfill --sample 100 --output /tmp/attribute-sample.csv
"""
import argparse
import asyncio
import csv
import json

from app.config import Settings
from app.recipes.ai import RecipeReasoner
from app.recipes.attribute_repository import RecipeAttributeRepository
from app.recipes.recipe_attributes import attribute_context, validate_attributes
from app.recipes.repository import RecipeRepository
from app.recipes.schemas import Recipe


# 원천 ID로 페이지를 진행한다. 기존 분류는 재사용하고 오류는 집계하며 운영 추천 큐는 변경하지 않는다.
async def backfill(limit: int, after_id: int, timeout: float, source: str | None = None) -> dict:
    settings = Settings()
    repository, reasoner = RecipeRepository(settings), RecipeReasoner(settings)
    store = RecipeAttributeRepository(repository)
    counts = {"scanned": 0, "reused": 0, "saved": 0, "failed": 0, "sourceChanged": 0, "nextAfterId": after_id}
    try:
        pool = await repository.pool.get()
        if not await pool.fetchval("SELECT to_regclass('recipe_ai.recipe_attributes') IS NOT NULL"):
            raise SystemExit("DB 소유자 권한으로 024 마이그레이션을 먼저 적용하세요.")
        rows = await pool.fetch("SELECT id,snapshot FROM recipe_ai.recipes WHERE status='ACTIVE' AND id>$1 AND ($3::text IS NULL OR source=$3) ORDER BY id LIMIT $2",
                                after_id, limit, source)
        for record in rows:
            counts["scanned"] += 1
            counts["nextAfterId"] = record["id"]
            try:
                recipe = Recipe.model_validate_json(record["snapshot"])
                row = recipe.model_dump(mode="json")
                if await store.load_many([row], settings.ollama_model):
                    counts["reused"] += 1
                    continue
                context = attribute_context(row)
                context["executionBudgetSeconds"] = max(0.1, min(30, timeout - 1))
                async with asyncio.timeout(timeout):
                    attributes, model = await reasoner.attributes(context)
                if model != settings.ollama_model:
                    raise ValueError("RECIPE_ATTRIBUTE_MODEL_MISMATCH")
                saved = await store.save(recipe, validate_attributes(row, attributes), model)
                counts["saved" if saved else "sourceChanged"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                counts["failed"] += 1
        return counts
    finally:
        await repository.close()
        await reasoner.close()


# 저장된 분류에서 무작위 표본을 뽑아 사람이 정답을 적을 수 있는 CSV로 내보낸다. 사용자 정보는 포함하지 않는다.
async def export_sample(size: int, output: str, source: str | None = None) -> dict:
    settings = Settings()
    repository = RecipeRepository(settings)
    try:
        pool = await repository.pool.get()
        rows = await pool.fetch("""SELECT r.source,r.source_recipe_id,r.snapshot,a.attributes FROM recipe_ai.recipe_attributes a
                                   JOIN recipe_ai.recipes r ON r.id=a.recipe_id
                                   WHERE r.status='ACTIVE' AND a.model_name=$1 AND ($3::text IS NULL OR r.source=$3) ORDER BY random() LIMIT $2""",
                                settings.ollama_model, size, source)
        fields = ("cuisine", "dishForm", "servingTemperature", "heaviness", "difficulty", "mealPurposes", "textures")
        with open(output, "w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(["source", "sourceRecipeId", "name", "stepCount", "ingredients", *fields, *(f"correct_{field}" for field in fields), "note"])
            for record in rows:
                snapshot, attributes = json.loads(record["snapshot"]), json.loads(record["attributes"])
                writer.writerow([record["source"], record["source_recipe_id"], snapshot["name"], len(snapshot["steps"]),
                                 ", ".join(item["name"] for item in snapshot["ingredients"])[:300],
                                 *("|".join(attributes[field]) if isinstance(attributes[field], list) else attributes[field] for field in fields),
                                 *([""] * len(fields)), ""])
        return {"exported": len(rows), "output": output}
    finally:
        await repository.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="승인 레시피 요리 성격 사전 분류")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=35)
    parser.add_argument("--source", help="지정한 원천만 처리한다. 예: HANSIK_PRECISION")
    parser.add_argument("--sample", type=int, help="분류 대신 저장된 결과에서 표본 검수 CSV를 만든다.")
    parser.add_argument("--output", default="attribute-sample.csv")
    args = parser.parse_args()
    if args.sample is not None:
        if not 1 <= args.sample <= 1000:
            parser.error("sample=1~1000 범위가 필요합니다.")
        print(json.dumps(asyncio.run(export_sample(args.sample, args.output, args.source)), ensure_ascii=False))
        return
    if not 1 <= args.limit <= 1000 or args.after_id < 0 or not 1 <= args.timeout <= 60:
        parser.error("limit=1~1000, after-id>=0, timeout=1~60 범위가 필요합니다.")
    result = asyncio.run(backfill(args.limit, args.after_id, args.timeout, args.source))
    print(json.dumps(result))
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
