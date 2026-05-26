-- ============================================================
-- 0002: アプリ実行ロール `teamagent_app` を分離して RLS を機能させる
-- ============================================================
-- Sprint 3 / Hotfix — migration 0001 適用後の検証で「`teamagent` が
-- documents/chunks の owner だと FORCE ROW LEVEL SECURITY を入れても
-- 実質 bypass される」事象を確認したため、業界標準のロール分離パターンに移行。
--
-- 設計:
--   - `teamagent`         = schema 管理 / migration 専用（owner）
--   - `teamagent_app`     = アプリ実行用（NOBYPASSRLS、owner ではない）
--     - SearchSkill / ingest パイプラインは session 開始時に SET ROLE teamagent_app
--     - そうすると owner-bypass が外れ、RLS が確実に効く
--
-- RDS の master ユーザー `teamagent` に teamagent_app を GRANT しておけば、
-- 単一接続から SET ROLE で切り替えられる（追加の接続パスワード不要）。
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'teamagent_app') THEN
        -- NOLOGIN: 直接接続させない（必ず teamagent 経由で SET ROLE）
        -- NOBYPASSRLS: 明示的に RLS を bypass しない
        CREATE ROLE teamagent_app NOLOGIN NOBYPASSRLS;
    END IF;

    -- teamagent から teamagent_app への切替を許可
    -- （teamagent ユーザーが SET ROLE teamagent_app できるようにする）
    -- 既に GRANT 済みなら何もしない
    IF NOT EXISTS (
        SELECT 1 FROM pg_auth_members am
        JOIN pg_roles parent ON am.roleid = parent.oid
        JOIN pg_roles child  ON am.member = child.oid
        WHERE parent.rolname = 'teamagent_app' AND child.rolname = 'teamagent'
    ) THEN
        GRANT teamagent_app TO teamagent;
    END IF;
END
$$;

-- アプリ実行に必要な権限を付与
-- documents / chunks には DML（SELECT/INSERT/UPDATE/DELETE）を許可
GRANT SELECT, INSERT, UPDATE, DELETE ON documents TO teamagent_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON chunks TO teamagent_app;
-- schema_migrations は読み取りのみ（migrate.py 自体は teamagent で実行）
GRANT SELECT ON schema_migrations TO teamagent_app;
-- USAGE: schema public への到達権
GRANT USAGE ON SCHEMA public TO teamagent_app;

-- 既存テーブル（proposals_chunks / proposals_chunks_contextual）も
-- アプリから読み書きできるように（後方互換、RLS はかかってないが INSERT/SELECT は必要）
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = 'public' AND table_name = 'proposals_chunks') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON proposals_chunks TO teamagent_app';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = 'public' AND table_name = 'proposals_chunks_contextual') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON proposals_chunks_contextual TO teamagent_app';
    END IF;
END
$$;

-- 将来追加されるテーブル / シーケンスにも自動で権限を付ける
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO teamagent_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO teamagent_app;

-- ============================================================
-- 検証用 COMMENT
-- ============================================================
COMMENT ON ROLE teamagent_app IS
    'TeamAgent v3.2: アプリ実行ロール（NOBYPASSRLS）。SearchSkill / ingest が SET ROLE で切り替える';
