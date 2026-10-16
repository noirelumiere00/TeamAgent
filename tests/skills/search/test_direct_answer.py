"""確定回答の種類は本文の正規表現で推測せず、生成した経路から渡す。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.skills.search import fanout
from teamagent.skills.search.expand import ExpansionResult
from tests.skills.search.test_campaign_files import _video, _video_campaign
from tests.skills.search.test_fanout import META, _bedrock, _vault_hit, make, run


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
@pytest.mark.parametrize("kind", ["not_found", "comparison", "video_metrics"])
def test_direct_answer_uses_code_and_preserves_sources_without_summary(
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
    hits = [_vault_hit(title="食品の事例")] if kind == "not_found" else campaigns
    skill, _ = make(hits)
    monkeypatch.setattr(skill, "_attach_campaign_files", lambda *a: None)
    skill._campaign_files = True
    skill._summarize = MagicMock(return_value=("根拠のないモデルの追記。", 0.01))
    query = (
        "宅配の事例"
        if kind == "not_found"
        else "施策を比較して"
        if kind == "comparison"
        else "施策実績"
    )
    out = run(skill, query=query, meta={**META, "_search_direct_answer": True})
    assert out.direct_answer_kind == kind
    assert out.direct_answer == out.answer
    assert "根拠のないモデルの追記" not in out.answer
    skill._summarize.assert_not_called()
    assert "direct_answer" not in out.model_dump()
    assert "direct_answer_kind" not in out.model_dump()
    if kind == "not_found":
        assert "宅配の事例は見つかりませんでした" in out.answer
    else:
        assert "https://docs.google.com/spreadsheets/d/1/edit" in out.answer
        assert "507,700" in out.answer
        assert "施策の反応合計" in out.answer
        if kind == "comparison":
            assert "施策比較表" in out.answer


def test_list_fast_is_an_explicit_direct_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    monkeypatch.setenv("SEARCH_FANOUT_LIST_FAST", "1")
    skill, _ = make([_vault_hit(source_uri="gdrive://A")])
    out = run(skill, query="資料を一覧で", meta={**META, "_search_direct_answer": True})
    assert out.direct_answer_kind == "list_fast"
    assert out.direct_answer == out.answer
    assert "https://drive.google.com/file/d/A/view" in out.answer


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
