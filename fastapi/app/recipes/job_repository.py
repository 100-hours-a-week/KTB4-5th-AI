"""레시피 완료/실패 결과만 영속화한다. 대기 큐는 FastAPI 메모리에서 관리한다."""

from app.errors import DependencyUnavailable
from app.recipes.repository import RecipeRepository
from app.recipes.schemas import RecipeJobView


class RecipeJobRepository:
    # 레시피 저장소의 기존 pool을 공유한다. 별도 pool/DDL은 생성하지 않는다.
    def __init__(self, repository: RecipeRepository) -> None:
        self.repository = repository

    # 같은 requestId의 완료 결과와 요청 해시를 반환한다. 접속/스키마 오류는 접수 전에 알린다.
    async def by_request(self, request_id: str) -> tuple[str, RecipeJobView] | None:
        return await self._load("request_id", request_id)

    # 재시작 후에도 작업 ID로 완료 결과를 조회한다.
    async def by_id(self, job_id: str) -> tuple[str, RecipeJobView] | None:
        return await self._load("job_id", job_id)

    # 내부에서 정한 열만 사용하여 저장된 terminal snapshot을 검증해 읽는다.
    async def _load(self, column: str, value: str) -> tuple[str, RecipeJobView] | None:
        try:
            pool = await self.repository.pool.get()
            row = await pool.fetchrow(f"SELECT request_hash,snapshot FROM recipe_ai.recommendation_results WHERE {column}=$1", value)
            return (row["request_hash"], RecipeJobView.model_validate_json(row["snapshot"])) if row else None
        except Exception as exc:
            raise DependencyUnavailable("레시피 작업 이력을 조회하지 못했습니다. DB 연결과 022 마이그레이션을 확인하세요.") from exc

    # 완료/실패 snapshot만 저장하며 requestId의 다른 내용 덮어쓰기는 허용하지 않는다.
    async def save(self, request_hash: str, view: RecipeJobView) -> None:
        try:
            pool = await self.repository.pool.get()
            await pool.execute("""INSERT INTO recipe_ai.recommendation_results(job_id,request_id,request_hash,snapshot)
                                VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(job_id) DO UPDATE SET snapshot=EXCLUDED.snapshot""",
                               view.jobId, view.requestId, request_hash, view.model_dump_json())
        except Exception as exc:
            raise DependencyUnavailable("레시피 작업 결과를 저장하지 못했습니다.") from exc
