-- ============================================================
-- 0033: history_view_audit（上司 5「質問と回答の履歴」第 1 段・管理者閲覧の監査）
-- ============================================================
-- 目的: 管理者（HISTORY_ADMIN_EMAILS・v1 は小俣さん 1 名）が他人の Aico との DM を
--       /admin/history で見たことを記録する。DM の本文は保存しない（表示のたびに Slack から読む）。
--       記録するのは「誰が・いつ・誰の（U… の SHA-256 先頭 16 hex）・何日分を」だけ。
--       設計: Artifacts/plan-20261007/history_view_design_v1_20261014.md
--
-- 守ること（0029 本人メモの管理者閲覧と同じ型）:
--   ① 既存のどのロールにも表の権限を与えない。監査を書けるのは SECURITY DEFINER 関数 1 本だけで、
--      その EXECUTE を持つのは専用の history_view_auditor（connect_web が SET ROLE する）だけ。
--   ② 表と関数の所有者は NOLOGIN NOBYPASSRLS の history_view_definer。FORCE RLS が所有者にも効く。
--      実行者（master）は definer へ SET ROLE できない（本 migration の最後で外す）。
--   ③ ON CONFLICT も RETURNING も使わない（0024 の地雷: 最小権限ロールでは失敗する）。
--   ④ 本人画面（/app/history）は監査しない（本人が自分の DM を見るだけ）。
--
-- ロール:
--   history_view_definer … 表と関数の所有者。誰も SET ROLE できない
--   history_view_auditor … 監査関数の EXECUTE だけ（connect_web が SET ROLE）
--
-- ロールバック（順序どおり）:
--   GRANT history_view_definer TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;
--   DROP FUNCTION public.history_view_record(text, text, text, integer, text);
--   DROP TABLE public.history_view_audit;
--   REVOKE history_view_definer FROM CURRENT_USER;
--   DROP ROLE history_view_auditor, history_view_definer;
--
-- 関連: 0029（本人メモ・同じ型）、0021（REVOKE ALL → 最小 GRANT）、0002（既定権限）、0024（ON CONFLICT の地雷）
--
-- 適用後の検証（読み取りだけ）:
--   SELECT relname, relrowsecurity, relforcerowsecurity, pg_get_userbyid(relowner)
--     FROM pg_class WHERE relname = 'history_view_audit';
--   SELECT has_table_privilege('teamagent_app', 'public.history_view_audit', 'SELECT');           -- f
--   SELECT has_function_privilege('public', 'public.history_view_record(text,text,text,integer,text)', 'EXECUTE');  -- f
-- ============================================================

-- 1) ロール（NOLOGIN NOBYPASSRLS NOINHERIT）。既存ロールが危ない属性を持っていたら止める（fail-closed）
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['history_view_definer', 'history_view_auditor'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN NOBYPASSRLS NOINHERIT', r);
    ELSIF EXISTS (
      SELECT 1 FROM pg_roles
       WHERE rolname = r
         AND (rolsuper OR rolcanlogin OR rolbypassrls OR rolcreaterole OR rolcreatedb)
    ) THEN
      RAISE EXCEPTION 'history_view role % has unsafe attributes', r;
    END IF;
  END LOOP;
END $$;

-- 2) 表（本文を持たない。管理者 email・対象の SHA-256 先頭 16 hex・日数・理由コードだけ）
CREATE TABLE IF NOT EXISTS public.history_view_audit (
  audit_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  actor_email  TEXT NOT NULL CHECK (actor_email ~ '^[^@[:space:]]+@[^@[:space:]]+$'),
  target_sha16 TEXT NOT NULL CHECK (target_sha16 ~ '^[0-9a-f]{16}$'),
  days         INTEGER NOT NULL CHECK (days BETWEEN 1 AND 30),
  reason_code  TEXT NOT NULL CHECK (reason_code ~ '^[a-z][a-z0-9_]{0,39}$')
);
CREATE INDEX IF NOT EXISTS idx_history_view_audit_target
  ON public.history_view_audit (target_sha16, occurred_at);

-- 3) RLS（ENABLE＋FORCE。書けるのは定義者の INSERT だけ・誰も読めない）
ALTER TABLE public.history_view_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.history_view_audit FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS hv_audit_definer_insert ON public.history_view_audit;
CREATE POLICY hv_audit_definer_insert ON public.history_view_audit
  FOR INSERT TO history_view_definer WITH CHECK (true);

-- 4) 表の権限（0002 の既定権限で付く teamagent_app 等の権限を打ち消す）
REVOKE ALL PRIVILEGES ON public.history_view_audit FROM PUBLIC;
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['teamagent_app', 'teamagent_dashboard'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('REVOKE ALL PRIVILEGES ON public.history_view_audit FROM %I', r);
    END IF;
  END LOOP;
END $$;
GRANT USAGE ON SCHEMA public TO history_view_definer, history_view_auditor;

-- 5) SECURITY DEFINER 関数（search_path 固定・表は public. で修飾・動的 SQL なし）
CREATE OR REPLACE FUNCTION public.history_view_record(
    p_admin_email text, p_team_id text, p_slack_user_id text, p_days integer, p_reason_code text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
  IF p_admin_email IS NULL OR p_admin_email !~ '^[^@[:space:]]+@[^@[:space:]]+$' THEN
    RAISE EXCEPTION 'hv_bad_admin' USING ERRCODE = '22023';
  END IF;
  IF p_team_id IS NULL OR p_team_id !~ '^T[A-Z0-9]{8,}$'
     OR p_slack_user_id IS NULL OR p_slack_user_id !~ '^U[A-Z0-9]{8,}$' THEN
    RAISE EXCEPTION 'hv_bad_principal' USING ERRCODE = '22023';
  END IF;
  IF p_days IS NULL OR p_days < 1 OR p_days > 30 THEN
    RAISE EXCEPTION 'hv_bad_days' USING ERRCODE = '22023';
  END IF;
  IF p_reason_code IS NULL OR p_reason_code !~ '^[a-z][a-z0-9_]{0,39}$' THEN
    RAISE EXCEPTION 'hv_bad_reason' USING ERRCODE = '22023';
  END IF;
  INSERT INTO public.history_view_audit (actor_email, target_sha16, days, reason_code)
  VALUES (lower(p_admin_email),
          left(encode(sha256(convert_to(p_team_id || ':' || p_slack_user_id, 'UTF8')), 'hex'), 16),
          p_days, p_reason_code);
  RETURN true;
END
$fn$;

REVOKE ALL ON FUNCTION public.history_view_record(text, text, text, integer, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.history_view_record(text, text, text, integer, text)
  TO history_view_auditor;

-- 6) 所有者を定義者ロールへ移し、実行者から定義者への経路を外す（0029 と同じ手順・PG16）
GRANT history_view_definer TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;
GRANT CREATE ON SCHEMA public TO history_view_definer;
ALTER TABLE public.history_view_audit OWNER TO history_view_definer;
ALTER FUNCTION public.history_view_record(text, text, text, integer, text) OWNER TO history_view_definer;
REVOKE CREATE ON SCHEMA public FROM history_view_definer;
REVOKE history_view_definer FROM CURRENT_USER;

-- 7) 実行者（master）が監査ロールへ SET ROLE できるようにする（INHERIT FALSE＝SET ROLE せずには使えない）
GRANT history_view_auditor TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;

-- 8) 最終確認（fail-closed）: 実行者が監査用ロールの権限を受け継いでおらず、定義者になれないこと
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['history_view_definer', 'history_view_auditor'] LOOP
    IF pg_has_role(current_user, r, 'USAGE') THEN
      RAISE EXCEPTION 'current_user must not inherit privileges of %', r;
    END IF;
  END LOOP;
  IF pg_has_role(current_user, 'history_view_definer', 'SET') THEN
    RAISE EXCEPTION 'current_user must not be able to SET ROLE history_view_definer';
  END IF;
  -- RDS master は rds_superuser 経由で pg_read_all_data / pg_write_all_data を INHERIT する。
  -- 管理者の全表アクセスは RDS の仕様で防げないため、current_user の実効表権限は検査しない。
  -- アプリ・監査ロールの実効権限（他ロール経由も含む）と、表への直接 ACL を別々に検査する。
  FOREACH r IN ARRAY ARRAY['teamagent_app', 'teamagent_dashboard', 'history_view_auditor'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      IF has_table_privilege(r, 'public.history_view_audit', 'SELECT')
         OR has_table_privilege(r, 'public.history_view_audit', 'INSERT')
         OR has_table_privilege(r, 'public.history_view_audit', 'UPDATE')
         OR has_table_privilege(r, 'public.history_view_audit', 'DELETE') THEN
        RAISE EXCEPTION '% must not touch history_view_audit without the function', r;
      END IF;
    END IF;
  END LOOP;
  IF EXISTS (
    SELECT 1 FROM pg_class c
    CROSS JOIN LATERAL aclexplode(c.relacl) a
    WHERE c.oid = 'public.history_view_audit'::regclass
      AND a.grantee <> 'history_view_definer'::regrole
  ) THEN
    RAISE EXCEPTION 'history_view_audit ACL must grant only to history_view_definer';
  END IF;
END $$;
