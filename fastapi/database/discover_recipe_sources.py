"""Tavily 기본 검색으로 원천 후보만 준비한다. 크롤링·본문 저장·DB 반영은 하지 않는다."""

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.recipes.tavily import TavilyDiscovery  # noqa: E402


# 명시한 검색어로 1회 검색하고 제목·URL·검토 상태만 신규 파일에 보존한다.
async def run(query: str, maximum: int, output: Path) -> dict:
    if output.exists():
        raise FileExistsError("출력 파일이 이미 존재합니다. 다른 경로를 사용하세요.")
    settings = Settings(_env_file=Path(__file__).resolve().parents[1] / ".env", recipe_tavily_max_attempts=1)
    started = time.monotonic()
    hits = await TavilyDiscovery(settings).search(query, maximum)
    result = {"source": "TAVILY_DISCOVERY", "searchDepth": "basic", "requests": 1, "query": query,
              "generatedAt": datetime.now(UTC).isoformat(), "elapsedSeconds": round(time.monotonic() - started, 3),
              "candidateCount": len(hits), "recipeImportedCount": 0, "items": [hit.model_dump() for hit in hits]}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return result


# 키는 fastapi/.env에서만 읽는다. 공개 원천 조사 용도로 기본 검색 한 번만 실행한다.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--max-results", type=int, default=5, choices=range(1, 11))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.query, args.max_results, args.output))
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
