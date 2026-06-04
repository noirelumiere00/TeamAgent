-- ============================================================
-- 0006: oauth_tokens テーブル — per-user OAuth refresh token の永続化
-- ============================================================
-- Workspace 5-7 サービス × 個人認可（per-user OAuth）の中核。各メンバーが自分の
-- Google を認可して得た refresh token を user_email 単位で保管し、リクエスト時に
-- 本人のトークンを選ぶ（docs/poc/workspace_integration_design.md §3）。
--
-- セキュリティ（G8）:
--   - refresh_token は **KMS で暗号化した ciphertext (BYTEA) のみ** を保存。平文は持たない。
--     → DB ダンプ / bastion 経由 psql でも、KMS Decrypt 権限(IAM)が無い限り読めない。
--   - RDS 自体も storage_encrypted=true（at-rest）。二重防御。
--   - RLS で「本人行（user_email = app.user_email GUC）」しか SELECT/変更できない。
--     → アプリにバグがあっても他人の token 行に触れない（per-user 分離の DB 側担保）。
--
-- 単一共有トークン（load_secrets.sh の teamagent/dev/google_oauth）からの移行先。
-- ============================================================

CREATE TABLE IF NOT EXISTS oauth_tokens (
    -- 正規化済み email（lower/trim、InMemoryTokenStore._norm と同じ規約）
    -- 空文字 / @ 無しを構造的に禁止（空 user_email で fail-closed と RLS が崩れるのを防ぐ）
    user_email          TEXT PRIMARY KEY
                        CHECK (user_email <> '' AND position('@' IN user_email) > 0),
    -- KMS Encrypt の CiphertextBlob。平文 refresh token は決して保存しない。
    refresh_token_enc   BYTEA NOT NULL,
    -- 認可済みスコープ（readonly 群）
    scopes              TEXT[] NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- updated_at の自動更新トリガー（0005 の流儀踏襲）
CREATE OR REPLACE FUNCTION oauth_tokens_set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS oauth_tokens_updated_at_trg ON oauth_tokens;
CREATE TRIGGER oauth_tokens_updated_at_trg
    BEFORE UPDATE ON oauth_tokens
    FOR EACH ROW EXECUTE FUNCTION oauth_tokens_set_updated_at();

-- RLS: 本人行のみ（app.user_email GUC と一致）。admin ロールは全行可（運用/失効管理）。
-- FORCE で table owner も RLS 対象にする（teamagent_app の越権を防ぐ）。
ALTER TABLE oauth_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE oauth_tokens FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS oauth_tokens_self ON oauth_tokens;
CREATE POLICY oauth_tokens_self ON oauth_tokens
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

-- teamagent_app に DML 権限（0002 の流儀）
GRANT SELECT, INSERT, UPDATE, DELETE ON oauth_tokens TO teamagent_app;

COMMENT ON TABLE oauth_tokens IS
    'per-user OAuth refresh token（KMS暗号化・本人行RLS）。docs/poc/workspace_integration_design.md §3';
COMMENT ON COLUMN oauth_tokens.refresh_token_enc IS
    'KMS Encrypt の CiphertextBlob。平文は保存しない（G8）。復号は KMS Decrypt 権限が必要';
