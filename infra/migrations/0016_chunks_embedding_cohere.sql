-- ============================================================
-- 0016: chunks.embedding_cohere — Bedrock Cohere Embed 移行用の並行列
-- ============================================================
-- 目的:
--   社内ナレッジ検索の埋め込みを 自前 LocalE5（chunks.embedding）から Bedrock Cohere
--   Embed multilingual v3 へ移行できるようにする。既存 e5 ベクトル列を **温存** したまま
--   Cohere ベクトルを **並行列** chunks.embedding_cohere に書く。検索側は EMBEDDING_COLUMN
--   env で読む列を切替える（既定 embedding＝e5・従来挙動）。
--
-- セキュリティ / プライバシー:
--   - 列の追加のみ。本文・PII は保存しない（embedding は数値ベクトル）。
--   - chunks は migration 0001/0002 で teamagent_app に GRANT 済。列追加は既存テーブルの
--     ALTER なので追加 GRANT 不要。
--
-- 安全性: IF NOT EXISTS で冪等。**追加のみ**（既存 embedding 列・データ・索引は不変）。
--   既存 e5 列を残すため、移行は env を戻すだけ（EMBEDDER_BACKEND=local /
--   EMBEDDING_COLUMN=embedding）で完全に rollback 可能（データ無損失）。
--   次元は e5 と同じ vector(1024)・L2 正規化済み（Cohere v3 は正規化済）なので
--   HNSW vector_cosine_ops 索引をそのまま流用できる。
--
-- 適用:
--   このマイグレーションを適用しただけでは本番挙動は 1 バイトも変わらない
--   （embedding_cohere は NULL のまま・検索は既定 embedding 列を読む）。実際の移行は
--   (1) 本マイグレーション適用 →
--   (2) EMBEDDER_BACKEND=cohere で scripts/reembed_chunks.py --target-column embedding_cohere
--       によりコーパスを並行列へ再 embed →
--   (3) eval（recall@k 非劣化）合格を確認後に
--       EMBEDDER_BACKEND=cohere / EMBEDDING_COLUMN=embedding_cohere を本番反映、
--   という順で人間ゲートを通して行う。
--
-- ロールバック:
--   DROP INDEX IF EXISTS chunks_embedding_cohere_hnsw_idx;
--   ALTER TABLE chunks DROP COLUMN IF EXISTS embedding_cohere;
--   （検索の rollback は env を embedding 列へ戻すだけでよく、列を残しても無害）
--
-- 関連:
--   src/teamagent/adapters/embeddings_client.py（BedrockCohereEmbedder / build_embedder_from_env）
--   src/teamagent/adapters/pgvector_client.py（search_similar_new_schema embedding_col 引数）
--   scripts/reembed_chunks.py（--target-column embedding_cohere）

-- Bedrock Cohere Embed multilingual v3（1024 次元）の並行列。既定 NULL。
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_cohere vector(1024);

-- HNSW: cosine 類似度の高速検索（既存 chunks_embedding_hnsw_idx と同型）。
-- embedding_cohere が全 NULL の間は索引は空で検索コストに無影響。
CREATE INDEX IF NOT EXISTS chunks_embedding_cohere_hnsw_idx
    ON chunks USING hnsw (embedding_cohere vector_cosine_ops);

COMMENT ON COLUMN chunks.embedding_cohere IS
    'Bedrock Cohere Embed multilingual v3 の 1024 次元ベクトル（e5 列 embedding と並行・移行用）';
