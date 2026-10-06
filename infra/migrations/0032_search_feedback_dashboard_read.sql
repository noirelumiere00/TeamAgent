-- ============================================================
-- 0032: search_feedback — 管理画面（teamagent_dashboard）からの読み取り
-- ============================================================
-- 目的:
--   connect-web /admin に「回答の評価（Slack・直近30日）」を出す
--   （src/teamagent/dashboard/queries.py の answer_feedback_summary）。
--   /admin は read-only ロール teamagent_dashboard で接続するが、0015 は search_feedback を
--   teamagent_app にしか GRANT していないため、この集計だけが権限不足で表示できない。
-- セキュリティ / プライバシー:
--   - 付けるのは管理画面専用ロールの SELECT だけ。teamagent_app の INSERT-only（0022）は不変
--     （app へ SELECT/UPDATE/DELETE を戻さない＝tests/adapters/test_search_feedback_migrations.py）。
--   - /admin は email allowlist（CONNECT_ADMIN_EMAILS・既定は小俣さんのみ）の後ろにある。
--   - 回答本文は保存していない（0015 の契約）。読めるのは query（検索語）と評価・note。
-- 安全性:
--   - GRANT は冪等。追加のみ（既存のテーブル・データ・他ロールの権限は不変）。
-- ロールバック:
--   REVOKE SELECT ON search_feedback FROM teamagent_dashboard;
-- 関連: infra/migrations/0007_usage_events.sql（teamagent_dashboard の作成）,
--       infra/migrations/0015_search_feedback.sql, 0022_search_feedback_score.sql
-- ============================================================

GRANT SELECT ON search_feedback TO teamagent_dashboard;

-- 適用後検証 (owner/admin の SSM トンネル経路):
--   SELECT grantee, privilege_type FROM information_schema.role_table_grants
--    WHERE table_name = 'search_feedback' ORDER BY grantee, privilege_type;
--   期待: teamagent_app=INSERT / teamagent_dashboard=SELECT（teamagent_app に SELECT は無い）
