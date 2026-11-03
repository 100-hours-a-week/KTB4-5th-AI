"""기존 정밀레시피 안내문 분리. 기본 dry-run, 배포 후 --apply로 변경 이력까지 반영."""

import argparse
import asyncio
import json
from pathlib import Path

from app.config import Settings
from app.recipes.repository import RecipeRepository
from app.recipes.schemas import Recipe
from app.recipes.source_cleanup import clean_precision_recipe


async def run(apply: bool, backup: Path | None) -> None:
    repository = RecipeRepository(Settings())
    pool = await repository.pool.get()
    try:
        rows = await pool.fetch("SELECT snapshot,raw_source,usage_policy FROM recipe_ai.recipes WHERE source=$1", "HANSIK_PRECISION")
        changes = []
        for row in rows:
            original = Recipe.model_validate_json(row["snapshot"])
            cleaned = clean_precision_recipe(original)
            if original != cleaned:
                changes.append((row, cleaned))
        if apply:
            if backup is None:
                raise ValueError("--apply에는 --backup 경로가 필요합니다.")
            backup.parent.mkdir(parents=True, exist_ok=True)
            with backup.open("x", encoding="utf-8") as stream:
                for row, _ in changes:
                    stream.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
            for row, cleaned in changes:
                raw = json.loads(row["raw_source"]) if isinstance(row["raw_source"], str) else row["raw_source"]
                await repository.save(cleaned, raw, row["usage_policy"])
        print(json.dumps({"apply": apply, "changed": len(changes), "recipes": [r.name for _, r in changes]}, ensure_ascii=False))
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.backup))
