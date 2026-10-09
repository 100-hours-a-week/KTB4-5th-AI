-- product_knowledge 초기 시드.
-- verification_status는 전부 'MODEL_INFERRED'다 — 사람이 화면에서 확인 버튼을 누른 적이 없다는
-- 뜻을 정직하게 남겨둔다. 다만 known_food_category()의 정규식 키워드 규칙도 지금까지 같은
-- 수준(개발자가 정한 값, 별도 인증 없음)의 신뢰로 Gemma를 건너뛰는 데 쓰여왔으므로, 이 표의
-- MODEL_INFERRED 항목도 동일한 신뢰 수준으로 취급한다. USER_CONFIRMED/ADMIN_CONFIRMED로
-- 올리려면 실제 검수 화면이 있어야 하며 이번 범위에 포함하지 않는다.
--
-- 1~29번은 카테고리가 명확한 식재료 일반명이라 shelf_life_rules 시드와 동일한 목록을 재사용했다.
-- 30~31번은 이번 세션에서 실제 GS25 영수증 테스트로 확인한 OCR 원문이다(app/domain/receipt_rules.py의
-- known_food_category 키워드 목록에 없어 Gemma가 놓쳤던 항목). "마운틴클래식/마운틴틀러스트"는
-- 정확한 정체를 확신할 수 없어 이번 시드에서 제외했다 — 잘못된 카테고리를 추측해 넣지 않는다.
BEGIN;

INSERT INTO receipt_ai.product_knowledge
  (canonical_name, normalized_name, category, suggested_storage_type, verification_status, source_type, source_reference)
VALUES
  ('우유', '우유', 'DAIRY', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('두부', '두부', 'TOFU_BEAN', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('계란', '계란', 'ETC', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('애호박', '애호박', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('대파', '대파', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('양파', '양파', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('돼지고기', '돼지고기', 'MEAT', 'FROZEN', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('닭고기', '닭고기', 'MEAT', 'FROZEN', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('소고기', '소고기', 'MEAT', 'FROZEN', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('새우', '새우', 'SEAFOOD', 'FROZEN', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('양배추', '양배추', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('오이', '오이', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('마늘', '마늘', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('감자', '감자', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('당근', '당근', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('콩나물', '콩나물', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('요거트', '요거트', 'DAIRY', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('버터', '버터', 'DAIRY', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('치즈', '치즈', 'DAIRY', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('식빵', '식빵', 'GRAIN_NOODLE', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('상추', '상추', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('시금치', '시금치', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('버섯', '버섯', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('햄', '햄', 'PROCESSED', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('소시지', '소시지', 'PROCESSED', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('베이컨', '베이컨', 'PROCESSED', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('딸기', '딸기', 'FRUIT', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('사과', '사과', 'FRUIT', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('바나나', '바나나', 'FRUIT', 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION', '일반 식재료명'),
  ('마늘빅프랑크', '마늘빅프랑크', 'PROCESSED', 'REFRIGERATED', 'MODEL_INFERRED', 'SESSION_BENCHMARK', 'GS25 영수증 테스트에서 Gemma가 놓친 실제 OCR 원문'),
  ('예거라들러레몬', '예거라들러레몬', 'BEVERAGE', 'REFRIGERATED', 'MODEL_INFERRED', 'SESSION_BENCHMARK', 'GS25 영수증 테스트에서 Gemma가 놓친 실제 OCR 원문, 카테고리 확신도는 상대적으로 낮음');

COMMIT;
