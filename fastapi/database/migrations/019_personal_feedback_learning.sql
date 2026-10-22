-- 한 번의 사용자 수정도 해당 냉장고에는 즉시 적용하고, 기존 다수 합의 승격은 전역 지식으로 유지한다.
BEGIN;

ALTER TABLE receipt_ai.learned_corrections
    ADD COLUMN scope VARCHAR(16) NOT NULL DEFAULT 'GLOBAL',
    ADD COLUMN refrigerator_id BIGINT,
    ADD COLUMN review_status VARCHAR(20) NOT NULL DEFAULT 'APPROVED';

ALTER TABLE receipt_ai.learned_corrections
    ADD CONSTRAINT ck_learned_corrections_scope CHECK (scope IN ('PERSONAL', 'GLOBAL')),
    ADD CONSTRAINT ck_learned_corrections_owner CHECK (
        (scope = 'PERSONAL' AND refrigerator_id IS NOT NULL)
        OR (scope = 'GLOBAL' AND refrigerator_id IS NULL)
    ),
    ADD CONSTRAINT ck_learned_corrections_review_status CHECK (
        review_status IN ('PENDING', 'APPROVED', 'REJECTED')
    );

DROP INDEX receipt_ai.uq_learned_corrections_key_active;
CREATE UNIQUE INDEX uq_learned_corrections_global_active
    ON receipt_ai.learned_corrections (learning_key)
    WHERE scope = 'GLOBAL' AND is_active;
CREATE UNIQUE INDEX uq_learned_corrections_personal_active
    ON receipt_ai.learned_corrections (learning_key, refrigerator_id)
    WHERE scope = 'PERSONAL' AND is_active;

ALTER TABLE receipt_ai.receipt_item_feedback
    DROP CONSTRAINT ck_item_feedback_eligibility;
ALTER TABLE receipt_ai.receipt_item_feedback
    ADD CONSTRAINT ck_item_feedback_eligibility CHECK (
        training_eligibility IN (
            'NOT_ELIGIBLE', 'PERSONAL_APPLIED', 'PENDING_REVIEW', 'APPLIED', 'REJECTED'
        )
    );

DROP INDEX receipt_ai.ix_item_feedback_learning_key;
CREATE INDEX ix_item_feedback_learning_key
    ON receipt_ai.receipt_item_feedback (learning_key)
    WHERE training_eligibility IN ('PERSONAL_APPLIED', 'PENDING_REVIEW', 'APPLIED');

-- 019 적용 전에 쌓인 동의 피드백도 해당 냉장고에서 즉시 재사용할 수 있게 한다.
INSERT INTO receipt_ai.learned_corrections (
    learning_key,
    corrected_name,
    normalized_name,
    category,
    support_count,
    agreement_ratio,
    scope,
    refrigerator_id,
    review_status,
    is_active
)
SELECT DISTINCT ON (learning_key, refrigerator_id)
       learning_key,
       target_name,
       target_normalized_name,
       target_category,
       1,
       1,
       'PERSONAL',
       refrigerator_id,
       'APPROVED',
       TRUE
FROM receipt_ai.receipt_item_feedback
WHERE model_improvement_consent
  AND action = 'CORRECTED'
  AND learning_key IS NOT NULL
  AND target_name IS NOT NULL
  AND target_normalized_name IS NOT NULL
  AND training_eligibility IN ('PENDING_REVIEW', 'APPLIED')
ORDER BY learning_key, refrigerator_id, created_at DESC, feedback_pk DESC
ON CONFLICT DO NOTHING;

UPDATE receipt_ai.receipt_item_feedback
SET training_eligibility = 'PERSONAL_APPLIED'
WHERE model_improvement_consent
  AND action = 'CORRECTED'
  AND learning_key IS NOT NULL
  AND training_eligibility = 'PENDING_REVIEW';

COMMIT;
