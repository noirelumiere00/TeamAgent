"""確定回答の種類は本文の正規表現で推測せず、生成した経路から渡す。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.skills.search import fanout
from teamagent.skills.search.expand import ExpansionResult
from tests.skills.search.test_campaign_files import _video, _video_campaign
from tests.skills.search.test_fanout import META, _bedrock, _converse, _vault_hit, make, run


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setenv("USE_COMPOSITE_SEARCH", "0")
    monkeypatch.delenv("SEARCH_FANOUT_LIST_FAST", raising=False)
    monkeypatch.setattr(fanout, "build_expansion_planner", lambda: MagicMock(bedrock=MagicMock()))
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), None, (), "empty", 1, 0.0)
    )
    monkeypatch.setattr(fanout, "build_summary_client", _bedrock)


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_direct_not_found_skips_summary(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    skill, _ = make([_vault_hit(title="食品の事例")])
    skill._summarize = MagicMock(side_effect=AssertionError("該当なしの要約は禁止"))
    out = run(skill, query="宅配の事例", meta={**META, "_search_direct_answer": True})
    assert out.direct_answer_kind == "not_found"
    assert out.direct_answer == out.answer
    assert "宅配の事例は見つかりませんでした" in out.answer
    skill._summarize.assert_not_called()
    assert "direct_answer" not in out.model_dump()
    assert "direct_answer_kind" not in out.model_dump()


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("kind", ["comparison", "video_metrics"])
def test_direct_answer_delivers_summary_before_campaign_evidence(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, kind: str
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    campaigns = [
        _video_campaign(
            1, "食品", "ハンバーグ", [_video("https://example.test/a", "投稿A", 507700, 129)]
        ),
        _video_campaign(
            2, "食品", "別施策", [_video("https://example.test/b", "投稿B", 300000, 73)]
        ),
    ]
    for hit in campaigns:
        hit.metadata["source_uri"] = f"https://docs.google.com/spreadsheets/d/{hit.chunk_id}/edit"
    skill, _ = make(campaigns)
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    skill._campaign_files = True
    summary = (
        "投稿Aは食事のアレンジ、投稿Bは調理の時短を紹介しています。"
        if kind == "comparison"
        else "施策は完了し、商談は検討中です。11月に連絡予定です。"
    )
    skill._bedrock.converse.return_value = _converse(summary)
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    query = (
        "食品のハンバーグと別施策を比較して何が効いた？"
        if kind == "comparison"
        else "食品のハンバーグは今どうなっている？"
    )
    out = run(skill, query=query, meta={**META, "_search_direct_answer": True})
    assert out.direct_answer_kind == kind
    assert out.direct_answer == out.answer
    assert summary in out.direct_answer
    skill._bedrock.converse.assert_called_once()
    assert "direct_answer" not in out.model_dump()
    assert "direct_answer_kind" not in out.model_dump()
    assert "https://docs.google.com/spreadsheets/d/1/edit" in out.answer
    assert "507,700" in out.answer
    assert "施策の反応合計" in out.answer
    if kind == "comparison":
        assert out.answer.startswith("# 施策比較表")
        assert "結論:" in out.answer
        assert "| 食品 / ハンバーグ |" in out.answer
        assert "| 食品 / 別施策 |" in out.answer
        metrics = out.answer.split("📊", 1)[1]
        assert "507,700" in metrics and "300,000" in metrics
        assert "https://example.test/a" in metrics and "https://example.test/b" in metrics
    else:
        assert out.answer.startswith(summary)


def test_list_fast_is_an_explicit_direct_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    monkeypatch.setenv("SEARCH_FANOUT_LIST_FAST", "1")
    skill, _ = make([_vault_hit(source_uri="gdrive://A")])
    skill._summarize = MagicMock(side_effect=AssertionError("LIST_FASTは要約を省く"))
    out = run(skill, query="資料を一覧で", meta={**META, "_search_direct_answer": True})
    assert out.direct_answer_kind == "list_fast"
    assert out.direct_answer == out.answer
    assert "https://drive.google.com/file/d/A/view" in out.answer
    skill._summarize.assert_not_called()


@pytest.mark.parametrize("wording", ["どちらが効いた", "どっちが効いた", "効果を比べて"])
def test_comparison_request_never_takes_list_fast(
    monkeypatch: pytest.MonkeyPatch, wording: str
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    monkeypatch.setenv("SEARCH_FANOUT_LIST_FAST", "1")
    campaigns = [
        _video_campaign(i, "食品", f"施策{i}", [_video(f"https://example.test/{i}", "投稿", i, i)])
        for i in (1, 2)
    ]
    skill, _ = make(campaigns)
    skill._campaign_files = True
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    out = run(
        skill,
        query=f"食品の施策1と施策2を一覧にして、{wording}？",
        meta={**META, "_search_direct_answer": True},
    )
    assert out.direct_answer_kind == "comparison"
    assert out.answer.startswith("# 施策比較表")
    assert "結論:" in out.answer


def test_summary_failure_never_posts_campaign_evidence_as_an_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    campaign = _video_campaign(
        1, "食品", "ハンバーグ", [_video("https://example.test/a", "投稿A", 507700, 129)]
    )
    skill, _ = make([campaign])
    skill._campaign_files = True
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    skill._summarize = MagicMock(side_effect=TimeoutError("summary timeout"))
    out = run(
        skill,
        query="食品のハンバーグは今どうなっている？",
        meta={**META, "_search_direct_answer": True},
    )
    skill._summarize.assert_called_once()
    assert out.direct_answer is None and out.direct_answer_kind is None


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_empty_summary_never_posts_campaign_totals_as_an_answer(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    campaign = _video_campaign(
        1, "食品", "ハンバーグ", [_video("https://example.test/a", "投稿A", 507700, 129)]
    )
    skill, _ = make([campaign])
    skill._campaign_files = True
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    skill._bedrock.converse.return_value = _converse("")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = run(
        skill,
        query="食品のハンバーグは今どうなっている？",
        meta={**META, "_search_direct_answer": True},
    )
    skill._bedrock.converse.assert_called_once()
    assert out.direct_answer is None and out.direct_answer_kind is None


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_flag_off_preserves_existing_summary_and_has_no_delivery_marker(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    skill, _ = make([_vault_hit(source_uri="gdrive://A")])
    summary = MagicMock(return_value=("従来の要約。", 0.01))
    monkeypatch.setattr(skill, "_summarize", summary)
    out = run(skill)
    assert "従来の要約。" in out.answer
    assert out.direct_answer_kind is None and out.direct_answer is None
    assert summary.call_count == 1
