-- product_knowledge 2차 시드.
-- 이번 세션에서 영수증2.jpg를 실제로 분석해보니, Gemma가 "양파"/"무"/"깻잎"/"브로콜리"처럼
-- 아주 흔한 채소명조차 0/5로 놓쳤다(상품번호 뒤 단일문자 코드 "P"가 상품명 앞에 그대로
-- 붙어 있었던 게 원인 중 하나였고, 그건 app/domain/receipt_rules.py의
-- _strip_product_type_code()로 별도 수정했다). "양파"는 003에 이미 있어 여기서는 뺐고,
-- 코드를 뗀 뒤에도 애초에 known_food_category() 정규식 목록에 없던 나머지 3건만 채운다.
-- "무"는 한 글자라 정규식 substring 매칭은 다른 단어(무료/무이자 등)와 오탐 위험이 커서
-- 정확 일치만 쓰는 product_knowledge에 넣었다 — 나머지 2건도 일관성을 위해 같이 넣었다.
-- 전부 verification_status='MODEL_INFERRED'(003과 동일 신뢰 수준, 사람 검수 없음).
BEGIN;

INSERT INTO receipt_ai.product_knowledge
  (canonical_name, normalized_name, category, suggested_storage_type, verification_status, source_type, source_reference)
VALUES
  ('무', '무', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'SESSION_BENCHMARK', '영수증2 테스트에서 Gemma가 놓친 일반 채소명'),
  ('깻잎', '깻잎', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'SESSION_BENCHMARK', '영수증2 테스트에서 Gemma가 놓친 일반 채소명(OCR 원문은 "챗잎" 오인식)'),
  ('브로콜리', '브로콜리', 'VEGETABLE', 'REFRIGERATED', 'MODEL_INFERRED', 'SESSION_BENCHMARK', '영수증2 테스트에서 Gemma가 놓친 일반 채소명(OCR 원문은 "브로커리" 오인식 — pg_trgm 유사도 폴백으로 매칭됨)');

COMMIT;
