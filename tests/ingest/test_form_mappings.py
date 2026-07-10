"""ingest/form_mappings.py のテスト（ナレッジ共有フォーム回答シートの列写像）。

ヘッダ・値の形はナレッジ共有シートの実データ（2026-07-03 dump・190 行）に基づく。
"""

from __future__ import annotations

import pytest

from teamagent.ingest.form_mappings import (
    derive_knowledge_client_name,
    map_knowledge_fields,
)

# 実シートのヘッダ 13 列（Drive API 実データで確認）
_REAL_HEADERS = (
    "ファイルをアップ",
    "正式社名",
    "案件名",
    "クライアント種別",
    "提案プロダクト",
    "資料の概要",
    "このナレッジのポイントはここ！",
    "なぜそのナレッジ（資料）を共有したのか？",
    "フリーコメント",
    "送信者",
    "タイムスタンプ",
    "ドライブ格納",
    "保管先フォルダID記録（GAS処理)",
)


def _fields(**overrides: str) -> dict[str, str]:
    base = dict.fromkeys(_REAL_HEADERS, "")
    base.update(overrides)
    return base


# -----------------------------------------------------------
# map_knowledge_fields — 写像とコアヘッダ閾値
# -----------------------------------------------------------
def test_map_knowledge_fields_real_headers() -> None:
    """実ヘッダの行が想定キーに写像される（空値列・運用列は含めない）。"""
    out = map_knowledge_fields(
        _fields(
            正式社名="株式会社デルタ製薬",
            案件名="新製品プロモーション",
            クライアント種別="TOP500 or ベス10,メーカー",
            提案プロダクト="ビデオリリース,タテガタ",
            資料の概要="提案",
            送信者="@山田太郎",
            フリーコメント="長文自由記述",
            タイムスタンプ="2025/06/17 13:14:45",
            ドライブ格納="20250617_デルタ製薬",
        )
    )
    assert out == {
        "client_company": "株式会社デルタ製薬",
        "client_case": "新製品プロモーション",
        "client_type": "TOP500 or ベス10,メーカー",
        "proposed_menu": "ビデオリリース,タテガタ",
        "knowledge_kind": "提案",
        "submitter": "@山田太郎",
    }


def test_map_knowledge_fields_point_and_reason_columns_mapped_when_filled() -> None:
    """実データでは全行空の 2 列も、値が入れば写像される（運用開始に備える）。"""
    out = map_knowledge_fields(
        _fields(
            正式社名="ゼータ工業",
            資料の概要="提案",
            クライアント種別="メーカー",
            **{
                "このナレッジのポイントはここ！": "競合比較の 5p が刺さった",
                "なぜそのナレッジ（資料）を共有したのか？": "他業界にも横展開できるため",
            },
        )
    )
    assert out["knowledge_point"] == "競合比較の 5p が刺さった"
    assert out["share_reason"] == "他業界にも横展開できるため"


def test_map_knowledge_fields_header_variants_normalized() -> None:
    """半角括弧・末尾記号のゆれは正規化して照合する。"""
    out = map_knowledge_fields(
        {
            "正式社名": "ゼータ工業",
            "案件名": "採用",
            "クライアント種別": "メーカー",
            "提案プロダクト": "タテガタ",
            "なぜそのナレッジ(資料)を共有したのか?": "受注の決め手だったため",  # 半角括弧・半角 ?
        }
    )
    assert out["share_reason"] == "受注の決め手だったため"


def test_map_knowledge_fields_core_threshold_not_met_returns_empty() -> None:
    """コアヘッダ 3 未満のシートは空 dict（非対象シートへの副作用ゼロ）。"""
    assert map_knowledge_fields({}) == {}
    assert map_knowledge_fields({"タイムスタンプ": "2026/06/30", "共有者": "山田"}) == {}
    # コア 2 つ（正式社名 + 案件名）では発火しない
    assert map_knowledge_fields({"正式社名": "A社", "案件名": "案件X", "備考": "メモ"}) == {}


def test_map_knowledge_fields_fb_sheet_headers_do_not_fire() -> None:
    """営業 FB シートのヘッダではコア 0 hit（「顧客名・案件名」≠「案件名」・相互排他）。"""
    fb_headers = {
        "タイムスタンプ": "2026/06/30 10:00:00",
        "商流": "代理店",
        "顧客名": "アルファ広告社",
        "顧客名・案件名": "ベータ商事 / ガンマ製品",
        "商談フェーズ": "ヒアリング",
        "商談感触（BANT）": "B",
        "提案メニュー": "UGC",
    }
    assert map_knowledge_fields(fb_headers) == {}


def test_map_knowledge_fields_core_detection_is_header_based() -> None:
    """コア判定は「列の存在」で行い、値が空でも発火する（map_fb_fields と同一挙動）。"""
    out = map_knowledge_fields(_fields(送信者="@山田太郎"))  # コア 5 列は存在・値は全部空
    assert out == {"submitter": "@山田太郎"}


# -----------------------------------------------------------
# derive_knowledge_client_name — 正式社名 → first-class client_name
# -----------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 法人格 prefix / suffix（実データ両形あり）
        ("株式会社GA technologies", "GA technologies"),
        ("TOTO株式会社", "TOTO"),
        ("株式会社 SABON Japan", "SABON Japan"),
        # 敬称
        ("カゴメ様", "カゴメ"),
        ("TBCグループ株式会社様", "TBCグループ"),
        # 末尾の括弧注記
        ("ロート製薬（代理店：博報堂）", "ロート製薬"),
        ("ユニー（商業施設）", "ユニー"),
        ("大東建託株式会社（代理店さんは読広）", "大東建託"),
        # 複数社連記は先頭社（'・' は社名内区切りなので分割しない）
        ("集英社／キリンビバレッジ／ドン・キホーテ", "集英社"),
        ("TORRAS/代理店ADEX", "TORRAS"),
        ("株式会社ネオジャパンさま／UCC上島珈琲株式会社さま", "ネオジャパン"),
        ("ユニ・チャーム株式会社", "ユニ・チャーム"),
        # 官公庁・略称・法人格なしはそのまま
        ("東京都", "東京都"),
        ("内閣府", "内閣府"),
        ("JCB", "JCB"),
        ("電通", "電通"),
        ("健康保険組合連合会", "健康保険組合連合会"),
    ],
)
def test_derive_knowledge_client_name(raw: str, expected: str) -> None:
    assert derive_knowledge_client_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "  ", "なし", "その他", "色々", "-", "不明"])
def test_derive_knowledge_client_name_placeholders_return_none(raw: str) -> None:
    assert derive_knowledge_client_name(raw) is None


def test_derive_knowledge_client_name_corporate_only_is_kept() -> None:
    """法人格の除去で空になる場合は除去前の値を保持する（silent drop させない）。"""
    assert derive_knowledge_client_name("株式会社") == "株式会社"
