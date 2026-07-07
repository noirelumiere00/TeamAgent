-- ============================================================
-- 0016: slack_oauth_tokens テーブル — per-user Slack OAuth (xoxp) の永続化
-- ============================================================
-- 各メンバーが自分の Slack を個人認可（User Token Scopes）して得た user token(xoxp) を
-- user_email 単位で保管し、リクエスト時に本人のトークンを選ぶ。Google の oauth_tokens
-- (0006) と対称の設計。xoxp は「本人としての検索・履歴読取」に使い、ワークスペース共有の
-- bot token(xoxb) とは別経路にする。
--
-- ⚠️ xoxp は本人なりすまし級の高感度資格情報（G8）。0006 と同じ二重防御:
--   - xoxp_token_enc は **KMS で暗号化した ciphertext (BYTEA) のみ** を保存。平文は持たない。
--   - RDS 自体も storage_encrypted=true（at-rest）。
--   - RLS で「本人行（user_email = app.user_email GUC）」しか SELECT/変更できない。
--     ⇒ 既存 pgvector.connection(app_role, user_email) の GUC をそのまま流用（RLS は 0006 と同型）。
-- ============================================================

CREATE TABLE IF NOT EXISTS slack_oauth_tokens (
    -- 正規化済み email（lower/trim、SlackTokenStore._norm と同じ規約）。RLS 束縛キー。
    user_email          TEXT PRIMARY KEY
                        CHECK (user_email <> '' AND position('@' IN user_email) > 0),
    -- KMS Encrypt の CiphertextBlob。平文 xoxp は決して保存しない。
    xoxp_token_enc      BYTEA NOT NULL,
    -- 参照/整合用（本人の Slack user id / workspace id）。RLS の束縛には使わない。
    slack_user_id       TEXT NOT NULL DEFAULT '',
    team_id             TEXT NOT NULL DEFAULT '',
    -- 認可済み user scope（search:read / *:history / users:read 等）
    scopes              TEXT[] NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 同一 Slack アカウントが複数 email 行に割れるのを防ぐ（id 判明時のみ）。
CREATE UNIQUE INDEX IF NOT EXISTS slack_oauth_tokens_slack_uid
    ON slack_oauth_tokens (team_id, slack_user_id)
    WHERE slack_user_id <> '';

-- updated_at の自動更新トリガー（0006 の流儀踏襲）
CREATE OR REPLACE FUNCTION slack_oauth_tokens_set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS slack_oauth_tokens_updated_at_trg ON slack_oauth_tokens;
CREATE TRIGGER slack_oauth_tokens_updated_at_trg
    BEFORE UPDATE ON slack_oauth_tokens
    FOR EACH ROW EXECUTE FUNCTION slack_oauth_tokens_set_updated_at();

-- RLS: 本人行のみ（app.user_email GUC と一致）。admin ロールは全行可（運用/失効管理）。
-- FORCE で table owner も RLS 対象にする（teamagent_app の越権を防ぐ）。0006 と同一ポリシー。
ALTER TABLE slack_oauth_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE slack_oauth_tokens FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS slack_oauth_tokens_self ON slack_oauth_tokens;
CREATE POLICY slack_oauth_tokens_self ON slack_oauth_tokens
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

-- teamagent_app に DML 権限（0002/0006 の流儀）
GRANT SELECT, INSERT, UPDATE, DELETE ON slack_oauth_tokens TO teamagent_app;

COMMENT ON TABLE slack_oauth_tokens IS
    'per-user Slack OAuth user token(xoxp)（KMS暗号化・本人行RLS）。Google oauth_tokens(0006) と対称。';
COMMENT ON COLUMN slack_oauth_tokens.xoxp_token_enc IS
    'KMS Encrypt の CiphertextBlob。平文 xoxp は保存しない（G8）。復号は KMS Decrypt 権限が必要';
