"""業種語彙の統合（2026-08-28）の契約テスト。

## 何を守るか

営業が「NewsTV 事例動画 ショート動画 UGC ヨーグルト 乳製品」で検索したところ、
``filter_industry=null`` になり、返った 10 件のうち **9 件が「ヨーグルト」も「乳製品」も
1 文字も含まない**（玩具・鉄道・金融・電機の提案書）状態で回答が生成された。

原因は業種語彙が 3 層に分かれて互いに一致していなかったこと:

  書き込み  ingest/classify.py      「例: … 等」＝ 開いた語彙（LLM が自由に生成）
  読み出し① search/knowledge_query.py  11 語の独自表（ヨーグルト無し・値も非正準）
  読み出し② skills/router.py           13 語の独自表（**「食品」自体が無い**）

本テストは「1 本に統合された状態」を機械的に固定する。
"""

from __future__ import annotations

import pytest

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.ingest.classify import _CLASSIFY_SYSTEM_PROMPT, _norm_industry
from teamagent.ingest.industry_taxonomy import (
    CANONICAL_INDUSTRIES,
    INDUSTRY_KEYWORDS,
    INDUSTRY_PROMPT_LIST,
    match_industry_keyword,
    normalize_industry,
)
from teamagent.skills.router import _LLM_ROUTER_INSTRUCTION, SkillRouter
from teamagent.skills.search.knowledge_query import extract_query_industry
from teamagent.skills.search.result_guard import build_result_header, is_self_org_name

# ── ① 正準語彙そのもの ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # 本番で実測された表記ゆれ（同一クライアントが 2 通りに保存されていた）
        ("旅行", "旅行・観光"),
        ("旅行・観光", "旅行・観光"),
        ("玩具・おもちゃ", "玩具"),
        ("電機・電子機器", "家電・電機"),
        ("運輸・鉄道", "運輸・交通"),
        # 旧 3 層に存在した語
        ("メーカー", "製造"),
        ("製造業", "製造"),
        ("食料品", "食品"),
        ("医療", "医療・製薬"),
        ("自治体", "自治体・公共"),
        # 正準値はそのまま
        ("食品", "食品"),
        ("IT", "IT"),
    ],
)
def test_normalize_industry_absorbs_the_drift(stored: str, expected: str) -> None:
    """保存済みの表記ゆれを正準値へ寄せる（再分類バッチ無しで揺れを吸収する唯一の手段）。"""
    assert normalize_industry(stored) == expected


@pytest.mark.parametrize("value", ["", "   ", None, "宇宙開発", "存在しない業種"])
def test_normalize_industry_returns_none_for_unknown(value: str | None) -> None:
    """未知値は「その他」へ潰さず None を返す。

    ``filter_industry`` は soft（industry = 値 OR NULL）なので、未知値を「その他」へ
    寄せると **「その他 ≠ 食品」として除外される**側に倒れる。None が安全側。
    """
    assert normalize_industry(value) is None


def test_prompt_list_is_generated_from_the_canonical_tuple() -> None:
    """LLM へ渡すリストは正準タプルから機械生成する（手書きすると 4 つ目の語彙になる）。"""
    for industry in CANONICAL_INDUSTRIES:
        assert industry in INDUSTRY_PROMPT_LIST
    assert INDUSTRY_PROMPT_LIST == " / ".join(CANONICAL_INDUSTRIES)


# ── ② 3 層が同じ語彙を見ているか ────────────────────────────────────────────


def test_keyword_table_yields_only_canonical_values() -> None:
    """高速路が返す値は必ず正準値（旧実装は "メーカー" 等の非正準値を返していた）。"""
    for industry in INDUSTRY_KEYWORDS:
        assert industry in CANONICAL_INDUSTRIES, industry


def test_keyword_table_has_no_single_character_generic_words() -> None:
    """1 文字の汎用語を禁じる。

    旧 router 実装は自治体に "市" / "県" を持っており、「市場調査」「都市開発」で
    誤爆していた。ここでヒットすると confidence 0.8 が確定し、
    **LLM ルーターが呼ばれなくなる**ため被害が大きい。
    """
    offenders = [
        (industry, kw) for industry, kws in INDUSTRY_KEYWORDS.items() for kw in kws if len(kw) == 1
    ]
    assert offenders == [], offenders


@pytest.mark.parametrize(
    "query",
    [
        "NewsTV 市場調査 の事例",  # 旧 "市" 誤爆
        "都市開発の提案",  # 旧 "市" 誤爆
        "アース製薬の過去資料",  # "製薬" が社名に当たる（実体は日用品）
        "ガストの事例",  # "ガス" が社名に当たる
    ],
)
def test_keyword_table_does_not_misfire_on_company_names(query: str) -> None:
    """社名の一部に当たって業種を誤判定しないこと。"""
    assert match_industry_keyword(query) is None


def test_llm_router_instruction_is_built_from_the_taxonomy() -> None:
    """ルーターの指示文に旧ハードコード 13 語が残っていないこと。"""
    assert INDUSTRY_PROMPT_LIST in _LLM_ROUTER_INSTRUCTION
    assert "食品" in _LLM_ROUTER_INSTRUCTION
    # 旧リストの並び（"製造業 / \n教育"）が残っていたら未統合
    assert "製造業 /" not in _LLM_ROUTER_INSTRUCTION


def test_classify_prompt_uses_a_closed_vocabulary() -> None:
    """書き込み側が「例 … 等」の開いた語彙に戻っていないこと。"""
    industry_line = next(
        line for line in _CLASSIFY_SYSTEM_PROMPT.splitlines() if line.startswith("- industry:")
    )
    assert "{industry_list}" not in _CLASSIFY_SYSTEM_PROMPT  # 置換漏れ
    assert "1 つだけ" in industry_line
    assert "等）" not in industry_line
    assert "食品" in industry_line


def test_classify_normalizes_but_keeps_unknown_industries() -> None:
    """正準化できたら正準値・できなければ生値（新業種の情報を捨てない）。"""
    assert _norm_industry("旅行") == "旅行・観光"
    assert _norm_industry("宇宙開発") == "宇宙開発"
    assert _norm_industry("") == ""


# ── 商材語は高速路で拾わず LLM へ委ねる ─────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    ["NewsTV 事例動画 ショート動画 UGC ヨーグルト 乳製品", "グラノーラの提案", "日本酒の施策"],
)
def test_product_words_fall_through_to_the_llm_router(query: str) -> None:
    """商材語は高速路で拾わない。

    ここに「ヨーグルト → 食品」を足して解決すると、次は「グラノーラ」で同じ事故が起きる。
    正しい経路は confidence を下げて LLM ルーターへ落とすこと。
    """
    assert extract_query_industry(query) is None
    decision = SkillRouter().route(query)
    assert decision.confidence < SkillRouter.LLM_FALLBACK_THRESHOLD


def test_industry_words_still_take_the_fast_path() -> None:
    """業界を名指しする語は LLM を呼ばずに解決する（コストと遅延の節約）。"""
    decision = SkillRouter().route("飲食業の事例")
    assert decision.extracted_filter == {"industry": "飲食"}
    assert decision.confidence >= SkillRouter.LLM_FALLBACK_THRESHOLD


# ── ③ 該当業種が 0 件のとき黙って要約しない ────────────────────────────────


def _hit(industry: str | None, *, client: str = "花王", score: float = 0.7) -> SearchHit:
    return SearchHit(
        chunk_id="c",
        content="x",
        score=score,
        metadata={"industry": industry, "cls_project": client},
    )


def test_industry_miss_is_stated_instead_of_silently_summarizing() -> None:
    """業種を絞ったのにその業種の資料が 0 件なら、明示する。

    実測: 食品の資料が 0 件なのに玩具・鉄道・金融の提案書から
    「ヨーグルト向け UGC 施策」の回答が生成された。
    """
    header = build_result_header(
        query="ヨーグルトの事例",
        hits=[_hit(None), _hit("")],
        weak_threshold=0.0,
        asked_industry="食品",
    )
    assert "「食品」に分類された資料は見つかりませんでした" in header


def test_industry_miss_is_silent_when_the_industry_is_present() -> None:
    header = build_result_header(
        query="ヨーグルトの事例",
        hits=[_hit("食品"), _hit(None)],
        weak_threshold=0.0,
        asked_industry="食品",
    )
    assert header == ""


def test_industry_miss_respects_the_stored_drift() -> None:
    """保存値 ``旅行・観光`` は ``旅行`` の絞り込みに一致する（誤警告を出さない）。"""
    header = build_result_header(
        query="旅行の事例",
        hits=[_hit("旅行・観光")],
        weak_threshold=0.0,
        asked_industry="旅行",
    )
    assert header == ""


# ── ④ 自社名を「指定されたクライアント」として扱わない ─────────────────────


@pytest.mark.parametrize("name", ["NewsTV", "ベクトル", "Vector", "Aico", "株式会社ベクトル"])
def test_self_org_names_are_recognized(name: str) -> None:
    assert is_self_org_name(name) is True


@pytest.mark.parametrize("name", ["花王", "株式会社ジャパングレイス", "", None])
def test_client_names_are_not_self_org(name: str | None) -> None:
    assert is_self_org_name(name) is False


def test_no_false_client_warning_for_own_product_name() -> None:
    """本番で起きた誤警告そのものの回帰。

    クライアント語彙に自社プロダクト名 "NewsTV" が入っていたため
    ``query_client="NewsTV"`` と確定し、top1 が花王の資料だったことで
    「⚠️ ご指定のクライアントの資料ではありません」が**回答の一番上**に出ていた。
    利用者はクライアントを指定していない。
    """
    header = build_result_header(
        query="NewsTV 事例動画 ショート動画 UGC ヨーグルト 乳製品",
        hits=[_hit(None, client="花王グループカスタマーマーケティング株式会社")],
        weak_threshold=0.0,
        query_client="NewsTV",
    )
    assert header == ""


def test_real_client_mismatch_still_warns() -> None:
    """自社名の除外で、本物の不一致まで黙らせていないこと（デグレ検出）。"""
    header = build_result_header(
        query="花王の提案書ある?",
        hits=[_hit(None, client="株式会社ジャパングレイス")],
        weak_threshold=0.0,
        query_client="花王",
    )
    assert "ご指定のクライアントの資料ではありません" in header
    assert "株式会社ジャパングレイス" in header
