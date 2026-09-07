"""検索結果の決定論ヘッダ（result_guard）と「次の一手」提案のテスト。

本番の失敗モードを再現する:
  - **低スコアヒット**: 質問に直接一致する資料が無いのに、要約器が残った低関連度の
    チャンクを根拠に自信のある口調で書く（営業には「これが答え」に見える）。
  - **client_name 不一致**: 「A 社の資料ある?」に対し top1 が B 社の資料。
    要約器が B 社名を明示しないと、関係ないクライアントが関連資料の顔で出る。

ヘッダは **LLM ではなくコード**が入れるので、要約本文が何であっても必ず先頭に載る。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import ConverseResponse, TokenUsage
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills._shared.next_step import DELIVER_SUGGESTION
from teamagent.skills.base import SkillContext
from teamagent.skills.search.result_guard import (
    WEAK_RESULT_NOTICE,
    aliases,
    build_result_header,
    clients_match,
    detect_query_client,
    explain_client_guard,
    find_client_mention,
    hit_client_name,
    normalize_client,
    prefix_header,
)
from teamagent.skills.search.schema import SearchInput
from teamagent.skills.search.skill import SearchSkill
from teamagent.skills.search.two_stage import TWO_STAGE_CTX_KEY, TWO_STAGE_ENV, TWO_STAGE_NOTICE

MISMATCH_HEAD = "⚠️ ご指定のクライアントの資料ではありません（ヒット: "


# ── フィクスチャ（実 DB 0 / 実 Bedrock 0 / 実 Slack 0）────────────────────────


def _hit(score: float, **meta: Any) -> SearchHit:
    return SearchHit(chunk_id=1, content="本文", score=score, metadata=dict(meta))


def _hit_full(score: float, *, content: str, **meta: Any) -> SearchHit:
    """本文を指定できる版（content は metadata でなく SearchHit.content に入る）。"""
    return SearchHit(chunk_id=1, content=content, score=score, metadata=dict(meta))


@pytest.fixture
def fake_bedrock() -> MagicMock:
    mock = MagicMock()
    mock.converse.return_value = ConverseResponse(
        text="花王の提案書では動画施策を提案しています。",
        usage=TokenUsage(
            input_tokens=100,
            output_tokens=40,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.001,
        ),
        model_id="jp.anthropic.claude-haiku-4-5",
        latency_ms=100,
        stop_reason="end_turn",
    )
    return mock


def _pgvector(hits: list[SearchHit], *, vocab: list[str] | None = None) -> MagicMock:
    mock = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    mock.connection.return_value = cm
    mock.search_similar.return_value = hits
    mock.list_client_names.return_value = list(vocab or [])
    return mock


class FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


def _skill(bedrock: MagicMock, pgvector: MagicMock) -> SearchSkill:
    return SearchSkill(
        bedrock=bedrock, pgvector=pgvector, embedder=FakeEmbedder(), target_table="proposal_chunks"
    )


# ── 純関数 ────────────────────────────────────────────────────────────────


def test_normalize_strips_legal_suffix_and_noise() -> None:
    assert normalize_client("株式会社 資生堂") == normalize_client("資生堂")
    assert normalize_client("日本ガイシ（株）") == normalize_client("日本ガイシ")
    assert normalize_client("Acme Co., Ltd.") == normalize_client("acme")
    assert normalize_client(None) == ""


def test_clients_match_is_fail_open_when_unknown() -> None:
    assert clients_match("資生堂", "株式会社資生堂") is True
    assert clients_match("資生堂", "花王") is False
    # 判定不能（空・1 文字）は警告を出さない側へ倒す
    assert clients_match("", "花王") is True
    assert clients_match("A", "花王") is True


def test_hit_client_name_falls_back_to_cls_project() -> None:
    assert hit_client_name(_hit(0.9, client_name="花王")) == "花王"
    assert hit_client_name(_hit(0.9, cls_project="資生堂")) == "資生堂"
    assert hit_client_name(_hit(0.9)) == ""


def test_detect_query_client_prefers_longest() -> None:
    vocab = ["ユニ", "ユニー", "花王"]
    assert detect_query_client("ユニーの2回目提案", vocab) == "ユニー"
    assert detect_query_client("何かの資料", vocab) is None


def test_no_header_when_results_are_good() -> None:
    hits = [_hit(0.72, client_name="花王")]
    assert build_result_header(query="花王の提案書", hits=hits, weak_threshold=0.3) == ""


def test_weak_header_fires_below_threshold() -> None:
    header = build_result_header(query="値引き規定", hits=[_hit(0.21)], weak_threshold=0.3)
    assert header == WEAK_RESULT_NOTICE


def test_weak_header_disabled_by_zero_threshold() -> None:
    assert build_result_header(query="値引き規定", hits=[_hit(0.21)], weak_threshold=0.0) == ""


def test_no_header_when_there_are_no_hits() -> None:
    """0 件は要約側が「見つかりませんでした」を返す＝警告を重ねない。"""
    assert build_result_header(query="花王", hits=[], weak_threshold=0.3) == ""


def test_client_mismatch_header_uses_hit_client() -> None:
    hits = [_hit(0.8, client_name="花王"), _hit(0.7, client_name="資生堂")]
    header = build_result_header(
        query="資生堂の提案書", hits=hits, weak_threshold=0.3, query_client="資生堂"
    )
    assert header == "⚠️ ご指定のクライアントの資料ではありません（ヒット: 花王）。"


def test_client_mismatch_detected_from_hit_vocabulary_alone() -> None:
    """クライアント辞書が引けなくても、ヒットの client_name 集合で照合できる。"""
    hits = [_hit(0.8, client_name="花王"), _hit(0.7, client_name="資生堂")]
    header = build_result_header(query="資生堂の提案書ある?", hits=hits, weak_threshold=0.3)
    assert header == "⚠️ ご指定のクライアントの資料ではありません（ヒット: 花王）。"


def test_no_mismatch_when_top1_is_the_asked_client() -> None:
    hits = [_hit(0.8, client_name="株式会社資生堂"), _hit(0.7, client_name="花王")]
    assert build_result_header(query="資生堂の提案書", hits=hits, weak_threshold=0.3) == ""


def test_no_mismatch_when_top1_client_is_unknown() -> None:
    """top1 に取引先メタが無いときは「違う」と断定しない（誤警告を出さない）。"""
    hits = [_hit(0.8), _hit(0.7, client_name="花王")]
    header = build_result_header(
        query="資生堂の提案書", hits=hits, weak_threshold=0.3, query_client="資生堂"
    )
    assert header == ""


def test_both_warnings_stack_in_order() -> None:
    hits = [_hit(0.12, client_name="花王")]
    header = build_result_header(
        query="資生堂の提案書", hits=hits, weak_threshold=0.3, query_client="資生堂"
    )
    assert header.splitlines() == [
        WEAK_RESULT_NOTICE,
        "⚠️ ご指定のクライアントの資料ではありません（ヒット: 花王）。",
    ]


def test_prefix_header_is_idempotent() -> None:
    assert prefix_header("", "本文") == "本文"
    assert prefix_header("⚠️ 注意", "") == "⚠️ 注意"
    once = prefix_header("⚠️ 注意", "本文")
    assert once == "⚠️ 注意\n\n本文"
    assert prefix_header("⚠️ 注意", once) == once


# ── skill 統合: ヘッダは要約本文の**先頭**へ必ず載る ─────────────────────────


def test_run_injects_weak_header_ahead_of_summary(
    fake_bedrock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SEARCH_WEAK_RESULT_THRESHOLD", raising=False)  # 既定 0.3
    pg = _pgvector([_hit(0.18, client_name="花王")])

    out = _skill(fake_bedrock, pg).run(
        input=SearchInput(query="値引き規定はどこ"), ctx=SkillContext(metadata={})
    )

    assert out.answer.startswith(WEAK_RESULT_NOTICE)
    # LLM の本文は消さずに残す（ヘッダは前置きであって置換ではない）。
    assert "動画施策" in out.answer or "花王" in out.answer


def test_run_injects_client_mismatch_header(fake_bedrock: MagicMock) -> None:
    pg = _pgvector([_hit(0.81, client_name="花王")], vocab=["花王", "資生堂"])

    out = _skill(fake_bedrock, pg).run(
        input=SearchInput(query="資生堂の提案書ある？"), ctx=SkillContext(metadata={})
    )

    assert out.answer.startswith(MISMATCH_HEAD + "花王）。")


def test_run_uses_explicit_filter_client_as_the_asked_client(fake_bedrock: MagicMock) -> None:
    """明示 filter_client は辞書一致より優先される（利用者の指定が最上位）。"""
    pg = _pgvector([_hit(0.81, client_name="花王")], vocab=[])

    out = _skill(fake_bedrock, pg).run(
        input=SearchInput(query="提案書ある？", filter_client="資生堂"),
        ctx=SkillContext(metadata={}),
    )

    assert out.answer.startswith(MISMATCH_HEAD + "花王）。")


def test_run_adds_no_header_for_good_results(fake_bedrock: MagicMock) -> None:
    pg = _pgvector([_hit(0.81, client_name="花王")], vocab=["花王"])

    out = _skill(fake_bedrock, pg).run(
        input=SearchInput(query="花王の提案書"), ctx=SkillContext(metadata={})
    )

    assert not out.answer.startswith("⚠️")


def test_kill_switch_restores_previous_behaviour(
    fake_bedrock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEARCH_RESULT_GUARD", "false")
    pg = _pgvector([_hit(0.05, client_name="花王")], vocab=["花王", "資生堂"])

    out = _skill(fake_bedrock, pg).run(
        input=SearchInput(query="資生堂の提案書"), ctx=SkillContext(metadata={})
    )

    assert not out.answer.startswith("⚠️")
    pg.list_client_names.assert_not_called()  # 辞書 SQL も引かない（余計な負荷ゼロ）


def test_fast_path_keeps_empty_answer(fake_bedrock: MagicMock) -> None:
    """include_answer=False（/app の fast path）は answer='' の契約を壊さない。"""
    pg = _pgvector([_hit(0.05, client_name="花王")], vocab=["花王", "資生堂"])

    out = _skill(fake_bedrock, pg).run(
        input=SearchInput(query="資生堂の提案書", include_answer=False),
        ctx=SkillContext(metadata={}),
    )

    assert out.answer == ""
    assert out.total_cost_usd == 0.0


def test_two_stage_first_response_carries_the_header(
    fake_bedrock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """二段返しの**第一報**（続報予告）にも同じヘッダが付く。"""
    monkeypatch.setenv(TWO_STAGE_ENV, "true")
    pg = _pgvector([_hit(0.11, client_name="花王")], vocab=["花王", "資生堂"])
    skill = _skill(fake_bedrock, pg)
    skill._slack = MagicMock()  # 後追い投稿はここでは検証しない
    monkeypatch.setattr(skill, "deliver_followup_answer", lambda **_: True)

    out = skill.run(
        input=SearchInput(query="資生堂の提案書"),
        ctx=SkillContext(
            metadata={TWO_STAGE_CTX_KEY: True, "channel_id": "C1", "thread_ts": "1.0"}
        ),
    )

    assert out.answer.startswith(WEAK_RESULT_NOTICE)
    assert TWO_STAGE_NOTICE in out.answer  # 予告文そのものは消していない


def test_retrieve_hits_reuse_path_does_not_query_client_vocabulary(
    fake_bedrock: MagicMock,
) -> None:
    """他 Skill の再利用口（retrieve_hits）は DB クエリが 1 本も増えない（後方互換）。"""
    pg = _pgvector([_hit(0.05, client_name="花王")], vocab=["花王"])

    _skill(fake_bedrock, pg).retrieve_hits("資生堂の提案書", SkillContext(metadata={}))

    pg.list_client_names.assert_not_called()


# ── 次の一手の提案（search フック）────────────────────────────────────────────


def _run_with_resolved_file(
    fake_bedrock: MagicMock, monkeypatch: pytest.MonkeyPatch, *, query: str = "花王の提案書"
) -> str:
    pg = _pgvector([_hit(0.81, client_name="花王", title="花王提案.pdf")], vocab=["花王"])
    skill = _skill(fake_bedrock, pg)
    monkeypatch.setattr(
        skill, "_resolve_file_urls", lambda hits, ctx: {"花王提案.pdf": "https://drive/x"}
    )
    return skill.run(input=SearchInput(query=query), ctx=SkillContext(metadata={})).answer


def test_suggestion_fires_when_a_real_file_is_resolved(
    fake_bedrock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USE_KNOWLEDGE_DELIVER", "true")
    assert _run_with_resolved_file(fake_bedrock, monkeypatch).endswith(DELIVER_SUGGESTION)


def test_suggestion_is_silent_when_receiving_tool_is_off(
    fake_bedrock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """knowledge_deliver が OFF の環境では、出来ない約束をしない。"""
    monkeypatch.delenv("USE_KNOWLEDGE_DELIVER", raising=False)
    assert DELIVER_SUGGESTION not in _run_with_resolved_file(fake_bedrock, monkeypatch)


def test_suggestion_is_silent_when_user_already_asked_for_the_file(
    fake_bedrock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """依頼が完結している（＝もう「送って」と言っている）ときは提案しない。"""
    monkeypatch.setenv("USE_KNOWLEDGE_DELIVER", "true")
    answer = _run_with_resolved_file(fake_bedrock, monkeypatch, query="花王の提案書を送って")
    assert DELIVER_SUGGESTION not in answer


def test_suggestion_is_silent_without_a_resolved_file(
    fake_bedrock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USE_KNOWLEDGE_DELIVER", "true")
    pg = _pgvector([_hit(0.81, client_name="花王")], vocab=["花王"])
    skill = _skill(fake_bedrock, pg)
    monkeypatch.setattr(skill, "_resolve_file_urls", lambda hits, ctx: {})

    out = skill.run(input=SearchInput(query="花王の提案書"), ctx=SkillContext(metadata={}))

    assert DELIVER_SUGGESTION not in out.answer


def test_suggestion_does_not_execute_anything(
    fake_bedrock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """提案は文字列を足すだけ。ツール実行・Slack 投稿・DB 書込を伴わない。"""
    monkeypatch.setenv("USE_KNOWLEDGE_DELIVER", "true")
    pg = _pgvector([_hit(0.81, client_name="花王", title="花王提案.pdf")], vocab=["花王"])
    skill = _skill(fake_bedrock, pg)
    slack = MagicMock()
    skill._slack = slack
    monkeypatch.setattr(
        skill, "_resolve_file_urls", lambda hits, ctx: {"花王提案.pdf": "https://drive/x"}
    )

    out = skill.run(input=SearchInput(query="花王の提案書"), ctx=SkillContext(metadata={}))

    assert out.answer.endswith(DELIVER_SUGGESTION)
    slack.post_message.assert_not_called()
    slack.upload_file.assert_not_called()
    # 検索以外の SQL は 1 本も走らない（配信は起きていない）。
    pg.list_by_metadata.assert_not_called()


# ── 便A-1: クライアント不一致警告の誤爆停止 ───────────────────────────────────
#
# 本番実測（2026-09-02〜04）の警告 4 件を fixture 化する。いずれも利用者は正しい依頼を
# しており、警告は誤り。本物の不一致（「資生堂の提案書」で top1 が花王の資料）は残す。

FUKUDA_QUERY = "過去案件でユニークユーザー数について触れている案件の資料を出して"
SUGINAKA_QUERY = "「（アース製薬）」の社内資料を最大3件。資料名・種別・日付・出典リンクを一覧で。"
KAWAKAMI_QUERY = "ホーユー株式会社"
NISHIKAWA_QUERY = "『エリスショーツ』の提案に必要だと思う情報を収集してください"
SHIMADA_QUERY = "Slackにある、NewsTVの事例の動画の中から探して"


# ── 段1: クエリ内クライアント検出の語境界（guard の asked 検出だけ）────────────


def test_find_client_mention_strict_rejects_katakana_internal_match() -> None:
    """「ユニークユーザー」の途中に語彙「ユニー」を当てない（福田 09-04 の真因）。"""
    vocab = ["ユニー", "エスエス製薬"]
    assert find_client_mention(FUKUDA_QUERY, vocab, strict=True) is None
    # boost / sort が使う緩い経路は現行維持（再現率を落とさない）
    assert find_client_mention(FUKUDA_QUERY, vocab, strict=False) == "ユニー"


def test_find_client_mention_strict_prefers_longest_at_boundary() -> None:
    assert find_client_mention("ユニーの2回目提案", ["ユニ", "ユニー"], strict=True) == "ユニー"
    assert detect_query_client("ユニーの2回目提案", ["ユニ", "ユニー"]) == "ユニー"


def test_find_client_mention_strict_treats_kanji_and_honorifics_as_boundary() -> None:
    """漢字 2 文字規則は撤回: 「花王様」「花王向け」「株式会社明治」「電通に」は成立する。"""
    assert (
        find_client_mention("花王様限定の縦型ソリューションパッケージ", ["花王"], strict=True)
        == "花王"
    )
    assert find_client_mention("花王向けの提案資料を1件探して", ["花王"], strict=True) == "花王"
    assert find_client_mention("株式会社明治のR-1に提案していて", ["明治"], strict=True) == "明治"
    assert find_client_mention("電通に提案した飲料系の資料", ["電通"], strict=True) == "電通"
    assert (
        find_client_mention("「（アース製薬）」の社内資料", ["アース製薬"], strict=True)
        == "アース製薬"
    )


def test_find_client_mention_strict_ascii_needs_three_chars_and_boundary() -> None:
    """「IR」は当てない（小倉 09-03）。英数字は英数字の隣で切れない。大文字小文字は畳む。"""
    assert (
        find_client_mention(
            "IR関連の提案をしている資料を5つピックアップして", ["IR", "アルコニックス"], strict=True
        )
        is None
    )
    assert find_client_mention("NGKXの資料", ["NGK"], strict=True) is None
    assert find_client_mention("NGK の資料", ["NGK"], strict=True) == "NGK"
    assert find_client_mention("somarcaの資料", ["SOMARCA"], strict=True) == "SOMARCA"


def test_find_client_mention_strict_uses_legal_suffix_stripped_surface() -> None:
    """語彙「株式会社資生堂」はクエリ「資生堂の提案書」に当たる（法人格を剥いだ表層）。"""
    assert (
        find_client_mention("資生堂の提案書", ["株式会社資生堂"], strict=True) == "株式会社資生堂"
    )
    assert find_client_mention("Aの資料", ["A"], strict=True) is None


def test_find_client_mention_strict_surface_does_not_strip_inside_ascii_words() -> None:
    """レビュー指摘（PR #397）: 語彙「Prince」の照合表層に「Pre」を加えない。

    境界なし regex だと _surfaces('Prince') に 'Pre' が入り、クエリに独立語 'Pre' が出ただけで
    asked='Prince' が立って警告が増える（仕様と逆方向）。
    """
    assert find_client_mention("Preの資料", ["Prince"], strict=True) is None
    assert find_client_mention("Ventの資料", ["Vincent"], strict=True) is None
    # 独立語の法人格を剥いだ表層は従来どおり当たる
    assert (
        find_client_mention("Prince Hotel の資料", ["Prince Hotel Inc."], strict=True)
        == "Prince Hotel Inc."
    )


def test_find_client_mention_does_not_crash_on_regex_metacharacters() -> None:
    """DB 由来の語彙に正規表現メタ文字があっても落ちない（str.find 実装）。"""
    vocab = ["(株)P&G+", "A[B]", "C++", ")("]
    assert find_client_mention("P&G+の資料", vocab, strict=True) == "(株)P&G+"
    assert find_client_mention("C++の資料", vocab, strict=True) == "C++"
    assert find_client_mention("何かの資料", vocab, strict=True) is None
    assert find_client_mention("何かの資料", vocab, strict=False) is None


def test_find_client_mention_strict_is_fast_for_large_vocabulary() -> None:
    import time as _time

    vocab = [f"クライアント{i}株式会社" for i in range(1000)] + ["ユニー"]
    query = "ユニーの提案書と" + "ユニークユーザー数の資料" * 4
    started = _time.perf_counter()
    for _ in range(5):
        assert find_client_mention(query, vocab, strict=True) == "ユニー"
    assert (_time.perf_counter() - started) / 5 < 0.5  # 1 クエリあたり（CI 余裕込み）


# ── 段2: ヒット側判定（cls_project / client_name / title / entities / 別名）───────


def test_alias_seed_is_symmetric_and_static() -> None:
    assert aliases("アース製薬") == {"ハビットプロ"}
    assert aliases("ハビットプロ") == {"アース製薬"}
    assert aliases("エリスショーツ") == {"大王製紙"}  # キー照合は双方向部分一致
    assert aliases("花王") == {"花王グループカスタマーマーケティング"}
    # 競合ペアは seed に無い（title / cls_entities の共起から作らない）
    assert "資生堂" not in aliases("花王")
    assert aliases("資生堂") == set()
    assert aliases(None) == set()


def test_guard_silences_when_title_names_the_asked_client() -> None:
    top = _hit(0.8, cls_project="ハビットプロ", title="提案_アース製薬様_ハビットプロ.pptx")
    assert explain_client_guard("アース製薬", top) == ("title", False)
    header = build_result_header(
        query=SUGINAKA_QUERY, hits=[top], weak_threshold=0.3, query_client="アース製薬"
    )
    assert header == ""


def test_guard_silences_via_static_alias_seed() -> None:
    """title も本文も無くても、seed（アース製薬↔ハビットプロ）で沈黙する。"""
    top = _hit(0.8, cls_project="ハビットプロ")
    assert explain_client_guard("アース製薬", top) == ("alias", False)
    assert (
        build_result_header(
            query=SUGINAKA_QUERY, hits=[top], weak_threshold=0.3, query_client="アース製薬"
        )
        == ""
    )
    # 対称: 「ハビットプロの資料」で top1 が cls_project=アース製薬
    assert explain_client_guard("ハビットプロ", _hit(0.8, cls_project="アース製薬")) == (
        "alias",
        False,
    )


def test_guard_silences_hoyu_somarca_via_entities_or_seed() -> None:
    top = _hit(0.8, cls_project="SOMARCA", cls_entities="ホーユー,SOMARCA")
    assert explain_client_guard("ホーユー株式会社", top) == ("entities", False)
    assert (
        build_result_header(
            query=KAWAKAMI_QUERY, hits=[top], weak_threshold=0.3, query_client="ホーユー株式会社"
        )
        == ""
    )
    # entities が無くても seed で沈黙
    assert explain_client_guard("ホーユー株式会社", _hit(0.8, cls_project="SOMARCA")) == (
        "alias",
        False,
    )


def test_guard_entities_gate_can_be_switched_off() -> None:
    """SEARCH_CLIENT_GUARD_ENTITIES=false 相当（use_entities=False）では entities を見ない。"""
    top = _hit(0.8, cls_project="ABC商事", cls_entities="ホーユー")
    assert explain_client_guard("ホーユー", top, use_entities=True) == ("entities", False)
    assert explain_client_guard("ホーユー", top, use_entities=False) == ("none", True)


def test_guard_silences_elis_shorts_via_alias_key_match() -> None:
    """asked が「エリスショーツ」でも seed キー「エリス」が引ける（exact 照合ではない）。"""
    top = _hit(0.8, cls_project="大王製紙株式会社")
    assert explain_client_guard("エリスショーツ", top) == ("alias", False)
    assert explain_client_guard("エリス", top) == ("alias", False)


def test_guard_ignores_chunk_content_and_keeps_real_mismatch() -> None:
    """本文に asked が何回出ても沈黙しない（content 判定を戻すと赤）。"""
    top = _hit_full(0.8, content="競合の資生堂は…資生堂の施策…資生堂は", cls_project="花王")
    assert explain_client_guard("資生堂", top) == ("none", True)
    header = build_result_header(
        query="資生堂の提案書", hits=[top], weak_threshold=0.3, query_client="資生堂"
    )
    assert header == MISMATCH_HEAD + "花王）。"
    # 別名の無い取引先も同様（本文 2 回でも沈黙しない）
    other = _hit_full(0.8, content="エリス エリス", cls_project="ABC商事")
    assert explain_client_guard("エリス", other) == ("none", True)


def test_guard_undetermined_cases_never_warn() -> None:
    assert explain_client_guard("A", _hit(0.8, cls_project="花王")) == ("undetermined", False)
    assert explain_client_guard("資生堂", _hit(0.8, cls_project="A")) == ("undetermined", False)
    assert explain_client_guard("資生堂", _hit(0.8)) == ("unknown_hit", False)


def test_guard_does_not_synthesize_competitor_alias_from_title_cooccurrence() -> None:
    """(花王, 資生堂) を title 共起から作らない。title に無い競合名は本物の不一致のまま。"""
    kao_doc = _hit(0.8, cls_project="花王", title="花王_提案")
    assert explain_client_guard("資生堂", kao_doc) == ("none", True)
    assert "資生堂" not in aliases("花王") and "花王" not in aliases("資生堂")


def test_decision_records_reason_without_strings_from_query_or_title() -> None:
    decision: dict[str, Any] = {}
    build_result_header(
        query=SUGINAKA_QUERY,
        hits=[_hit(0.8, cls_project="ハビットプロ", title="提案_アース製薬様_ハビットプロ.pptx")],
        weak_threshold=0.3,
        query_client="アース製薬",
        decision=decision,
    )
    assert decision == {"asked_source": "caller", "matched_via": "title", "warned": False}
    detected: dict[str, Any] = {}
    build_result_header(
        query="資生堂の提案書",
        hits=[_hit(0.8, client_name="花王"), _hit(0.7, client_name="資生堂")],
        weak_threshold=0.3,
        decision=detected,
    )
    assert detected == {"asked_source": "hits", "matched_via": "none", "warned": True}
    self_org: dict[str, Any] = {}
    build_result_header(
        query=SHIMADA_QUERY,
        hits=[_hit(0.8, client_name="花王")],
        weak_threshold=0.3,
        query_client="NewsTV",
        decision=self_org,
    )
    assert self_org == {"asked_source": "caller", "matched_via": "self_org", "warned": False}


# ── run レベル（本番実例 4 件 + 自社名 + 本物の不一致維持）────────────────────


def _pgvector_new_schema(hits: list[SearchHit], *, vocab: list[str] | None = None) -> MagicMock:
    mock = _pgvector(hits, vocab=vocab)
    mock.search_similar_new_schema.return_value = hits
    mock.search_drive_by_client_names.return_value = []
    return mock


def _skill_new_schema(bedrock: MagicMock, pgvector: MagicMock) -> SearchSkill:
    return SearchSkill(
        bedrock=bedrock, pgvector=pgvector, embedder=FakeEmbedder(), use_new_schema=True
    )


def test_run_fukuda_unique_users_query_gets_no_client_warning(fake_bedrock: MagicMock) -> None:
    """福田 09-04: クライアント無指定の依頼に「ユニー」誤検出で警告が付いていた。"""
    pg = _pgvector(
        [_hit(0.8, cls_project="エスエス製薬株式会社")], vocab=["ユニー", "エスエス製薬"]
    )
    out = _skill(fake_bedrock, pg).run(input=SearchInput(query=FUKUDA_QUERY), ctx=SkillContext())
    assert not out.answer.startswith("⚠️ ご指定")


def test_run_suginaka_filter_client_with_brackets_is_normalized_and_silent(
    fake_bedrock: MagicMock,
) -> None:
    """杉中 09-03: filter_client=「（アース製薬）」→ ILIKE は「アース製薬」で引き、
    top1 が cls_project=ハビットプロ（同社ブランド）でも警告しない。"""
    pg = _pgvector_new_schema([_hit(0.8, cls_project="ハビットプロ")], vocab=[])
    out = _skill_new_schema(fake_bedrock, pg).run(
        input=SearchInput(query=SUGINAKA_QUERY, filter_client="（アース製薬）"),
        ctx=SkillContext(),
    )
    assert not out.answer.startswith("⚠️")
    first = pg.search_similar_new_schema.call_args_list[0]
    assert first.kwargs["metadata_contains"] == {"__client__": "アース製薬"}


def test_run_kawakami_hoyu_query_top_somarca_is_silent(fake_bedrock: MagicMock) -> None:
    """川上 09-02: 「ホーユー株式会社」→ top1 SOMARCA（ホーユーのブランド）。"""
    pg = _pgvector([_hit(0.8, cls_project="SOMARCA")], vocab=["ホーユー", "SOMARCA"])
    out = _skill(fake_bedrock, pg).run(input=SearchInput(query=KAWAKAMI_QUERY), ctx=SkillContext())
    assert not out.answer.startswith("⚠️")


def test_run_nishikawa_elis_shorts_top_daio_is_silent(fake_bedrock: MagicMock) -> None:
    """西河 09-02: filter_client=エリスショーツ → top1 大王製紙株式会社（エリスは同社ブランド）。"""
    pg = _pgvector(
        [_hit(0.8, cls_project="大王製紙株式会社")], vocab=["エリス", "大王製紙株式会社"]
    )
    out = _skill(fake_bedrock, pg).run(
        input=SearchInput(query=NISHIKAWA_QUERY, filter_client="エリスショーツ"),
        ctx=SkillContext(),
    )
    assert not out.answer.startswith("⚠️")


def test_run_self_org_name_in_query_is_not_a_client(fake_bedrock: MagicMock) -> None:
    """嶋田 08-28: 自社プロダクト名 NewsTV はクライアント指定ではない（run レベル固定）。"""
    pg = _pgvector([_hit(0.8, client_name="花王")], vocab=["NewsTV", "花王"])
    out = _skill(fake_bedrock, pg).run(input=SearchInput(query=SHIMADA_QUERY), ctx=SkillContext())
    assert not out.answer.startswith("⚠️")


def test_run_real_mismatch_still_warns_even_if_content_mentions_asked(
    fake_bedrock: MagicMock,
) -> None:
    """本物の不一致は残す: 「資生堂の提案書」で top1 が花王資料（本文に資生堂が何度出ても）。"""
    top = _hit_full(0.81, content="競合の資生堂は…資生堂…資生堂", client_name="花王")
    pg = _pgvector([top], vocab=["花王", "資生堂"])
    out = _skill(fake_bedrock, pg).run(
        input=SearchInput(query="資生堂の提案書ある？"), ctx=SkillContext()
    )
    assert out.answer.startswith(MISMATCH_HEAD + "花王）。")


def test_run_entities_env_gate_off_disables_entities_route(
    fake_bedrock: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    hits = [_hit(0.8, cls_project="ABC商事", cls_entities="ホーユー")]
    monkeypatch.setenv("SEARCH_CLIENT_GUARD_ENTITIES", "false")
    out_off = _skill(fake_bedrock, _pgvector(hits, vocab=["ホーユー"])).run(
        input=SearchInput(query="ホーユーの資料"), ctx=SkillContext()
    )
    assert out_off.answer.startswith(MISMATCH_HEAD + "ABC商事）。")
    monkeypatch.delenv("SEARCH_CLIENT_GUARD_ENTITIES", raising=False)  # 既定 ON
    out_on = _skill(fake_bedrock, _pgvector(hits, vocab=["ホーユー"])).run(
        input=SearchInput(query="ホーユーの資料"), ctx=SkillContext()
    )
    assert not out_on.answer.startswith("⚠️")


def test_match_client_lenient_path_keeps_boost_recall(fake_bedrock: MagicMock) -> None:
    """boost / sort の緩い substring は現行維持（strict は guard の asked 検出だけ）。"""
    skill = _skill(fake_bedrock, _pgvector([], vocab=["ユニー", "エリス"]))
    conn = MagicMock()
    assert skill._match_client(FUKUDA_QUERY, conn, "r1") == "ユニー"
    assert skill._match_client(FUKUDA_QUERY, conn, "r1", strict=True) is None
    assert skill._match_client("エリスショーツの提案", conn, "r1") == "エリス"
    assert skill._match_client("エリスショーツの提案", conn, "r1", strict=True) is None


def test_client_vocabulary_cache_is_keyed_by_groups_and_role(fake_bedrock: MagicMock) -> None:
    """語彙キャッシュは (user_groups, user_role) 単位（利用者横断の共有をやめる）＋TTL。"""
    pg = _pgvector([_hit(0.8, client_name="花王")], vocab=["花王"])
    skill = _skill(fake_bedrock, pg)
    ctx_a = SkillContext(metadata={"user_groups": ["sales"], "user_role": "member"})
    ctx_b = SkillContext(metadata={"user_groups": ["exec"], "user_role": "admin"})
    skill.run(input=SearchInput(query="花王の提案書"), ctx=ctx_a)
    skill.run(input=SearchInput(query="花王の提案書"), ctx=ctx_a)
    assert pg.list_client_names.call_count == 1  # 同じキーは再取得しない
    skill.run(input=SearchInput(query="花王の提案書"), ctx=ctx_b)
    assert pg.list_client_names.call_count == 2  # 別キーは別取得
    skill._client_vocab_ttl_s = 0.0
    skill.run(input=SearchInput(query="花王の提案書"), ctx=ctx_a)
    assert pg.list_client_names.call_count == 3  # TTL 切れで再取得


def test_client_vocabulary_failure_is_cached_and_logged_with_exc_type_only(
    fake_bedrock: MagicMock,
) -> None:
    from structlog.testing import capture_logs

    pg = _pgvector([_hit(0.8, client_name="花王")], vocab=[])
    pg.list_client_names.side_effect = RuntimeError("SELECT ... FROM documents failed: 秘密")
    skill = _skill(fake_bedrock, pg)
    with capture_logs() as logs:
        out = skill.run(input=SearchInput(query="資生堂の提案書"), ctx=SkillContext())
        skill.run(input=SearchInput(query="資生堂の提案書"), ctx=SkillContext())
    assert not out.answer.startswith("⚠️ ご指定")  # 語彙が引けなければ警告しない（fail-open）
    assert pg.list_client_names.call_count == 1  # 失敗も固定（毎リクエスト再試行しない）
    failed = [e for e in logs if e.get("event") == "search_client_vocab_failed"]
    assert len(failed) == 1
    assert failed[0]["exc_type"] == "RuntimeError"
    assert "秘密" not in repr(failed[0]) and "SELECT" not in repr(failed[0])


def test_client_guard_decision_log_contract(fake_bedrock: MagicMock) -> None:
    """search_client_guard_decision は asked 非 None の全リクエストで出る。kwargs は固定 4 つ。
    クエリ原文・資料名・クライアント名は載せない（G8）。"""
    from structlog.testing import capture_logs

    title = "提案_アース製薬様_ハビットプロ.pptx"
    pg = _pgvector([_hit(0.8, cls_project="ハビットプロ", title=title)], vocab=["アース製薬"])
    skill = _skill(fake_bedrock, pg)
    with capture_logs() as logs:
        skill.run(
            input=SearchInput(query=SUGINAKA_QUERY, filter_client="アース製薬"),
            ctx=SkillContext(request_id="req-guard-1"),
        )
        skill.run(
            input=SearchInput(query="アース製薬の資料"), ctx=SkillContext(request_id="req-guard-2")
        )
        skill.run(input=SearchInput(query="何かの資料"), ctx=SkillContext(request_id="req-guard-3"))
    events = [e for e in logs if e.get("event") == "search_client_guard_decision"]
    assert [e["request_id"] for e in events] == ["req-guard-1", "req-guard-2"]  # 指定なしは出ない
    for e in events:
        keys = set(e) - {"event", "log_level", "skill", "user_id"}
        assert keys == {"request_id", "asked_source", "matched_via", "warned"}
        assert e["matched_via"] == "title" and e["warned"] is False
        blob = repr(e)
        assert SUGINAKA_QUERY not in blob and title not in blob and "アース製薬" not in blob
    assert events[0]["asked_source"] == "filter"
    assert events[1]["asked_source"] == "vocab"
    # 警告が無いので search_result_guard_header は出ない（既存の契約）
    assert not [e for e in logs if e.get("event") == "search_result_guard_header"]


def test_knowledge_deliver_fukuda_initial_comment_has_no_client_warning(
    fake_bedrock: MagicMock,
) -> None:
    """福田 09-04 の実表示面: knowledge_deliver のファイルコメントに誤警告が載らない。"""
    from unittest.mock import AsyncMock

    from teamagent.skills.knowledge_deliver.schema import KnowledgeDeliverInput
    from teamagent.skills.knowledge_deliver.skill import KnowledgeDeliverSkill

    top = _hit(
        0.8,
        cls_project="エスエス製薬株式会社",
        source_type="gdrive",
        source_uri="gdrive://F1",
        title="エスエス製薬_提案.pdf",
    )
    search = _skill(fake_bedrock, _pgvector([top], vocab=["ユニー", "エスエス製薬"]))
    slack = MagicMock()
    slack.lookup_user_id_by_email = AsyncMock(return_value="U1")
    slack.open_dm = AsyncMock(return_value="D1")
    slack.upload_file = AsyncMock(return_value=True)
    gdrive = MagicMock()
    gdrive.download_file_bytes.return_value = b"%PDF-1.4 fake"
    out = KnowledgeDeliverSkill(search=search, slack=slack, gdrive=gdrive).run(
        KnowledgeDeliverInput(query=FUKUDA_QUERY),
        SkillContext(metadata={"user_email": "u@vectorinc.co.jp"}),
    )
    assert out.delivered_count == 1
    comment = slack.upload_file.await_args.kwargs.get("initial_comment") or ""
    assert not comment.startswith("⚠️ ご指定")
    assert not out.answer.startswith("⚠️ ご指定")
