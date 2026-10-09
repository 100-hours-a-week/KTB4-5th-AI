-- 다중 실물 상품 실험에서 이미지와 OCR로 직접 확인한 상품명 및 반복 오인식 별칭.
BEGIN;

INSERT INTO receipt_ai.product_knowledge
  (canonical_name, normalized_name, category, suggested_storage_type, verification_status, source_type, source_reference)
VALUES
  ('생생감자칩', '생생감자칩', 'PROCESSED', 'REFRIGERATED', 'MODEL_INFERRED', 'SESSION_BENCHMARK', '2026-10-01 다중 실물 상품 이미지'),
  ('가루비 감자원 오리지널', '가루비감자원오리지널', 'PROCESSED', 'REFRIGERATED', 'MODEL_INFERRED', 'SESSION_BENCHMARK', '2026-10-01 다중 실물 상품 이미지'),
  ('노브랜드 커스터드', '노브랜드커스터드', 'PROCESSED', 'REFRIGERATED', 'MODEL_INFERRED', 'SESSION_BENCHMARK', '2026-10-01 다중 실물 상품 이미지'),
  ('오리온 카스타드', '오리온카스타드', 'PROCESSED', 'REFRIGERATED', 'MODEL_INFERRED', 'SESSION_BENCHMARK', '2026-10-01 다중 실물 상품 이미지'),
  ('컵누들 우동맛', '컵누들우동맛', 'PROCESSED', 'REFRIGERATED', 'MODEL_INFERRED', 'SESSION_BENCHMARK', '2026-10-01 다중 실물 상품 이미지'),
  ('크랩마스터', '크랩마스터', 'SEAFOOD', 'REFRIGERATED', 'MODEL_INFERRED', 'SESSION_BENCHMARK', '2026-10-01 다중 실물 상품 이미지'),
  ('팔도 비빔면', '팔도비빔면', 'PROCESSED', 'REFRIGERATED', 'MODEL_INFERRED', 'SESSION_BENCHMARK', '2026-10-01 다중 실물 상품 이미지'),
  ('오뚜기 진비빔면', '오뚜기진비빔면', 'PROCESSED', 'REFRIGERATED', 'MODEL_INFERRED', 'SESSION_BENCHMARK', '2026-10-01 다중 실물 상품 이미지')
ON CONFLICT DO NOTHING;

INSERT INTO receipt_ai.product_aliases (product_id, alias_text, normalized_alias, verification_status)
SELECT pk.product_id, v.alias, v.normalized_alias, 'MODEL_INFERRED'
FROM (VALUES
  ('성생감자칩', '성생감자칩', '생생감자칩'),
  ('가루바감자원', '가루바감자원', '가루비감자원오리지널'),
  ('컵누드', '컵누드', '컵누들우동맛'),
  ('비방면', '비방면', '팔도비빔면'),
  ('비범면', '비범면', '오뚜기진비빔면'),
  ('진비범면', '진비범면', '오뚜기진비빔면')
) AS v(alias, normalized_alias, canonical)
JOIN receipt_ai.product_knowledge pk ON pk.normalized_name = v.canonical AND pk.is_active
ON CONFLICT (normalized_alias) WHERE is_active DO NOTHING;

COMMIT;
