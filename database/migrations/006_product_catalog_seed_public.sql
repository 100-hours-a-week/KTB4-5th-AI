-- product_knowledge 3차 시드 — 식품안전나라 K-FIND 가공식품 DB(공개 데이터셋) 대량 적재.
-- database/seed_product_catalog_public.py가 database/seed_data/product_catalog_public_seed.csv를
-- 생성한다(238,817건). 대분류(절임류/조림류는 중분류)로 카테고리를 한 갈래로 확신할 수 있는
-- 항목만 골랐고, 애매한 대분류(농산가공식품류·조림류·특수영양식품 등, 약 3.4만 건)는 근거
-- 없이 카테고리를 추측해 넣지 않는다는 원칙에 따라 제외했다. 전부 verification_status=
-- 'MODEL_INFERRED'다(사람 검수 없음 — 003/005와 동일한 신뢰 수준으로 취급).
--
-- 003/005에서 이미 넣은 34건과 정규화명이 겹칠 수 있어(예: "우유") ON CONFLICT DO NOTHING으로
-- 기존 행을 덮어쓰지 않는다. \copy는 psql 클라이언트가 실행하는 파일 시스템 기준이므로,
-- 이 파일을 psql -f로 실행하는 디렉터리(예: ~/dameokja)에서 실행해야 상대경로가 맞는다.
BEGIN;

CREATE TEMP TABLE tmp_product_catalog_seed (
    canonical_name VARCHAR(255),
    normalized_name VARCHAR(255),
    category VARCHAR(20),
    suggested_storage_type VARCHAR(20),
    verification_status VARCHAR(20),
    source_type VARCHAR(24),
    source_reference VARCHAR(2048)
);

\copy tmp_product_catalog_seed FROM 'database/seed_data/product_catalog_public_seed.csv' WITH (FORMAT csv, HEADER true)

INSERT INTO receipt_ai.product_knowledge
    (canonical_name, normalized_name, category, suggested_storage_type, verification_status, source_type, source_reference)
SELECT canonical_name, normalized_name, category, suggested_storage_type, verification_status, source_type, source_reference
FROM tmp_product_catalog_seed
ON CONFLICT (normalized_name, (COALESCE(barcode, ''))) WHERE is_active DO NOTHING;

COMMIT;
