-- ============================================================
-- 0001: Unified `documents` + `chunks` schema with ACL & RLS
-- ============================================================
-- Sprint 3 / PR-1 — source-agnostic 検索基盤
--
-- 設計判断（Sentry Agent + Plan Agent 調査 + 2026-05-26 ユーザー確認）:
--   - source_type ENUM で pdf / gdrive / gmail / slack 横断
--   - documents.acl_emails TEXT[] + Postgres RLS で機密保護
--   - external_id UNIQUE で idempotency (Drive fileId, Gmail msgId, Slack ts)
--   - 既存 proposals_chunks / proposals_chunks_contextual は触らず温存
--   - 後方互換のため proposals_chunks に owner_email / acl_emails / source_type 列追加
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- source_type ENUM
-- ============================================================
-- 既存 ENUM がある場合は何もしない（pg は DROP TYPE IF EXISTS が依存制約で失敗するため、
-- DO ブロックで存在チェックする）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'document_source_type') THEN
        CREATE TYPE document_source_type AS ENUM (
            'pdf',      -- 既存 proposals_chunks 由来の PDF
            'gdrive',   -- Google Drive 経由（Sprint 3 で追加）
            'gmail',    -- Gmail メッセージ（Sprint 3 で追加）
            'slack',    -- Slack チャネル履歴（Sprint 3 で追加）
            'other'     -- 将来拡張用
        );
    END IF;
END
$$;

-- ============================================================
-- documents: source 横断のメタデータ + ACL
-- ============================================================
CREATE TABLE IF NOT EXISTS documents (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_type     document_source_type NOT NULL,
    -- 外部参照 URI: gdrive://<fileId>, gmail://<msgId>, slack://<channel>/<ts>, file:///path
    source_uri      TEXT,
    -- 外部システムの一意 ID（重複防止 / idempotency key）
    -- Drive: fileId, Gmail: messageId, Slack: channel_id + thread_ts
    external_id     TEXT NOT NULL,
    title           TEXT,
    -- ingest 実行者のメールアドレス（CLAUDE.md 6-bis のトレーサビリティ）
    owner_email     TEXT NOT NULL,
    -- ACL: アクセス許可されたユーザー（Drive permissions.list の結果を写像）
    acl_emails      TEXT[] NOT NULL DEFAULT '{}',
    -- ACL: アクセス許可されたグループ（Workspace group emails）
    acl_groups      TEXT[] NOT NULL DEFAULT '{}',
    -- 顧客機密タグ（案件 ID 等、社外秘判定用）
    client_code     TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- 元データの最終更新時刻（changes.list 差分判定用）
    modified_at     TIMESTAMPTZ,
    -- TeamAgent 側の取り込み時刻
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 同じ source の同じ external_id を二重投入させない
    CONSTRAINT documents_source_external_unique UNIQUE (source_type, external_id)
);

-- インデックス：
-- 1) ACL: GIN で配列 contains 検索を高速化（RLS 評価で多用）
CREATE INDEX IF NOT EXISTS documents_acl_emails_idx
    ON documents USING gin (acl_emails);
CREATE INDEX IF NOT EXISTS documents_acl_groups_idx
    ON documents USING gin (acl_groups);
-- 2) source ごとの最新順アクセス（差分監視 / ingest 進捗確認）
CREATE INDEX IF NOT EXISTS documents_source_modified_idx
    ON documents (source_type, modified_at DESC NULLS LAST);
-- 3) owner 経由のアクセス
CREATE INDEX IF NOT EXISTS documents_owner_idx
    ON documents (owner_email);

-- ============================================================
-- chunks: 検索対象本体（documents の N:1 子）
-- ============================================================
CREATE TABLE IF NOT EXISTS chunks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_idx       INT NOT NULL,
    -- 元テキスト（埋め込み / 表示用）
    content         TEXT NOT NULL,
    -- Anthropic Contextual Retrieval（前置詞付き）
    contextualized  TEXT,
    -- 1024 次元（multilingual-e5-large 想定）
    embedding       vector(1024),
    -- PDF / Slack のメッセージ番号など
    page_num        INT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 同じドキュメント内で chunk_idx 重複させない
    CONSTRAINT chunks_doc_idx_unique UNIQUE (document_id, chunk_idx)
);

-- HNSW: cosine 類似度の高速検索（pgvector 0.7+ 推奨）
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_document_idx
    ON chunks (document_id);

-- ============================================================
-- Row Level Security (RLS) — ACL 自動フィルタ
-- ============================================================
-- Slack ハンドラ等の呼び出し側で:
--   SET LOCAL app.user_email = 'taro@vectorinc.co.jp';
--   SET LOCAL app.user_groups = 'sales@vectorinc.co.jp,managers@vectorinc.co.jp';
--   SET LOCAL app.user_role = 'member';  -- or 'admin' で全件
-- を実行してから検索すると、自動的に ACL でフィルタされる。
--
-- current_setting('app.user_email', true) の第二引数 true = missing_ok（未設定でも例外を出さない）
-- 未設定なら NULL となり、ANY(acl_emails) は false で何も見えない fail-safe。
-- ============================================================

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;  -- superuser 以外でも RLS を強制

-- 既存ポリシーがあれば作り直し（idempotent migration のため）
DROP POLICY IF EXISTS documents_user_acl ON documents;
CREATE POLICY documents_user_acl ON documents
    FOR SELECT
    USING (
        -- 1) 管理者 bypass
        current_setting('app.user_role', true) = 'admin'
        -- 2) 自分が ingest した
        OR current_setting('app.user_email', true) = owner_email
        -- 3) ACL emails に明示的に含まれる
        OR current_setting('app.user_email', true) = ANY(acl_emails)
        -- 4) ACL groups に所属
        OR (
            current_setting('app.user_groups', true) IS NOT NULL
            AND current_setting('app.user_groups', true) <> ''
            AND EXISTS (
                SELECT 1 FROM unnest(acl_groups) g
                WHERE g = ANY(string_to_array(current_setting('app.user_groups', true), ','))
            )
        )
    );

-- INSERT / UPDATE / DELETE は ingest パイプライン専用ロールに限定するため、
-- アプリ用 role には WITH CHECK で owner_email = 自分 を強制する（将来 role 分離する時に効く）
DROP POLICY IF EXISTS documents_owner_insert ON documents;
CREATE POLICY documents_owner_insert ON documents
    FOR INSERT
    WITH CHECK (
        current_setting('app.user_role', true) = 'admin'
        OR current_setting('app.user_email', true) = owner_email
    );

-- chunks は documents の RLS 経由でフィルタされるので、JOIN で評価
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS chunks_via_document ON chunks;
CREATE POLICY chunks_via_document ON chunks
    FOR SELECT
    USING (
        EXISTS (SELECT 1 FROM documents d WHERE d.id = chunks.document_id)
    );

-- ============================================================
-- 後方互換: 既存 proposals_chunks に owner / ACL 列を追加
-- ============================================================
-- 既存データは「PDF, 所有者不明, ACL 全員許可」相当として扱う。
-- 完全移行は Sprint 4 で proposals_chunks → documents/chunks への ETL を別途実施。
DO $$
BEGIN
    -- proposals_chunks が存在する場合のみ ALTER
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = 'public' AND table_name = 'proposals_chunks') THEN
        ALTER TABLE proposals_chunks
            ADD COLUMN IF NOT EXISTS source_type document_source_type DEFAULT 'pdf';
        ALTER TABLE proposals_chunks
            ADD COLUMN IF NOT EXISTS owner_email TEXT;
        ALTER TABLE proposals_chunks
            ADD COLUMN IF NOT EXISTS acl_emails TEXT[] DEFAULT '{}';
        ALTER TABLE proposals_chunks
            ADD COLUMN IF NOT EXISTS external_id TEXT;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = 'public' AND table_name = 'proposals_chunks_contextual') THEN
        ALTER TABLE proposals_chunks_contextual
            ADD COLUMN IF NOT EXISTS source_type document_source_type DEFAULT 'pdf';
        ALTER TABLE proposals_chunks_contextual
            ADD COLUMN IF NOT EXISTS owner_email TEXT;
        ALTER TABLE proposals_chunks_contextual
            ADD COLUMN IF NOT EXISTS acl_emails TEXT[] DEFAULT '{}';
        ALTER TABLE proposals_chunks_contextual
            ADD COLUMN IF NOT EXISTS external_id TEXT;
    END IF;
END
$$;

-- ============================================================
-- 検証用クエリ（migration runner が COMMIT 前に出す）
-- ============================================================
COMMENT ON TABLE documents IS
    'TeamAgent v3.2: source-agnostic document metadata with ACL (Sprint 3 / migration 0001)';
COMMENT ON TABLE chunks IS
    'TeamAgent v3.2: embedding-bearing chunks linked to documents (Sprint 3 / migration 0001)';
COMMENT ON COLUMN documents.acl_emails IS
    'ACL: emails permitted to access. Empty array = no one (fail-safe). Use SET LOCAL app.user_email before SELECT.';
COMMENT ON COLUMN documents.external_id IS
    'Idempotency key from source system: Drive fileId / Gmail msgId / Slack channel:thread_ts';
