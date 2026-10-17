-- 영수증에서 자주 등장하지만 기존 규칙에 없던 깻잎과 브로콜리의 냉장 보관 참고 범위를 추가한다.
-- 자연상태 농산물에는 국내 법정 소비기한이 없으므로 API에서는 확정일이 아니라
-- ESTIMATED_CONSUMPTION_WINDOW로만 반환한다.
--
-- 근거: USDA ARS Agriculture Handbook 66, The Commercial Storage of Fruits,
-- Vegetables, and Florist and Nursery Stocks.
--   - 일반 잎채소: 0~2°C에서 통상 10~14일
--   - 브로콜리: 0°C, RH 98~100%에서 2~3주
-- 가정용 냉장고의 온도·습도 편차가 있으므로 화면에는 추정 범위와 출처를 함께 표시한다.
BEGIN;

INSERT INTO receipt_ai.shelf_life_rules (
    normalized_ingredient_name, storage_type, package_state, reference_date_type,
    duration_min_days, duration_max_days, source_name, source_url, source_published_at,
    verified_at, fresh_only
)
SELECT v.name, 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', v.min_days, v.max_days,
       'USDA ARS Agriculture Handbook 66',
       'https://www.ars.usda.gov/ARSUserFiles/oc/np/CommercialStorage/CommercialStorage.pdf',
       NULL, CURRENT_TIMESTAMP, true
FROM (VALUES
    ('깻잎', 10, 14),
    ('브로콜리', 14, 21),
    ('브로커리', 14, 21)
) AS v(name, min_days, max_days)
WHERE NOT EXISTS (
    SELECT 1
    FROM receipt_ai.shelf_life_rules r
    WHERE r.is_active
      AND r.normalized_ingredient_name = v.name
      AND r.storage_type = 'REFRIGERATED'
);

COMMIT;
