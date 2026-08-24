"""FMT レンダラテスト用の共有フィクスチャ（U1裁定の9枚構成を再現）。"""

from __future__ import annotations

import base64
import struct
import zlib
from typing import Any

from teamagent.skills.omiyage_report.contract import VOICE_UNMEASURED_NOTE

CTA = "上位10本の冒頭・価格・商品説明まで詳しく比較した事例が必要な方はご連絡ください。"
# 便1制約行の正 = 2026-08-24 計測経路裁定の共用定数（telop計測済み・voiceのみ未計測）
BEN1_CONSTRAINT = VOICE_UNMEASURED_NOTE


def make_png_bytes(
    width: int = 9, height: int = 16, rgb: tuple[int, int, int] = (162, 71, 101)
) -> bytes:
    """単色PNG（stdlibのみ・PILなし環境用）。"""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width
    body = zlib.compress(row * height)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", body) + chunk(b"IEND", b"")
    )


def make_png_data_uri(**kwargs: Any) -> str:
    return "data:image/png;base64," + base64.b64encode(make_png_bytes(**kwargs)).decode("ascii")


def make_card(
    url_slug: str = "7300000000000000001", *, caption: str | None = None
) -> dict[str, Any]:
    return {
        "source_url": f"https://www.tiktok.com/@someone/video/{url_slug}",
        "image": {"data_uri": make_png_data_uri(), "image_kind": "provided_thumbnail"},
        **({"caption": caption} if caption else {}),
    }


def make_ranking_card(index: int) -> dict[str, Any]:
    card = make_card(f"73000000000000000{10 + index}")
    card.update(
        {
            "account_name": f"アカウント{index}",
            "content_summary": f"「化粧水どれも同じと思ってない？」の実演紹介 {index}",
            "views": 2_200_000 - index * 100_000,
            "eg_rate_pct": [0.28, 6.31, 12.24, 0.82, 3.22][index - 1],
            "followers": 146_100 + index,
            "brand": "a" if index <= 3 else "b",
        }
    )
    return card


def make_deck_meta(**overrides: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "addressee": "花王株式会社 御中",
        "cover_title": "TikTok検索面\nデータ確認資料",
        "abstract": "一般キーワードとブランド名のTikTok検索結果を実測し、露出シェア・登場率・PR表記差を確認する。",
        "category_en": "TIKTOK SEARCH SNAPSHOT REPORT",
        "running_head": "TIKTOK SEARCH SNAPSHOT — MQURE × LASANA",
        "issuer": "株式会社プラチナム",
        "brand_a": {"name": "エムキュア"},
        "brand_b": {"name": "ラサーナ"},
        "part_titles": ["検索面の実態"],
        "method_target_constraints": [
            "TikTok検索（未ログイン相当）で上位表示データを取得し全件を決定論集計",
            "一般KW「ヘアケア」120本／ブランド名検索 各120本",
            BEN1_CONSTRAINT,
        ],
    }
    meta.update(overrides)
    return meta


def make_slides() -> list[dict[str, Any]]:
    """U1裁定: A → B(PART1) → 露出導入(C・Q番号なし「現状」) → Q1 D → Q2 C → Q3 C → Q4 D → Q5 E → H。"""
    return [
        {
            "type": "A",
            "heading": "",
            "data": {
                "thumbnail_pair": [
                    make_card("7300000000000000001"),
                    make_card("7300000000000000002"),
                ]
            },
        },
        {
            "type": "B",
            "heading": "",
            "data": {
                "part": 1,
                "title": "検索面の実態",
                "abstract": "一般KW検索120本とブランド名検索を全件解析し、露出の取り合いと語られ方を確認する。",
                "q_list": [
                    {"q_number": "Q1", "question": "誰が取り上げていて、階層ごとの効果は？"},
                    {
                        "q_number": "Q2",
                        "question": "伸びているのは、PR表記あり投稿か・なし投稿か？",
                    },
                    {"q_number": "Q3", "question": "どんな界隈で語られているか？"},
                    {"q_number": "Q4", "question": "キーワードはどの経路で登場するか？"},
                    {"q_number": "Q5", "question": "最も伸びている動画は何か？"},
                ],
            },
        },
        {
            "type": "C",
            "part": 1,
            "q_number": "現状",
            "heading": "一般KW「ヘアケア」の検索上位は、どちらが取っているか？",
            "lead": "一般KW検索上位120本のブランド露出シェア",
            "tag": {
                "variant": "発見",
                "text": "一般KW上位の露出はエムキュア12本・ラサーナ9本。第三者投稿が多数派で、公式起点の露出は少ない。",
            },
            "data": {
                "groups": [
                    {"label": "露出本数", "value_a": 12, "value_b": 9, "unit": "本"},
                    {"label": "露出シェア", "value_a": 10.0, "value_b": 7.5, "unit": "%"},
                ],
                "note_a": "上位120本中",
                "note_b": "上位120本中",
            },
        },
        {
            "type": "D",
            "part": 1,
            "q_number": "Q1",
            "heading": "誰が取り上げていて、フォロワー階層ごとの効果は？",
            "lead": "インフルエンサー階層別パフォーマンス",
            "tag": {
                "variant": "発見",
                "text": "メガ帯が平均EG率でも最高。ナノ帯優位の通説とは逆の分布だった。",
            },
            "data": {
                "columns": ["フォロワー階層", "本数", "平均再生数", "平均EG率"],
                "rows": [
                    ["ナノ（〜1万）", "117", "334,017", "1.24%"],
                    ["マイクロ（1〜10万）", "53", "283,717", "2.13%"],
                    ["ミドル（10〜50万）", "24", "430,150", "2.49%"],
                    ["メガ（50万〜）", "7", "1,625,728", "2.96%"],
                ],
                "example": make_card(
                    "7300000000000000003", caption="メガ帯の代表例（再生1,800万・EG0.82%）"
                ),
            },
        },
        {
            "type": "C",
            "part": 1,
            "q_number": "Q2",
            "heading": "伸びているのは、PR表記あり投稿か・なし投稿か？",
            "lead": "PR表記あり／なし別の平均EG率",
            "tag": {
                "variant": "結論",
                "text": "両ブランドともPR表記なし群のEG率が上。広告色を感じさせない文脈が反応を得ている。",
            },
            "data": {
                "groups": [
                    {"label": "PR表記あり 平均EG率", "value_a": 0.93, "value_b": 0.97, "unit": "%"},
                    {"label": "PR表記なし 平均EG率", "value_a": 1.94, "value_b": 1.35, "unit": "%"},
                ],
                "note_a": "PR表記あり（データ上） 51/204本",
                "note_b": "PR表記あり（データ上） 21/110本",
                "example": make_card(
                    "7300000000000000004", caption="PR表記なし・EG1.11%の推し語り実例"
                ),
            },
        },
        {
            "type": "C",
            "part": 1,
            "q_number": "Q3",
            "heading": "どんな界隈で語られているか？",
            "lead": "動画解析由来の界隈クラスタ別 件数と平均EG率（分類は推定・分母=解析できた本数）",
            "tag": {
                "variant": "所見",
                "text": "正直レビュー/検証系が最多。平均EG率の最高は別クラスタで、件数と反応は一致しない。",
            },
            "data": {
                "groups": [
                    {
                        "label": "正直レビュー/検証系",
                        "value_a": 1.20,
                        "value_b": 1.10,
                        "unit": "%",
                        "count_a": 43,
                        "count_b": 21,
                    },
                    {
                        "label": "成分オタク系",
                        "value_a": 1.64,
                        "value_b": 0.90,
                        "unit": "%",
                        "count_a": 10,
                        "count_b": 8,
                    },
                    {
                        "label": "ベスコス/まとめ系",
                        "value_a": 2.15,
                        "value_b": 1.02,
                        "unit": "%",
                        "count_a": 17,
                        "count_b": 9,
                    },
                    {
                        "label": "メンズ美容系",
                        "value_a": 1.88,
                        "value_b": 2.05,
                        "unit": "%",
                        "count_a": 10,
                        "count_b": 12,
                    },
                ],
            },
        },
        {
            "type": "D",
            "part": 1,
            "q_number": "Q4",
            "heading": "キーワードは、どの経路で登場しているか？",
            "lead": "登場率と頻出ハッシュタグ（caption / hashtag / telop の3経路・voiceのみ未計測）",
            "tag": {
                "variant": "発見",
                "text": "登場はハッシュタグ経路が最多。テロップ経路は視覚AI読取由来で、音声のみ未計測。",
            },
            "data": {
                "columns": ["検索軸", "分母", "caption登場率", "hashtag登場率", "telop登場率"],
                "rows": [
                    ["ブランド名検索", "120本", "45.0%", "62.5%", "38.3%"],
                    ["競合名検索", "120本", "41.7%", "58.3%", "35.0%"],
                    ["一般KW検索", "120本", "12.5%", "20.8%", "10.0%"],
                ],
            },
        },
        {
            "type": "E",
            "part": 1,
            "q_number": "Q5",
            "heading": "最も伸びている動画は何か？",
            "lead": "オントピックTOP5（再生数順・取得順位のスナップショット）",
            "tag": {
                "variant": "発見",
                "text": "再生数トップはEG率トップではない。小規模アカウントの突出EGが混在する。",
            },
            "data": {"cards": [make_ranking_card(index) for index in range(1, 6)]},
        },
        {
            "type": "H",
            "q_number": "総括",
            "heading": "検索データから見えた3つの型",
            "lead": "各Qの発見の再掲",
            "data": {
                "summary_rows": [
                    {
                        "number": 1,
                        "pattern": "メガ帯が効率でも最高",
                        "description": "階層が上がるほどEG率も伸びる右肩上がりの分布だった。",
                    },
                    {
                        "number": 2,
                        "pattern": "PR表記なし群が上",
                        "description": "両ブランドともPR表記なし群の平均EG率がPR表記あり群を上回った。",
                    },
                    {
                        "number": 3,
                        "pattern": "登場経路はハッシュタグ優位",
                        "description": "キーワード登場はハッシュタグ経路が最多で、テロップ経路が続いた。",
                    },
                ],
                "cta": True,
                "conclusion": "検索面の露出は第三者投稿が主戦場。次は上位動画の構成解剖で勝ち筋を特定する。",
            },
        },
    ]


def make_deck_content(**meta_overrides: Any) -> dict[str, Any]:
    return {"deck_meta": make_deck_meta(**meta_overrides), "slides": make_slides()}
