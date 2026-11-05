"""0037/0038 適用直後の一回きり作業（2026-11-04）。mcp のタスク定義で動かす。
1) 11/02 にアカウントから補完した動画（シート由来でない）を ingest_sources={p3b} に直し、その 11/02 の公開数値を vl_public_metrics に写す
2) 事例の読み替え 4 件を登録（小俣さん 11/04 決定。既にある決定は上書きしない）
VL_APPLY=0 は件数だけ。
"""

import json
import os

from teamagent.adapters.pgvector_client import PgVectorClient

APPLY = os.environ.get("VL_APPLY") == "1"
ALIASES = [
    ("TikTok", "20260717", "ラフォーレ", "セール"),
    ("トップバリュ", "昭和レトロ", "AEON", None),
    ("アップガレージ", "メディア", "アップガレージ", "アプリキャンペーン"),
    ("ローソン九州", "ばりスゴ", "ローソン九州", "ホークス応援"),
]
WINDOW = (
    "2026-11-02 09:30:00+00",
    "2026-11-02 09:50:00+00",
)  # 補完を流した時間（18:30〜18:50 JST）
res = {}
with PgVectorClient.from_env().connection(
    app_role="teamagent_app", user_email=None, user_role="admin", application_name="vl-post-0038"
) as c:
    cond = "first_ingested_at BETWEEN %s AND %s AND advertiser IS NULL AND campaign IS NULL"
    res["backfilled"] = c.execute(
        f"SELECT count(*) AS n FROM vl_videos WHERE {cond}", WINDOW
    ).fetchone()["n"]
    if APPLY:
        res["sources_updated"] = c.execute(
            f"UPDATE vl_videos SET ingest_sources='{{p3b}}' WHERE {cond} AND ingest_sources='{{sheet}}'",
            WINDOW,
        ).rowcount
        res["public_metrics_copied"] = c.execute(
            """INSERT INTO vl_public_metrics (platform, video_id, captured_on, captured_at, total_plays, total_likes,
                  total_comments, total_shares, total_saves, metric_observation)
                SELECT m.platform, m.video_id, m.captured_on, v.first_ingested_at, m.total_plays::bigint, m.total_likes::bigint,
                  m.total_comments::bigint, m.total_shares::bigint, m.total_saves::bigint,
                  '{"source": "apify_backfill_20261102"}'::jsonb
                FROM vl_metrics m JOIN vl_videos v USING (platform, video_id)
                WHERE v.first_ingested_at BETWEEN %s AND %s AND v.advertiser IS NULL AND v.campaign IS NULL
                  AND m.captured_on = '2026-11-02'
                ON CONFLICT DO NOTHING""",
            WINDOW,
        ).rowcount
        res["aliases_registered"] = sum(
            c.execute(
                """INSERT INTO vl_case_aliases (from_advertiser, from_campaign, to_advertiser, to_campaign, decided_by)
               VALUES (%s,%s,%s,%s,'<actor-email>') ON CONFLICT DO NOTHING""",
                a,
            ).rowcount
            for a in ALIASES
        )
    res["aliases_total"] = c.execute("SELECT count(*) AS n FROM vl_case_aliases").fetchone()["n"]
print("video_library post0038 " + json.dumps(res, ensure_ascii=False))
