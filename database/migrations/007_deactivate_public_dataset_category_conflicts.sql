-- 006에서 넣은 대량 시드(식품안전나라 K-FIND 가공식품 DB) 중 카테고리를 신뢰할 수 없는
-- 항목 831건을 비활성화한다(삭제가 아니라 is_active=false — 원본 raw 데이터 근거를
-- 남겨두고 복구 가능하게 함). 데이터 자체에서 기계적으로 도출한 두 가지 근거만 사용했고,
-- 카테고리를 추측해서 채우지 않는다는 원칙을 그대로 따른다.
--
-- 1) 정규화명 충돌(745건): database/seed_product_catalog_public.py가 읽는 원본
--    31만 행 안에서 같은 정규화명이 서로 다른 대분류(카테고리)로 동시에 등장하는 경우.
--    예: "갈비탕"은 MEAT 15건·PROCESSED 19건으로 원본 데이터 자체가 카테고리에 합의하지
--    못한다. 006은 이런 경우 먼저 나온 행을 그냥 채택했는데, 이는 사실상 엑셀 파일 내
--    등장 순서로 정해진 것과 같다.
-- 2) 흔한 생과일·생채소 원물명(96건, 위 745건과 중복 제외): "수박"처럼 원본 데이터에서는
--    카테고리가 하나로만 나오지만(정규화명 충돌이 아님), 그 하나가 구조적으로 틀렸다.
--    식품안전나라 "가공식품 DB"는 애초에 생과일·생채소 카테고리를 다루지 않는 가공식품
--    전용 데이터셋이라, "수박"이라는 이름의 가공식품(수박맛 빙과류 등)이 우연히 하나
--    있으면 그게 실제 생수박의 정규화명과 충돌한다. 흔한 생과일·채소 98개를 검증해
--    31개가 이 문제였고(2개는 우연히 정상 — 깐마늘·죽순, 목록에서 제외), 검증하지 않은
--    나머지는 안전하게 걸러지도록 목록에 포함했다(실제 DB에 없으면 no-op).
--
-- database/seed_data/public_dataset_category_exclusions.csv는 로컬 원본 엑셀(31만 행,
-- 저장소에 커밋 안 함)을 다시 읽어 기계적으로 생성했다 — 재현 방법은
-- docs/database.md 참고.
BEGIN;

CREATE TEMP TABLE tmp_category_exclusions (
    normalized_name VARCHAR(255)
);

\copy tmp_category_exclusions FROM 'database/seed_data/public_dataset_category_exclusions.csv' WITH (FORMAT csv, HEADER true)

UPDATE receipt_ai.product_knowledge pk
SET is_active = false
FROM tmp_category_exclusions ex
WHERE pk.normalized_name = ex.normalized_name
  AND pk.source_type = 'PUBLIC_DATASET'
  AND pk.is_active;

COMMIT;
