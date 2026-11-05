import json

from teamagent.adapters.pgvector_client import PgVectorClient

with PgVectorClient.from_env().connection(
    app_role="teamagent_app",
    user_email=None,
    user_role="admin",
    application_name="vl-import-caption",
) as c:
    r = c.execute(
        "SELECT video_id, account_handle, caption, hashtags FROM vl_videos WHERE 'url_manual' = ANY(ingest_sources) ORDER BY first_ingested_at DESC LIMIT 1"
    ).fetchone()
print("video_library importcaption " + json.dumps(dict(r), ensure_ascii=False))
