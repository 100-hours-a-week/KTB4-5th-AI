-- 신선식품 보관기간 규칙이 이름만 겹치는 가공식품에 적용되지 않게 한다.
--
-- 문제: shelf_life_rules는 부분 문자열로 매칭한다(app/adapters/shelf_life.py). 그래서 과자 "감자깡"(OCR
-- "감자암")이 USDA "감자"(냉장 7~14일) 규칙에 걸려 소비기한 추정이 붙었다(2026-09-22 실험 (14)). 같은
-- 이유로 "사과주스"가 "사과", "양파링"이 "양파" 규칙에 걸릴 수 있다.
--
-- 조치: fresh_only 컬럼을 추가하고, 원물 그대로의 신선식품 규칙(채소·과일·육류·수산)에만 true를 둔다.
-- 조회 시 상품 분류가 VEGETABLE·FRUIT·MEAT·SEAFOOD일 때만 이 규칙을 쓴다(pipeline._FRESH_CATEGORIES).
-- 가공식품 기준(식약처 참고값: 두부·햄·소시지·우유·요거트 등)과 계란은 false로 둬서 분류와 관계없이 쓴다
-- — 계란은 Gemma가 ETC·DAIRY 등으로 분류하는 경우가 있어 막으면 추정을 잃는다.
BEGIN;

ALTER TABLE receipt_ai.shelf_life_rules
    ADD COLUMN IF NOT EXISTS fresh_only boolean NOT NULL DEFAULT false;

UPDATE receipt_ai.shelf_life_rules
SET fresh_only = true
WHERE source_name = 'USDA FSIS FoodKeeper'
  AND normalized_ingredient_name IN (
      '애호박', '대파', '양파', '양배추', '오이', '마늘', '감자', '당근', '콩나물', '상추', '시금치', '버섯', '무',
      '딸기', '사과', '바나나',
      '돼지고기', '닭고기', '소고기', '새우'
  );

COMMIT;
