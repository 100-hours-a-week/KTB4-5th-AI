"""USDA FSIS FoodKeeper 데이터에서 한국 영수증에 자주 등장하는 식재료의
보관기간을 뽑아 shelf_life_rules INSERT SQL을 생성한다.

원본 데이터 출처: USDA FSIS FoodKeeper (https://catalog.data.gov/dataset/fsis-foodkeeper-data)
이 스크립트가 읽는 raw CSV(database/seed_data/foodkeeper_ingredients_raw.csv)는
GitHub 미러(https://github.com/jelera/food-shelflife-db)에서 내려받은 원본 그대로다.

이 스크립트는 DB에 아무것도 쓰지 않는다. database/seed_data/shelf_life_rules_seed.sql을
생성만 하며, 실제 적용은 사람이 내용을 검토한 뒤 psql로 직접 실행해야 한다.

app.domain.receipt_rules.storage_for_category()가 MEAT/SEAFOOD는 항상 FROZEN,
그 외 카테고리는 항상 REFRIGERATED로만 조회하므로, 각 식재료는 실제로 조회될
storage_type 하나만 채운다(둘 다 채우지 않음 — 조회되지 않는 행은 만들지 않는다).

reference_date_type은 전부 PURCHASE_DATE만 사용한다. 파이프라인이 아직
"개봉일"을 별도로 추적하지 않으므로 OPENED_DATE 기준 값(Refrigerate_After_Opening)은
지금 구조에서 정확히 계산할 수 없어 제외했다.
"""

import csv
from pathlib import Path

SEED_DATA_DIR = Path(__file__).parent / "seed_data"
RAW_CSV = SEED_DATA_DIR / "foodkeeper_ingredients_raw.csv"
OUTPUT_SQL = SEED_DATA_DIR / "shelf_life_rules_seed.sql"

SOURCE_NAME = "USDA FSIS FoodKeeper"
SOURCE_URL = "https://catalog.data.gov/dataset/fsis-foodkeeper-data"
# data.gov 카탈로그 페이지에 표시된 최종 갱신일. 정확한 일자를 재확인하려면
# 위 SOURCE_URL을 다시 확인해야 한다.
SOURCE_PUBLISHED_AT = "2025-01-22"

UNIT_TO_DAYS = {"Days": 1, "Weeks": 7, "Months": 30}

# (한글 정규화명, FoodKeeper Name, FoodKeeper Name_subtitle 또는 None, storage_type, 기간 컬럼 접두어)
# 기간 컬럼 접두어는 "DOP_Refrigerate" 또는 "DOP_Freeze" 중 하나이며 raw CSV의
# {접두어}_Min/{접두어}_Max/{접두어}_Metric 컬럼에서 값을 가져온다.
ENTRIES = [
    ("우유", "Milk", "ultra-pasteurized", "REFRIGERATED", "Refrigerate"),
    ("두부", "Tofu", None, "REFRIGERATED", "DOP_Refrigerate"),
    ("계란", "Eggs", "in shell", "REFRIGERATED", "DOP_Refrigerate"),
    ("애호박", "Zucchini", "fresh, whole", "REFRIGERATED", "Refrigerate"),
    ("대파", "Onions", "spring or green", "REFRIGERATED", "DOP_Refrigerate"),
    ("양파", "Onions", "yellow, white, red, etc.", "REFRIGERATED", "DOP_Refrigerate"),
    ("돼지고기", "Pork", "loin chops, boneless", "FROZEN", "DOP_Freeze"),
    ("닭고기", "Chicken", "whole", "FROZEN", "DOP_Freeze"),
    ("소고기", "Beef", "steaks", "FROZEN", "DOP_Freeze"),
    ("새우", "Shrimp, crayfish", None, "FROZEN", "DOP_Freeze"),
    ("양배추", "Cabbage", None, "REFRIGERATED", "DOP_Refrigerate"),
    ("오이", "Cucumbers", None, "REFRIGERATED", "DOP_Refrigerate"),
    ("마늘", "Garlic", None, "REFRIGERATED", "DOP_Refrigerate"),
    ("감자", "Potatoes", None, "REFRIGERATED", "DOP_Refrigerate"),
    ("당근", "Carrots, parsnips", None, "REFRIGERATED", "DOP_Refrigerate"),
    ("콩나물", "Bean sprouts", None, "REFRIGERATED", "DOP_Refrigerate"),
    ("요거트", "Yogurt", None, "REFRIGERATED", "DOP_Refrigerate"),
    ("버터", "Butter", None, "REFRIGERATED", "DOP_Refrigerate"),
    ("치즈", "Cheese", "hard such as cheddar, swiss, block parmesan", "REFRIGERATED", "DOP_Refrigerate"),
    ("식빵", "Bread", "homemade", "REFRIGERATED", "Refrigerate"),
    ("상추", "Lettuce", "iceberg, romaine", "REFRIGERATED", "DOP_Refrigerate"),
    ("시금치", "Lettuce", "leaf, spinach", "REFRIGERATED", "DOP_Refrigerate"),
    ("버섯", "Mushrooms", None, "REFRIGERATED", "DOP_Refrigerate"),
    ("햄", "Ham", "fully cooked, slices, half, or spiral cut", "REFRIGERATED", "DOP_Refrigerate"),
    ("소시지", "Sausage", "fully cooked smoked links, kielbasa", "REFRIGERATED", "DOP_Refrigerate"),
    ("베이컨", "Bacon", None, "REFRIGERATED", "DOP_Refrigerate"),
    ("딸기", "Strawberries", None, "REFRIGERATED", "DOP_Refrigerate"),
    ("사과", "Apples", None, "REFRIGERATED", "DOP_Refrigerate"),
    ("바나나", "Bananas", None, "REFRIGERATED", "Refrigerate"),
]


def load_rows() -> list[dict]:
    with RAW_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def find_row(rows: list[dict], name: str, subtitle: str | None) -> dict:
    candidates = [r for r in rows if r["Name"].strip() == name]
    if subtitle is None:
        matches = [r for r in candidates if not r["Name_subtitle"].strip()]
    else:
        matches = [r for r in candidates if r["Name_subtitle"].strip() == subtitle]
    if not matches:
        raise ValueError(f"FoodKeeper 행을 찾지 못함: Name={name!r} subtitle={subtitle!r}")
    if len(matches) > 1:
        raise ValueError(f"FoodKeeper 행이 여러 개 매칭됨: Name={name!r} subtitle={subtitle!r}")
    return matches[0]


def days(row: dict, prefix: str) -> tuple[int, int]:
    metric = row[f"{prefix}_Metric"].strip()
    unit = UNIT_TO_DAYS.get(metric)
    if unit is None:
        raise ValueError(f"환산 불가능한 단위: {metric!r} (행: {row['Name']} {row['Name_subtitle']})")
    minimum = int(row[f"{prefix}_Min"])
    maximum = int(row[f"{prefix}_Max"])
    return minimum * unit, maximum * unit


def escape(value: str) -> str:
    return value.replace("'", "''")


def build_sql() -> str:
    rows = load_rows()
    lines = [
        "-- USDA FSIS FoodKeeper 데이터 기반 보관기간 시드.",
        "-- database/seed_shelf_life_seed.py가 자동 생성함 — 직접 수정하지 말고 스크립트와 ENTRIES를 고칠 것.",
        "BEGIN;",
        "",
    ]
    for korean_name, fk_name, fk_subtitle, storage_type, prefix in ENTRIES:
        row = find_row(rows, fk_name, fk_subtitle)
        min_days, max_days = days(row, prefix)
        package_state = "UNOPENED"
        reference_date_type = "PURCHASE_DATE"
        label = f"{fk_name}" + (f" ({fk_subtitle})" if fk_subtitle else "")
        lines.append(f"-- {korean_name} <- FoodKeeper: {label}")
        lines.append(
            "INSERT INTO receipt_ai.shelf_life_rules "
            "(normalized_ingredient_name, storage_type, package_state, reference_date_type, "
            "duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at) "
            "VALUES ("
            f"'{escape(korean_name)}', '{storage_type}', '{package_state}', '{reference_date_type}', "
            f"{min_days}, {max_days}, "
            f"'{escape(SOURCE_NAME)}', '{escape(SOURCE_URL)}', '{SOURCE_PUBLISHED_AT}', CURRENT_TIMESTAMP"
            ");"
        )
        lines.append("")
    lines.append("COMMIT;")
    return "\n".join(lines)


def main() -> None:
    sql = build_sql()
    OUTPUT_SQL.write_text(sql, encoding="utf-8")
    print(f"{OUTPUT_SQL}에 {len(ENTRIES)}개 항목 작성 완료")


if __name__ == "__main__":
    main()
