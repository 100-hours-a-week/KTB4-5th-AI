"""인간 검토 라벨과 맛 추정 결과를 비교한다. UNKNOWN을 정답으로 계산하지 않는다."""
import argparse
import json
from pathlib import Path

from app.recipes.ai_contract import TasteProfile
from app.recipes.taste_profiles import KINDS


# 동일 원문 / 모델 키로 정답과 예측을 연결한다. 지표는 검토 라벨 기준이며 관능평가 정답률과 구분한다.
def evaluate(predictions: dict[str, TasteProfile], labels: list[dict]) -> dict:
    if len({item["profileKey"] for item in labels}) != len(labels):
        raise ValueError("DUPLICATE_REVIEW_LABEL")
    totals = {kind:{"tp":0,"fp":0,"fn":0,"reviewed":0,"abstained":0} for kind in sorted(KINDS)}
    for item in labels:
        if not item.get("reviewer") or set(item["dimensions"]) != KINDS:
            raise ValueError("COMPLETE_HUMAN_REVIEW_REQUIRED")
        profile = predictions.get(item["profileKey"])
        by_kind = {d.kind:d.status for d in profile.dimensions} if profile else {}
        for kind, expected in item["dimensions"].items():
            if expected not in {"SUPPORTED", "NOT_SUPPORTED", "UNKNOWN"}:
                raise ValueError("INVALID_REVIEW_STATUS")
            if expected == "UNKNOWN":
                continue
            actual = by_kind.get(kind, "UNKNOWN")
            stats = totals[kind]
            stats["reviewed"] += 1
            stats["abstained"] += actual == "UNKNOWN"
            stats["tp"] += actual == expected == "SUPPORTED"
            stats["fp"] += actual == "SUPPORTED" and expected == "NOT_SUPPORTED"
            stats["fn"] += actual != "SUPPORTED" and expected == "SUPPORTED"
    for stats in totals.values():
        stats["precision"] = stats["tp"] / (stats["tp"] + stats["fp"]) if stats["tp"] + stats["fp"] else None
        stats["recall"] = stats["tp"] / (stats["tp"] + stats["fn"]) if stats["tp"] + stats["fn"] else None
        stats["coverage"] = 1-stats["abstained"]/stats["reviewed"] if stats["reviewed"] else None
    return {"referenceType":"HUMAN_REVIEWED_RECIPE_TEXT", "dimensions":totals}


# 검토 파일이 없으면 점수를 생성하지 않는다. 입력은 profileKey로 원문 버전을 구분해야 한다.
def main() -> None:
    parser = argparse.ArgumentParser(description="레시피 원문 검토 라벨 기준 맛 평가")
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--labels", required=True)
    args = parser.parse_args()
    records = json.loads(Path(args.predictions).read_text())
    predictions = {item["profileKey"]:TasteProfile.model_validate(item["profile"]) for item in records}
    labels = json.loads(Path(args.labels).read_text())
    print(json.dumps(evaluate(predictions, labels), ensure_ascii=False))


if __name__ == "__main__":
    main()
