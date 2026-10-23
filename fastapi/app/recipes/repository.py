import base64
import json

import asyncpg

from app.adapters._cache import LazyPool
from app.config import Settings
from app.errors import DependencyUnavailable, InvalidRequest
from app.recipes.schemas import ChangesResponse, Recipe, normalize


class RecipeRepository:
    # 설정을 받아 지연 연결 pool을 구성한다. 앱 시작 시 DDL을 실행하지 않는다.
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool = LazyPool(self._connect)

    # 기존 AI PostgreSQL에 연결하며 접속 정보나 SQL 오류 원문을 응답에 노출하지 않는다.
    async def _connect(self) -> asyncpg.Pool:
        if not self.settings.database_url:
            raise DependencyUnavailable("레시피 PostgreSQL이 설정되지 않았습니다.")
        return await asyncpg.create_pool(self.settings.database_url, min_size=1, max_size=self.settings.database_pool_max_size, command_timeout=15)

    # 재료가 하나 이상 겹치는 활성 스냅샷을 읽어 도메인 모델로 검증한다.
    async def find(self, names: list[str]) -> list[Recipe]:
        try:
            pool = await self.pool.get()
            rows = await pool.fetch("SELECT snapshot FROM recipe_ai.recipes WHERE status='ACTIVE' AND ingredient_names && $1::text[]", names)
            return [Recipe.model_validate_json(row["snapshot"]) for row in rows]
        except DependencyUnavailable:
            raise
        except (asyncpg.PostgresError, OSError, TimeoutError, ValueError) as exc:
            raise DependencyUnavailable("레시피 DB 조회에 실패했습니다. 연결과 마이그레이션을 확인하세요.") from exc

    # 검증된 원본과 출처를 원자적으로 저장한다. 전역 쓰기 락으로 cursor와 commit 순서를 일치시킨다.
    async def save(self, recipe: Recipe, raw: dict, policy: str) -> None:
        try:
            pool = await self.pool.get()
            async with pool.acquire() as connection, connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(184729020)")
                previous = await connection.fetchval(
                    "SELECT snapshot FROM recipe_ai.recipes WHERE source=$1 AND source_recipe_id=$2", recipe.source, recipe.sourceRecipeId,
                )
                if previous and Recipe.model_validate_json(previous) == recipe:
                    return
                payload = recipe.model_dump_json()
                await connection.execute(
                    """INSERT INTO recipe_ai.recipes(source,source_recipe_id,status,ingredient_names,snapshot,raw_source,usage_policy)
                       VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7)
                       ON CONFLICT(source,source_recipe_id) DO UPDATE SET status=EXCLUDED.status,
                       ingredient_names=EXCLUDED.ingredient_names,snapshot=EXCLUDED.snapshot,raw_source=EXCLUDED.raw_source,
                       usage_policy=EXCLUDED.usage_policy,updated_at=now()""",
                    recipe.source, recipe.sourceRecipeId, recipe.status, [normalize(item.name) for item in recipe.ingredients], payload, json.dumps(raw, ensure_ascii=False), policy,
                )
                await connection.execute("INSERT INTO recipe_ai.changes(snapshot) VALUES($1::jsonb)", payload)
        except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
            raise DependencyUnavailable("레시피 DB 저장에 실패했습니다.") from exc

    # 불투명 cursor의 마지막 변경 번호를 검증해 반환한다. cursor는 인증 수단이 아니다.
    @staticmethod
    def decode_cursor(cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            if len(cursor) > 100:
                raise ValueError
            value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if not isinstance(value, dict) or set(value) != {"v", "after"} or value["v"] != 1 or type(value["after"]) is not int or value["after"] < 0:
                raise ValueError
            return value["after"]
        except (ValueError, TypeError, UnicodeError) as exc:
            raise InvalidRequest("올바르지 않은 레시피 동기화 cursor입니다.") from exc

    # append-only 변경 이력을 keyset으로 읽는다. 페이지 전체 적용 후에만 다음 cursor를 저장한다.
    async def changes(self, cursor: str | None, limit: int) -> ChangesResponse:
        after = self.decode_cursor(cursor)
        try:
            pool = await self.pool.get()
            rows = await pool.fetch("SELECT change_id,snapshot FROM recipe_ai.changes WHERE change_id>$1 ORDER BY change_id LIMIT $2", after, limit + 1)
            page = rows[:limit]
            last = page[-1]["change_id"] if page else after
            token = base64.urlsafe_b64encode(json.dumps({"v": 1, "after": last}, separators=(",", ":")).encode()).decode()
            return ChangesResponse(items=[Recipe.model_validate_json(row["snapshot"]) for row in page], nextCursor=token, hasMore=len(rows) > limit)
        except (asyncpg.PostgresError, OSError, TimeoutError, ValueError) as exc:
            raise DependencyUnavailable("레시피 변경 이력 조회에 실패했습니다.") from exc

    # 앱 종료 시 DB 연결을 반납한다.
    async def close(self) -> None:
        await self.pool.close()
