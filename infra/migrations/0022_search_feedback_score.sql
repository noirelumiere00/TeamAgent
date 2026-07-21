-- ============================================================
-- 0022: search_feedback — AI回答の4段階評価(score)・検索相関ID・answer同定ハッシュ・INSERT-only化
-- ============================================================
-- 目的:
--   /search の AI 要約(answer)への4段階評価(score 1..4)と相関キーを追加する。
--   既存 rating(-1/+1) は温存し、アプリ層が score から決定論写像して両方書く
--   （4,3→+1 / 2,1→-1）。写像は本 migration の CHECK でも強制する。
-- セキュリティ / プライバシー:
--   - 本文(answer/chunk content)は引き続き保存しない（0015 の契約は不変）。
--   - note に自由記述(PII混入リスク)が初めて流入するため、app ロールを INSERT-only 化する。
--     SELECT を含む読み取りは owner/admin 経路（トンネル）限定。
--   - search_session_id はフロント生成 UUID（英数とハイフンのみ・信頼しない補助キー）。
-- 安全性:
--   - ADD COLUMN IF NOT EXISTS / 命名 CHECK の存在チェックで冪等。追加のみ。
-- ロールバック:
--   GRANT SELECT ON search_feedback TO teamagent_app;
--   DROP INDEX IF EXISTS search_feedback_session_idx;
--   ALTER TABLE search_feedback DROP CONSTRAINT IF EXISTS search_feedback_score_rating_map;
--   ALTER TABLE search_feedback DROP CONSTRAINT IF EXISTS search_feedback_score_range;
--   ALTER TABLE search_feedback DROP COLUMN IF EXISTS answer_id;
--   ALTER TABLE search_feedback DROP COLUMN IF EXISTS search_session_id;
--   ALTER TABLE search_feedback DROP COLUMN IF EXISTS score;
-- 関連: src/teamagent/connect_web/app.py（/api/v1/feedback, /api/v1/search）

ALTER TABLE search_feedback ADD COLUMN IF NOT EXISTS score SMALLINT;
ALTER TABLE search_feedback ADD COLUMN IF NOT EXISTS search_session_id TEXT;
ALTER TABLE search_feedback ADD COLUMN IF NOT EXISTS answer_id TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'search_feedback_score_range') THEN
        ALTER TABLE search_feedback
            ADD CONSTRAINT search_feedback_score_range
            CHECK (score IS NULL OR score BETWEEN 1 AND 4);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'search_feedback_score_rating_map') THEN
        ALTER TABLE search_feedback
            ADD CONSTRAINT search_feedback_score_rating_map
            CHECK (score IS NULL
                   OR (score >= 3 AND rating = 1)
                   OR (score <= 2 AND rating = -1));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS search_feedback_session_idx
    ON search_feedback (search_session_id, created_at DESC)
    WHERE search_session_id IS NOT NULL;

REVOKE SELECT, UPDATE, DELETE ON search_feedback FROM teamagent_app;

COMMENT ON COLUMN search_feedback.score IS
    'AI回答の4段階評価。4=◎期待どおり/3=○おおむね/2=△物足りない/1=×見当違い。NULL=旧👍/👎行';
COMMENT ON COLUMN search_feedback.answer_id IS
    'answer 本文の SHA-256 先頭16hex（サーバー計算・本文非保存のまま同一 answer を同定。クライアント申告値のため単独では受領事実を証明しない）';
COMMENT ON COLUMN search_feedback.search_session_id IS
    'フロント生成の検索実行相関ID（信頼しない補助キー）';

-- 適用後検証 (owner/admin の SSM トンネル経路):
--   SELECT conname, pg_get_constraintdef(oid)
--     FROM pg_constraint
--    WHERE conrelid = 'search_feedback'::regclass
--      AND conname IN ('search_feedback_score_range', 'search_feedback_score_rating_map');
--   SELECT indexname FROM pg_indexes WHERE indexname = 'search_feedback_session_idx';
--   SELECT privilege_type FROM information_schema.role_table_grants
--    WHERE table_name = 'search_feedback' AND grantee = 'teamagent_app';
