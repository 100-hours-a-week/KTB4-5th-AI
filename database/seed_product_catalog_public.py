"""식품안전나라(식약처) K-FIND 가공식품 DB에서 product_knowledge 대량 시드 CSV를 생성한다.

원본 데이터 출처: 식품안전나라 식품영양성분 데이터베이스 "가공식품 DB"
(https://various.foodsafetykorea.go.kr/nutrient/ > 영양성분 DB 내려받기)
로그인 없이 "시스템 개선을 위한 활용정보 입력" 설문만 작성하면 엑셀로 받을 수 있다.
받은 원본 엑셀(약 200MB, 31만여 행)은 용량 문제로 이 저장소에 커밋하지 않는다 —
이 스크립트를 실행할 때 --input으로 로컬 경로를 직접 지정한다.

이 스크립트는 DB에 아무것도 쓰지 않는다. database/seed_data/product_catalog_public_seed.csv를
생성만 하며, 실제 적용은 마이그레이션(006_product_catalog_seed_public.sql)이 \\copy로 적재한다.

카테고리 매핑은 식품대분류명(및 절임류/조림류는 식품중분류명)을 기준으로 하되,
카테고리를 한 갈래로 확신할 수 없는 대분류(농산가공식품류, 조림류, 동물성가공식품류,
특수영양식품, 당류, 기타식품류, 특수의료용도식품, 알가공품류, 벌꿀 및 화분가공품류 등)는
제외했다 — 근거 없이 카테고리를 추측해 넣지 않는다는 이 프로젝트의 원칙을 따른다.
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.domain.receipt_rules import normalize_product_name  # noqa: E402

SEED_DATA_DIR = Path(__file__).parent / "seed_data"
OUTPUT_CSV = SEED_DATA_DIR / "product_catalog_public_seed.csv"

SOURCE_TYPE = "PUBLIC_DATASET"
SOURCE_REFERENCE = "식품안전나라 K-FIND 가공식품 DB(2026-08-28 갱신, https://various.foodsafetykorea.go.kr/nutrient/)"

# 식품대분류명 -> Category. 대분류 하나로 카테고리를 확신할 수 있는 것만 넣었다.
LARGE_CATEGORY_MAP = {
    "과자류·빵류 또는 떡류": "PROCESSED",
    "즉석식품류": "PROCESSED",
    "음료류": "BEVERAGE",
    "식육가공품 및 포장육": "MEAT",
    "수산가공식품류": "SEAFOOD",
    "조미식품": "SEASONING",
    "코코아가공품류 또는 초콜릿류": "PROCESSED",
    "면류": "GRAIN_NOODLE",
    "식용유지류": "SEASONING",
    "유가공품류": "DAIRY",
    "빙과류": "PROCESSED",
    "잼류": "PROCESSED",
    "장류": "SEASONING",
    "두부류 또는 묵류": "TOFU_BEAN",
    "주류": "BEVERAGE",
}

# "절임류 또는 조림류"는 중분류로 더 나눈다 — 절임류/김치류만 채소로 확신 가능하고
# 조림류(멸치조림/고기조림 등)는 재료가 뒤섞여 있어 제외한다.
PICKLE_MID_CATEGORY_MAP = {
    "절임류": "VEGETABLE",
    "김치류": "VEGETABLE",
}

# MEAT/SEAFOOD는 storage_for_category()가 항상 FROZEN으로 조회하고, 그 외는
# 항상 REFRIGERATED로 조회한다(shelf_life_rules 시드와 동일 원칙 — 조회 안 되는
# storage_type 값은 만들지 않는다).
FROZEN_CATEGORIES = {"MEAT", "SEAFOOD"}


def determine_category(large: str, mid: str) -> str | None:
    if large == "절임류 또는 조림류":
        return PICKLE_MID_CATEGORY_MAP.get(mid)
    return LARGE_CATEGORY_MAP.get(large)


def clean_name(raw: str) -> str:
    return normalize_product_name(raw.lstrip("﻿").strip())


def build_rows(xlsx_path: Path) -> list[tuple[str, str, str, str]]:
    from python_calamine import CalamineWorkbook

    wb = CalamineWorkbook.from_path(str(xlsx_path))
    sheet_rows = wb.get_sheet_by_index(0).to_python()

    seen: dict[str, tuple[str, str, str, str]] = {}
    skipped_short = 0
    skipped_uncategorized = 0
    for row in sheet_rows[1:]:
        if len(row) < 12:
            continue
        food_name, large_category, mid_category = row[1], row[7], row[11]
        category = determine_category(large_category, mid_category)
        if category is None:
            skipped_uncategorized += 1
            continue
        canonical = clean_name(str(food_name))
        if len(canonical) < 2:
            skipped_short += 1
            continue
        normalized = canonical
        storage_type = "FROZEN" if category in FROZEN_CATEGORIES else "REFRIGERATED"
        # 이미 나온 정규화명이면 먼저 들어온 것을 유지한다(정확 일치 유니크 제약 대비).
        seen.setdefault(normalized, (canonical, normalized, category, storage_type))

    print(f"카테고리 미확정으로 제외: {skipped_uncategorized}건, 정제 후 이름이 너무 짧아 제외: {skipped_short}건")
    return list(seen.values())


def escape_csv_field(value: str) -> str:
    if any(ch in value for ch in (',', '"', '\n')):
        return '"' + value.replace('"', '""') + '"'
    return value


def write_csv(rows: list[tuple[str, str, str, str]]) -> None:
    SEED_DATA_DIR.mkdir(exist_ok=True)
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="\n") as f:
        f.write("canonical_name,normalized_name,category,suggested_storage_type,verification_status,source_type,source_reference\n")
        for canonical, normalized, category, storage_type in rows:
            fields = [
                escape_csv_field(canonical),
                escape_csv_field(normalized),
                category,
                storage_type,
                "MODEL_INFERRED",
                SOURCE_TYPE,
                escape_csv_field(SOURCE_REFERENCE),
            ]
            f.write(",".join(fields) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="식품안전나라에서 받은 가공식품 DB 원본 xlsx 경로")
    args = parser.parse_args()

    rows = build_rows(args.input)
    write_csv(rows)
    print(f"{OUTPUT_CSV}에 {len(rows)}건 작성 완료")


if __name__ == "__main__":
    main()
