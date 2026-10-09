-- 소비기한 추정 규칙을 국내 기준(식약처 식품유형별 소비기한 참고값)으로 보강한다.
--
-- 배경: 기존 shelf_life_rules 29건은 전부 USDA FSIS FoodKeeper(미국) 기준이었다. 국내에는
-- 2023-01-01부터 「식품 등의 표시·광고에 관한 법률」에 따른 소비기한 표시제가 시행됐고,
-- 식약처가 식품유형별 "소비기한 참고값"을 공개했다. 가공식품은 이 값을 우선 쓴다.
--
-- 출처(원문 표 그대로): 식약처 식품유형별 소비기한 참고값, 서울지방식품의약품안전청 2023-01-19
-- https://www.mfds.go.kr/brd/m_827/view.do?seq=3616
--
--   식품유형    평균 유통기한(일)  소비기한 참고값(일)
--   가공유            16               24
--   과채주스          20               35
--   두부              17               23
--   발효유            18               32
--   빵류              20               31
--   소시지            39               56
--   어묵              29               42
--   유산균음료        18               26
--   프레스햄          43               66
--   햄                38               57
--
-- 날짜 환산: 참고값은 "제조일부터" 일수인데 영수증에는 제조일이 없고 구매일만 있다. 그래서
-- 구매일부터 남은 기간을 범위로 잡는다.
--   - 가장 늦게 산 경우: 판매 허용 기한(=유통기한) 마지막 날에 샀다면 남은 기간은
--     소비기한 − 유통기한 → duration_min_days
--   - 가장 일찍 산 경우: 제조 당일 샀다면 남은 기간은 소비기한 전체 → duration_max_days
--   예) 두부: 구매일 + 6일 ~ 구매일 + 23일. 원문 두 숫자만으로 도출한 보수적 범위다.
--
-- 법상 표시 대상이 아닌 것(여기서 다루지 않음):
--   - 계란을 제외한 자연상태식품(양파·무 같은 신선 농산물)은 소비기한 표시 대상이 아니라 국내
--     법정 기준값이 없다. 이런 품목은 기존 USDA 규칙을 계속 쓴다(아래 무 1건 추가).
--   - 맥주·장류·식초·절임류(장아찌 등)는 소비기한이 아니라 품질유지기한 표시 대상이다.
--   - 우유류(냉장 흰우유)는 2031-01-01부터 소비기한이 적용돼 아직 참고값이 없다. 가공유(맛우유 등)는
--     이미 적용 대상이다.
--
-- 이름 매칭은 app/adapters/shelf_life.py의 부분 문자열 규칙을 따른다(더 긴 이름 우선). 그래서
-- "초코우유"(4자)가 기존 "우유"(2자)보다, "마늘빅프랑크"의 "프랑크"(3자)가 "마늘"(2자)보다 먼저
-- 잡힌다. 1글자 이름("햄", "무")은 상품명 끝에 올 때만 매칭된다.
BEGIN;

-- 국내 기준이 생긴 품목의 USDA 규칙은 비활성화한다(삭제 아님 — 되돌릴 수 있게). 같은 이름이
-- 두 출처에 동시에 살아 있으면 어느 쪽이 걸릴지가 정렬 순서에 좌우되기 때문이다.
UPDATE receipt_ai.shelf_life_rules
SET is_active = false
WHERE is_active
  AND source_name = 'USDA FSIS FoodKeeper'
  AND normalized_ingredient_name IN ('두부', '햄', '소시지', '요거트', '식빵');

INSERT INTO receipt_ai.shelf_life_rules (
    normalized_ingredient_name, storage_type, package_state, reference_date_type,
    duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at
)
SELECT v.name, 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', v.min_days, v.max_days,
       '식약처 식품유형별 소비기한 참고값', 'https://www.mfds.go.kr/brd/m_827/view.do?seq=3616',
       DATE '2023-01-19', CURRENT_TIMESTAMP
FROM (VALUES
    -- 가공유 (16 → 24)
    ('가공유', 8, 24), ('맛우유', 8, 24), ('초코우유', 8, 24), ('딸기우유', 8, 24),
    ('바나나우유', 8, 24), ('커피우유', 8, 24),
    -- 과채주스 (20 → 35)
    ('주스', 15, 35), ('쥬스', 15, 35),
    -- 두부 (17 → 23)
    ('두부', 6, 23),
    -- 발효유 (18 → 32)
    ('발효유', 14, 32), ('요거트', 14, 32), ('요구르트', 14, 32),
    -- 빵류 (20 → 31) — "빵" 한 글자는 "빵가루" 등에 걸려 넣지 않았다
    ('식빵', 11, 31), ('모닝빵', 11, 31),
    -- 소시지 (39 → 56)
    ('소시지', 17, 56), ('소세지', 17, 56), ('프랑크', 17, 56), ('비엔나', 17, 56),
    -- 어묵 (29 → 42)
    ('어묵', 13, 42), ('오뎅', 13, 42),
    -- 유산균음료 (18 → 26)
    ('유산균음료', 8, 26),
    -- 프레스햄 (43 → 66)
    ('프레스햄', 23, 66),
    -- 햄 (38 → 57)
    ('햄', 19, 57)
) AS v(name, min_days, max_days)
WHERE NOT EXISTS (
    SELECT 1 FROM receipt_ai.shelf_life_rules r
    WHERE r.is_active AND r.normalized_ingredient_name = v.name AND r.storage_type = 'REFRIGERATED'
);

-- 무: 국내 법정 기준 없음(자연상태식품). 농촌진흥청 농사로 "무 - 가정에서의 보관방법"도 보관법만
-- 있고 일수는 없다. 그래서 기존과 같은 USDA FoodKeeper의 Radishes(냉장, 구매일 기준 10~14일)를
-- 쓴다 — 적환무 기준이라 한국 무와 품종이 다르다는 점은 한계로 남긴다.
INSERT INTO receipt_ai.shelf_life_rules (
    normalized_ingredient_name, storage_type, package_state, reference_date_type,
    duration_min_days, duration_max_days, source_name, source_url, source_published_at, verified_at
)
SELECT '무', 'REFRIGERATED', 'UNOPENED', 'PURCHASE_DATE', 10, 14,
       'USDA FSIS FoodKeeper', 'https://catalog.data.gov/dataset/fsis-foodkeeper-data',
       DATE '2025-01-22', CURRENT_TIMESTAMP
WHERE NOT EXISTS (
    SELECT 1 FROM receipt_ai.shelf_life_rules r
    WHERE r.is_active AND r.normalized_ingredient_name = '무' AND r.storage_type = 'REFRIGERATED'
);

COMMIT;
