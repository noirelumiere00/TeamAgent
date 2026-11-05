import json

from teamagent.adapters.pgvector_client import PgVectorClient

with PgVectorClient.from_env().connection(
    app_role="teamagent_app", user_email=None, user_role="admin", application_name="vl-import-check"
) as c:
    b = c.execute(
        "SELECT batch_id, created_at FROM vl_url_import_batches ORDER BY created_at DESC LIMIT 3"
    ).fetchall()
    rows = c.execute("""SELECT r.status, count(*) AS n FROM vl_url_import_rows r
        WHERE r.batch_id = (SELECT batch_id FROM vl_url_import_batches ORDER BY created_at DESC LIMIT 1) GROUP BY 1""").fetchall()
    v = c.execute("""SELECT count(*) AS n, count(*) FILTER (WHERE thumbnail_s3_key IS NOT NULL) AS thumbs,
        count(*) FILTER (WHERE is_pr) AS pr, count(*) FILTER (WHERE origin='external') AS external,
        count(*) FILTER (WHERE page_is_ad) AS ad
        FROM vl_videos WHERE 'url_manual' = ANY(ingest_sources)""").fetchone()
    ctl = c.execute("SELECT paused, pause_reason FROM vl_public_fetch_control").fetchall()
print(
    "video_library importcheck "
    + json.dumps(
        {
            "batches": [
                dict(x, batch_id=str(x["batch_id"]), created_at=str(x["created_at"])) for x in b
            ],
            "rows": [dict(r) for r in rows],
            "videos": dict(v),
            "control": [dict(x) for x in ctl],
        },
        ensure_ascii=False,
    )
)
