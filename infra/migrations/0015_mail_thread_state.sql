-- ============================================================
-- 0015: mail_thread_state テーブル — メールサマリーのボタン操作状態（per-user・RLS）
-- ============================================================
-- インタラクティブなメールサマリー（[対応する][対応済み][後で][…]）の状態を保持する。
--   - status: open（未対応）/ done（対応済み）/ snoozed（後で・再通知待ち）/ muted（今後通知しない）
--   - 「後で」= snoozed + snooze_until。reminder スキャンジョブが期限到来分を再通知する。
--   - 「対応済み」「今後通知しない」= 再通知停止。「取り消す」で open に戻す。
--
-- セキュリティ（oauth_tokens=0006 と同型）:
--   - RLS で「本人行（user_email = app.user_email GUC）」しか SELECT/変更できない。
--     interactivity ハンドラはボタンを押した本人を Slack で解決し app.user_email にセットする
--     ＝他人のスレッド状態を操作できない（per-user 分離の DB 側担保）。
--   - reminder スキャンジョブは admin ロール（app.user_role='admin'）で全行を走査する
--     （morning_digest と同じ信頼境界のバックエンド）。
--   - thread_id は Gmail の不透明 ID（生 messageId ではない）。生本文・生件名は保存しない
--     （subject_scrubbed は DLP マスク後）。
-- ============================================================

CREATE TABLE IF NOT EXISTS mail_thread_state (
    -- 正規化済み email（oauth_tokens と同規約）。空 / @ 無しを構造的に禁止。
    user_email          TEXT NOT NULL
                        CHECK (user_email <> '' AND position('@' IN user_email) > 0),
    -- Gmail スレッド ID（このスレッド単位で状態を持つ）。
    thread_id           TEXT NOT NULL CHECK (thread_id <> ''),
    -- 状態。CHECK で語彙を固定（不正値で RLS/scan が崩れるのを防ぐ）。
    status              TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'done', 'snoozed', 'muted')),
    -- 「後で」の再通知時刻（status='snoozed' の時のみ意味を持つ）。
    snooze_until        TIMESTAMPTZ,
    -- 再通知メッセージ描画用の DLP マスク済みメタ（生件名・生 From は保存しない）。
    subject_scrubbed    TEXT NOT NULL DEFAULT '',
    counterpart_masked  TEXT NOT NULL DEFAULT '',
    -- 直近に（再）通知した時刻。多重リマインド抑止に使う。
    last_notified_at    TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_email, thread_id)
);

-- reminder スキャン（status='snoozed' AND snooze_until<=now()）の索引。
CREATE INDEX IF NOT EXISTS mail_thread_state_due_idx
    ON mail_thread_state (status, snooze_until);

-- updated_at 自動更新トリガー（0006 の流儀踏襲）
CREATE OR REPLACE FUNCTION mail_thread_state_set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS mail_thread_state_updated_at_trg ON mail_thread_state;
CREATE TRIGGER mail_thread_state_updated_at_trg
    BEFORE UPDATE ON mail_thread_state
    FOR EACH ROW EXECUTE FUNCTION mail_thread_state_set_updated_at();

-- RLS: 本人行のみ（app.user_email GUC と一致）。admin ロールは全行可（reminder scan / 運用）。
-- FORCE で table owner も RLS 対象にする（teamagent_app の越権を防ぐ）。
ALTER TABLE mail_thread_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE mail_thread_state FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS mail_thread_state_self ON mail_thread_state;
CREATE POLICY mail_thread_state_self ON mail_thread_state
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

-- teamagent_app に DML 権限（0002 / 0006 の流儀）
GRANT SELECT, INSERT, UPDATE, DELETE ON mail_thread_state TO teamagent_app;

COMMENT ON TABLE mail_thread_state IS
    'メールサマリーのボタン操作状態（open/done/snoozed/muted・本人行RLS）。0015';
COMMENT ON COLUMN mail_thread_state.snooze_until IS
    '「後で」の再通知時刻。reminder スキャンが status=snoozed AND snooze_until<=now() を再通知';
