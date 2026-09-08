"""client_match（クライアント名照合の純関数）のテスト。

result_guard の警告抑止用 ``hit_is_about_client`` と、rerank 用 ``_hit_matches_client``
（移設・挙動不変）を分けて固定する。前者は **本文（content）を見ない**ことが安全装置。
"""

from __future__ import annotations

from typing import Any

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.search import rerank
from teamagent.skills.search.client_match import (
    _hit_matches_client,
    hit_entities,
    hit_is_about_client,
    names_overlap,
    normalize_client,
    normalize_filter_client,
)


def _hit_full(score: float = 0.8, *, content: str = "本文", **meta: Any) -> SearchHit:
    return SearchHit(chunk_id=1, content=content, score=score, metadata=dict(meta))


# ── names_overlap（fail-open 無しの双方向部分一致）────────────────────────────


def test_names_overlap_is_bidirectional_after_normalization() -> None:
    assert names_overlap("アース製薬", "アース製薬株式会社") is True
    assert names_overlap("大王製紙株式会社", "大王製紙") is True
    assert names_overlap("エリスショーツ", "エリス") is True
    assert names_overlap("花王", "資生堂") is False


def test_names_overlap_does_not_fail_open_on_short_values() -> None:
    """clients_match と違い、判定不能は False（「何にでも当たる」を作らない）。"""
    assert names_overlap("A", "花王") is False
    assert names_overlap("", "花王") is False
    assert names_overlap("花王", None) is False


# ── normalize_filter_client（LLM が渡す filter_client の前処理）──────────────


def test_normalize_filter_client_strips_brackets_honorific_and_legal_suffix() -> None:
    assert normalize_filter_client("（アース製薬）") == "アース製薬"
    assert normalize_filter_client("「エリスショーツ」") == "エリスショーツ"
    assert normalize_filter_client("花王様") == "花王"
    assert normalize_filter_client("ホーユー株式会社") == "ホーユー"
    assert normalize_filter_client("  資生堂 ") == "資生堂"


def test_normalize_filter_client_keeps_ascii_names_containing_legal_suffix_letters() -> None:
    """レビュー指摘（PR #397）: ASCII 法人格は語境界つき。「Vincent」の中の inc を削らない。

    境界なしだと 'Vincent'→'Vent'、'Prince Hotel'→'Pre Hotel' になり、ILIKE が 0 件 →
    fail-open 再検索 → 無関係 top1 → 「本物っぽい」不一致警告、の連鎖が ASCII 名で新たに起きる。
    """
    assert normalize_filter_client("Vincent") == "Vincent"
    assert normalize_filter_client("Principal") == "Principal"
    assert normalize_filter_client("Lincoln") == "Lincoln"
    assert normalize_filter_client("Corpus") == "Corpus"
    assert normalize_filter_client("Scorpion") == "Scorpion"
    assert normalize_filter_client("Prince Hotel") == "Prince Hotel"
    # 独立語の法人格は従来どおり剥がす
    assert normalize_filter_client("Prince Hotel Inc.") == "Prince Hotel"
    assert normalize_filter_client("Shiseido Co., Ltd.") == "Shiseido"
    assert normalize_filter_client("Kao Corporation") == "Kao"
    assert normalize_filter_client("Acme Incorporated") == "Acme"


def test_normalize_filter_client_strips_bracketed_kabu_abbreviation() -> None:
    """レビュー指摘（PR #397）: 「（株）」「(株)」は括弧より先に法人格として剥がす。

    括弧を先に剥ぐと「株」だけが残り、ILIKE '%日本ガイシ株%' が cls_project='日本ガイシ' に
    当たらない（§3-6「法人格除去」の最頻出略記）。
    """
    assert normalize_filter_client("日本ガイシ（株）") == "日本ガイシ"
    assert normalize_filter_client("(株)P&G") == "P&G"
    assert normalize_filter_client("㈱明治") == "明治"
    # 括弧の内側に法人格が残る形（括弧の後の 2 回目の適用で剥がれる）
    assert normalize_filter_client("（株式会社ホーユー）") == "ホーユー"


def test_normalize_client_does_not_eat_legal_suffix_letters_inside_ascii_words() -> None:
    """両側正規化でも「Vincent」と「Vent」を同一視しない（境界なしでは両方 'vent' だった）。"""
    assert normalize_client("Vincent") == "vincent"
    assert normalize_client("Prince Hotel Inc.") == "princehotel"
    assert names_overlap("Vincent", "Vent") is False
    assert names_overlap("Prince Hotel", "Prince Hotel Inc.") is True


def test_normalize_filter_client_keeps_value_when_result_is_too_short() -> None:
    assert normalize_filter_client("IR") == "IR"
    assert normalize_filter_client("様") == "様"
    assert normalize_filter_client("") is None
    assert normalize_filter_client(None) is None


# ── hit_entities ───────────────────────────────────────────────────────────


def test_hit_entities_accepts_csv_and_list() -> None:
    assert hit_entities(_hit_full(cls_entities="ホーユー, SOMARCA,")) == ["ホーユー", "SOMARCA"]
    assert hit_entities(_hit_full(cls_entities=["祇園辻利", "サンマルクカフェ"])) == [
        "祇園辻利",
        "サンマルクカフェ",
    ]
    assert hit_entities(_hit_full()) == []


# ── hit_is_about_client（警告抑止用・本文は見ない）────────────────────────────


def test_about_client_matches_cls_project_client_name_title() -> None:
    assert hit_is_about_client(_hit_full(cls_project="株式会社資生堂"), "資生堂") == "cls_project"
    assert hit_is_about_client(_hit_full(client_name="花王"), "花王") == "client_name"
    assert (
        hit_is_about_client(
            _hit_full(cls_project="ハビットプロ", title="提案_アース製薬様_ハビットプロ.pptx"),
            "アース製薬",
        )
        == "title"
    )


def test_about_client_matches_entities_csv_and_list() -> None:
    hit = _hit_full(cls_project="SOMARCA", cls_entities="ホーユー,SOMARCA")
    assert hit_is_about_client(hit, "ホーユー株式会社") == "entities"
    hit_list = _hit_full(cls_project="SOMARCA", cls_entities=["ホーユー", "SOMARCA"])
    assert hit_is_about_client(hit_list, "ホーユー") == "entities"


def test_about_client_entities_can_be_disabled() -> None:
    hit = _hit_full(cls_project="SOMARCA", cls_entities="ホーユー,SOMARCA")
    assert hit_is_about_client(hit, "ホーユー", use_entities=False) is None


def test_about_client_uses_aliases_and_reports_alias() -> None:
    hit = _hit_full(cls_project="大王製紙株式会社")
    assert hit_is_about_client(hit, "エリス", aliases=["大王製紙"]) == "alias"
    assert hit_is_about_client(hit, "エリス") is None


def test_about_client_ignores_content_even_when_repeated() -> None:
    """本文に asked が何回出ても一致扱いにしない（競合比較ページで沈黙させない）。"""
    hit = _hit_full(cls_project="花王", content="競合の資生堂は…資生堂の施策…資生堂は")
    assert hit_is_about_client(hit, "資生堂") is None
    # 並べ替え用は本文一致で True（挙動不変・用途が違う）
    assert _hit_matches_client(hit, "資生堂") is True


def test_about_client_returns_none_when_nothing_matches() -> None:
    assert hit_is_about_client(_hit_full(cls_project="花王", title="花王_提案"), "資生堂") is None


# ── _hit_matches_client 移設（rerank から import できる・挙動不変）──────────────


def test_hit_matches_client_is_reexported_from_rerank() -> None:
    assert rerank._hit_matches_client is _hit_matches_client


def test_hit_matches_client_behaviour_unchanged() -> None:
    assert _hit_matches_client(_hit_full(cls_project="祇園辻利コラボ"), "祇園辻利") is True
    assert _hit_matches_client(_hit_full(content="サンマルクカフェ×祇園辻利"), "サンマルクカフェ")
    assert _hit_matches_client(_hit_full(content="Aを含む長文"), "A") is False
    assert _hit_matches_client(_hit_full(), "") is False
