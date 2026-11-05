import json

from teamagent.adapters.pgvector_client import PgVectorClient

with PgVectorClient.from_env().connection(
    app_role="teamagent_app", user_email=None, user_role="admin", application_name="vl-origin-check"
) as c:
    r = c.execute("""SELECT count(*) FILTER (WHERE NOT ('sheet'=ANY(ingest_sources))) AS no_sheet_marker,
       count(*) FILTER (WHERE NOT ('sheet'=ANY(ingest_sources)) AND NOT EXISTS (
         SELECT 1 FROM vl_accounts a WHERE a.platform=v.platform
         AND lower(ltrim(a.handle,'@'))=lower(ltrim(v.account_handle,'@'))
         AND a.operator_label IN ('NewsTV','ベクトル（VCL）','ベクトル九州','イニシャル','gracemode'))) AS would_flip_external,
       count(*) AS own_total
       FROM vl_videos v WHERE origin='own'""").fetchone()
    ext = c.execute("SELECT count(*) AS n FROM vl_videos WHERE origin='external'").fetchone()["n"]
print("video_library origincheck " + json.dumps({**dict(r), "external_now": ext}))
