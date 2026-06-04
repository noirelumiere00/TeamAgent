-- ============================================================
-- 0007: usage_events — 管理画面の一次データ（1リクエスト1行）
-- ============================================================
-- コスト・利用・レイテンシ・エラーは現状 structlog（bedrock_converse 等）にしか出ておらず、
-- トレンドを SQL で引けない。管理画面（利用状況/コスト/レイテンシ/エラー/混雑）の一次ソースとして、
-- dispatch_auto の出口で「1リクエスト = 1行」を永続化する。設計:
--   docs/poc/scale_countermeasures_dropin_spec.md / 管理画面設計（Agent協議）。
--
-- セキュリティ / プライバシー:
--   - **本文は保存しない**（query_chars で規模のみ）。PII/secret/トークンを列に持たない（G8）。
--   - cost_usd は skill 出力の total_cost_usd（Gemini 込みの権威コスト）。
--   - 読みは管理用途のみ: RLS で app.user_role='admin' の接続だけが SELECT 可。
--     書きは Bot（teamagent_app）が INSERT のみ（SELECT 権限は与えない＝二重防御）。
--   - 専用 read-only ロール teamagent_dashboard を切り、管理画面はこれで read する。
-- ============================================================

-- リクエストの最終ステータス（dispatch_auto 出口で確定）。enum でなく TEXT+CHECK（拡張容易）。
CREATE TABLE IF NOT EXISTS usage_events (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- トレース ID（Bot の req-xxxx、Sentry tag と突合可能）。再試行/二重書込に安全な UNIQUE。
    request_id       TEXT NOT NULL,
    occurred_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_email       TEXT,                  -- 解決失敗時 NULL 許容（user_id で救済）
    user_id          TEXT,                  -- Slack U...（email 解決失敗時の予備キー）
    skill            TEXT NOT NULL,         -- search/clientkarte/.../operation_log/unknown
    cost_usd         NUMERIC(12, 6) NOT NULL DEFAULT 0,  -- skill.total_cost_usd（権威・Gemini込）
    latency_ms       INTEGER,               -- dispatch 入口→出口の壁時計 ms
    input_tokens     INTEGER,               -- Bedrock 明細を集約できた時のみ（無ければ NULL）
    output_tokens    INTEGER,
    status           TEXT NOT NULL DEFAULT 'ok'
                     CHECK (status IN ('ok', 'error', 'queue_full', 'timeout')),
    error_code       TEXT,                  -- 例外クラス名等（本文は入れない）
    throttle_retries INTEGER NOT NULL DEFAULT 0,
    query_chars      INTEGER,               -- クエリ文字数のみ（本文は保存しない）
    via              TEXT,                  -- mention/dm/slash 等の経路
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT usage_events_request_id_unique UNIQUE (request_id)
);

CREATE INDEX IF NOT EXISTS usage_events_occurred_idx
    ON usage_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS usage_events_user_occurred_idx
    ON usage_events (user_email, occurred_at DESC);
CREATE INDEX IF NOT EXISTS usage_events_skill_occurred_idx
    ON usage_events (skill, occurred_at DESC);
-- エラー一覧（直近の失敗）用の部分インデックス
CREATE INDEX IF NOT EXISTS usage_events_error_idx
    ON usage_events (occurred_at DESC) WHERE status <> 'ok';

-- Bedrock 呼び出し明細（任意・drill-down 用）。画面のコスト合計には使わない（二重計上回避）。
CREATE TABLE IF NOT EXISTS usage_event_calls (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id         TEXT NOT NULL,
    occurred_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind               TEXT NOT NULL,       -- 'converse' | 'rerank'
    model_id           TEXT NOT NULL,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd           NUMERIC(12, 6) NOT NULL DEFAULT 0,
    latency_ms         INTEGER,
    stop_reason        TEXT
);
CREATE INDEX IF NOT EXISTS usage_event_calls_request_idx
    ON usage_event_calls (request_id);
CREATE INDEX IF NOT EXISTS usage_event_calls_model_occurred_idx
    ON usage_event_calls (model_id, occurred_at DESC);

-- ------------------------------------------------------------
-- read-only ロール teamagent_dashboard（管理画面専用・0002 の流儀）
-- ------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'teamagent_dashboard') THEN
        -- NOLOGIN: 直接接続させない（teamagent 経由で SET ROLE）。NOBYPASSRLS で RLS 対象。
        CREATE ROLE teamagent_dashboard NOLOGIN NOBYPASSRLS;
    END IF;
    -- master ユーザー teamagent から SET ROLE teamagent_dashboard を許可（未付与時のみ）
    IF NOT EXISTS (
        SELECT 1 FROM pg_auth_members am
        JOIN pg_roles parent ON am.roleid = parent.oid
        JOIN pg_roles child  ON am.member = child.oid
        WHERE parent.rolname = 'teamagent_dashboard' AND child.rolname = 'teamagent'
    ) THEN
        GRANT teamagent_dashboard TO teamagent;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO teamagent_dashboard;

-- ------------------------------------------------------------
-- RLS: 管理閲覧専用（admin GUC のみ SELECT 可）。Bot は INSERT のみ。
-- ------------------------------------------------------------
ALTER TABLE usage_events      ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_events      FORCE  ROW LEVEL SECURITY;
ALTER TABLE usage_event_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_event_calls FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS usage_events_admin_read ON usage_events;
CREATE POLICY usage_events_admin_read ON usage_events
    FOR SELECT
    USING (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS usage_events_app_insert ON usage_events;
CREATE POLICY usage_events_app_insert ON usage_events
    FOR INSERT
    WITH CHECK (true);

DROP POLICY IF EXISTS usage_event_calls_admin_read ON usage_event_calls;
CREATE POLICY usage_event_calls_admin_read ON usage_event_calls
    FOR SELECT
    USING (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS usage_event_calls_app_insert ON usage_event_calls;
CREATE POLICY usage_event_calls_app_insert ON usage_event_calls
    FOR INSERT
    WITH CHECK (true);

-- ------------------------------------------------------------
-- 権限: Bot=INSERT のみ / 管理画面=SELECT のみ（最小権限）
-- ------------------------------------------------------------
GRANT INSERT ON usage_events      TO teamagent_app;
GRANT INSERT ON usage_event_calls TO teamagent_app;
GRANT SELECT ON usage_events      TO teamagent_dashboard;
GRANT SELECT ON usage_event_calls TO teamagent_dashboard;
-- oauth_tokens の連携状況も管理画面で見るが、**暗号化列 refresh_token_enc は列単位で除外**。
-- これで dashboard ロールは ciphertext を SELECT する権限自体を持たない（復号以前に読めない）。
GRANT SELECT (user_email, scopes, created_at, updated_at) ON oauth_tokens TO teamagent_dashboard;

COMMENT ON TABLE usage_events IS
    '管理画面の一次データ: 1リクエスト1行（利用/コスト/レイテンシ/エラー）。本文/PIIは保存しない';
COMMENT ON COLUMN usage_events.cost_usd IS
    'skill.total_cost_usd（Gemini込の権威コスト）。画面のコスト合計は必ずこの列を使う';
COMMENT ON TABLE usage_event_calls IS
    'Bedrock 呼び出し明細（任意・drill-down 用）。画面コスト合計には使わない（二重計上回避）';
COMMENT ON ROLE teamagent_dashboard IS
    '管理画面専用 read-only ロール（NOLOGIN）。usage/metrics と oauth_tokens 非暗号列のみ SELECT 可';
