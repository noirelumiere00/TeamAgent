"""検索上位チェックのテスト用データと Bedrock の代役。

面のデータは 2026-09-25 に本番で取った「スパイスカレー 作り方」TikTok 上位 15 本
（アカウント・本文・再生数は実物どおり）に、レポートに出ていなかった項目
（表示名・フォロワー・保存・投稿日・尺・タグ）を仮の値で足したもの。
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

JST = dt.timezone(dt.timedelta(hours=9))
NOW = int(dt.datetime(2026, 9, 25, 12, 0, tzinfo=JST).timestamp())
KEYWORD = "スパイスカレー 作り方"

# (rank, account_id, account_name, followers, plays, saves, days_ago, duration, title, hashtags)
_ROWS: list[tuple[int, str, str, int, int, int, int, int, str, list[str]]] = [
    (1, "gonosara", "ごのさら", 123_000, 351_000, 11_200, 120, 58,
     "【4つでいい。本格スパイスカレー】 スパイスカレー。 料理をより好きになった",
     ["スパイスカレー", "料理", "簡単レシピ"]),
    (2, "spice_koki", "スパイスこうき", 86_000, 223_000, 15_200, 200, 45,
     "スパイスカレーを作るなら、 まずはこの4つだけ覚えておけばOK👌 ✔ クミン：カ",
     ["スパイスカレー", "スパイス"]),
    (3, "katokenoshokutaku", "加藤家の食卓", 150_000, 826_000, 9_900, 400, 62,
     "@katokenosyokutaku 🍚🥢 #cooking#", ["cooking", "料理"]),
    (4, "itamae_shinya", "板前しんや", 45_000, 20_000, 900, 30, 75,
     "【カレールーは卒業！】30分で作れる本格スパイスカレー", ["スパイスカレー", "料理"]),
    (5, "pasta.mori", "パスタ森", 320_000, 274_000, 6_000, 260, 90,
     "料理人が辿り着いた最高のスパイスチキンカレー#tiktok料理 #料理 #簡単レシピ",
     ["tiktok料理", "料理", "簡単レシピ"]),
    (6, "musuicurry", "無水カレー研究所", 28_000, 859_000, 30_000, 500, 52,
     "どうしても痩せたいから密かに食べていた無水スパイスカレー @ナチュラル専科 #PR",
     ["PR", "スパイスカレー"]),
    (7, "haha_home_cooking", "母の家庭料理", 8_000, 45_000, 1_300, 60, 40,
     "わが家の定番🍛スパイスカレーの作り方① #料理動画 #家庭料理 #スパイスカレー",
     ["料理動画", "家庭料理", "スパイスカレー"]),
    (8, "kurashiru.com", "クラシル", 5_200_000, 46_000, 2_100, 700, 60,
     "【スパイスを使うタイミングは3回！風味立つチキンカレーの作り方】｜#クラシル #料理",
     ["クラシル", "料理"]),
    (9, "spice_koki", "スパイスこうき", 86_000, 43_000, 2_500, 90, 38,
     "失敗しない！僕流スパイスの黄金比TOP4👇 これだけ押さえれば誰でも絶品カレーが",
     ["スパイスカレー", "スパイス"]),
    (10, "indocurryko", "インドカレー子", 210_000, 207_000, 8_000, 330, 55,
     "付き合ってはいけない男の特徴 ※レシピあり タクコ(1：1：1）を覚えて。",
     ["スパイスカレー"]),
    (11, "kurashiru.com", "クラシル", 5_200_000, 417_000, 12_000, 900, 70,
     '名店シェフ直伝！お店の味"スパイスチキンカレー"の作り方｜クラシル',
     ["クラシル", "スパイスカレー"]),
    (12, "okanetameteiekatchauzo", "お金貯めて家買っちゃうぞ", 510_000, 283_000, 5_000, 150,
     60, "１分でわかる！スパイスが効いた無水カレーが野菜の旨み抜群でうますぎた！",
     ["節約レシピ"]),
    (13, "fumicurry", "ふみカレー", 3_000, 13_000, 700, 20, 80,
     "スパイスカレー初心者向け。 失敗しにくい作り方、まとめていきます。 #スパイスカレー",
     ["スパイスカレー"]),
    (14, "kantanrecipi", "かんたんレシピ", 6_000, 69_000, 3_000, 45, 33,
     "スパイスカレーって意外と簡単に作れちゃうんです🫣🍛基本の4種類で作ってみたよ",
     ["スパイスカレー", "簡単レシピ"]),
    (15, "isojimahuuhu", "いそじま夫婦", 9_000, 71_000, 1_500, 75, 48,
     "世界一うまいスパイスカレーの作り方！！#料理 #簡単レシピ #晩御飯",
     ["料理", "簡単レシピ", "晩御飯"]),
]  # fmt: skip


def s3_rows(keyword: str = KEYWORD) -> list[dict[str, Any]]:
    """tiktok_acquire の posts.normalized.json と同じ形（media/operations.py の行）。"""
    return [
        {
            "pid": f"p{rank:04d}",
            "kw": keyword,
            "rank_display": rank,
            "url": f"https://www.tiktok.com/@{account}/video/{7_400_000_000_000_000_000 + rank}",
            "title": title,
            "account_id": account,
            "account_name": name,
            "followers": followers,
            "plays": plays,
            "likes": plays // 20,
            "comments": plays // 500,
            "shares": plays // 300,
            "saves": saves,
            "create_time": NOW - days * 86_400,
            "duration": duration,
            "hashtags": tags,
            "cover_url": "",
        }
        for rank, account, name, followers, plays, saves, days, duration, title, tags in _ROWS
    ]


# 本番の分類器の揺れを入れた分類結果（アカウント→カテゴリ）。
#  - katokenoshokutaku: 15 万人の料理家を ugc と答える（実例）→ コードで creator に直す
#  - musuicurry: 旧語彙の gourmet で答える → creator に寄せる
CLASSIFY_BY_ACCOUNT = {
    "gonosara": "creator",
    "spice_koki": "creator",
    "katokenoshokutaku": "ugc",
    "itamae_shinya": "creator",
    "pasta.mori": "influencer",
    "musuicurry": "gourmet",
    "haha_home_cooking": "ugc",
    "kurashiru.com": "media",
    "indocurryko": "creator",
    "okanetameteiekatchauzo": "influencer",
    "fumicurry": "ugc",
    "kantanrecipi": "ugc",
    "isojimahuuhu": "ugc",
}

# 集計にある数字だけで書いた結論（15本・7本・68%・0本・90日・6本・1万〜10万・4つ）。
GROUNDED_CONCLUSION: dict[str, Any] = {
    "headline": "料理系クリエイターが上位15本中7本を持ち、再生の68%を取る面",
    "winning": {
        "text": "クリエイター7本で再生の68%。保存率の上位は2位と9位のスパイス配合の解説",
        "ranks": [2, 9],
    },
    "gap": {"text": "公式は0本。直近90日以内の投稿は6本で、入れ替わりは緩やか", "ranks": []},
    "actions": [
        {
            "text": "フォロワー1万〜10万人の料理クリエイターと、スパイスを4つに絞る切り口で組む",
            "ranks": [1, 2, 14],
        }
    ],
    "angles": [
        {"label": "スパイスを4つに絞る", "ranks": [1, 2, 14]},
        {"label": "実在しない順位だけの切り口", "ranks": [99]},
    ],
}


class _Usage:
    def __init__(self, cost: float) -> None:
        self.cost_usd = cost


class _Resp:
    def __init__(self, text: str, cost: float = 0.001) -> None:
        self.text = text
        self.usage = _Usage(cost)


class FakeBedrock:
    """本番の BedrockClient.converse と同じ形（text・usage.cost_usd）を返す代役。

    分類はプロンプトの投稿一覧を読んで CLASSIFY_BY_ACCOUNT で答え、本番どおりコードフェンスで
    包む。結論は `conclusion` をそのまま JSON で返す（文字列を渡せばそのまま返す＝壊れた応答）。
    """

    def __init__(
        self,
        *,
        conclusion: dict[str, Any] | str | None = None,
        analyze_error: Exception | None = None,
        by_account: dict[str, str] | None = None,
    ) -> None:
        self.conclusion = GROUNDED_CONCLUSION if conclusion is None else conclusion
        self.analyze_error = analyze_error
        self.by_account = CLASSIFY_BY_ACCOUNT if by_account is None else by_account
        self.prompts: list[str] = []

    def converse(self, messages: list[dict[str, Any]], **kw: Any) -> _Resp:
        text = messages[0]["content"][0]["text"]
        self.prompts.append(text)
        if "検索面の読み" in text:
            if self.analyze_error is not None:
                raise self.analyze_error
            body = (
                self.conclusion
                if isinstance(self.conclusion, str)
                else json.dumps(self.conclusion, ensure_ascii=False)
            )
            return _Resp(body, 0.002)
        posts = json.loads(text.split("# 投稿一覧\n", 1)[1])
        cats = {p["id"]: self.by_account.get(p["account"], "ugc") for p in posts}
        return _Resp("```json\n" + json.dumps({"categories": cats}) + "\n```")
