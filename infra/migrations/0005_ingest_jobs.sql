-- ============================================================
-- 0005: ingest_jobs テーブル — 一括取り込みの state machine 永続化
-- ============================================================
-- Sprint 4 で営業 PDF 200〜500 件を Drive から一括取り込みするにあたり、
-- 部分失敗からの再開と進捗観測のための per-document state machine を
-- RDS に永続化する。S3 / DynamoDB は overkill（500 件規模、すでに Postgres あり）。
--
-- State 遷移（docs/v3.2/sprint4_pdf_mass_ingest_design_v1.md 参照）:
--   SCANNED → DOWNLOADED → EXTRACTED → EMBEDDED → COMMITTED
--                ↓ N回失敗
--           FAILED_TRANSIENT → POISON
--
-- 1 job = 1 (source, external_id) の取り込み試行。同じ document の再実行は
-- 同 row を UPDATE する（external_id UNIQUE）。
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ingest_job_state') THEN
        CREATE TYPE ingest_job_state AS ENUM (
            'SCANNED',          -- Drive list_files で見つけた段階
            'DOWNLOADED',       -- download_file_bytes 完了
            'EXTRACTED',        -- pypdf テキスト抽出完了
            'EMBEDDED',         -- 全 chunk の embedding 完了（DB 投入直前）
            'COMMITTED',        -- documents + chunks 投入完了（成功）
            'FAILED_TRANSIENT', -- 一時失敗（Drive 429 / Bedrock throttle 等、リトライ対象）
            'POISON'            -- N 回失敗の dead-letter（人手調査が必要）
        );
    END IF;
END
$$;


CREATE TABLE IF NOT EXISTS ingest_jobs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    -- (source_type, external_id) で document と 1:1 対応
    source_type     document_source_type NOT NULL,
    external_id     TEXT NOT NULL,
    state           ingest_job_state NOT NULL DEFAULT 'SCANNED',
    -- バッチ識別: 同一 batch_id の job をまとめて再実行できる
    batch_id        TEXT,
    -- リトライカウント（attempt N 回失敗で POISON に遷移）
    attempt_count   INT NOT NULL DEFAULT 0,
    max_attempts    INT NOT NULL DEFAULT 5,
    -- 最後の失敗理由（POISON 化したときの human-readable な手掛かり）
    last_error      TEXT,
    -- 各 stage 完了タイムスタンプ（観測用）
    scanned_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    downloaded_at   TIMESTAMPTZ,
    extracted_at    TIMESTAMPTZ,
    embedded_at     TIMESTAMPTZ,
    committed_at    TIMESTAMPTZ,
    -- 補助メタデータ（PDF size / chunk count 等の集計用）
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 同じ source の同じ external_id を二重登録させない
    CONSTRAINT ingest_jobs_source_external_unique UNIQUE (source_type, external_id)
);

-- インデックス：
-- 1) batch 単位の進捗集計（COUNT(*) FILTER (WHERE state = ...) で使う）
CREATE INDEX IF NOT EXISTS ingest_jobs_batch_state_idx
    ON ingest_jobs (batch_id, state);
-- 2) FAILED_TRANSIENT の再実行検索（cron で再キューに入れる）
CREATE INDEX IF NOT EXISTS ingest_jobs_state_updated_idx
    ON ingest_jobs (state, updated_at DESC);
-- 3) POISON の dead-letter 一覧
CREATE INDEX IF NOT EXISTS ingest_jobs_poison_idx
    ON ingest_jobs (created_at DESC) WHERE state = 'POISON';

-- updated_at の自動更新トリガー（手書きで UPDATE 句に書き忘れないため）
CREATE OR REPLACE FUNCTION ingest_jobs_set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ingest_jobs_updated_at_trg ON ingest_jobs;
CREATE TRIGGER ingest_jobs_updated_at_trg
    BEFORE UPDATE ON ingest_jobs
    FOR EACH ROW EXECUTE FUNCTION ingest_jobs_set_updated_at();

-- teamagent_app role に DML 権限付与（migration 0002 の流儀踏襲）
GRANT SELECT, INSERT, UPDATE, DELETE ON ingest_jobs TO teamagent_app;

COMMENT ON TABLE ingest_jobs IS
    'TeamAgent v3.2 Sprint 4: 一括取り込みの state machine 永続化（docs/v3.2/sprint4_pdf_mass_ingest_design_v1.md）';
COMMENT ON COLUMN ingest_jobs.state IS
    'SCANNED → DOWNLOADED → EXTRACTED → EMBEDDED → COMMITTED（成功路）または FAILED_TRANSIENT → POISON（失敗路）';
COMMENT ON COLUMN ingest_jobs.batch_id IS
    '同一実行バッチを識別する任意 ID。CLI で --batch-id を指定すると同 ID の FAILED_TRANSIENT だけ再実行できる';
