-- 날짜 용어를 소비기한으로 통일한다. 과거 SELL_BY로 저장된 분석 결과는 USE_BY로 변환하고,
-- 이후 신규 결과에는 USE_BY·예상 소비기한·미상만 허용한다.
UPDATE receipt_ai.receipt_analysis_items
SET expiration_date_type = 'USE_BY'
WHERE expiration_date_type = 'SELL_BY';

ALTER TABLE receipt_ai.receipt_analysis_items
    DROP CONSTRAINT IF EXISTS ck_receipt_items_expiration_type;

ALTER TABLE receipt_ai.receipt_analysis_items
    ADD CONSTRAINT ck_receipt_items_expiration_type CHECK (
        expiration_date_type IN ('USE_BY', 'UNKNOWN', 'ESTIMATED_CONSUMPTION_WINDOW')
    );
