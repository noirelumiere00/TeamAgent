-- ============================================================
-- 0025: digest_ack テーブル — 朝ダイジェスト確認済み項目
-- ============================================================
-- 目的: 本人が確認した Gmail スレッド／Slack 返信漏れカードを 30 日間保持し、
-- 翌朝以降のダイジェストから除外する。item_key は生 ID ではなく、ユーザーを
-- 混ぜた sha256 の先頭 16 hex のみを保存する（G3 を DB 層でも維持）。
--
-- ON CONFLICT と権限:
--   teamagent_app の INSERT ... ON CONFLICT は、arbiter 列の SELECT 権限と
--   SELECT の RLS ポリシーを両方要求する（0024 で確認済み）。本テーブルは
--   active 読み取りにも SELECT が必要なため、列単位ではなく表単位で
--   SELECT, INSERT, UPDATE, DELETE を付与し、単一の本人行 policy の USING と
--   WITH CHECK で読み書きの双方を束縛する。
-- ロールバック:
--   REVOKE SELECT, INSERT, UPDATE, DELETE ON digest_ack FROM teamagent_app;
--   DROP POLICY IF EXISTS digest_ack_self ON digest_ack;
--   DROP INDEX IF EXISTS idx_digest_ack_expires;
--   DROP TABLE IF EXISTS digest_ack;
-- 関連: infra/migrations/0017_video_quota.sql,
--       infra/migrations/0024_usage_events_conflict_read.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS digest_ack (
    user_email TEXT NOT NULL
               CHECK (user_email <> '' AND position('@' IN user_email) > 0),
    -- m = Gmail スレッド、s = Slack 返信漏れカード。
    item_kind  CHAR(1) NOT NULL CHECK (item_kind IN ('m','s')),
    -- 生 ID は保存せず、sha256 の先頭 16 hex のみを保持する。
    item_key   CHAR(16) NOT NULL CHECK (item_key ~ '^[0-9a-f]{16}$'),
    -- 新着判定の基準値（秒またはメッセージ数）。
    anchor     BIGINT NOT NULL DEFAULT 0 CHECK (anchor >= 0),
    acked_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_email, item_kind, item_key)
);

CREATE INDEX IF NOT EXISTS idx_digest_ack_expires ON digest_ack (expires_at);

-- RLS: 本人行のみ（app.user_email GUC・0017 と同型）。FORCE で owner にも適用。
ALTER TABLE digest_ack ENABLE ROW LEVEL SECURITY;
ALTER TABLE digest_ack FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS digest_ack_self ON digest_ack;
CREATE POLICY digest_ack_self ON digest_ack
    USING (
        (current_setting('app.user_email', true) <> ''
         AND user_email = current_setting('app.user_email', true))
        OR current_setting('app.user_role', true) = 'admin'
    )
    WITH CHECK (
        (current_setting('app.user_email', true) <> ''
         AND user_email = current_setting('app.user_email', true))
        OR current_setting('app.user_role', true) = 'admin'
    );

-- ON CONFLICT と active 読み取りに必要な SELECT を含む。全操作は RLS で自行に限定。
GRANT SELECT, INSERT, UPDATE, DELETE ON digest_ack TO teamagent_app;
