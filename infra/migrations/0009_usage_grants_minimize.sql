-- ============================================================
-- 0009: usage/metrics テーブルの teamagent_app 権限を INSERT のみに最小化
-- ============================================================
-- 0007/0008 は「Bot(teamagent_app) は INSERT のみ・SELECT 不可（RLS と二重防御）」を意図したが、
-- 0002 の `ALTER DEFAULT PRIVILEGES ... GRANT SELECT,INSERT,UPDATE,DELETE ... TO teamagent_app`
-- が **新規テーブル作成時に SELECT/UPDATE/DELETE も自動付与**するため、実体は full DML だった。
-- （RLS により Bot は admin GUC 無しで 0 行＝実害は無いが、設計/ドキュメントと実体が食い違う。）
--
-- そこで余剰権限を REVOKE し、「Bot は INSERT のみ」を**実体化**する（RLS と grant の二重防御）。
-- INSERT は残す（usage_recorder / metrics_snapshot が書く）。teamagent_dashboard は対象外
-- （0002 の default privileges は teamagent_app 宛のみ＝dashboard は 0007/0008 の明示 SELECT だけ）。
-- REVOKE は冪等（無い権限の REVOKE は no-op）。
-- ============================================================

REVOKE SELECT, UPDATE, DELETE ON usage_events FROM teamagent_app;
REVOKE SELECT, UPDATE, DELETE ON usage_event_calls FROM teamagent_app;
REVOKE SELECT, UPDATE, DELETE ON runtime_metrics FROM teamagent_app;

COMMENT ON TABLE usage_events IS
    '管理画面の一次データ: 1リクエスト1行。Bot=INSERTのみ(0009で最小化)・管理者GUCのみSELECT(RLS)';
