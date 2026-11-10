-- 실물·영수증에서 자주 나오는 가공식품 중 규칙이 없어 소비기한 예상 범위가 비던 식품유형을 보강한다.
--
-- 배경: 2026-11-10 운영 확인에서 상품 사진·영수증 항목 대부분이 EXPIRATION_NOT_FOUND만 받고
-- estimatedRange가 비었다. shelf_life_rules에 신선식품·일부 가공식품 30여 건만 있었기 때문이다.
--
-- 출처(원문 표 그대로): 식약처 보도자료 "식품유형별 소비기한 참고값" 2022-12-21, 정책브리핑 게재
-- https://www.korea.kr/news/policyNewsView.do?newsId=148909080
--
--   식품유형        평균 유통기한(일)  소비기한 참고값(일)
--   과자                  45               81
--   과채음료              11               20
--   생면                  35               42
--   묵류                  16               19
--   베이컨류              25               28
--   간편조리세트           6                8
--   전란액                 3                4
--
-- 날짜 환산은 010과 같다(구매일 기준): min = 소비기한 − 유통기한(판매 마지막 날 구매),
-- max = 소비기한 전체(제조 당일 구매). 예) 과자: 구매일 + 36일 ~ 구매일 + 81일.
--
-- 넣지 않은 것과 이유:
--   - 유탕면(라면)·주류·먹는샘물: 공식 원문에서 일수를 확인하지 못했다. 주류는 소비기한 표시 대상이
--     아니다. 이런 상품은 포장에 찍힌 날짜를 사용자가 입력한다.
--   - 신선편의식품(샐러드 6→8): "샐러드"가 "샐러드유"·"샐러드드레싱"에도 부분 일치해 넣지 않았다.
--   - 냉동 소시지: 식약처 참고값은 냉장 기준뿐이다.
--
-- 이름 매칭은 app/adapters/shelf_life.py 규칙을 따른다. 2자 이상은 부분 문자열, 1자("칩", "묵")는
-- 상품명 끝에 올 때만 맞는다("감자칩", "도토리묵"). fresh_only=false라 분류와 관계없이 쓴다.
-- "생생감자칩"처럼 "감자"가 들어간 과자는 감자 규칙이 fresh_only라 가공식품 분류에서는 제외되고 "칩"이 맞는다.
BEGIN;

-- 베이컨은 국내 기준(베이컨류)이 생겨 USDA 규칙(7일)을 비활성화한다. 010과 같은 방식(삭제 아님).
UPDATE receipt_ai.shelf_life_rules
SET is_active = false
WHERE is_active
  AND source_name = 'USDA FSIS FoodKeeper'
  AND normalized_ingredient_name = '베이컨';

INSERT INTO receipt_ai.shelf_life_rules (
    normalized_ingredient_name, storage_type, package_state, reference_date_type,
    duration_min_days, duration_max_days, source_name, source_url, source_published_at,
    verified_at, fresh_only
)
SELECT v.name, 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', v.min_days, v.max_days,
       '식약처 식품유형별 소비기한 참고값', 'https://www.korea.kr/news/policyNewsView.do?newsId=148909080',
       DATE '2022-12-21', CURRENT_TIMESTAMP, false
FROM (VALUES
    -- 과자 (45 → 81)
    ('과자', 36, 81), ('스낵', 36, 81), ('쿠키', 36, 81), ('크래커', 36, 81), ('비스킷', 36, 81), ('칩', 36, 81),
    -- 과채음료 (11 → 20)
    ('과채음료', 9, 20), ('과즙음료', 9, 20),
    -- 생면 (35 → 42)
    ('생면', 7, 42), ('생우동', 7, 42), ('생칼국수', 7, 42),
    -- 묵류 (16 → 19)
    ('묵', 3, 19),
    -- 베이컨류 (25 → 28)
    ('베이컨', 3, 28),
    -- 간편조리세트 (6 → 8)
    ('밀키트', 2, 8),
    -- 전란액 (3 → 4)
    ('전란액', 1, 4)
) AS v(name, min_days, max_days)
WHERE NOT EXISTS (
    SELECT 1 FROM receipt_ai.shelf_life_rules r
    WHERE r.is_active AND r.normalized_ingredient_name = v.name AND r.storage_type = 'REFRIGERATED'
);

COMMIT;
