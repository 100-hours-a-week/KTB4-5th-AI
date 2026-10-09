BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS receipt_ai;

CREATE OR REPLACE FUNCTION receipt_ai.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

-- 백엔드 재료 마스터가 아니라 OCR 정규화와 분류 재사용을 위한 AI 보조 지식이다.
CREATE TABLE receipt_ai.product_knowledge (
    product_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_name VARCHAR(255) NOT NULL,
    normalized_name VARCHAR(255) NOT NULL,
    barcode VARCHAR(32),
    category VARCHAR(20) NOT NULL,
    suggested_storage_type VARCHAR(20),
    verification_status VARCHAR(20) NOT NULL DEFAULT 'MODEL_INFERRED',
    source_type VARCHAR(24) NOT NULL,
    source_reference VARCHAR(2048),
    source_version VARCHAR(128),
    verified_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_product_knowledge_barcode UNIQUE (barcode),
    CONSTRAINT ck_product_knowledge_category CHECK (
        category IN ('VEGETABLE', 'FRUIT', 'MEAT', 'SEAFOOD', 'DAIRY', 'TOFU_BEAN', 'GRAIN_NOODLE', 'PROCESSED', 'SEASONING', 'BEVERAGE', 'ETC')
    ),
    CONSTRAINT ck_product_knowledge_storage CHECK (
        suggested_storage_type IS NULL OR suggested_storage_type IN ('REFRIGERATED', 'FROZEN')
    ),
    CONSTRAINT ck_product_knowledge_verification CHECK (
        verification_status IN ('MODEL_INFERRED', 'USER_CONFIRMED', 'ADMIN_CONFIRMED', 'REJECTED')
    ),
    CONSTRAINT ck_product_knowledge_verified_at CHECK (
        (verification_status IN ('USER_CONFIRMED', 'ADMIN_CONFIRMED') AND verified_at IS NOT NULL)
        OR verification_status IN ('MODEL_INFERRED', 'REJECTED')
    )
);

CREATE UNIQUE INDEX uq_product_knowledge_identity_active
    ON receipt_ai.product_knowledge (normalized_name, COALESCE(barcode, ''))
    WHERE is_active;
CREATE INDEX ix_product_knowledge_name_trgm
    ON receipt_ai.product_knowledge USING GIN (normalized_name gin_trgm_ops)
    WHERE is_active;

CREATE TRIGGER trg_product_knowledge_updated_at
BEFORE UPDATE ON receipt_ai.product_knowledge
FOR EACH ROW EXECUTE FUNCTION receipt_ai.set_updated_at();

-- OCR에서 자주 깨지는 표기를 특정 상품 지식과 연결한다.
CREATE TABLE receipt_ai.product_aliases (
    alias_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id BIGINT NOT NULL,
    alias_text VARCHAR(255) NOT NULL,
    normalized_alias VARCHAR(255) NOT NULL,
    verification_status VARCHAR(20) NOT NULL DEFAULT 'MODEL_INFERRED',
    verified_at TIMESTAMPTZ,
    occurrence_count BIGINT NOT NULL DEFAULT 1,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_product_aliases_product FOREIGN KEY (product_id)
        REFERENCES receipt_ai.product_knowledge (product_id) ON DELETE CASCADE,
    CONSTRAINT ck_product_aliases_verification CHECK (
        verification_status IN ('MODEL_INFERRED', 'USER_CONFIRMED', 'ADMIN_CONFIRMED', 'REJECTED')
    ),
    CONSTRAINT ck_product_aliases_verified_at CHECK (
        (verification_status IN ('USER_CONFIRMED', 'ADMIN_CONFIRMED') AND verified_at IS NOT NULL)
        OR verification_status IN ('MODEL_INFERRED', 'REJECTED')
    ),
    CONSTRAINT ck_product_aliases_occurrence CHECK (occurrence_count > 0)
);

CREATE UNIQUE INDEX uq_product_aliases_normalized_active
    ON receipt_ai.product_aliases (normalized_alias)
    WHERE is_active;
CREATE INDEX ix_product_aliases_trgm
    ON receipt_ai.product_aliases USING GIN (normalized_alias gin_trgm_ops)
    WHERE is_active;
CREATE INDEX ix_product_aliases_product_id
    ON receipt_ai.product_aliases (product_id);

CREATE TRIGGER trg_product_aliases_updated_at
BEFORE UPDATE ON receipt_ai.product_aliases
FOR EACH ROW EXECUTE FUNCTION receipt_ai.set_updated_at();

-- 영수증에 소비기한이 없을 때 관리용 예상일을 계산할 수 있는 검수된 근거만 저장한다.
CREATE TABLE receipt_ai.shelf_life_rules (
    shelf_life_rule_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id BIGINT,
    normalized_ingredient_name VARCHAR(255) NOT NULL,
    storage_type VARCHAR(20) NOT NULL,
    package_state VARCHAR(16) NOT NULL,
    reference_date_type VARCHAR(24) NOT NULL,
    duration_min_days INTEGER NOT NULL,
    duration_max_days INTEGER NOT NULL,
    source_name VARCHAR(255) NOT NULL,
    source_url VARCHAR(2048) NOT NULL,
    source_published_at DATE,
    verified_at TIMESTAMPTZ NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_shelf_life_rules_product FOREIGN KEY (product_id)
        REFERENCES receipt_ai.product_knowledge (product_id) ON DELETE SET NULL,
    CONSTRAINT ck_shelf_life_rules_storage CHECK (storage_type IN ('REFRIGERATED', 'FROZEN')),
    CONSTRAINT ck_shelf_life_rules_package CHECK (package_state IN ('UNOPENED', 'OPENED', 'UNKNOWN')),
    CONSTRAINT ck_shelf_life_rules_reference CHECK (
        reference_date_type IN ('PURCHASE_DATE', 'MANUFACTURED_DATE', 'OPENED_DATE')
    ),
    CONSTRAINT ck_shelf_life_rules_duration CHECK (
        duration_min_days >= 0 AND duration_max_days >= duration_min_days
    )
);

CREATE INDEX ix_shelf_life_rules_lookup
    ON receipt_ai.shelf_life_rules (normalized_ingredient_name, storage_type, package_state)
    WHERE is_active;
CREATE INDEX ix_shelf_life_rules_product_id
    ON receipt_ai.shelf_life_rules (product_id)
    WHERE product_id IS NOT NULL;

CREATE TRIGGER trg_shelf_life_rules_updated_at
BEFORE UPDATE ON receipt_ai.shelf_life_rules
FOR EACH ROW EXECUTE FUNCTION receipt_ai.set_updated_at();

COMMIT;
