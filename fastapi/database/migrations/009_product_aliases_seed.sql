-- product_aliases 첫 시드 — 실제로 반복 관찰된 OCR 오인식 2건.
--
-- 지금까지 별칭 테이블을 비워둔 건 "별칭이 필요한 실제 사례를 아직 못 봤다"는 이유였다. 이제
-- 근거가 생겼다: 영수증2(img_122)에서 OCR이 "브로콜리"를 "브로커리"로, "깻잎"을 "챗잎"으로
-- 계속 잘못 읽는다(113장·58장 배치에서 반복 재현, 005 주석에도 기록). "브로커리"는 4글자라
-- 길이 필터로 못 막는데, 카탈로그에 실존하는 정상 가공식품 "브로커리팩"(PROCESSED)과 유사도
-- 0.57로 매칭돼 VEGETABLE 대신 PROCESSED로 나왔다. 데이터가 틀린 게 아니라 OCR 오인식이라
-- 별칭이 맞는 도구다 — 정확 일치 단계에서 별칭이 표준명보다 먼저 조회되므로(app/adapters/
-- product_catalog.py의 _LOOKUP_QUERY priority 1) 유사도 폴백에 도달하기 전에 잡힌다.
--
-- 근거는 영수증 1장(같은 이미지가 여러 배치에서 반복)이라 넓게 추측하지 않고 실제 관찰된 2건만
-- 넣는다. 표준명은 이미 활성 행이 있는 브로콜리(005)·깻잎(005)을 조인해 가리킨다.
-- verification_status='MODEL_INFERRED'(사람 검수 없음, 003/005와 동일 신뢰 수준).
BEGIN;

INSERT INTO receipt_ai.product_aliases (product_id, alias_text, normalized_alias, verification_status)
SELECT pk.product_id, v.alias, v.alias, 'MODEL_INFERRED'
FROM (VALUES ('브로커리', '브로콜리'), ('챗잎', '깻잎')) AS v(alias, canonical)
JOIN receipt_ai.product_knowledge pk ON pk.normalized_name = v.canonical AND pk.is_active
ON CONFLICT (normalized_alias) WHERE is_active DO NOTHING;

COMMIT;
