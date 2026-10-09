-- 001_shelf_life_and_product_knowledge.sql가 먼저 적용되어 receipt_ai 스키마와
-- receipt_ai.set_updated_at()이 이미 존재한다고 가정한다.
BEGIN;

-- 분석 요청, 처리 상태와 최종 응답 스냅샷을 보관한다. 작업 큐 자체는 FastAPI 메모리에 둔다.
CREATE TABLE receipt_ai.receipt_analyses (
    receipt_analysis_pk BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    analysis_id VARCHAR(64) NOT NULL UNIQUE,
    request_id VARCHAR(100) NOT NULL UNIQUE,
    image_object_key VARCHAR(512) NOT NULL,
    image_sha256 CHAR(64) NOT NULL,
    input_hint VARCHAR(16) NOT NULL DEFAULT 'AUTO',
    locale VARCHAR(16) NOT NULL DEFAULT 'ko-KR',
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Seoul',
    status VARCHAR(16) NOT NULL,
    stage VARCHAR(20),
    document_type VARCHAR(16),
    document_confidence NUMERIC(5, 4),
    image_quality_score NUMERIC(5, 4),
    image_quality_issues JSONB NOT NULL DEFAULT '[]'::JSONB,
    pipeline_version VARCHAR(64),
    ocr_version VARCHAR(64),
    llm_model VARCHAR(128),
    normalizer_version VARCHAR(64),
    policy_version VARCHAR(64),
    final_payload JSONB,
    error_code VARCHAR(64),
    error_message VARCHAR(500),
    error_retryable BOOLEAN,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    purge_after TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_receipt_analyses_sha256 CHECK (image_sha256 ~ '^[0-9a-fA-F]{64}$'),
    CONSTRAINT ck_receipt_analyses_input_hint CHECK (input_hint IN ('AUTO', 'RECEIPT', 'PRODUCT')),
    CONSTRAINT ck_receipt_analyses_status CHECK (status IN ('QUEUED', 'PROCESSING', 'COMPLETED', 'FAILED')),
    CONSTRAINT ck_receipt_analyses_stage CHECK (
        stage IS NULL OR stage IN ('QUEUED', 'CLASSIFYING', 'EXTRACTING', 'ENRICHING', 'VALIDATING', 'COMPLETED', 'FAILED')
    ),
    CONSTRAINT ck_receipt_analyses_document_type CHECK (document_type IS NULL OR document_type IN ('RECEIPT', 'OTHER')),
    CONSTRAINT ck_receipt_analyses_document_confidence CHECK (
        document_confidence IS NULL OR document_confidence BETWEEN 0 AND 1
    ),
    CONSTRAINT ck_receipt_analyses_quality_score CHECK (
        image_quality_score IS NULL OR image_quality_score BETWEEN 0 AND 1
    ),
    CONSTRAINT ck_receipt_analyses_terminal_state CHECK (
        (status = 'COMPLETED' AND completed_at IS NOT NULL AND final_payload IS NOT NULL AND error_code IS NULL)
        OR (status = 'FAILED' AND completed_at IS NOT NULL AND error_code IS NOT NULL AND final_payload IS NOT NULL)
        OR (status IN ('QUEUED', 'PROCESSING') AND completed_at IS NULL AND final_payload IS NULL)
    )
);

CREATE INDEX ix_receipt_analyses_status_submitted
    ON receipt_ai.receipt_analyses (status, submitted_at);
CREATE INDEX ix_receipt_analyses_image_sha256
    ON receipt_ai.receipt_analyses (image_sha256);
CREATE INDEX ix_receipt_analyses_purge_after
    ON receipt_ai.receipt_analyses (purge_after)
    WHERE purge_after IS NOT NULL;

CREATE TRIGGER trg_receipt_analyses_updated_at
BEFORE UPDATE ON receipt_ai.receipt_analyses
FOR EACH ROW EXECUTE FUNCTION receipt_ai.set_updated_at();

-- purge_after가 비어 있으면 submitted_at 기준 기본 보관기간(90일, 팀 확정 정책)을 채운다.
CREATE OR REPLACE FUNCTION receipt_ai.set_default_purge_after()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.purge_after IS NULL THEN
        NEW.purge_after := NEW.submitted_at + INTERVAL '90 days';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_receipt_analyses_default_purge_after
BEFORE INSERT ON receipt_ai.receipt_analyses
FOR EACH ROW EXECUTE FUNCTION receipt_ai.set_default_purge_after();

-- 영수증 한 줄에서 확정하거나 추정한 값을 API 응답 구조 그대로 분해해 저장한다.
CREATE TABLE receipt_ai.receipt_analysis_items (
    receipt_analysis_item_pk BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    analysis_id VARCHAR(64) NOT NULL,
    item_id VARCHAR(64) NOT NULL,
    line_no INTEGER NOT NULL,
    display_status VARCHAR(20) NOT NULL,
    name_value VARCHAR(255) NOT NULL,
    normalized_name VARCHAR(255),
    name_source VARCHAR(24) NOT NULL,
    name_confidence NUMERIC(5, 4) NOT NULL,
    name_evidence_text VARCHAR(500),
    category VARCHAR(20) NOT NULL,
    category_source VARCHAR(24) NOT NULL,
    category_confidence NUMERIC(5, 4) NOT NULL,
    storage_type VARCHAR(20) NOT NULL,
    measure_type VARCHAR(10) NOT NULL,
    quantity SMALLINT,
    quantity_source VARCHAR(24),
    quantity_confidence NUMERIC(5, 4),
    weight_value NUMERIC(10, 3),
    weight_unit VARCHAR(10) NOT NULL DEFAULT 'NONE',
    weight_source VARCHAR(24) NOT NULL,
    weight_confidence NUMERIC(5, 4) NOT NULL,
    weight_evidence_text VARCHAR(500),
    expiration_date DATE,
    expiration_date_type VARCHAR(30) NOT NULL DEFAULT 'UNKNOWN',
    expiration_source VARCHAR(24),
    expiration_confidence NUMERIC(5, 4),
    expiration_evidence_text VARCHAR(500),
    expiration_estimated_range_from DATE,
    expiration_estimated_range_to DATE,
    expiration_assumptions JSONB NOT NULL DEFAULT '[]'::JSONB,
    expiration_evidence_ids JSONB NOT NULL DEFAULT '[]'::JSONB,
    review_reasons JSONB NOT NULL DEFAULT '[]'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_receipt_items_analysis FOREIGN KEY (analysis_id)
        REFERENCES receipt_ai.receipt_analyses (analysis_id) ON DELETE CASCADE,
    CONSTRAINT uq_receipt_items_api_id UNIQUE (analysis_id, item_id),
    CONSTRAINT uq_receipt_items_line UNIQUE (analysis_id, line_no),
    CONSTRAINT ck_receipt_items_line_no CHECK (line_no > 0),
    CONSTRAINT ck_receipt_items_display_status CHECK (
        display_status IN ('RECOGNIZED', 'UNRECOGNIZED', 'NEEDS_REVIEW', 'AI_ESTIMATED')
    ),
    CONSTRAINT ck_receipt_items_category CHECK (
        category IN ('VEGETABLE', 'FRUIT', 'MEAT', 'SEAFOOD', 'DAIRY', 'TOFU_BEAN', 'GRAIN_NOODLE', 'PROCESSED', 'SEASONING', 'BEVERAGE', 'ETC')
    ),
    CONSTRAINT ck_receipt_items_storage_type CHECK (storage_type IN ('REFRIGERATED', 'FROZEN')),
    CONSTRAINT ck_receipt_items_measure_type CHECK (measure_type IN ('COUNT', 'WEIGHT')),
    CONSTRAINT ck_receipt_items_measurement CHECK (
        (measure_type = 'COUNT' AND quantity BETWEEN 1 AND 100 AND weight_value IS NULL AND weight_unit = 'NONE')
        OR (measure_type = 'WEIGHT' AND quantity IS NULL AND weight_value > 0 AND weight_unit IN ('G', 'ML'))
    ),
    CONSTRAINT ck_receipt_items_quantity_metadata CHECK (
        (quantity IS NULL AND quantity_source IS NULL AND quantity_confidence IS NULL)
        OR (quantity IS NOT NULL AND quantity_source IS NOT NULL AND quantity_confidence BETWEEN 0 AND 1)
    ),
    CONSTRAINT ck_receipt_items_expiration_type CHECK (
        expiration_date_type IN ('USE_BY', 'SELL_BY', 'UNKNOWN', 'ESTIMATED_CONSUMPTION_WINDOW')
    ),
    CONSTRAINT ck_receipt_items_expiration_metadata CHECK (
        (expiration_date IS NULL AND expiration_estimated_range_from IS NULL
            AND expiration_source IS NULL AND expiration_confidence IS NULL)
        OR (expiration_date IS NOT NULL AND expiration_estimated_range_from IS NULL
            AND expiration_source IS NOT NULL AND expiration_confidence BETWEEN 0 AND 1)
        OR (expiration_date IS NULL AND expiration_estimated_range_from IS NOT NULL
            AND expiration_estimated_range_to IS NOT NULL
            AND expiration_estimated_range_to >= expiration_estimated_range_from
            AND expiration_source = 'KNOWLEDGE_ESTIMATE' AND expiration_confidence BETWEEN 0 AND 1)
    ),
    CONSTRAINT ck_receipt_items_confidence CHECK (
        name_confidence BETWEEN 0 AND 1
        AND category_confidence BETWEEN 0 AND 1
        AND weight_confidence BETWEEN 0 AND 1
    ),
    CONSTRAINT ck_receipt_items_sources CHECK (
        name_source IN ('OCR', 'RECEIPT_LINE', 'CATALOG', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED')
        AND category_source IN ('OCR', 'RECEIPT_LINE', 'CATALOG', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED')
        AND weight_source IN ('OCR', 'RECEIPT_LINE', 'CATALOG', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED')
        AND (quantity_source IS NULL OR quantity_source IN ('OCR', 'RECEIPT_LINE', 'CATALOG', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED'))
        AND (expiration_source IS NULL OR expiration_source IN ('OCR', 'RECEIPT_LINE', 'CATALOG', 'MODEL_INFERENCE', 'BUSINESS_RULE', 'USER_CONFIRMED', 'KNOWLEDGE_ESTIMATE'))
    )
);

CREATE INDEX ix_receipt_items_normalized_name
    ON receipt_ai.receipt_analysis_items (normalized_name);
CREATE INDEX ix_receipt_items_category
    ON receipt_ai.receipt_analysis_items (category);

CREATE TRIGGER trg_receipt_analysis_items_updated_at
BEFORE UPDATE ON receipt_ai.receipt_analysis_items
FOR EACH ROW EXECUTE FUNCTION receipt_ai.set_updated_at();

-- purge_after가 지난 분석을 삭제한다. receipt_analysis_items는 ON DELETE CASCADE로 함께 정리된다.
-- 주의: receipt_item_feedback 테이블이 나중에 추가되면(동의 정책 승인 후),
-- 그 마이그레이션에서 이 함수를 CREATE OR REPLACE로 갱신해 APPROVED 피드백이 달린 분석은
-- 보관기간이 지나도 삭제하지 않도록 보호 조건을 추가해야 한다. 지금은 그 테이블이 없으므로 무조건 삭제한다.
-- 스케줄링은 이 마이그레이션이 아니라 pg_cron 또는 외부 스케줄러가 담당해야 한다. 예:
--   SELECT cron.schedule('purge-receipt-analyses', '0 * * * *',
--     $$SELECT receipt_ai.purge_expired_analyses()$$);
CREATE OR REPLACE FUNCTION receipt_ai.purge_expired_analyses()
RETURNS BIGINT
LANGUAGE plpgsql
AS $$
DECLARE
    purged_count BIGINT;
BEGIN
    DELETE FROM receipt_ai.receipt_analyses
    WHERE purge_after IS NOT NULL
      AND purge_after < CURRENT_TIMESTAMP;
    GET DIAGNOSTICS purged_count = ROW_COUNT;
    RETURN purged_count;
END;
$$;

COMMIT;
