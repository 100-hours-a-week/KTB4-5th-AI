-- category_source에 CATALOG_FUZZY를 허용한다.
-- product_knowledge 정확 일치가 실패했을 때 pg_trgm 유사도로 근접 매칭된 카테고리를
-- CATALOG(정확 일치)와 구분해서 표시하기 위한 값이다. receipt_analysis_items는 아직
-- 실제로 쓰이지 않지만(인메모리만 사용) 앱 레벨 Source enum과 어긋나지 않게 스키마를 맞춰둔다.
BEGIN;

ALTER TABLE receipt_ai.receipt_analysis_items
    DROP CONSTRAINT ck_receipt_items_sources;

ALTER TABLE receipt_ai.receipt_analysis_items
    ADD CONSTRAINT ck_receipt_items_sources CHECK (
        name_source IN ('OCR', 'RECEIPT_LINE', 'CATALOG', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED')
        AND category_source IN ('OCR', 'RECEIPT_LINE', 'CATALOG', 'CATALOG_FUZZY', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED')
        AND weight_source IN ('OCR', 'RECEIPT_LINE', 'CATALOG', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED')
        AND (quantity_source IS NULL OR quantity_source IN ('OCR', 'RECEIPT_LINE', 'CATALOG', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED'))
        AND (expiration_source IS NULL OR expiration_source IN ('OCR', 'RECEIPT_LINE', 'CATALOG', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED', 'KNOWLEDGE_ESTIMATE'))
    );

COMMIT;
