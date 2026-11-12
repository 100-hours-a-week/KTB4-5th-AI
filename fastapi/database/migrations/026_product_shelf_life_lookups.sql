-- 상품명별 제조사 신고 소비기한 조회 결과를 저장한다. 같은 상품명을 다시 만나면 식품안전나라 API를 부르지 않는다.
--
-- 출처: 식품안전나라 Open API I1250 「식품(첨가물)품목제조보고」 POG_DAYCNT(소비기한, 자유 문자열)
-- https://www.foodsafetykorea.go.kr/api/openApiInfo.do?svc_no=I1250
--
-- duration_days가 NULL인 행은 "조회했지만 믿을 만한 값이 없음"(이름 불일치·값 불일치·해석 불가)이다.
-- 앱은 이 행도 재사용해 같은 상품명으로 API를 반복 호출하지 않고, fetched_at이 오래되면 다시 조회한다.
-- API 장애로 조회 자체를 못 한 경우는 저장하지 않는다.
--
-- 앱 배포 전에 적용하지 않아도 분석은 실패하지 않는다(테이블이 없으면 저장 없이 매번 조회).
-- dev 앱 계정은 CREATE 권한이 없어 소유자 계정으로 실행해야 한다.
BEGIN;

CREATE TABLE IF NOT EXISTS receipt_ai.product_shelf_life_lookups (
    query_name TEXT PRIMARY KEY CHECK (length(query_name) BETWEEN 2 AND 100),
    duration_days INTEGER CHECK (duration_days IS NULL OR duration_days BETWEEN 1 AND 3650),
    match_kind TEXT NOT NULL CHECK (match_kind IN ('EXACT', 'CONSENSUS', 'NONE')),
    matched_product_name TEXT,
    item_type TEXT,
    raw_text TEXT,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE receipt_ai.product_shelf_life_lookups IS
    '상품명별 식품안전나라 품목제조보고 소비기한 조회 캐시. 제조일 기준 일수이며 확정 소비기한이 아님';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'receipt_ai') THEN
        GRANT USAGE ON SCHEMA receipt_ai TO receipt_ai;
        GRANT SELECT, INSERT, UPDATE ON receipt_ai.product_shelf_life_lookups TO receipt_ai;
    END IF;
END $$;

COMMIT;
