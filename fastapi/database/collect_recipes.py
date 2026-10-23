"""개발용 레시피 준비 파일을 수집한다. DB·백엔드·운영 환경에 쓰지 않는다."""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.recipes.food_safety import FoodSafetyProvider, parse_recipe  # noqa: E402


# 레코드를 JSONL로 기록하고 페이지 실패에도 이미 받은 원본은 보존한다.
def write_line(stream, record: dict) -> None:
    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    stream.flush()


# 원천 페이지를 읽고 일시적인 429·서버·접속 오류만 제한적으로 재시도한다.
def fetch_page(provider: FoodSafetyProvider, start: int, end: int) -> dict:
    for attempt in range(3):
        try:
            return provider._fetch(None, start, end)
        except (URLError, TimeoutError) as exc:
            if isinstance(exc, HTTPError) and exc.code != 429 and exc.code < 500:
                raise
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    raise RuntimeError("페이지 요청 실패")


# 신규 디렉터리에 원본·검증 통과본·검토 대상을 분리 저장하고 수집 요약을 반환한다.
def collect(provider: FoodSafetyProvider, output: Path, maximum: int, batch: int) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    summary = {"source": "FOOD_SAFETY_KOREA", "dataset": "COOKRCP01", "startedAt": datetime.now(UTC).isoformat(),
               "sourceUrl": "https://www.foodsafetykorea.go.kr/api/newDatasetDetail.do?svc_no=COOKRCP01", "uniqueRows": 0,
               "validated": 0, "needsReview": 0, "pages": 0, "duplicates": 0, "databaseImported": False,
               "storageUsageApproved": False, "status": "COLLECTING"}
    seen = set()
    started = time.monotonic()
    try:
        with (
            (output / "raw.jsonl").open("x", encoding="utf-8") as raw_stream,
            (output / "validated.jsonl").open("x", encoding="utf-8") as valid_stream,
            (output / "needs_review.jsonl").open("x", encoding="utf-8") as review_stream,
        ):
            for start in range(1, maximum + 1, batch):
                body = fetch_page(provider, start, min(start + batch - 1, maximum))
                service = body.get("COOKRCP01", {})
                code = service.get("RESULT", {}).get("CODE")
                if code == "INFO-200":
                    summary["status"] = "SOURCE_EXHAUSTED"
                    break
                if code != "INFO-000" or not isinstance(service.get("row"), list):
                    raise ValueError("원천 응답 코드·형식 오류")
                rows = service["row"]
                summary["pages"] += 1
                summary["sourceReportedTotal"] = service.get("total_count")
                for row in rows:
                    if not isinstance(row, dict) or not str(row.get("RCP_SEQ") or "").strip():
                        raise ValueError("원천 ID 누락")
                    source_id = str(row["RCP_SEQ"])
                    if source_id in seen:
                        summary["duplicates"] += 1
                        continue
                    seen.add(source_id)
                    write_line(raw_stream, row)
                    summary["uniqueRows"] += 1
                    reason = "UNPARSED_INGREDIENT_OR_MISSING_FIELD"
                    try:
                        recipe = parse_recipe(row)
                    except (ValueError, TypeError, KeyError) as exc:
                        recipe = None
                        reason = type(exc).__name__
                    if recipe:
                        write_line(valid_stream, recipe.model_dump(mode="json"))
                        summary["validated"] += 1
                    else:
                        write_line(review_stream, {"sourceRecipeId": source_id, "name": row.get("RCP_NM"), "reason": reason})
                        summary["needsReview"] += 1
                print(json.dumps({key: summary[key] for key in ("pages", "uniqueRows", "validated", "needsReview")}), flush=True)
                if not rows or len(rows) < batch:
                    summary["status"] = "SOURCE_EXHAUSTED"
                    break
                time.sleep(0.5)
            else:
                summary["status"] = "LIMIT_REACHED"
    except Exception as exc:
        # URL에 들어가는 인증키가 노출되지 않도록 예외 원문은 출력·저장하지 않는다.
        summary["status"] = "INTERRUPTED"
        summary["errorType"] = type(exc).__name__
        raise RuntimeError(f"수집 중단: {type(exc).__name__}. 준비 파일과 summary.json을 확인하세요.") from None
    finally:
        summary["finishedAt"] = datetime.now(UTC).isoformat()
        summary["elapsedSeconds"] = round(time.monotonic() - started, 3)
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


# 명시된 최대 건수까지만 개발용 원본을 수집한다. 기본 .env는 fastapi/.env에서 읽는다.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-records", type=int, default=2000)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "recipe_staging" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args()
    if not 1 <= args.max_records <= 10000 or not 1 <= args.batch_size <= 100:
        parser.error("max-records는 1~10000, batch-size는 1~100이어야 합니다.")
    settings = Settings(_env_file=Path(__file__).resolve().parents[1] / ".env")
    if not settings.recipe_food_safety_api_key:
        parser.error("RECIPE_FOOD_SAFETY_API_KEY가 필요합니다.")
    print(json.dumps(collect(FoodSafetyProvider(settings), args.output, args.max_records, args.batch_size), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
