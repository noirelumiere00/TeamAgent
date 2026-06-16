-- 0014 Batch C1: audit_log テーブル（ingest#25）+ documents/chunks.metadata の GIN 索引（ingest#9）
--
-- 目的:
--   (1) ingest / 取り込み系の監査ログを 1 か所に集約する append-only テーブルを用意。
--       本文（PII）は保存せず、誰が・いつ・何を（source_kind/external_id/request_id）と
--       構造化 detail(JSONB) だけを残す（usage_events と同じ「本文を持たない」原則）。
--   (2) documents.metadata / chunks.metadata への部分一致・包含検索を索引化（@> / ? 演算子）。
--       メタデータ駆動のフィルタ（client_code 横断・mime_type 絞り込み等）の土台。
--
-- 安全性: 全て IF NOT EXISTS で冪等。追加のみ（既存テーブル・索引・データは不変）。
-- ロールバック:
--   DROP INDEX IF EXISTS chunks_metadata_gin_idx;
--   DROP INDEX IF EXISTS documents_metadata_gin_idx;
--   DROP TABLE IF EXISTS audit_log;
--
-- 関連: docs/v3.2/ops/risk_register.md・docs/v3.2/ingest_pipeline_v1.md

-- ------------------------------------------------------------
-- (1) audit_log（append-only・本文なし）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 実行主体（owner_email / "system" など）。RLS 評価には使わない（運用監査のみ）。
    actor           TEXT,
    -- 監査イベント種別。例: 'ingest_commit' | 'ingest_failed' | 'connector_sync'
    action          TEXT NOT NULL,
    -- 対象 source の種別/ID（pipeline._run_kind の kind / spec 識別子に対応）。
    source_kind     TEXT,
    external_id     TEXT,
    -- 取り込み実行を貫く相関 ID（ingest-xxxx）。
    request_id      TEXT,
    -- 構造化された付随情報（件数・サイズ・エラー要約など。本文・PII は入れない）。
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 時系列の直近参照（運用ダッシュボード/手動調査）。
CREATE INDEX IF NOT EXISTS audit_log_occurred_idx
    ON audit_log (occurred_at DESC);
-- action 別の絞り込み（失敗だけ追う等）。
CREATE INDEX IF NOT EXISTS audit_log_action_idx
    ON audit_log (action, occurred_at DESC);

-- teamagent_app role に DML 権限付与（migration 0002 / 0005 の流儀踏襲）。
GRANT SELECT, INSERT ON audit_log TO teamagent_app;

COMMENT ON TABLE audit_log IS
    'TeamAgent v3.2 Batch C1: ingest/取り込み系の append-only 監査ログ（本文・PII は保持しない）';

-- ------------------------------------------------------------
-- (2) metadata の GIN 索引（包含 @> / キー存在 ? の高速化）
-- ------------------------------------------------------------
-- jsonb_path_ops は @> 包含クエリに特化し、索引サイズが小さく更新も軽い。
CREATE INDEX IF NOT EXISTS documents_metadata_gin_idx
    ON documents USING gin (metadata jsonb_path_ops);
CREATE INDEX IF NOT EXISTS chunks_metadata_gin_idx
    ON chunks USING gin (metadata jsonb_path_ops);

-- 適用後の検証 (P0・SSM トンネル):
--   SELECT count(*) FROM audit_log;  -- 0 を期待（初回作成）
--   INSERT INTO audit_log (actor, action, source_kind, external_id, request_id, detail)
--     VALUES ('system', 'ingest_commit', 'gdrive', 'TEST_F1', 'ingest-test', '{"docs":1}'::jsonb);
--   SELECT action, source_kind, detail FROM audit_log ORDER BY occurred_at DESC LIMIT 1;
--   DELETE FROM audit_log WHERE external_id='TEST_F1';
--   SELECT indexname FROM pg_indexes
--     WHERE indexname IN ('documents_metadata_gin_idx','chunks_metadata_gin_idx');  -- 2行
--   EXPLAIN SELECT * FROM documents WHERE metadata @> '{"client_code":"伊藤園"}'::jsonb;  -- GIN 索引利用
