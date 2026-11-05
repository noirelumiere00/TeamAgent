import json

from teamagent.adapters.pgvector_client import PgVectorClient

with PgVectorClient.from_env().connection(
    app_role="teamagent_app", user_email=None, user_role="admin", application_name="vl-case-check"
) as c:
    rows = c.execute("""SELECT l.status, l.advertiser, l.campaign, l.confidence, v.account_handle,
            left(regexp_replace(coalesce(v.caption,''),'\\s+',' ','g'),70) AS cap
        FROM vl_video_campaign_links l JOIN vl_videos v USING (platform, video_id)
        WHERE l.source='ai' ORDER BY l.decided_at DESC LIMIT 25""").fetchall()
    cnt = c.execute(
        "SELECT status, count(*) AS n FROM vl_video_campaign_links WHERE source='ai' GROUP BY 1"
    ).fetchall()
    cases = c.execute(
        "SELECT count(DISTINCT (advertiser, campaign)) AS n FROM vl_videos_effective WHERE coalesce(campaign,'')<>''"
    ).fetchone()["n"]
print(
    "video_library casecheck "
    + json.dumps(
        {
            "counts": [dict(r) for r in cnt],
            "cases": cases,
            "rows": [dict(r, confidence=float(r["confidence"] or 0)) for r in rows],
        },
        ensure_ascii=False,
    )
)
