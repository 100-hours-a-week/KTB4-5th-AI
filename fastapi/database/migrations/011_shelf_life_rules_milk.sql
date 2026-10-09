-- 흰우유 소비기한 추정을 국내 기준에 가깝게 바꾼다.
--
-- 기존 "우유" 행(001 시드, USDA FoodKeeper)은 Milk (ultra-pasteurized), 즉 멸균우유 값(30~90일)이라
-- 국내 일반 냉장 흰우유에 쓰기엔 너무 길었다. 최소값 규칙(pipeline._estimate_expiration)으로도
-- 구매일 + 30일이 나왔다.
--
-- 국내 기준 확인 결과(식약처 식품 유형별 소비기한 설정 보고서, 한국식품산업협회 참고값 검색
-- https://www.kfia.or.kr/kfia/sub.php?menukey=1513, 유가공품류):
--   식품유형        조건          기존 유통기한   소비기한 참고값
--   우유            멸균·실온      16주           140일    ← 멸균우유뿐, 냉장 흰우유 아님
--   강화우유        살균·냉장      16일           24일     (19-2-1-1)
--   강화우유        살균·냉장      15일           23일     (19-2-1-2)
--   유당분해우유    살균·냉장      16일           24일     (19-2-3-1)
-- 냉장 흰우유(우유류)는 소비기한 적용이 2031-01-01로 유예돼 자체 참고값이 없다. 원유·저지방우유를
-- 살균해 냉장 유통하는 점이 같은 강화우유·유당분해우유 값을 대리 기준으로 쓴다.
--
-- 날짜 환산은 010과 같다: min = 참고값 − 기존 유통기한(24 − 16 = 8), max = 참고값(24).
-- 추정은 항상 min만 쓰므로 실제 결과는 구매일 + 8일(가공유와 같은 값).
BEGIN;

UPDATE receipt_ai.shelf_life_rules
SET is_active = false
WHERE is_active
  AND source_name = 'USDA FSIS FoodKeeper'
  AND normalized_ingredient_name = '우유';

INSERT INTO receipt_ai.shelf_life_rules (
    normalized_ingredient_name, storage_type, package_state, reference_date_type,
    duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at
)
SELECT v.name, 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', v.min_days, v.max_days, v.source_name,
       'https://www.kfia.or.kr/kfia/sub.php?menukey=1513', NULL, CURRENT_TIMESTAMP
FROM (VALUES
    -- 흰우유: 자체 기준 없음 → 강화우유·유당분해우유 냉장 참고값을 대리 적용
    ('우유', 8, 24, '식약처 소비기한 참고값(강화우유·유당분해우유 대리 적용, 흰우유 자체 기준 없음)'),
    -- 강화우유 (16 → 24)
    ('강화우유', 8, 24, '식약처 식품유형별 소비기한 참고값'),
    ('칼슘우유', 8, 24, '식약처 식품유형별 소비기한 참고값'),
    -- 유당분해우유 (16 → 24)
    ('유당분해우유', 8, 24, '식약처 식품유형별 소비기한 참고값'),
    ('락토프리', 8, 24, '식약처 식품유형별 소비기한 참고값'),
    ('소화가잘되는우유', 8, 24, '식약처 식품유형별 소비기한 참고값')
) AS v(name, min_days, max_days, source_name)
WHERE NOT EXISTS (
    SELECT 1 FROM receipt_ai.shelf_life_rules r
    WHERE r.is_active AND r.normalized_ingredient_name = v.name AND r.storage_type = 'REFRIGERATED'
);

COMMIT;
