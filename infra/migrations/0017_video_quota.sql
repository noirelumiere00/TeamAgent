-- ============================================================
-- 0017: video_usage テーブル — 動画分析のユーザー×月クォータ台帳（v0.3 Task 10）
-- ============================================================
-- 目的: Gemini（Vertex=GCP課金・AWS Budgets 対象外）の動画分析に月間上限を設ける。
-- 計数単位は「Gemini に投げた実本数」（Step 0 裁定: video_analysis=1本/呼、
-- video_algorithm=実分析本数。video_approval は計数のみ・ブロックしない）。
-- month は **JST の YYYY-MM**（裁定: 利用者全員 JST。Budgets(UTC) との境界ズレは許容）。
--
-- 既存 usage_events(0007) を流用しない理由（監査）: app ロールは INSERT のみで
-- SELECT 不可（admin 専用設計）＝クォータ判定に使えない。本テーブルは
-- 「本人行のみ SELECT/INSERT/UPDATE 可」の RLS で原子的 upsert を行う。
--
-- ℹ️ 採番重複（0016×2本）は解消済み: slack_oauth_tokens を 0018 へ改番（PR#202）。
--    現在 0016 は chunks_embedding_cohere のみ。デプロイ時は本番 schema_migrations の
--    '0016'=chunks を一応確認（本番=search-filters系＝chunks 適用済の前提）。
-- ============================================================

CREATE TABLE IF NOT EXISTS video_usage (
    -- 正規化済み email（lower/trim）。RLS 束縛キー。
    user_email  TEXT NOT NULL
                CHECK (user_email <> '' AND position('@' IN user_email) > 0),
    -- JST の月（'2026-07' 形式）。アプリ側が JST で算出して渡す。
    month       CHAR(7) NOT NULL CHECK (month ~ '^[0-9]{4}-[0-9]{2}$'),
    used        INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_email, month)
);

-- updated_at の自動更新トリガー（0006/0016 の流儀踏襲）
CREATE OR REPLACE FUNCTION video_usage_set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_video_usage_updated_at ON video_usage;
CREATE TRIGGER trg_video_usage_updated_at
    BEFORE UPDATE ON video_usage
    FOR EACH ROW EXECUTE FUNCTION video_usage_set_updated_at();

-- RLS: 本人行のみ（app.user_email GUC・0006/0016 と同型）。FORCE で owner にも適用。
ALTER TABLE video_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE video_usage FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS video_usage_self ON video_usage;
CREATE POLICY video_usage_self ON video_usage
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

-- アプリロールへ最小権限（SELECT は自行のみ RLS で束縛される）。
GRANT SELECT, INSERT, UPDATE ON video_usage TO teamagent_app;
