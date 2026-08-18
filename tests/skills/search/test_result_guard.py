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
    build_result_header,
    clients_match,
    detect_query_client,
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
