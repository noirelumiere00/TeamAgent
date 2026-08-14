-- ============================================================
-- 0024: usage_events — ON CONFLICT に必要な最小 SELECT 権限の付与
-- ============================================================
-- 経緯:
--   0007 の設計は「Bot(teamagent_app) は INSERT のみ」の最小権限だったが、
--   usage_recorder の INSERT ... ON CONFLICT (request_id) DO NOTHING は
--   PostgreSQL 仕様上 (1) arbiter 列 request_id の SELECT 権限と
--   (2) SELECT の RLS ポリシーの両方を要求する。欠けると
--   permission denied → RLS violation の順で失敗する（2026-08-14 本番実測
--   usage_event_write_failed / docker postgres:16 で再現・修正・検証済み）。
-- セキュリティ:
--   - SELECT 権限は request_id **列のみ**（列単位 GRANT）。query_text 等の
--     他列は引き続き読めない（再現環境で permission denied を実測確認）。
--   - RLS ポリシーは teamagent_app 限定。admin ゲート
--     （usage_events_admin_read）と管理ページ email allowlist は不変。
-- 安全性:
--   - GRANT は冪等。CREATE POLICY は IF NOT EXISTS 相当が無いため
--     DROP POLICY IF EXISTS → CREATE で冪等化（0007 と同じ流儀）。
-- ロールバック:
--   REVOKE SELECT (request_id) ON usage_events FROM teamagent_app;
--   DROP POLICY IF EXISTS usage_events_app_conflict_read ON usage_events;
-- 関連: src/teamagent/runtime/usage_recorder.py（_INSERT_SQL）,
--       infra/migrations/0007_usage_events.sql, 0023_usage_query_text.sql

GRANT SELECT (request_id) ON usage_events TO teamagent_app;

DROP POLICY IF EXISTS usage_events_app_conflict_read ON usage_events;
CREATE POLICY usage_events_app_conflict_read ON usage_events
    FOR SELECT
    TO teamagent_app
    USING (true);
-- ============================================================
