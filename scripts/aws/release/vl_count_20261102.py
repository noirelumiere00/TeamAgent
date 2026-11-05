import json

from teamagent.adapters.pgvector_client import PgVectorClient

G = [
    "emme_tokyo.jp",
    "noako_cosme",
    "maripii_cosme",
    "nene_cosme_nene",
    "emme_beauty_life",
    "emme_cosme",
    "tesuko_test",
    "emme_make_cosme",
    "emme_make_",
    "emme_makeup__",
    "emme_mirupi",
    "mayo___media",
    "uni___media",
    "meluffe_media",
    "nomnom_dog",
]
with PgVectorClient.from_env().connection(
    app_role="teamagent_app", user_email=None, user_role="admin", application_name="vl-count"
) as c:
    g = c.execute(
        "SELECT account_handle h, count(*) n, max(posted_at)::date last FROM vl_videos WHERE account_handle = ANY(%s) GROUP BY 1 ORDER BY 2 DESC",
        (G,),
    ).fetchall()
    t = c.execute(
        "SELECT count(*) n, max(posted_at)::date last, count(*) FILTER (WHERE posted_at >= '2026-10-16') recent, count(*) FILTER (WHERE thumbnail_s3_key IS NULL) no_thumb FROM vl_videos"
    ).fetchone()
print(
    "video_library count "
    + json.dumps(
        {"grace": [dict(r, last=str(r["last"])) for r in g], "total": dict(t, last=str(t["last"]))},
        ensure_ascii=False,
    )
)
