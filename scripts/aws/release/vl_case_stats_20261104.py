import json

from teamagent.adapters.pgvector_client import PgVectorClient

with PgVectorClient.from_env().connection(
    app_role="teamagent_app", user_email=None, user_role="admin", application_name="vl-case-stats"
) as c:
    r = c.execute("""SELECT
        count(*) FILTER (WHERE coalesce(raw.campaign,'')<>'') AS sheet_in_case,
        count(*) FILTER (WHERE coalesce(e.campaign,'')<>'') AS now_in_case,
        count(*) FILTER (WHERE coalesce(e.advertiser,'')<>'') AS now_with_advertiser,
        count(*) FILTER (WHERE coalesce(raw.advertiser,'')<>'') AS sheet_with_advertiser,
        count(*) FILTER (WHERE e.case_inferred) AS ai_rows,
        count(*) FILTER (WHERE e.is_pr AND coalesce(e.campaign,'')='') AS pr_without_case
      FROM vl_videos_effective e JOIN vl_videos raw USING (platform, video_id)""").fetchone()
    top = c.execute("""SELECT e.advertiser, coalesce(e.campaign,'(広告主だけ)') AS campaign, count(*) AS n
      FROM vl_videos_effective e WHERE e.case_inferred GROUP BY 1,2 ORDER BY 3 DESC LIMIT 12""").fetchall()
print(
    "video_library casestats "
    + json.dumps({"stats": dict(r), "top": [dict(t) for t in top]}, ensure_ascii=False)
)
