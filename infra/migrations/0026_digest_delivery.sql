-- ============================================================
-- 0026: digest_delivery テーブル — 朝ダイジェストの「その日もう送った」印
-- ============================================================
-- 目的: 個人別配信時刻（予約発火）と既定時刻の一括実行の **二重配信を止める**。
-- 同一 (user_email, digest_date) を 1 行しか作らせず、INSERT ... ON CONFLICT DO NOTHING
-- の rowcount を「自分が送る権利を取れたか」の判定に使う（claim）。アプリ側の分岐では
-- なく DB の一意制約が唯一の調停者なので、planner 再実行・予約重複・一括実行の同時走行
-- でも 2 通は物理的に出ない。
--
-- ⚠️ 本テーブルが無い環境では claim が例外になる。呼び出し側は **fail-closed**
--    （送らない）で倒す。ここを fail-open にすると 29 名に 2 通届く。
--
-- user_email を保存する理由: 一括実行の除外集合を引くのに必要で、oauth_tokens が既に
-- 同じ値を持っている（新たな PII の増加にはならない）。RLS は本人行に限定。
--
-- ロールバック:
--   REVOKE SELECT, INSERT, DELETE ON digest_delivery FROM teamagent_app;
--   DROP POLICY IF EXISTS digest_delivery_self ON digest_delivery;
--   DROP INDEX IF EXISTS idx_digest_delivery_date;
--   DROP TABLE IF EXISTS digest_delivery;
-- 関連: infra/migrations/0025_digest_ack.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS digest_delivery (
    user_email   TEXT NOT NULL
                 CHECK (user_email <> '' AND position('@' IN user_email) > 0),
    -- JST の暦日（YYYY-MM-DD）。時刻は持たない＝「その日 1 回」の粒度。
    digest_date  DATE NOT NULL,
    -- 'scheduled'（個人別予約の発火） / 'bulk'（既定時刻の一括実行）。観測用。
    origin       TEXT NOT NULL DEFAULT 'bulk' CHECK (origin IN ('scheduled', 'bulk')),
    claimed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_email, digest_date)
);

-- 期限切れ掃除用（14 日保持。診断の猶予だけ取り、長期保持はしない）。
-- 掃除の実行者は claim（adapters/digest_delivery_store.py）。claim と同じ
-- トランザクションで `DELETE FROM digest_delivery WHERE expires_at < NOW()` を流すため、
-- 掃除ジョブ・cron を別に建てない（RLS で消えるのは本人行のみ）。
CREATE INDEX IF NOT EXISTS idx_digest_delivery_date ON digest_delivery (expires_at);

-- RLS: 本人行のみ（app.user_email GUC・0025 と同型）。FORCE で owner にも適用。
ALTER TABLE digest_delivery ENABLE ROW LEVEL SECURITY;
ALTER TABLE digest_delivery FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS digest_delivery_self ON digest_delivery;
CREATE POLICY digest_delivery_self ON digest_delivery
    USING (user_email = current_setting('app.user_email', true))
    WITH CHECK (user_email = current_setting('app.user_email', true));

-- ON CONFLICT DO NOTHING は arbiter 列の SELECT 権限と SELECT の RLS を要求する
-- （0024/0025 で実測済み）。表単位で付与する。
GRANT SELECT, INSERT, DELETE ON digest_delivery TO teamagent_app;

-- 適用後の検証 (SSM トンネル):
--   SET app.user_email = 'komata@example.com';
--   SET ROLE teamagent_app;
--   INSERT INTO digest_delivery (user_email, digest_date, origin, expires_at)
--     VALUES ('komata@example.com', CURRENT_DATE, 'scheduled', NOW() + INTERVAL '14 days')
--     ON CONFLICT DO NOTHING;               -- 1 行
--   INSERT INTO digest_delivery (user_email, digest_date, origin, expires_at)
--     VALUES ('komata@example.com', CURRENT_DATE, 'bulk', NOW() + INTERVAL '14 days')
--     ON CONFLICT DO NOTHING;               -- 0 行（＝二重配信を止められている）
--   DELETE FROM digest_delivery WHERE user_email = 'komata@example.com';
