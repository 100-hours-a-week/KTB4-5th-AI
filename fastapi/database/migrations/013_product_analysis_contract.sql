BEGIN;

-- 실물 상품 분석 결과를 이력 테이블에 저장할 수 있도록 API의 PRODUCT 유형과 출처 enum을 맞춘다.
ALTER TABLE receipt_ai.receipt_analyses
    DROP CONSTRAINT ck_receipt_analyses_document_type,
    ADD CONSTRAINT ck_receipt_analyses_document_type
        CHECK (document_type IS NULL OR document_type IN ('RECEIPT', 'PRODUCT', 'OTHER'));

ALTER TABLE receipt_ai.receipt_analysis_items
    DROP CONSTRAINT ck_receipt_items_sources,
    ADD CONSTRAINT ck_receipt_items_sources CHECK (
        name_source IN ('OCR', 'BARCODE', 'VISION', 'RECEIPT_LINE', 'CATALOG', 'CATALOG_FUZZY', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED')
        AND category_source IN ('OCR', 'BARCODE', 'VISION', 'RECEIPT_LINE', 'CATALOG', 'CATALOG_FUZZY', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED')
        AND weight_source IN ('OCR', 'BARCODE', 'VISION', 'RECEIPT_LINE', 'CATALOG', 'CATALOG_FUZZY', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED')
        AND (quantity_source IS NULL OR quantity_source IN ('OCR', 'BARCODE', 'VISION', 'RECEIPT_LINE', 'CATALOG', 'CATALOG_FUZZY', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED'))
        AND (expiration_source IS NULL OR expiration_source IN ('OCR', 'BARCODE', 'VISION', 'RECEIPT_LINE', 'CATALOG', 'CATALOG_FUZZY', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED', 'KNOWLEDGE_ESTIMATE'))
    );

COMMIT;
