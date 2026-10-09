-- 식재료 카테고리 코드를 백엔드 API 명세(KTB4-5th-wiki "API 명세" 6. 카테고리 전체 목록)와 통일한다.
--   GRAIN_NOODLE → GRAINS_NOODLE
--   PROCESSED    → PROCESSED_FOOD
--   ETC          → OTHER
-- 001~015는 이미 적용된 이력이라 수정하지 않는다. 새로 DB를 만들 때도 001~015가 기존 코드로 적재한 뒤
-- 이 마이그레이션이 한 번에 바꾼다. 그래서 database/seed_product_catalog_public.py가 만드는 시드 CSV(006용)는
-- 기존 코드를 유지한다. 앱(app/schemas.py Category)은 이 마이그레이션 이후의 코드만 읽을 수 있으므로
-- 반드시 앱 배포 전에 적용한다.
BEGIN;

ALTER TABLE receipt_ai.product_knowledge
    DROP CONSTRAINT ck_product_knowledge_category;

UPDATE receipt_ai.product_knowledge
SET category = CASE category
        WHEN 'GRAIN_NOODLE' THEN 'GRAINS_NOODLE'
        WHEN 'PROCESSED' THEN 'PROCESSED_FOOD'
        WHEN 'ETC' THEN 'OTHER'
    END
WHERE category IN ('GRAIN_NOODLE', 'PROCESSED', 'ETC');

ALTER TABLE receipt_ai.product_knowledge
    ADD CONSTRAINT ck_product_knowledge_category CHECK (
        category IN ('VEGETABLE', 'FRUIT', 'MEAT', 'SEAFOOD', 'DAIRY', 'TOFU_BEAN', 'GRAINS_NOODLE', 'PROCESSED_FOOD', 'SEASONING', 'BEVERAGE', 'OTHER')
    );

-- 분석 이력 테이블은 현재 앱이 쓰지 않지만 API enum과 어긋나지 않게 함께 맞춘다.
ALTER TABLE receipt_ai.receipt_analysis_items
    DROP CONSTRAINT ck_receipt_items_category;

UPDATE receipt_ai.receipt_analysis_items
SET category = CASE category
        WHEN 'GRAIN_NOODLE' THEN 'GRAINS_NOODLE'
        WHEN 'PROCESSED' THEN 'PROCESSED_FOOD'
        WHEN 'ETC' THEN 'OTHER'
    END
WHERE category IN ('GRAIN_NOODLE', 'PROCESSED', 'ETC');

ALTER TABLE receipt_ai.receipt_analysis_items
    ADD CONSTRAINT ck_receipt_items_category CHECK (
        category IN ('VEGETABLE', 'FRUIT', 'MEAT', 'SEAFOOD', 'DAIRY', 'TOFU_BEAN', 'GRAINS_NOODLE', 'PROCESSED_FOOD', 'SEASONING', 'BEVERAGE', 'OTHER')
    );

COMMIT;
