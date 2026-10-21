-- 表示済み資料の参照だけを本人・実会話ごとに10分保持する。本文・質問は保存しない。
-- 読出時は expires_at > NOW() が必須。同じ本人の次回保存時に期限切れ行を削除する。
-- 適用は別途。rollback: REVOKE ALL ON document_followup_refs FROM teamagent_app;
-- DROP TABLE document_followup_refs;

CREATE TABLE IF NOT EXISTS document_followup_refs (
    user_email TEXT NOT NULL
        CHECK (user_email = lower(user_email) AND position('@' IN user_email) > 1),
    -- 署名済み team/user/email/channel/thread から算出する。run/session は再起動で変わるため含めない。
    conversation_key TEXT NOT NULL CHECK (conversation_key ~ '^[0-9a-f]{64}$'),
    refs JSONB NOT NULL CHECK (
        jsonb_typeof(refs) = 'array'
        AND jsonb_array_length(refs) <= 5
        AND octet_length(refs::text) <= 65536
    ),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_email, conversation_key)
);

ALTER TABLE document_followup_refs ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_followup_refs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS document_followup_refs_self ON document_followup_refs;
CREATE POLICY document_followup_refs_self ON document_followup_refs
    USING (user_email = current_setting('app.user_email', true))
    WITH CHECK (user_email = current_setting('app.user_email', true));
REVOKE ALL ON document_followup_refs FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON document_followup_refs TO teamagent_app;
