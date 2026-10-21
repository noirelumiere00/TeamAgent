"""検索成功と短文化失敗を区別し、取得根拠を返す。外部接続はしない。"""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from teamagent.skills.search import fanout
from tests.skills.search.test_fanout import META, _vault_hit, make, run
from tests.skills.search.test_fanout import setup as setup

NO_ANSWER = "取得した資料では、質問への回答を確認できませんでした。"


def _hits():
    return [
        replace(
            _vault_hit(
                i,
                title=f"匿名案件{i}",
                source_uri=f"gdrive://FILE{i}",
                document_id=f"doc-{i}",
                cls_industry="食品",
                updated_at="2025-09-18",
            ),
            content="食品のショート動画を提案。配信面と出演者を組み合わせた。",
        )
        for i in range(1, 7)
    ]


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("drive_first_enabled", [False, True])
@pytest.mark.parametrize(
    "rejected", ["投稿は900万回再生です。[S:1]", "根拠のない主張。[S:999]", NO_ANSWER + "[S:1]", ""]
)
def test_found_with_rejected_compact_prose_returns_source_cards(
    monkeypatch, fanout_enabled, drive_first_enabled, rejected
):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", str(int(drive_first_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    skill, _ = make(_hits())
    skill._summarize = MagicMock(return_value=(rejected, 0.003))
    out = run(
        skill,
        query="食品メーカーのショート動画の事例はある？",
        meta={
            **META,
            "_search_compact_answer": True,
            "_search_direct_answer": True,
            "_search_direct_answer_kinds": ("list_fast",),
        },
    )
    assert out.found and out.hits
    assert NO_ANSWER not in out.answer
    assert out.answer.startswith("*匿名案件1*（2025年9月）")
    assert "• 食品のショート動画を提案。\n• 配信面と出演者を組み合わせた。" in out.answer
    assert out.answer.count("|資料を見る>") == 5
    assert "900万" not in out.answer and "根拠のない主張" not in out.answer
    assert "探した範囲" not in out.answer and "出典:" not in out.answer
    assert out.answer_format == "list"
    assert out.direct_answer == out.answer and out.direct_answer_kind == "list_fast"
    if drive_first_enabled:
        assert [r.drive_file_id for r in out.displayed_refs] == [f"FILE{i}" for i in range(1, 6)]
    skill._summarize.assert_called_once()


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("rejected", ["投稿は900万回再生です。[S:1]", NO_ANSWER + "[S:1]", ""])
def test_other_questions_retry_the_normal_answer_without_restoring_rejected_prose(
    monkeypatch, fanout_enabled, rejected
):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    skill, _ = make(_hits())
    skill._summarize = MagicMock(
        side_effect=lambda *a, **kw: (
            (rejected, 0.003)
            if kw.get("compact_sources") is not None
            else ("配信面と出演者を組み合わせた記録があります。", 0.004)
        )
    )
    out = run(skill, query="進捗はどうなっている？", meta={**META, "_search_compact_answer": True})
    assert out.found and out.hits
    assert NO_ANSWER not in out.answer and "900万" not in out.answer
    assert "配信面と出演者を組み合わせた記録があります。" in out.answer
    assert out.answer_format is None
    assert skill._summarize.call_count == 2
    assert out.total_cost_usd == pytest.approx(0.007 + (0.002 if fanout_enabled else 0))


@pytest.mark.parametrize("query", ["資料を探して", "案件はある？", "提案書はある？"])
def test_document_requests_use_the_same_card_fallback(monkeypatch, query):
    skill, _ = make(_hits())
    skill._summarize = MagicMock(return_value=("根拠なし。[S:999]", 0.0))
    out = run(skill, query=query, meta={**META, "_search_compact_answer": True})
    assert out.found and out.answer.startswith("*匿名案件1*")
    assert "|資料を見る>" in out.answer and NO_ANSWER not in out.answer


@pytest.mark.parametrize("model", ["", NO_ANSWER + "[S:1]", "投稿は900万回です。[S:1]"])
def test_empty_compact_renderer_signals_failure_instead_of_absence(model):
    from teamagent.skills.search.compact import render

    assert render(model, {"1": ("資料", "https://example.test/file", "記録です。")}) == ""


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_reported_question_keeps_the_existing_case_list_path(monkeypatch, fanout_enabled):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])
    skill, _ = make(_hits())
    skill._summarize = MagicMock(side_effect=AssertionError("case lists need no model"))
    out = run(
        skill,
        query="食品メーカーのショート動画の事例ってある？",
        meta={**META, "_search_compact_answer": True},
    )
    assert out.found and out.answer.startswith("*匿名案件1*")
    assert NO_ANSWER not in out.answer and out.answer.count("|資料を見る>") == 5
    skill._summarize.assert_not_called()


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_normal_retry_failure_still_returns_retrieved_sources(monkeypatch, fanout_enabled):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    skill, _ = make(_hits())
    skill._summarize = MagicMock(side_effect=[("根拠なし。[S:999]", 0.0), TimeoutError()])
    out = run(skill, query="進捗は？", meta={**META, "_search_compact_answer": True})
    assert out.found and out.answer
    assert NO_ANSWER not in out.answer and "根拠なし" not in out.answer
    assert "https://drive.google.com/file/d/FILE1/view" in out.answer
    assert out.answer_format is None and out.direct_answer is None


def test_notice_only_does_not_hide_an_empty_compact_answer():
    from teamagent.skills.search.compact import is_empty, render

    notice = "関連度が低い参考資料です。"
    answer = render("根拠なし。[S:999]", {}, notice=notice)
    assert answer == notice
    assert is_empty(answer, notice=notice)


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("complete", [False, True])
def test_zero_hits_keep_the_existing_complete_or_incomplete_decision(
    monkeypatch, fanout_enabled, complete
):
    from teamagent.skills.search.composite import SlackLookup

    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    skill, _ = make([])
    skill._alias_expansion = False
    lookup = SlackLookup(
        status="ok" if complete else "error", reason="ok" if complete else "timeout"
    )
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: lookup)
    monkeypatch.setattr(skill, "_start_slack_lookup", lambda *a: object())
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: lookup)
    skill._summarize = MagicMock(side_effect=AssertionError("zero hits need no summary"))
    out = run(skill, query="資料はある？", meta={**META, "_search_compact_answer": True})
    assert not out.found and not out.hits
    assert ("検索が完了せず" in out.answer) is not complete
    assert NO_ANSWER not in out.answer
    skill._summarize.assert_not_called()


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_found_never_returns_the_empty_claim_even_outside_compact(monkeypatch, fanout_enabled):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    skill, _ = make(_hits())
    skill._summarize = MagicMock(return_value=(NO_ANSWER, 0.0))
    out = run(skill, query="進捗は？", meta=META)
    assert out.found and out.answer
    assert NO_ANSWER not in out.answer
    assert "https://drive.google.com/file/d/FILE1/view" in out.answer
