import json

from teamagent.adapters.pgvector_client import PgVectorClient

ROWS = [
    ("emme_tokyo.jp", "EMME [エメ] / コスメ・美容", "美容関連アカウント"),
    ("noako_cosme", "のあこ💭コスメと美容", "美容関連アカウント"),
    ("maripii_cosme", "まりぴー🌸コスメ・メイク", "美容関連アカウント"),
    ("nene_cosme_nene", "ねね🧸コスメ・美容", "美容関連アカウント"),
    ("emme_beauty_life", "わた🩰コスメとメイク", "美容関連アカウント"),
    ("emme_cosme", "めーたん🧁コスメと美容", "美容関連アカウント"),
    ("tesuko_test", "テス子🧪商品をテストする女", "美容関連アカウント"),
    ("emme_make_cosme", "ちぴこ🌷コスメ・美容", "美容関連アカウント"),
    ("emme_make_", "りり🤍コスメとメイク", "美容関連アカウント"),
    ("emme_makeup__", "にゃむ🐈‍⬛コスメ・メイク", "美容関連アカウント"),
    ("emme_mirupi", "みるぴ🍒コスメ・メイク", "美容関連アカウント"),
    ("mayo___media", "まよ🐈トレンド先取りちゃん", "トレンドアカウント"),
    ("uni___media", "うに🪽バズ速報ちゃん", "トレンドアカウント"),
    ("meluffe_media", "Meluffe(メルフィー) | トレンド情報をお届け🧸💕", "トレンドアカウント"),
    ("nomnom_dog", "nomnom", "犬アカウント"),
]
n = 0
with PgVectorClient.from_env().connection(
    app_role="teamagent_app",
    user_email=None,
    user_role="admin",
    application_name="vl-grace-accounts",
) as c:
    for h, name, cls in ROWS:
        n += c.execute(
            "INSERT INTO vl_accounts (platform, handle, display_name, operator_label, classification) VALUES ('tiktok',%s,%s,'gracemode',%s) ON CONFLICT (platform, handle) DO NOTHING",
            (h, name, cls),
        ).rowcount
    total = c.execute(
        "SELECT count(*) AS n FROM vl_accounts WHERE operator_label='gracemode'"
    ).fetchone()["n"]
print("video_library grace " + json.dumps({"inserted": n, "gracemode_accounts": total}))
