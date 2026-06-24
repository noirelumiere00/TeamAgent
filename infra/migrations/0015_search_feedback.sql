-- ============================================================
-- 0015: search_feedback — 資料検索 Web UI の 👍/👎 フィードバック
-- ============================================================
-- 目的:
--   connect-web の「小俣さん専用 資料検索 Web UI」(P4) で、AI 要約(answer) と
--   個別ヒット(chunk) に対する 👍/👎 を 1 行 1 評価で永続化する。検索品質の
--   オフライン評価（gold set の補強・回帰検知）の一次データとして使う。
--
-- セキュリティ / プライバシー:
--   - 本文(answer/chunk content)は保存しない。query（検索語）と評価対象の ID のみ。
--   - user_email は cookie セッションから取得（クライアント入力を信用しない）。
--   - note は任意の短いメモ（PII を促さない UI 文言にする）。
--
-- 安全性: IF NOT EXISTS で冪等。追加のみ（既存テーブル・データは不変）。
-- ロールバック:
--   DROP INDEX IF EXISTS search_feedback_user_created_idx;
--   DROP TABLE IF EXISTS search_feedback;
--
-- 関連: src/teamagent/connect_web/app.py（/api/v1/feedback）

CREATE TABLE IF NOT EXISTS search_feedback (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- 評価者（cookie セッションの本人 email・lower 正規化）。
    user_email   TEXT NOT NULL,
    -- 評価対象の検索語（本文ではなくクエリ文字列のみ）。
    query        TEXT NOT NULL,
    -- 評価対象の種別。'answer'=AI 要約全体 / 'chunk'=個別ヒット。
    target_type  TEXT NOT NULL CHECK (target_type IN ('answer', 'chunk')),
    -- 個別ヒットのとき、元 document / chunk を後追いできる識別子（任意）。
    doc_id       TEXT,
    chunk_id     BIGINT,
    -- 評価値。+1=👍 / -1=👎。
    rating       SMALLINT NOT NULL CHECK (rating IN (-1, 1)),
    -- 任意の短いメモ（本文・PII は入れない運用）。
    note         TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 本人の最近の評価を時系列で引く（個人ダッシュボード/評価集計用）。
CREATE INDEX IF NOT EXISTS search_feedback_user_created_idx
    ON search_feedback (user_email, created_at DESC);

-- teamagent_app role に INSERT 権限付与（migration 0002 / 0005 / 0014 の流儀踏襲）。
-- connect-web は teamagent_app で接続し、本テーブルに評価を書き込む。
GRANT SELECT, INSERT ON search_feedback TO teamagent_app;

COMMENT ON TABLE search_feedback IS
    '資料検索 Web UI(P4) の 👍/👎 フィードバック。本文は保存せず query/対象IDのみ（評価の一次データ）';
