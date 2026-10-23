-- 선택적 검색 실험용. 앱 시작 시 실행하지 않는다. pgvector 설치와 DDL 권한이 필요하다.
BEGIN;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS recipe_ai.recipe_embeddings (
    recipe_id bigint NOT NULL REFERENCES recipe_ai.recipes(id) ON DELETE CASCADE,
    model_name text NOT NULL,
    model_revision text NOT NULL,
    text_version text NOT NULL,
    source_revision text NOT NULL,
    text_sha256 text NOT NULL CHECK (text_sha256 ~ '^[a-f0-9]{64}$'),
    embedding vector(384) NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (recipe_id, model_name, model_revision, text_version)
);
-- 현재 870건에서는 exact cosine 검색으로 검증한다. ANN 인덱스는 실측 후 결정한다.
COMMIT;
