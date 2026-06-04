-- ============================================================
-- 0008: runtime_metrics — RequestGate / 接続プールの定期スナップショット
-- ============================================================
-- GateMetrics（同時実行・キュー・拒否）と PoolStats（接続プール）は Bot プロセスの
-- メモリ内・揮発値で、別プロセスの管理画面からは直接読めない。Bot 内の常駐タスクが
-- 一定間隔（既定15秒）でスナップショットを1行ずつ INSERT し、画面はこれを read する。
--
-- 設計: 管理画面（Agent協議）。書きは Bot（teamagent_app）INSERT のみ、読みは admin GUC のみ。
-- 累計カウンタ（accepted/rejected_* 等）は区間差分でレートを出せるよう生値を保持する。
-- instance_id で将来のマルチプロセス/マルチホストにも備える（MVPは単一プロセス）。
-- ============================================================

CREATE TABLE IF NOT EXISTS runtime_metrics (
    id                       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    captured_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    instance_id              TEXT NOT NULL DEFAULT '',  -- host:pid（複数プロセス集約用）
    -- RequestGate（GateMetrics）
    gate_in_flight           INTEGER NOT NULL,
    gate_peak_in_flight      INTEGER NOT NULL,
    gate_waiting             INTEGER NOT NULL,
    gate_peak_waiting        INTEGER NOT NULL,
    gate_accepted            BIGINT  NOT NULL,
    gate_completed           BIGINT  NOT NULL,
    gate_failed              BIGINT  NOT NULL,
    gate_rejected_queue_full BIGINT  NOT NULL,
    gate_rejected_timeout    BIGINT  NOT NULL,
    gate_concurrency         INTEGER NOT NULL,          -- 設定値（飽和率の分母）
    gate_queue_max           INTEGER NOT NULL,
    -- 接続プール（PoolStats）。プール無効（直結）時は NULL 許容。
    pool_max_size            INTEGER,
    pool_in_use              INTEGER,
    pool_idle                INTEGER,
    pool_open_total          INTEGER,
    pool_created             BIGINT,
    pool_closed              BIGINT,
    pool_timeouts            BIGINT,
    pool_reset_failures      BIGINT
);
CREATE INDEX IF NOT EXISTS runtime_metrics_captured_idx
    ON runtime_metrics (captured_at DESC);

ALTER TABLE runtime_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE runtime_metrics FORCE  ROW LEVEL SECURITY;

DROP POLICY IF EXISTS runtime_metrics_admin_read ON runtime_metrics;
CREATE POLICY runtime_metrics_admin_read ON runtime_metrics
    FOR SELECT
    USING (current_setting('app.user_role', true) = 'admin');
DROP POLICY IF EXISTS runtime_metrics_app_insert ON runtime_metrics;
CREATE POLICY runtime_metrics_app_insert ON runtime_metrics
    FOR INSERT
    WITH CHECK (true);

GRANT INSERT ON runtime_metrics TO teamagent_app;
GRANT SELECT ON runtime_metrics TO teamagent_dashboard;

COMMENT ON TABLE runtime_metrics IS
    'RequestGate/接続プールの定期スナップショット（既定15秒）。Bot揮発メトリクスを画面化する';
