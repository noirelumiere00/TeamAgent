import json
import os

from teamagent.adapters.pgvector_client import PgVectorClient

rows = json.loads(os.environ["VL_BULK_ROWS"])
apply = os.environ.get("VL_BULK_APPLY") == "1"
by = "<actor-email>"
with PgVectorClient.from_env().connection(
    app_role="teamagent_app",
    user_email=None,
    user_role="admin",
    application_name="vl-bulk-campaign-types",
) as c:
    res = {"missing": [], "already_set": [], "inserted": 0, "would_insert": 0}
    for adv, camp, cat in rows:
        if not c.execute(
            "SELECT 1 FROM vl_campaigns WHERE advertiser=%s AND campaign=%s", (adv, camp)
        ).fetchone():
            res["missing"].append(adv + "/" + camp)
            continue
        if c.execute(
            "SELECT 1 FROM vl_campaign_settings WHERE advertiser=%s AND campaign=%s", (adv, camp)
        ).fetchone():
            res["already_set"].append(adv + "/" + camp)
            continue
        if apply:
            res["inserted"] += c.execute(
                "INSERT INTO vl_campaign_settings (advertiser,campaign,business_category,updated_by) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (adv, camp, cat, by),
            ).rowcount
        else:
            res["would_insert"] += 1
    res["settings_total"] = c.execute("SELECT count(*) AS n FROM vl_campaign_settings").fetchone()[
        "n"
    ]
print("video_library bulk_types " + json.dumps(res, ensure_ascii=False))
