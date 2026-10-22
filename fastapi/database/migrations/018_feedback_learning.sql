-- 사용자 확정값 피드백과 피드백에서 승격한 학습 지식을 저장한다.
--
-- 피드백은 분석 결과(002의 receipt_analyses.final_payload)와 사용자가 재고로 확정한 값을 항목별로
-- 비교해 남긴다. 학습 대상은 사용자가 AI 결과를 "수정"한 항목(CORRECTED)의 상품명·카테고리뿐이다.
-- 사용자가 새로 추가(ADDED)하거나 삭제(DELETED)한 항목, 그대로 확정(CONFIRMED)한 항목은 기록만 하고
-- 학습하지 않는다. 한 사용자의 잘못된 수정이 지식을 오염시키지 않도록, 같은 OCR 원문에 대해 서로 다른
-- 냉장고 N곳 이상이 같은 값으로 고쳤고 다른 값으로 고친 비율이 낮을 때만 learned_corrections로 승격한다.
-- 승격 기준은 애플리케이션 설정(FEEDBACK_PROMOTION_MIN_SUPPORT, FEEDBACK_PROMOTION_MIN_AGREEMENT)이다.
--
-- 상품 카탈로그 조회 쿼리가 learned_corrections를 함께 읽는다. 테이블이 없으면 애플리케이션은 학습 지식 없이
-- 기존 카탈로그로 조회하고 5분마다 다시 확인하므로, 이 마이그레이션은 배포 전후 어느 때 적용해도 된다.
-- 피드백 API는 이 마이그레이션 전에는 503을 반환한다.
BEGIN;

CREATE TABLE receipt_ai.receipt_item_feedback (
    feedback_pk BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    feedback_id VARCHAR(64) NOT NULL,
    analysis_id VARCHAR(64) NOT NULL,
    refrigerator_id BIGINT NOT NULL,
    item_id VARCHAR(64),
    action VARCHAR(16) NOT NULL,
    -- 분석 결과 원본 항목. 분석 이력이 보관기간으로 삭제돼도 피드백의 근거가 남도록 복사해 둔다.
    original_item JSONB,
    confirmed JSONB,
    changed_fields JSONB NOT NULL DEFAULT '[]'::JSONB,
    -- 정규화한 OCR 원문. 카탈로그 조회 키와 같은 규칙(normalize_product_name)으로 만든다.
    learning_key VARCHAR(255),
    target_name VARCHAR(255),
    target_normalized_name VARCHAR(255),
    target_category VARCHAR(20),
    model_improvement_consent BOOLEAN NOT NULL,
    training_eligibility VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_item_feedback_action CHECK (action IN ('CONFIRMED', 'CORRECTED', 'ADDED', 'DELETED')),
    CONSTRAINT ck_item_feedback_item_id CHECK (
        (action = 'ADDED' AND item_id IS NULL AND original_item IS NULL AND confirmed IS NOT NULL)
        OR (action = 'CORRECTED' AND item_id IS NOT NULL AND original_item IS NOT NULL AND confirmed IS NOT NULL)
        OR (action IN ('CONFIRMED', 'DELETED') AND item_id IS NOT NULL AND original_item IS NOT NULL AND confirmed IS NULL)
    ),
    CONSTRAINT ck_item_feedback_eligibility CHECK (
        training_eligibility IN ('NOT_ELIGIBLE', 'PENDING_REVIEW', 'APPLIED', 'REJECTED')
    ),
    -- 학습 후보는 동의하고 수정한 항목뿐이다.
    CONSTRAINT ck_item_feedback_learning_scope CHECK (
        training_eligibility = 'NOT_ELIGIBLE'
        OR (action = 'CORRECTED' AND model_improvement_consent AND learning_key IS NOT NULL
            AND target_normalized_name IS NOT NULL AND target_category IS NOT NULL)
    ),
    CONSTRAINT ck_item_feedback_target_category CHECK (
        target_category IS NULL OR target_category IN (
            'VEGETABLE', 'FRUIT', 'MEAT', 'SEAFOOD', 'DAIRY', 'TOFU_BEAN',
            'GRAINS_NOODLE', 'PROCESSED_FOOD', 'SEASONING', 'BEVERAGE', 'OTHER'
        )
    )
);

-- 같은 분석 항목에 대한 중복 저장을 추가로 막는다. 분석 전체의 멱등성은 여러 항목을 한 피드백으로
-- 저장해야 하므로 애플리케이션이 analysis_id advisory lock 안에서 기존 행을 재조회해 보장한다.
CREATE UNIQUE INDEX uq_item_feedback_analysis_item
    ON receipt_ai.receipt_item_feedback (analysis_id, item_id)
    WHERE item_id IS NOT NULL;
CREATE INDEX ix_item_feedback_analysis
    ON receipt_ai.receipt_item_feedback (analysis_id);
CREATE INDEX ix_item_feedback_learning_key
    ON receipt_ai.receipt_item_feedback (learning_key)
    WHERE training_eligibility IN ('PENDING_REVIEW', 'APPLIED');

CREATE TRIGGER trg_receipt_item_feedback_updated_at
BEFORE UPDATE ON receipt_ai.receipt_item_feedback
FOR EACH ROW EXECUTE FUNCTION receipt_ai.set_updated_at();

-- 여러 사용자의 일치하는 수정으로 승격된 지식. 카탈로그 조회에서 별칭·표준명보다 먼저 쓴다.
-- 기존 product_knowledge·product_aliases를 직접 바꾸지 않으므로, 문제가 생기면 is_active만 끄면 된다.
CREATE TABLE receipt_ai.learned_corrections (
    correction_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    learning_key VARCHAR(255) NOT NULL,
    corrected_name VARCHAR(255) NOT NULL,
    normalized_name VARCHAR(255) NOT NULL,
    category VARCHAR(20) NOT NULL,
    support_count INTEGER NOT NULL,
    agreement_ratio NUMERIC(5, 4) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    deactivated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_learned_corrections_category CHECK (
        category IN (
            'VEGETABLE', 'FRUIT', 'MEAT', 'SEAFOOD', 'DAIRY', 'TOFU_BEAN',
            'GRAINS_NOODLE', 'PROCESSED_FOOD', 'SEASONING', 'BEVERAGE', 'OTHER'
        )
    ),
    CONSTRAINT ck_learned_corrections_support CHECK (support_count > 0),
    CONSTRAINT ck_learned_corrections_agreement CHECK (agreement_ratio BETWEEN 0 AND 1),
    CONSTRAINT ck_learned_corrections_active CHECK (is_active OR deactivated_at IS NOT NULL)
);

CREATE UNIQUE INDEX uq_learned_corrections_key_active
    ON receipt_ai.learned_corrections (learning_key)
    WHERE is_active;

CREATE TRIGGER trg_learned_corrections_updated_at
BEFORE UPDATE ON receipt_ai.learned_corrections
FOR EACH ROW EXECUTE FUNCTION receipt_ai.set_updated_at();

-- 002 주석은 피드백 테이블이 생기면 보관기간 정리에서 피드백이 달린 분석을 보호하라고 했다. 피드백 행이
-- 원본 항목을 original_item에 복사해 두므로 분석 이력이 삭제돼도 학습 근거가 남는다. 그래서
-- receipt_ai.purge_expired_analyses()는 바꾸지 않고, 분석 이력은 기존 보관기간(90일)대로 삭제한다.

COMMIT;
