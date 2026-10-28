"""취향이 없는 요청의 계절/식사 시간대와 재현 가능한 요청별 무작위 순서를 구성한다."""

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo


# 서버의 한국 시각을 받아 계절과 시간대를 반환한다. 요청별 seed는 멱등 재실행에도 유지한다.
def recommendation_context(request_id: str, now: datetime | None = None) -> dict[str, str]:
    local = (now or datetime.now(ZoneInfo("Asia/Seoul"))).astimezone(ZoneInfo("Asia/Seoul"))
    season = "SPRING" if local.month in {3, 4, 5} else "SUMMER" if local.month in {6, 7, 8} else "AUTUMN" if local.month in {9, 10, 11} else "WINTER"
    meal = "BREAKFAST" if 5 <= local.hour < 11 else "LUNCH" if 11 <= local.hour < 16 else "DINNER" if 16 <= local.hour < 22 else "LATE_NIGHT"
    return {"localDate": local.date().isoformat(), "timezone": "Asia/Seoul", "season": season, "mealTime": meal,
            "variationSeed": hashlib.sha256(request_id.encode()).hexdigest()[:16]}


# 같은 요청과 후보에서는 같은 순서다. 새 requestId는 동등 후보의 새로운 순서를 제공한다.
def variation_key(row: dict, seed: str) -> str:
    return hashlib.sha256(f"{seed}:{row['source']}:{row['sourceRecipeId']}".encode()).hexdigest()
