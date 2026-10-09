-- product_knowledge 4차 시드 — 흔한 생과일·생채소·곡류 이름의 정확 일치 큐레이션 46건.
--
-- 왜 필요한가: "수박"처럼 짧은 생과일·생채소는 007에서 오답 행을 비활성화한 뒤 정확 일치 행이
-- 없었고, 유사도 폴백은 이름에 그 단어가 들어간 가공식품("퓨어 수박"=BEVERAGE, "성주 참외"=
-- PROCESSED, "복숭아 잼"=PROCESSED)을 끌어왔다. 질의문이 3글자 이하면 유사도 폴백을 건너뛰게
-- 바꾼 뒤에는(app/adapters/product_catalog.py) Gemma로 넘어가는데, 상품행 복원이 실패한 영수증은
-- 가격 힌트가 없어 Gemma가 이런 짧은 이름을 제외했다(img_081 "수박" 실측). 정확 일치 행은 힌트나
-- Gemma에 의존하지 않고 결정적으로 잡히므로 003/005의 "무"·"깻잎"과 같은 방식으로 등록한다.
--
-- 카테고리는 추측이 아니라 이름 자체로 명백한 것만 골랐다(과일=FRUIT, 채소=VEGETABLE, 쌀=
-- GRAIN_NOODLE, 콩=TOFU_BEAN). 견과·꿀처럼 앱 카테고리에 딱 맞는 게 없는 이름("밤", "꿀")은
-- 뺐다. 이미 활성 행이 있는 사과·딸기·바나나·감자·대파·시금치·양배추는 겹치지 않게 제외했고,
-- ON CONFLICT DO NOTHING으로 이중 안전장치를 뒀다. 007에서 잘못된 카테고리로 비활성화한
-- 이름(수박·레몬·망고·멜론·복숭아·오렌지·키위·토마토 등)은 여기서 올바른 카테고리로 다시 등록된다.
-- 전부 verification_status='MODEL_INFERRED'(003/005와 동일 신뢰 수준, 사람 검수 없음).
BEGIN;

INSERT INTO receipt_ai.product_knowledge
  (canonical_name, normalized_name, category, suggested_storage_type, verification_status, source_type, source_reference)
SELECT n, n, c, 'REFRIGERATED', 'MODEL_INFERRED', 'INTERNAL_CURATION',
       '흔한 생과일·생채소·곡류 정확 일치 큐레이션(짧은 이름의 유사도 오탐·힌트 의존 방지)'
FROM (VALUES
  ('수박','FRUIT'), ('참외','FRUIT'), ('포도','FRUIT'), ('청포도','FRUIT'), ('복숭아','FRUIT'),
  ('자두','FRUIT'), ('살구','FRUIT'), ('귤','FRUIT'), ('감','FRUIT'), ('홍시','FRUIT'),
  ('배','FRUIT'), ('키위','FRUIT'), ('망고','FRUIT'), ('멜론','FRUIT'), ('오렌지','FRUIT'),
  ('레몬','FRUIT'), ('자몽','FRUIT'), ('한라봉','FRUIT'), ('블루베리','FRUIT'), ('라즈베리','FRUIT'),
  ('크랜베리','FRUIT'), ('체리','FRUIT'), ('무화과','FRUIT'), ('석류','FRUIT'), ('아보카도','FRUIT'),
  ('파인애플','FRUIT'), ('매실','FRUIT'), ('두리안','FRUIT'), ('구아바','FRUIT'), ('대추','FRUIT'),
  ('토마토','VEGETABLE'), ('방울토마토','VEGETABLE'), ('파','VEGETABLE'), ('쪽파','VEGETABLE'),
  ('고추','VEGETABLE'), ('청양고추','VEGETABLE'), ('가지','VEGETABLE'), ('생강','VEGETABLE'),
  ('단호박','VEGETABLE'), ('늙은호박','VEGETABLE'), ('취나물','VEGETABLE'), ('고구마','VEGETABLE'),
  ('배추','VEGETABLE'), ('부추','VEGETABLE'),
  ('쌀','GRAIN_NOODLE'), ('콩','TOFU_BEAN')
) AS v(n, c)
ON CONFLICT (normalized_name, (COALESCE(barcode, ''))) WHERE is_active DO NOTHING;

COMMIT;
