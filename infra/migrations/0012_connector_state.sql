-- 0012 Wave2-⑤ 増分同期: connector_state テーブル（source_kind × source_id 単位で前回 cursor/oldest/revision を永続化）
--
-- 方針: ingest pipeline を「初回はフル走査・2 回目以降は前回 cursor 以降の差分だけ」に
-- 切り替える土台を作る。本 migration では **テーブルとインデックスだけ** を追加し、
-- pipeline 側のコード変更は本 migration では行わない（既存ingest挙動は不変）。
-- pipeline の cursor 駆動実装（USE_INCREMENTAL_SYNC=true で opt-in）は Wave3 で行う前提。
--
-- ロールバック: DROP TABLE connector_state;（依存テーブルなし）
-- 冪等: IF NOT EXISTS で複数回実行しても安全。
--
-- 関連: docs/v3.2/slo_v1.md §5 SLI 実装状況・docs/v3.2/ingest_pipeline_v1.md

CREATE TABLE IF NOT EXISTS connector_state (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- 取り込み源の種別。pipeline._run_kind の引数 kind と一致。
    source_kind     TEXT NOT NULL,
    -- 種別ごとのスコープ単位 ID:
    --   slack         → channel_id (C0XYZ)
    --   gdrive        → folder_id  (1aBcD...)
    --   gsheets       → sheet_id   (1xYz...)
    --   shared_drives → drive_id   (0AOyf...)
    source_id       TEXT NOT NULL,
    -- 種別ごとのページング/差分基点（どれか 1〜複数を使う）:
    --   gdrive        → changes.list の next_start_page_token
    --   slack         → 直近 oldest 補助
    --   gsheets       → 直近 last row_idx hint
    cursor          TEXT,
    -- Slack conversations.history の oldest (epoch sec)。次回はこの値以降だけ取る。
    oldest          DOUBLE PRECISION,
    -- Sheets の最終取込済 row_idx, Drive の file modifiedTime epoch などの汎用カウンタ。
    revision        BIGINT,
    -- 直近成功時刻。NULL = 一度も成功していない（初回フル走査が必要）。
    last_success_at TIMESTAMPTZ,
    -- 連続失敗カウンタ（しきい値超で #ops alert・backoff の根拠に使う）。
    attempt_count   INT NOT NULL DEFAULT 0,
    -- 直近のエラー文字列（手動再開時のヒント）。
    last_error      TEXT,
    -- handler 固有の拡張領域。スキーマを今後足したくなる前提の逃げ場。
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT connector_state_source_kind_check
        CHECK (source_kind IN ('slack', 'gdrive', 'gsheets', 'shared_drives'))
);

-- 主アクセスパターン: handler が (source_kind, source_id) で1行ロードして更新する。
CREATE UNIQUE INDEX IF NOT EXISTS connector_state_source_unique
    ON connector_state(source_kind, source_id);

-- 「最近成功していない source」を cron 実行時に抽出するための副インデックス。
-- NULLS FIRST により「一度も成功していない (last_success_at IS NULL)」レコードが先頭に来る。
CREATE INDEX IF NOT EXISTS connector_state_last_success_idx
    ON connector_state(source_kind, last_success_at ASC NULLS FIRST);

-- updated_at の自動更新トリガ（既存テーブル群と同じ流儀。0007_usage_events.sql の trigger を参照）。
CREATE OR REPLACE FUNCTION trg_connector_state_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS connector_state_set_updated_at ON connector_state;
CREATE TRIGGER connector_state_set_updated_at
    BEFORE UPDATE ON connector_state
    FOR EACH ROW EXECUTE FUNCTION trg_connector_state_set_updated_at();

-- 適用後の検証 (P0・SSMトンネル):
--   SELECT count(*) FROM connector_state;  -- 0 を期待（初回作成）
--   INSERT INTO connector_state (source_kind, source_id) VALUES ('slack', 'TEST_C0XYZ');
--   SELECT source_kind, source_id, attempt_count, created_at, updated_at FROM connector_state;
--   UPDATE connector_state SET cursor='ABC' WHERE source_id='TEST_C0XYZ';
--   SELECT updated_at > created_at FROM connector_state WHERE source_id='TEST_C0XYZ';  -- trigger 確認・t を期待
--   DELETE FROM connector_state WHERE source_id='TEST_C0XYZ';
