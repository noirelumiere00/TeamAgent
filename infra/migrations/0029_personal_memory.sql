-- ============================================================
-- 0029: personal_memory（DM 本人メモ v1・M5）
-- ============================================================
-- 目的: 1 対 1 DM の本人メモ（200 字以内の要約だけ・会話の本文は持たない）を、本人だけが
--       読み書きできる形で保存する。設計: docs/architecture/hermes_migration_design.md §10b
--
-- 守ること（M5 の出口条件）:
--   ① admin 例外を入れない（0025/0017/0006 の `OR app.user_role='admin'` を写さない）。
--   ② 既存のどのロールにも表の権限を与えない。表を読み書きできるのは専用の
--      personal_memory_app だけで、master（本 migration の実行者）には
--      「SET ROLE はできるが権限は受け継がない（INHERIT FALSE）」で与える。
--      → master 直結（朝ダイジェスト・backfill）・teamagent_app（ingest・mcp の既存経路）・
--        teamagent_dashboard（利用状況）からは、admin GUC を立てても permission denied。
--        master が BYPASSRLS を持っていても、表の権限そのものが無いので読めない
--        （handoff day0-4:265-270「master 接続では FORCE RLS でもすり抜けた」への構造的な対策）。
--   ③ 表と関数の所有者は NOLOGIN NOBYPASSRLS の personal_memory_definer。FORCE RLS が所有者にも効く。
--      他人の行に届くのは SECURITY DEFINER 関数 2 本（管理者閲覧・退職削除）だけで、
--      閲覧は「監査 INSERT → 行を返す」を 1 本の関数で行う（監査に失敗したら 1 行も返らない）。
--   ④ ON CONFLICT も RETURNING も使わない（0024 の地雷: 最小権限ロールでは失敗する）。
--
-- ロール:
--   personal_memory_app          … mcp の本人メモ store が SET ROLE する（本人行だけ・GUC app.pm_principal）
--   personal_memory_definer      … 表と関数の所有者。誰も SET ROLE できない（本 migration の最後で外す）
--   personal_memory_admin_reader … 管理者閲覧関数の EXECUTE だけ（connect_web が SET ROLE・M6）
--   personal_memory_retirer      … 退職削除関数の EXECUTE だけ（朝ダイジェストの掃除段が SET ROLE・M6）
--
-- 以後この 3 表を変える migration は、所有者が definer なので、冒頭で
--   GRANT personal_memory_definer TO CURRENT_USER WITH INHERIT FALSE, SET TRUE; SET ROLE personal_memory_definer;
-- を行い、最後に RESET ROLE; REVOKE personal_memory_definer FROM CURRENT_USER; で戻す。
--
-- ロールバック（順序どおり）:
--   GRANT personal_memory_definer TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;
--   DROP FUNCTION public.personal_memory_admin_view(text,text,text,text);
--   DROP FUNCTION public.personal_memory_retire_delete(text,text,text);
--   DROP TABLE public.personal_memory_audit, public.personal_memory_entries, public.personal_memory_profiles;
--   REVOKE personal_memory_definer FROM CURRENT_USER;
--   DROP ROLE personal_memory_admin_reader, personal_memory_retirer, personal_memory_app, personal_memory_definer;
--
-- 関連: 0026/0027（admin 例外なしの本人行 RLS）、0021（REVOKE ALL → 最小 GRANT）、0002（既定権限）、0024（ON CONFLICT の地雷）
--
-- 適用後の検証（読み取りだけ）:
--   SELECT relname, relrowsecurity, relforcerowsecurity, pg_get_userbyid(relowner)
--     FROM pg_class WHERE relname LIKE 'personal_memory_%' AND relkind = 'r';
--   SELECT has_table_privilege('teamagent_app', 'public.personal_memory_entries', 'SELECT');   -- f
--   SELECT has_table_privilege(current_user, 'public.personal_memory_entries', 'SELECT');     -- f（INHERIT FALSE）
--   SELECT has_function_privilege('public', 'public.personal_memory_admin_view(text,text,text,text)', 'EXECUTE');  -- f
-- ============================================================

-- 1) ロール（NOLOGIN NOBYPASSRLS NOINHERIT）
--   既存ロールの属性を ALTER ROLE で揃え直すことは、CREATEROLE の一般ロール（本番の master）には
--   できない（PG16）。代わりに、既存ロールが危ない属性を持っていたら migration ごと止める（fail-closed）。
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY[
    'personal_memory_app', 'personal_memory_definer',
    'personal_memory_admin_reader', 'personal_memory_retirer'
  ] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN NOBYPASSRLS NOINHERIT', r);
    ELSIF EXISTS (
      SELECT 1 FROM pg_roles
       WHERE rolname = r
         AND (rolsuper OR rolcanlogin OR rolbypassrls OR rolcreaterole OR rolcreatedb)
    ) THEN
      RAISE EXCEPTION 'personal_memory role % has unsafe attributes', r;
    END IF;
  END LOOP;
END $$;

-- 2) 表
CREATE TABLE IF NOT EXISTS public.personal_memory_profiles (
  team_id             TEXT NOT NULL CHECK (team_id ~ '^T[A-Z0-9]{8,}$'),
  slack_user_id       TEXT NOT NULL CHECK (slack_user_id ~ '^U[A-Z0-9]{8,}$'),
  user_email          TEXT NOT NULL CHECK (user_email <> '' AND position('@' IN user_email) > 0),
  state               TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'frozen')),
  noticed_at          TIMESTAMPTZ,
  version             BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
  erase_confirm_until TIMESTAMPTZ,
  admin_view_count    INTEGER NOT NULL DEFAULT 0 CHECK (admin_view_count >= 0),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (team_id, slack_user_id)
);

CREATE TABLE IF NOT EXISTS public.personal_memory_entries (
  entry_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  team_id       TEXT NOT NULL,
  slack_user_id TEXT NOT NULL,
  target        TEXT NOT NULL CHECK (target IN ('user', 'memory')),
  -- 本文ではなく 200 字以内の要約。§ は Hermes の項目区切り（"\n§\n"）を壊すので入れない
  content       TEXT NOT NULL CHECK (char_length(content) BETWEEN 1 AND 200
                                     AND content = btrim(content)
                                     AND position('§' IN content) = 0
                                     -- 1 行だけ（改行で返信前の枠の終わりを偽装させない）
                                     AND content !~ '[\x01-\x1f\x7f-\x9f\u2028\u2029]'),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  FOREIGN KEY (team_id, slack_user_id)
    REFERENCES public.personal_memory_profiles (team_id, slack_user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_pm_entries_owner
  ON public.personal_memory_entries (team_id, slack_user_id, target, created_at);

-- 監査（本文を持たない。管理者 email・profile の SHA-256 先頭 16 hex・件数・理由コードだけ）
CREATE TABLE IF NOT EXISTS public.personal_memory_audit (
  audit_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  occurred_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  actor_kind    TEXT NOT NULL CHECK (actor_kind IN ('self', 'system', 'admin', 'retire')),
  actor_email   TEXT CHECK (actor_email IS NULL
                            OR (actor_email <> '' AND position('@' IN actor_email) > 0)),
  profile_sha16 TEXT NOT NULL CHECK (profile_sha16 ~ '^[0-9a-f]{16}$'),
  action        TEXT NOT NULL CHECK (action IN ('notice_ack', 'learn_applied', 'forget', 'freeze',
                                                'resume', 'erase_all', 'admin_view', 'retire_delete')),
  item_count    INTEGER NOT NULL CHECK (item_count >= 0),
  reason_code   TEXT NOT NULL CHECK (reason_code ~ '^[a-z][a-z0-9_]{0,39}$'),
  CHECK ((actor_kind = 'admin') = (actor_email IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_pm_audit_profile
  ON public.personal_memory_audit (profile_sha16, occurred_at);

-- 3) RLS（3 表とも ENABLE＋FORCE。admin 分岐なし）
ALTER TABLE public.personal_memory_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.personal_memory_profiles FORCE ROW LEVEL SECURITY;
ALTER TABLE public.personal_memory_entries  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.personal_memory_entries  FORCE ROW LEVEL SECURITY;
ALTER TABLE public.personal_memory_audit    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.personal_memory_audit    FORCE ROW LEVEL SECURITY;

-- 本人（personal_memory_app だけ）。GUC app.pm_principal は store が txn-local で入れる 'T…:U…'
DROP POLICY IF EXISTS pm_profiles_self ON public.personal_memory_profiles;
CREATE POLICY pm_profiles_self ON public.personal_memory_profiles FOR ALL TO personal_memory_app
  USING (current_setting('app.pm_principal', true) <> ''
         AND team_id || ':' || slack_user_id = current_setting('app.pm_principal', true))
  WITH CHECK (current_setting('app.pm_principal', true) <> ''
         AND team_id || ':' || slack_user_id = current_setting('app.pm_principal', true));

DROP POLICY IF EXISTS pm_entries_self ON public.personal_memory_entries;
CREATE POLICY pm_entries_self ON public.personal_memory_entries FOR ALL TO personal_memory_app
  USING (current_setting('app.pm_principal', true) <> ''
         AND team_id || ':' || slack_user_id = current_setting('app.pm_principal', true))
  WITH CHECK (current_setting('app.pm_principal', true) <> ''
         AND team_id || ':' || slack_user_id = current_setting('app.pm_principal', true));

-- 本人は自分の監査を書けるが読めない（閲覧回数は profiles.admin_view_count で返す）
DROP POLICY IF EXISTS pm_audit_self_insert ON public.personal_memory_audit;
CREATE POLICY pm_audit_self_insert ON public.personal_memory_audit FOR INSERT TO personal_memory_app
  WITH CHECK (current_setting('app.pm_principal', true) <> ''
              AND actor_kind IN ('self', 'system') AND actor_email IS NULL
              AND profile_sha16 = left(encode(sha256(convert_to(
                    current_setting('app.pm_principal', true), 'UTF8')), 'hex'), 16));

-- 定義者（SECURITY DEFINER 関数の所有者だけが持つ。所有者の BYPASSRLS に依存しない）
DROP POLICY IF EXISTS pm_profiles_definer_select ON public.personal_memory_profiles;
CREATE POLICY pm_profiles_definer_select ON public.personal_memory_profiles
  FOR SELECT TO personal_memory_definer USING (true);
DROP POLICY IF EXISTS pm_profiles_definer_update ON public.personal_memory_profiles;
CREATE POLICY pm_profiles_definer_update ON public.personal_memory_profiles
  FOR UPDATE TO personal_memory_definer USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS pm_profiles_definer_delete ON public.personal_memory_profiles;
CREATE POLICY pm_profiles_definer_delete ON public.personal_memory_profiles
  FOR DELETE TO personal_memory_definer USING (true);
DROP POLICY IF EXISTS pm_entries_definer_select ON public.personal_memory_entries;
CREATE POLICY pm_entries_definer_select ON public.personal_memory_entries
  FOR SELECT TO personal_memory_definer USING (true);
DROP POLICY IF EXISTS pm_entries_definer_delete ON public.personal_memory_entries;
CREATE POLICY pm_entries_definer_delete ON public.personal_memory_entries
  FOR DELETE TO personal_memory_definer USING (true);
DROP POLICY IF EXISTS pm_audit_definer_insert ON public.personal_memory_audit;
CREATE POLICY pm_audit_definer_insert ON public.personal_memory_audit
  FOR INSERT TO personal_memory_definer WITH CHECK (actor_kind IN ('admin', 'retire'));

-- 4) 表の権限（0002 の既定権限で付く teamagent_app 等の権限を打ち消してから最小限を付ける）
REVOKE ALL PRIVILEGES ON public.personal_memory_profiles, public.personal_memory_entries,
  public.personal_memory_audit FROM PUBLIC;
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['teamagent_app', 'teamagent_dashboard'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format(
        'REVOKE ALL PRIVILEGES ON public.personal_memory_profiles, public.personal_memory_entries, '
        'public.personal_memory_audit FROM %I', r);
    END IF;
  END LOOP;
END $$;
-- profiles の UPDATE は状態（凍結・告知・版）の更新と SELECT ... FOR UPDATE に要る
GRANT SELECT, INSERT, UPDATE ON public.personal_memory_profiles TO personal_memory_app;
GRANT SELECT, INSERT, DELETE ON public.personal_memory_entries  TO personal_memory_app;
GRANT INSERT                 ON public.personal_memory_audit    TO personal_memory_app;
GRANT USAGE ON SCHEMA public TO personal_memory_app, personal_memory_definer,
  personal_memory_admin_reader, personal_memory_retirer;

-- 5) SECURITY DEFINER 関数 2 本（search_path 固定・表はすべて public. で修飾・動的 SQL なし）
CREATE OR REPLACE FUNCTION public.personal_memory_admin_view(
    p_admin_email text, p_team_id text, p_slack_user_id text, p_reason_code text)
RETURNS TABLE (entry_no integer, entry_target text, entry_content text,
               entry_updated_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
  v_count integer;
BEGIN
  IF p_admin_email IS NULL OR p_admin_email !~ '^[^@[:space:]]+@[^@[:space:]]+$' THEN
    RAISE EXCEPTION 'pm_bad_admin' USING ERRCODE = '22023';
  END IF;
  IF p_team_id IS NULL OR p_team_id !~ '^T[A-Z0-9]{8,}$'
     OR p_slack_user_id IS NULL OR p_slack_user_id !~ '^U[A-Z0-9]{8,}$' THEN
    RAISE EXCEPTION 'pm_bad_principal' USING ERRCODE = '22023';
  END IF;
  IF p_reason_code IS NULL OR p_reason_code !~ '^[a-z][a-z0-9_]{0,39}$' THEN
    RAISE EXCEPTION 'pm_bad_reason' USING ERRCODE = '22023';
  END IF;
  SELECT count(*) INTO v_count FROM public.personal_memory_entries e
   WHERE e.team_id = p_team_id AND e.slack_user_id = p_slack_user_id;
  -- 表示より前に監査を確定する（失敗すれば例外になり、行は 1 行も返らない）
  INSERT INTO public.personal_memory_audit
    (actor_kind, actor_email, profile_sha16, action, item_count, reason_code)
  VALUES ('admin', lower(p_admin_email),
          left(encode(sha256(convert_to(p_team_id || ':' || p_slack_user_id, 'UTF8')), 'hex'), 16),
          'admin_view', v_count, p_reason_code);
  UPDATE public.personal_memory_profiles p
     SET admin_view_count = p.admin_view_count + 1, updated_at = now()
   WHERE p.team_id = p_team_id AND p.slack_user_id = p_slack_user_id;
  RETURN QUERY
    SELECT (row_number() OVER (ORDER BY e.target, e.created_at, e.entry_id))::integer,
           e.target, e.content, e.updated_at
      FROM public.personal_memory_entries e
     WHERE e.team_id = p_team_id AND e.slack_user_id = p_slack_user_id
     ORDER BY 1;
END
$fn$;

CREATE OR REPLACE FUNCTION public.personal_memory_retire_delete(
    p_team_id text, p_slack_user_id text, p_reason_code text)
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
  v_count integer;
BEGIN
  IF p_team_id IS NULL OR p_team_id !~ '^T[A-Z0-9]{8,}$'
     OR p_slack_user_id IS NULL OR p_slack_user_id !~ '^U[A-Z0-9]{8,}$' THEN
    RAISE EXCEPTION 'pm_bad_principal' USING ERRCODE = '22023';
  END IF;
  IF p_reason_code IS NULL OR p_reason_code !~ '^[a-z][a-z0-9_]{0,39}$' THEN
    RAISE EXCEPTION 'pm_bad_reason' USING ERRCODE = '22023';
  END IF;
  SELECT count(*) INTO v_count FROM public.personal_memory_entries e
   WHERE e.team_id = p_team_id AND e.slack_user_id = p_slack_user_id;
  INSERT INTO public.personal_memory_audit
    (actor_kind, actor_email, profile_sha16, action, item_count, reason_code)
  VALUES ('retire', NULL,
          left(encode(sha256(convert_to(p_team_id || ':' || p_slack_user_id, 'UTF8')), 'hex'), 16),
          'retire_delete', v_count, p_reason_code);
  DELETE FROM public.personal_memory_entries e
   WHERE e.team_id = p_team_id AND e.slack_user_id = p_slack_user_id;
  DELETE FROM public.personal_memory_profiles p
   WHERE p.team_id = p_team_id AND p.slack_user_id = p_slack_user_id;
  RETURN v_count;
END
$fn$;

REVOKE ALL ON FUNCTION public.personal_memory_admin_view(text, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.personal_memory_retire_delete(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.personal_memory_admin_view(text, text, text, text)
  TO personal_memory_admin_reader;
GRANT EXECUTE ON FUNCTION public.personal_memory_retire_delete(text, text, text)
  TO personal_memory_retirer;

-- 6) 所有者を定義者ロールへ移す
--   ALTER ... OWNER には「実行者が新所有者へ SET ROLE できる」ことと「新所有者が schema に CREATE を持つ」ことが要る（PG16）。
--   移したあとは、実行者（本番は master の teamagent、CI は postgres）から定義者への経路を外す。
GRANT personal_memory_definer TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;
GRANT CREATE ON SCHEMA public TO personal_memory_definer;
ALTER TABLE public.personal_memory_profiles OWNER TO personal_memory_definer;
ALTER TABLE public.personal_memory_entries  OWNER TO personal_memory_definer;
ALTER TABLE public.personal_memory_audit    OWNER TO personal_memory_definer;
ALTER FUNCTION public.personal_memory_admin_view(text, text, text, text) OWNER TO personal_memory_definer;
ALTER FUNCTION public.personal_memory_retire_delete(text, text, text) OWNER TO personal_memory_definer;
REVOKE CREATE ON SCHEMA public FROM personal_memory_definer;
REVOKE personal_memory_definer FROM CURRENT_USER;

-- 7) 実行者（master）がアプリ・管理者閲覧・退職削除のロールへ SET ROLE できるようにする。
--   INHERIT FALSE なので、master のまま（SET ROLE せずに）表を読むことも関数を実行することもできない。
GRANT personal_memory_app          TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;
GRANT personal_memory_admin_reader TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;
GRANT personal_memory_retirer      TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;

-- 8) 最終確認（fail-closed）: 実行者が本人メモ用ロールの権限を「受け継いで」いないこと。
--   PG16 では CREATEROLE の一般ロールが作ったロールに作成者の ADMIN だけが付き、既定では受け継がない。
--   ただし createrole_self_grant に inherit が入っていると受け継いでしまい、master（BYPASSRLS の可能性あり）の
--   直結経路から全員分が読めるようになる。その場合はここで migration ごと止める。
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY[
    'personal_memory_app', 'personal_memory_definer',
    'personal_memory_admin_reader', 'personal_memory_retirer'
  ] LOOP
    IF pg_has_role(current_user, r, 'USAGE') THEN
      RAISE EXCEPTION 'current_user must not inherit privileges of %', r;
    END IF;
  END LOOP;
  IF pg_has_role(current_user, 'personal_memory_definer', 'SET') THEN
    RAISE EXCEPTION 'current_user must not be able to SET ROLE personal_memory_definer';
  END IF;
  IF has_table_privilege(current_user, 'public.personal_memory_entries', 'SELECT')
     OR has_table_privilege(current_user, 'public.personal_memory_profiles', 'SELECT') THEN
    RAISE EXCEPTION 'current_user must not read personal_memory tables without SET ROLE';
  END IF;
END $$;
