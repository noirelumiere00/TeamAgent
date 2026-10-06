-- ============================================================
-- 0030: digest_preferences テーブル — 朝ダイジェストの「本人ごとの設定」
-- ============================================================
-- 目的: 利用者が Aico の DM で言った「Slack の欄はいらない」「来週まで止めて」
-- 「タスクって付く予定はリマインドしないで」等を、本人が変える/戻すまで **ずっと**
-- 保持し、毎朝の配信（scripts/run_morning_digest_fargate.py）がそれを読んで中身を変える。
--
-- 1 人 1 行。中身は JSON（項目の妥当性はアプリ側の pydantic
-- ``teamagent.skills.morning_digest.preferences.DigestPreferences`` が書く時も読む時も検査する）。
-- DB 側は「object であること」「大きすぎないこと」だけを縛る（項目を増やすたびに
-- migration を足さずに済むように）。
--
-- ⚠️ 読めないときの倒し方は **既定の設定で配信**（fail-open）。止めたはずの人に 1 通
--    届くのは「うるさい」で済むが、設定表の障害で全員に届かないのは見逃しを生む。
--
-- RLS: 本人行のみ（app.user_email GUC・0025/0026/0027 と同型）。FORCE で owner にも適用。
--
-- ロールバック:
--   REVOKE SELECT, INSERT, UPDATE, DELETE ON digest_preferences FROM teamagent_app;
--   DROP POLICY IF EXISTS digest_preferences_self ON digest_preferences;
--   DROP TABLE IF EXISTS digest_preferences;
-- 関連: infra/migrations/0025_digest_ack.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS digest_preferences (
    user_email   TEXT PRIMARY KEY
                 CHECK (user_email <> '' AND position('@' IN user_email) > 0),
    prefs        JSONB NOT NULL
                 CHECK (jsonb_typeof(prefs) = 'object' AND octet_length(prefs::text) <= 4096),
    -- 楽観ロック用。書くたびに +1（同時に 2 か所から変えたとき、後から来た方を黙って勝たせない）。
    version      INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE digest_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE digest_preferences FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS digest_preferences_self ON digest_preferences;
CREATE POLICY digest_preferences_self ON digest_preferences
    USING (user_email = current_setting('app.user_email', true))
    WITH CHECK (user_email = current_setting('app.user_email', true));

-- ON CONFLICT DO UPDATE は arbiter 列の SELECT 権限と SELECT の RLS を要求する
-- （0024/0025 で実測済み）。表単位で付与する。
GRANT SELECT, INSERT, UPDATE, DELETE ON digest_preferences TO teamagent_app;

-- 適用後の検証 (SSM トンネル):
--   SET app.user_email = 'komata@example.com';
--   SET ROLE teamagent_app;
--   INSERT INTO digest_preferences (user_email, prefs) VALUES ('komata@example.com', '{}')
--     ON CONFLICT (user_email) DO UPDATE SET prefs = EXCLUDED.prefs;   -- 1 行
--   INSERT INTO digest_preferences (user_email, prefs) VALUES ('other@example.com', '{}');
--     -- ERROR: new row violates row-level security policy（他人の行は書けない）
--   DELETE FROM digest_preferences WHERE user_email = 'komata@example.com';
