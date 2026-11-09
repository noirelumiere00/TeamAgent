-- Durable, hash-only credentials for the local MCP issuer. No upstream OAuth tokens.
-- The migration runner owns the transaction. Runtime callers receive function EXECUTE only.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='library_mcp_definer') THEN
    CREATE ROLE library_mcp_definer NOLOGIN NOBYPASSRLS NOINHERIT;
  ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='library_mcp_definer'
    AND (rolsuper OR rolcanlogin OR rolbypassrls OR rolcreaterole OR rolcreatedb OR rolinherit)) THEN
    RAISE EXCEPTION 'unsafe library MCP owner';
  END IF;
END $$;

CREATE TABLE public.library_mcp_clients (
  client_id text PRIMARY KEY CHECK (length(client_id) BETWEEN 16 AND 128),
  resource text NOT NULL CHECK (resource ~ '^https://[^/?#[:space:]]+/mcp$'),
  redirect_uris text[] NOT NULL CHECK (cardinality(redirect_uris) BETWEEN 1 AND 8),
  scopes text[] NOT NULL CHECK (scopes=ARRAY['library:read']::text[]),
  client_name text CHECK (length(client_name)<=200),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  disabled_at timestamptz
);
CREATE TABLE public.library_mcp_auth_requests (
  tx_hash bytea PRIMARY KEY CHECK (octet_length(tx_hash)=32),
  client_id text NOT NULL REFERENCES public.library_mcp_clients,
  resource text NOT NULL,
  redirect_uri text NOT NULL CHECK (length(redirect_uri)<=2048),
  redirect_explicit boolean NOT NULL,
  state text CHECK (length(state)<=1024),
  code_challenge text NOT NULL CHECK (code_challenge ~ '^[A-Za-z0-9_-]{43}$'),
  scopes text[] NOT NULL CHECK (scopes=ARRAY['library:read']::text[]),
  csrf_hash bytea CHECK (octet_length(csrf_hash)=32),
  browser_hash bytea CHECK (octet_length(browser_hash)=32),
  subject text,
  verified_hd text,
  expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '10 minutes',
  consumed_at timestamptz,
  CHECK (subject IS NOT NULL OR verified_hd IS NULL)
);
CREATE TABLE public.library_mcp_grants (
  grant_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id text NOT NULL REFERENCES public.library_mcp_clients,
  subject text NOT NULL CHECK (subject=lower(btrim(subject)) AND subject ~ '^[^@[:space:]]+@[^@[:space:]]+$' AND length(subject)<=320),
  verified_hd text CHECK (verified_hd=lower(verified_hd) AND verified_hd ~ '^[a-z0-9][a-z0-9.-]{0,252}$'),
  resource text NOT NULL,
  scopes text[] NOT NULL CHECK (scopes=ARRAY['library:read']::text[]),
  redirect_uri text NOT NULL CHECK (length(redirect_uri)<=2048),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  last_used_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL DEFAULT clock_timestamp()+interval '90 days',
  revoked_at timestamptz
);
CREATE TABLE public.library_mcp_credentials (
  token_hash bytea PRIMARY KEY CHECK (octet_length(token_hash)=32),
  kind text NOT NULL CHECK (kind IN ('code','access','refresh')),
  grant_id uuid NOT NULL REFERENCES public.library_mcp_grants ON DELETE CASCADE,
  scopes text[] NOT NULL CHECK (scopes=ARRAY['library:read']::text[]),
  expires_at timestamptz NOT NULL,
  consumed_at timestamptz,
  predecessor_hash bytea CHECK (octet_length(predecessor_hash)=32),
  redirect_uri text,
  redirect_explicit boolean,
  code_challenge text,
  CHECK (kind<>'code' OR (redirect_uri IS NOT NULL AND redirect_explicit IS NOT NULL AND code_challenge IS NOT NULL))
);
CREATE TABLE public.library_mcp_rate_buckets (
  key_hash bytea NOT NULL CHECK (octet_length(key_hash)=32),
  window_start bigint NOT NULL,
  window_seconds integer NOT NULL CHECK (window_seconds BETWEEN 1 AND 86400),
  attempts integer NOT NULL CHECK (attempts>0),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (key_hash,window_start,window_seconds)
);
CREATE INDEX library_mcp_requests_expiry ON public.library_mcp_auth_requests(expires_at);
CREATE INDEX library_mcp_grants_subject ON public.library_mcp_grants(subject,expires_at);
CREATE INDEX library_mcp_grants_client ON public.library_mcp_grants(client_id);
CREATE INDEX library_mcp_credentials_grant ON public.library_mcp_credentials(grant_id);
CREATE INDEX library_mcp_credentials_expiry ON public.library_mcp_credentials(expires_at);
CREATE INDEX library_mcp_rates_expiry ON public.library_mcp_rate_buckets(expires_at);

-- All data access, including the owner's, is subject to an explicit policy.
DO $$
DECLARE t text; r text;
BEGIN
  FOREACH t IN ARRAY ARRAY['library_mcp_clients','library_mcp_auth_requests',
    'library_mcp_grants','library_mcp_credentials','library_mcp_rate_buckets'] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',t);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',t);
    EXECUTE format('CREATE POLICY library_mcp_owner ON public.%I TO library_mcp_definer USING (true) WITH CHECK (true)',t);
    EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC',t);
    FOREACH r IN ARRAY ARRAY['teamagent_app','teamagent_dashboard'] LOOP
      IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
        EXECUTE format('REVOKE ALL ON public.%I FROM %I',t,r);
      END IF;
    END LOOP;
  END LOOP;
END $$;

CREATE FUNCTION public.library_mcp_register_client(p_id text,p_resource text,p_redirects text[],p_scopes text[],p_name text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF p_id IS NULL OR p_id !~ '^[A-Za-z0-9_-]{16,128}$'
    OR p_scopes IS DISTINCT FROM ARRAY['library:read']::text[]
    OR cardinality(p_redirects) NOT BETWEEN 1 AND 8
    OR EXISTS(SELECT 1 FROM unnest(p_redirects) u WHERE u IS NULL OR length(u)>2048
      OR u !~ '^(https://[^[:space:]#]+|http://(localhost|127[.]0[.]0[.]1)(:[0-9]{1,5})?/callback)$') THEN
    RAISE EXCEPTION 'invalid MCP client' USING ERRCODE='22023';
  END IF;
  INSERT INTO public.library_mcp_clients(client_id,resource,redirect_uris,scopes,client_name)
    VALUES(p_id,p_resource,p_redirects,p_scopes,p_name);
  RETURN true;
END $$;

CREATE FUNCTION public.library_mcp_get_client(p_id text,p_resource text)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
  SELECT jsonb_build_object('client_id',client_id,'redirect_uris',redirect_uris,
    'token_endpoint_auth_method','none','grant_types',ARRAY['authorization_code','refresh_token'],
    'response_types',ARRAY['code'],'scope','library:read','client_name',client_name,
    'client_id_issued_at',extract(epoch FROM created_at)::bigint)
  FROM public.library_mcp_clients WHERE client_id=p_id AND resource=p_resource AND disabled_at IS NULL
$$;

CREATE FUNCTION public.library_mcp_create_request(p_tx bytea,p_client text,p_resource text,p_redirect text,p_explicit boolean,p_state text,p_challenge text,p_scopes text[])
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  IF NOT EXISTS(SELECT 1 FROM public.library_mcp_clients WHERE client_id=p_client
      AND resource=p_resource AND disabled_at IS NULL AND p_redirect=ANY(redirect_uris)
      AND p_scopes=scopes) THEN
    RAISE EXCEPTION 'invalid MCP authorization' USING ERRCODE='22023';
  END IF;
  INSERT INTO public.library_mcp_auth_requests(tx_hash,client_id,resource,redirect_uri,
    redirect_explicit,state,code_challenge,scopes)
    VALUES(p_tx,p_client,p_resource,p_redirect,p_explicit,p_state,p_challenge,p_scopes);
  RETURN true;
END $$;

CREATE FUNCTION public.library_mcp_get_request(p_tx bytea,p_resource text)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
  SELECT jsonb_build_object('client_id',r.client_id,'email',r.subject,'verified_hd',r.verified_hd,
    'expires_at',extract(epoch FROM r.expires_at),'csrf_hash',encode(r.csrf_hash,'hex'),
    'browser_hash',encode(r.browser_hash,'hex'),'params',jsonb_build_object(
      'redirect_uri',r.redirect_uri,'redirect_uri_provided_explicitly',r.redirect_explicit,
      'state',r.state,'code_challenge',r.code_challenge,'scopes',r.scopes,'resource',r.resource))
  FROM public.library_mcp_auth_requests r JOIN public.library_mcp_clients c USING(client_id)
  WHERE r.tx_hash=p_tx AND r.resource=p_resource AND c.resource=p_resource
    AND r.expires_at>clock_timestamp() AND r.consumed_at IS NULL AND c.disabled_at IS NULL
$$;

CREATE FUNCTION public.library_mcp_bind_request(p_tx bytea,p_csrf bytea,p_email text,p_hd text,p_browser bytea,p_resource text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE r public.library_mcp_auth_requests;
BEGIN
  IF octet_length(p_csrf)<>32 OR octet_length(p_browser)<>32 OR p_csrf IS NULL OR p_browser IS NULL
    OR (p_email IS NULL AND p_hd IS NOT NULL)
    OR (p_email IS NOT NULL AND (p_email<>lower(btrim(p_email)) OR length(p_email)>320
      OR p_email !~ '^[^@[:space:]]+@[^@[:space:]]+$' OR p_hd !~ '^[a-z0-9][a-z0-9.-]{0,252}$')) THEN
    RETURN false;
  END IF;
  SELECT a.* INTO r FROM public.library_mcp_auth_requests a
    JOIN public.library_mcp_clients c USING(client_id)
    WHERE tx_hash=p_tx AND a.resource=p_resource AND c.resource=p_resource
      AND c.disabled_at IS NULL FOR UPDATE OF a;
  IF NOT FOUND OR r.expires_at<=clock_timestamp() OR r.consumed_at IS NOT NULL
    OR (r.browser_hash IS NOT NULL AND r.browser_hash<>p_browser)
    OR (r.subject IS NOT NULL AND (r.subject IS DISTINCT FROM p_email OR r.verified_hd IS DISTINCT FROM p_hd)) THEN
    RETURN false;
  END IF;
  UPDATE public.library_mcp_auth_requests SET browser_hash=p_browser,csrf_hash=p_csrf,
    subject=coalesce(r.subject,p_email),verified_hd=coalesce(r.verified_hd,p_hd) WHERE tx_hash=p_tx;
  RETURN true;
END $$;

CREATE FUNCTION public.library_mcp_authorize_request(p_tx bytea,p_csrf bytea,p_email text,p_code bytea,p_browser bytea,p_resource text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE r public.library_mcp_auth_requests; g uuid;
BEGIN
  SELECT a.* INTO r FROM public.library_mcp_auth_requests a
    JOIN public.library_mcp_clients c USING(client_id)
    WHERE tx_hash=p_tx AND a.resource=p_resource AND c.resource=p_resource
      AND c.disabled_at IS NULL FOR UPDATE OF a;
  IF NOT FOUND OR r.expires_at<=clock_timestamp() OR r.consumed_at IS NOT NULL
    OR r.subject IS NULL OR r.subject IS DISTINCT FROM p_email
    OR r.browser_hash IS DISTINCT FROM p_browser OR r.csrf_hash IS DISTINCT FROM p_csrf THEN
    RETURN NULL;
  END IF;
  INSERT INTO public.library_mcp_grants(client_id,subject,verified_hd,resource,scopes,redirect_uri)
    VALUES(r.client_id,r.subject,r.verified_hd,r.resource,r.scopes,r.redirect_uri) RETURNING grant_id INTO g;
  INSERT INTO public.library_mcp_credentials(token_hash,kind,grant_id,scopes,expires_at,
    redirect_uri,redirect_explicit,code_challenge)
    VALUES(p_code,'code',g,r.scopes,clock_timestamp()+interval '60 seconds',r.redirect_uri,r.redirect_explicit,r.code_challenge);
  UPDATE public.library_mcp_auth_requests SET consumed_at=clock_timestamp() WHERE tx_hash=p_tx;
  RETURN jsonb_build_object('grant_id',g,'client_id',r.client_id,'subject',r.subject,'verified_hd',r.verified_hd,
    'resource',r.resource,'scopes',r.scopes,'redirect_uri',r.redirect_uri,'state',r.state);
END $$;

CREATE FUNCTION public.library_mcp_cancel_request(p_tx bytea,p_csrf bytea,p_browser bytea,p_resource text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE n integer;
BEGIN
  UPDATE public.library_mcp_auth_requests a SET consumed_at=clock_timestamp()
    FROM public.library_mcp_clients c WHERE a.client_id=c.client_id AND c.disabled_at IS NULL
      AND a.resource=p_resource AND c.resource=p_resource AND tx_hash=p_tx
      AND csrf_hash=p_csrf AND browser_hash=p_browser
      AND a.consumed_at IS NULL AND a.expires_at>clock_timestamp();
  GET DIAGNOSTICS n=ROW_COUNT;
  RETURN n=1;
END $$;

-- Private helpers return a fixed projection, never credential hashes or raw credentials.
CREATE FUNCTION public.library_mcp_credential_record(p_hash bytea,p_kind text,p_client text,p_resource text,p_consumed boolean)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
  SELECT jsonb_build_object('grant_id',g.grant_id,'client_id',g.client_id,'subject',g.subject,
    'verified_hd',g.verified_hd,'scopes',t.scopes,'resource',g.resource,
    'expires_at',extract(epoch FROM t.expires_at),'consumed',t.consumed_at IS NOT NULL,
    'redirect_uri',t.redirect_uri,'redirect_uri_provided_explicitly',t.redirect_explicit,'code_challenge',t.code_challenge)
  FROM public.library_mcp_credentials t JOIN public.library_mcp_grants g USING(grant_id)
    JOIN public.library_mcp_clients c ON c.client_id=g.client_id
  WHERE t.token_hash=p_hash AND t.kind=p_kind AND (p_client IS NULL OR c.client_id=p_client)
    AND g.resource=p_resource AND c.resource=p_resource AND c.disabled_at IS NULL
    AND g.revoked_at IS NULL AND g.expires_at>clock_timestamp() AND t.expires_at>clock_timestamp()
    AND (p_consumed OR t.consumed_at IS NULL)
$$;
CREATE FUNCTION public.library_mcp_get_code(p_hash bytea,p_client text,p_resource text)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
  SELECT public.library_mcp_credential_record(p_hash,'code',p_client,p_resource,false)
$$;
CREATE FUNCTION public.library_mcp_get_refresh(p_hash bytea,p_client text,p_resource text,p_consumed boolean)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
  SELECT public.library_mcp_credential_record(p_hash,'refresh',p_client,p_resource,p_consumed)
$$;
CREATE FUNCTION public.library_mcp_get_access(p_hash bytea,p_resource text)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
  SELECT public.library_mcp_credential_record(p_hash,'access',NULL,p_resource,false)
$$;

CREATE FUNCTION public.library_mcp_exchange(p_hash bytea,p_kind text,p_client text,p_scopes text[],p_access bytea,p_refresh bytea,p_resource text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE g public.library_mcp_grants; t public.library_mcp_credentials;
  access_exp timestamptz; refresh_exp timestamptz;
BEGIN
  -- Every exchange/revoke locks the grant first, so a revoke racing rotation wins safely.
  SELECT a.* INTO g FROM public.library_mcp_grants a
    JOIN public.library_mcp_credentials k ON k.grant_id=a.grant_id
    JOIN public.library_mcp_clients c ON c.client_id=a.client_id
    WHERE k.token_hash=p_hash AND k.kind=p_kind AND a.client_id=p_client
      AND a.resource=p_resource AND c.resource=p_resource AND c.disabled_at IS NULL FOR UPDATE OF a;
  IF NOT FOUND OR g.revoked_at IS NOT NULL OR g.expires_at<=clock_timestamp() THEN
    RETURN jsonb_build_object('status','invalid_grant');
  END IF;
  SELECT * INTO t FROM public.library_mcp_credentials WHERE token_hash=p_hash FOR UPDATE;
  IF t.expires_at<=clock_timestamp() THEN RETURN jsonb_build_object('status','invalid_grant'); END IF;
  IF t.consumed_at IS NOT NULL THEN
    IF p_kind='refresh' THEN
      UPDATE public.library_mcp_grants SET revoked_at=clock_timestamp() WHERE grant_id=g.grant_id;
      RETURN jsonb_build_object('status','replay');
    END IF;
    RETURN jsonb_build_object('status','invalid_grant');
  END IF;
  IF p_kind NOT IN ('code','refresh') OR p_scopes IS DISTINCT FROM t.scopes
    OR p_access=p_refresh OR p_hash=p_access OR p_hash=p_refresh
    OR octet_length(p_access)<>32 OR octet_length(p_refresh)<>32 THEN
    RETURN jsonb_build_object('status','invalid_grant');
  END IF;
  access_exp:=clock_timestamp()+interval '10 minutes';
  refresh_exp:=clock_timestamp()+interval '90 days';
  UPDATE public.library_mcp_credentials SET consumed_at=clock_timestamp() WHERE token_hash=p_hash;
  UPDATE public.library_mcp_grants SET last_used_at=clock_timestamp(),expires_at=refresh_exp WHERE grant_id=g.grant_id;
  INSERT INTO public.library_mcp_credentials(token_hash,kind,grant_id,scopes,expires_at,predecessor_hash)
    VALUES(p_access,'access',g.grant_id,p_scopes,access_exp,p_hash),
      (p_refresh,'refresh',g.grant_id,p_scopes,refresh_exp,p_hash);
  RETURN jsonb_build_object('status','ok','grant_id',g.grant_id,'client_id',g.client_id,
    'subject',g.subject,'verified_hd',g.verified_hd,'scopes',p_scopes,'resource',g.resource,
    'expires_at',extract(epoch FROM access_exp),'refresh_expires_at',extract(epoch FROM refresh_exp));
END $$;
CREATE FUNCTION public.library_mcp_exchange_code(p_hash bytea,p_client text,p_access bytea,p_refresh bytea,p_resource text)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
  SELECT public.library_mcp_exchange(p_hash,'code',p_client,ARRAY['library:read']::text[],p_access,p_refresh,p_resource)
$$;
CREATE FUNCTION public.library_mcp_rotate_refresh(p_hash bytea,p_client text,p_scopes text[],p_access bytea,p_refresh bytea,p_resource text)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
  SELECT public.library_mcp_exchange(p_hash,'refresh',p_client,p_scopes,p_access,p_refresh,p_resource)
$$;

CREATE FUNCTION public.library_mcp_revoke_token(p_hash bytea,p_client text,p_resource text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
  UPDATE public.library_mcp_grants g SET revoked_at=coalesce(revoked_at,clock_timestamp())
    WHERE g.client_id=p_client AND g.resource=p_resource AND EXISTS(
      SELECT 1 FROM public.library_mcp_credentials t WHERE t.grant_id=g.grant_id
        AND t.token_hash=p_hash AND t.kind IN ('access','refresh'));
  RETURN true;
END $$;
CREATE FUNCTION public.library_mcp_list_grants(p_email text,p_resource text)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
  SELECT coalesce(jsonb_agg(item ORDER BY created DESC),'[]'::jsonb) FROM (
    SELECT g.created_at AS created,jsonb_build_object('grant_id',g.grant_id,'client_id',g.client_id,
      'client_name',c.client_name,'scopes',g.scopes,'redirect_uri',g.redirect_uri,'created_at',extract(epoch FROM g.created_at),
      'last_used_at',extract(epoch FROM g.last_used_at),'expires_at',extract(epoch FROM g.expires_at)) AS item
    FROM public.library_mcp_grants g JOIN public.library_mcp_clients c USING(client_id)
    WHERE g.subject=p_email AND g.resource=p_resource AND c.resource=p_resource
      AND c.disabled_at IS NULL AND g.revoked_at IS NULL AND g.expires_at>clock_timestamp()
      AND EXISTS(SELECT 1 FROM public.library_mcp_credentials t WHERE t.grant_id=g.grant_id AND t.kind='refresh')
    ORDER BY g.created_at DESC LIMIT 100
  ) rows
$$;
CREATE FUNCTION public.library_mcp_revoke_grant(p_id text,p_email text,p_resource text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE n integer;
BEGIN
  UPDATE public.library_mcp_grants SET revoked_at=coalesce(revoked_at,clock_timestamp())
    WHERE grant_id::text=p_id AND subject=p_email AND resource=p_resource;
  GET DIAGNOSTICS n=ROW_COUNT;
  RETURN n>0;
END $$;
CREATE FUNCTION public.library_mcp_rate_limit(p_key bytea,p_limit integer,p_window integer)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE bucket bigint; n integer;
BEGIN
  IF p_limit NOT BETWEEN 1 AND 1000 OR p_window NOT BETWEEN 1 AND 86400 THEN
    RAISE EXCEPTION 'invalid MCP rate bound' USING ERRCODE='22023';
  END IF;
  bucket:=floor(extract(epoch FROM clock_timestamp())/p_window)::bigint*p_window;
  INSERT INTO public.library_mcp_rate_buckets(key_hash,window_start,window_seconds,attempts,expires_at)
    VALUES(p_key,bucket,p_window,1,to_timestamp(bucket+p_window))
    ON CONFLICT(key_hash,window_start,window_seconds) DO UPDATE SET
      attempts=least(public.library_mcp_rate_buckets.attempts+1,1001)
    RETURNING attempts INTO n;
  RETURN n<=p_limit;
END $$;
CREATE FUNCTION public.library_mcp_cleanup(p_limit integer)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE removed integer:=0; n integer;
BEGIN
  IF p_limit NOT BETWEEN 1 AND 1000 THEN RAISE EXCEPTION 'invalid MCP cleanup bound' USING ERRCODE='22023'; END IF;
  DELETE FROM public.library_mcp_auth_requests WHERE tx_hash IN (
    SELECT tx_hash FROM public.library_mcp_auth_requests WHERE expires_at<clock_timestamp() LIMIT p_limit);
  GET DIAGNOSTICS n=ROW_COUNT; removed:=removed+n;
  DELETE FROM public.library_mcp_rate_buckets WHERE (key_hash,window_start,window_seconds) IN (
    SELECT key_hash,window_start,window_seconds FROM public.library_mcp_rate_buckets
      WHERE expires_at<clock_timestamp() LIMIT p_limit-removed);
  GET DIAGNOSTICS n=ROW_COUNT; removed:=removed+n;
  DELETE FROM public.library_mcp_credentials WHERE token_hash IN (
    SELECT t.token_hash FROM public.library_mcp_credentials t JOIN public.library_mcp_grants g USING(grant_id)
      WHERE g.revoked_at<clock_timestamp()-interval '30 days'
        OR (t.kind IN ('code','refresh') AND t.expires_at<clock_timestamp())
        OR (t.kind='access' AND t.expires_at<clock_timestamp()-interval '90 days')
      LIMIT p_limit-removed);
  GET DIAGNOSTICS n=ROW_COUNT; removed:=removed+n;
  DELETE FROM public.library_mcp_grants WHERE grant_id IN (
    SELECT g.grant_id FROM public.library_mcp_grants g WHERE
      (g.expires_at<clock_timestamp()-interval '30 days' OR g.revoked_at<clock_timestamp()-interval '30 days'
        OR g.created_at<clock_timestamp()-interval '1 day')
      AND NOT EXISTS(SELECT 1 FROM public.library_mcp_credentials c WHERE c.grant_id=g.grant_id)
      LIMIT p_limit-removed);
  GET DIAGNOSTICS n=ROW_COUNT; removed:=removed+n;
  DELETE FROM public.library_mcp_clients WHERE client_id IN (
    SELECT c.client_id FROM public.library_mcp_clients c WHERE c.created_at<clock_timestamp()-interval '1 day'
      AND NOT EXISTS(SELECT 1 FROM public.library_mcp_auth_requests a WHERE a.client_id=c.client_id)
      AND NOT EXISTS(SELECT 1 FROM public.library_mcp_grants g WHERE g.client_id=c.client_id)
      LIMIT p_limit-removed);
  GET DIAGNOSTICS n=ROW_COUNT; removed:=removed+n;
  RETURN removed;
END $$;

-- Transfer ownership, strip default PUBLIC/function/table grants, expose only named operations.
GRANT USAGE ON SCHEMA public TO library_mcp_definer;
GRANT library_mcp_definer TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;
GRANT CREATE ON SCHEMA public TO library_mcp_definer;
DO $$
DECLARE t text; f record;
BEGIN
  FOREACH t IN ARRAY ARRAY['library_mcp_clients','library_mcp_auth_requests',
    'library_mcp_grants','library_mcp_credentials','library_mcp_rate_buckets'] LOOP
    EXECUTE format('ALTER TABLE public.%I OWNER TO library_mcp_definer',t);
  END LOOP;
  FOR f IN SELECT p.oid::regprocedure AS signature,p.proname FROM pg_proc p WHERE p.oid=ANY(ARRAY[
    'public.library_mcp_register_client(text,text,text[],text[],text)'::regprocedure,
    'public.library_mcp_get_client(text,text)'::regprocedure,
    'public.library_mcp_create_request(bytea,text,text,text,boolean,text,text,text[])'::regprocedure,
    'public.library_mcp_get_request(bytea,text)'::regprocedure,
    'public.library_mcp_bind_request(bytea,bytea,text,text,bytea,text)'::regprocedure,
    'public.library_mcp_authorize_request(bytea,bytea,text,bytea,bytea,text)'::regprocedure,
    'public.library_mcp_cancel_request(bytea,bytea,bytea,text)'::regprocedure,
    'public.library_mcp_credential_record(bytea,text,text,text,boolean)'::regprocedure,
    'public.library_mcp_get_code(bytea,text,text)'::regprocedure,
    'public.library_mcp_get_refresh(bytea,text,text,boolean)'::regprocedure,
    'public.library_mcp_get_access(bytea,text)'::regprocedure,
    'public.library_mcp_exchange(bytea,text,text,text[],bytea,bytea,text)'::regprocedure,
    'public.library_mcp_exchange_code(bytea,text,bytea,bytea,text)'::regprocedure,
    'public.library_mcp_rotate_refresh(bytea,text,text[],bytea,bytea,text)'::regprocedure,
    'public.library_mcp_revoke_token(bytea,text,text)'::regprocedure,
    'public.library_mcp_list_grants(text,text)'::regprocedure,
    'public.library_mcp_revoke_grant(text,text,text)'::regprocedure,
    'public.library_mcp_rate_limit(bytea,integer,integer)'::regprocedure,
    'public.library_mcp_cleanup(integer)'::regprocedure
  ]) LOOP
    -- Set ACLs while CURRENT_USER is still the owner. Membership has INHERIT FALSE;
    -- revoking after ownership transfer would only warn and leave PUBLIC EXECUTE intact.
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f.signature);
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM teamagent_app',f.signature);
    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='teamagent_dashboard') THEN
      EXECUTE format('REVOKE ALL ON FUNCTION %s FROM teamagent_dashboard',f.signature);
    END IF;
    IF f.proname NOT IN ('library_mcp_credential_record','library_mcp_exchange') THEN
      EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO teamagent_app',f.signature);
    END IF;
    EXECUTE format('ALTER FUNCTION %s OWNER TO library_mcp_definer',f.signature);
  END LOOP;
  IF has_function_privilege('teamagent_app','public.library_mcp_credential_record(bytea,text,text,text,boolean)','EXECUTE')
    OR has_function_privilege('teamagent_app','public.library_mcp_exchange(bytea,text,text,text[],bytea,bytea,text)','EXECUTE') THEN
    RAISE EXCEPTION 'unsafe library MCP private helper privilege';
  END IF;
END $$;
REVOKE CREATE ON SCHEMA public FROM library_mcp_definer;
REVOKE library_mcp_definer FROM CURRENT_USER;

DO $$
DECLARE r text; t text;
BEGIN
  FOREACH r IN ARRAY ARRAY['teamagent_app','teamagent_dashboard'] LOOP
    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
      IF pg_has_role(r,'library_mcp_definer','USAGE') OR pg_has_role(r,'library_mcp_definer','SET') THEN
        RAISE EXCEPTION 'unsafe library MCP role membership';
      END IF;
      FOREACH t IN ARRAY ARRAY['library_mcp_clients','library_mcp_auth_requests',
        'library_mcp_grants','library_mcp_credentials','library_mcp_rate_buckets'] LOOP
        IF has_table_privilege(r,'public.'||t,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
          RAISE EXCEPTION 'unsafe library MCP direct table privilege';
        END IF;
      END LOOP;
    END IF;
  END LOOP;
END $$;
