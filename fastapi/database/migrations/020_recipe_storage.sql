-- 운영 적용은 별도 승인 후 실행한다. 기존 receipt_ai 및 백엔드 MySQL은 변경하지 않는다.
BEGIN;
CREATE SCHEMA IF NOT EXISTS recipe_ai;
CREATE TABLE IF NOT EXISTS recipe_ai.recipes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source text NOT NULL,
    source_recipe_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('ACTIVE', 'INACTIVE')),
    ingredient_names text[] NOT NULL,
    snapshot jsonb NOT NULL,
    raw_source jsonb NOT NULL,
    usage_policy text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source, source_recipe_id)
);
CREATE INDEX IF NOT EXISTS recipe_ingredient_names_idx ON recipe_ai.recipes USING gin(ingredient_names);
CREATE TABLE IF NOT EXISTS recipe_ai.changes (
    change_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
-- changes는 삭제하지 않는다. 모든 쓰기는 repository의 동일 advisory lock 아래 수행한다.
COMMIT;
