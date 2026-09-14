-- ============================================================
-- 0027: digest_notice テーブル — 「お知らせ系 DM を同じ日に 2 通出さない」印
-- ============================================================
-- 目的: カレンダー未連携者への週 1 回のお知らせ（planner が出す 1 行 DM）を **冪等**
-- にする。planner のターゲットは retry_policy { maximum_retry_attempts = 1 }
-- （infra/terraform/morning_digest_schedule.tf）なので、途中で落ちて再実行されると
-- 未連携者全員に同じ DM が 2 通届く。配信予約の方は schedule 名が決定的で
-- ConflictException を成功扱いにするため再実行に耐えるが、お知らせには何も無かった。
--
-- ⚠️ なぜ digest_delivery（0026）へ相乗りしないか。2 つの理由でどちらも事故になる:
--   1. 0026 の主キーは (user_email, digest_date)＝「その日の **本文** を送る権」。
--      未連携者もダイジェスト本文（メール節）は一括実行で受け取るので、お知らせが
--      先に claim を取ると **その人のその日のダイジェストが丸ごと消える**。
--   2. 0026 の origin は CHECK (origin IN ('scheduled','bulk'))。'unlinked_notice' は
--      そもそも INSERT できない。
--   よって「同型・別テーブル」にする（claim の作法・RLS・14 日保持は 0026 と同じ）。
--
-- 設計は 0026 と同じ: 判定は DB の一意制約に委ね、INSERT ... ON CONFLICT DO NOTHING の
-- rowcount が 1 のときだけ送る。アプリ側の if 文で調停しない。
--
-- ⚠️ 本テーブルが無い環境では claim が例外になる。呼び出し側は **fail-closed**
--    （送らない）で倒す。お知らせは週 1 回なので、1 回落ちても翌週に出る。
--
-- ロールバック:
--   REVOKE SELECT, INSERT, DELETE ON digest_notice FROM teamagent_app;
--   DROP POLICY IF EXISTS digest_notice_self ON digest_notice;
--   DROP INDEX IF EXISTS idx_digest_notice_expires;
--   DROP TABLE IF EXISTS digest_notice;
-- 関連: infra/migrations/0026_digest_delivery.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS digest_notice (
    user_email   TEXT NOT NULL
                 CHECK (user_email <> '' AND position('@' IN user_email) > 0),
    -- お知らせの種類。増やすときはここへ足す（種類ごとに 1 日 1 通）。
    notice_kind  TEXT NOT NULL CHECK (notice_kind IN ('calendar_unlinked')),
    -- JST の暦日（YYYY-MM-DD）。時刻は持たない＝「その日 1 回」の粒度。
    notice_date  DATE NOT NULL,
    claimed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_email, notice_kind, notice_date)
);

-- 期限切れ掃除用（14 日保持。0026 と同じく claim と同じトランザクションで流す）。
CREATE INDEX IF NOT EXISTS idx_digest_notice_expires ON digest_notice (expires_at);

-- RLS: 本人行のみ（app.user_email GUC・0025/0026 と同型）。FORCE で owner にも適用。
ALTER TABLE digest_notice ENABLE ROW LEVEL SECURITY;
ALTER TABLE digest_notice FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS digest_notice_self ON digest_notice;
CREATE POLICY digest_notice_self ON digest_notice
    USING (user_email = current_setting('app.user_email', true))
    WITH CHECK (user_email = current_setting('app.user_email', true));

-- ON CONFLICT DO NOTHING は arbiter 列の SELECT 権限と SELECT の RLS を要求する
-- （0024/0025/0026 で実測済み）。表単位で付与する。
GRANT SELECT, INSERT, DELETE ON digest_notice TO teamagent_app;

-- 適用後の検証 (SSM トンネル):
--   SET app.user_email = 'komata@example.com';
--   SET ROLE teamagent_app;
--   INSERT INTO digest_notice (user_email, notice_kind, notice_date, expires_at)
--     VALUES ('komata@example.com', 'calendar_unlinked', CURRENT_DATE,
--             NOW() + INTERVAL '14 days')
--     ON CONFLICT DO NOTHING;               -- 1 行
--   INSERT INTO digest_notice (user_email, notice_kind, notice_date, expires_at)
--     VALUES ('komata@example.com', 'calendar_unlinked', CURRENT_DATE,
--             NOW() + INTERVAL '14 days')
--     ON CONFLICT DO NOTHING;               -- 0 行（＝2 通目を止められている）
--   DELETE FROM digest_notice WHERE user_email = 'komata@example.com';
