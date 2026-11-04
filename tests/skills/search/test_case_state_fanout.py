"""fanout の再作文を固定描画で止める。"""

from importlib import import_module

import pytest

from teamagent.skills.search import fanout
from teamagent.skills.search.expand import ExpansionResult
from tests.skills.search.test_case_state_evidence import row

fixtures = import_module("tests.skills.search.test_fanout")


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1")
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", fixtures.ME)
    monkeypatch.setenv("USE_COMPOSITE_SEARCH", "1")
    monkeypatch.setattr(fanout, "build_expansion_planner", lambda: None)
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), None, (), "empty", 1, 0.0)
    )
    monkeypatch.setattr(fanout, "build_summary_client", fixtures._bedrock)


@pytest.mark.parametrize("query", ["案件の現状", "◯◯の進捗は？", "◯◯は受注した？"])
def test_case_state_finalizer_rejects_model_claims_in_fanout_status_answers(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", fixtures.ME)
    skill, _ = fixtures.make([row(state="proposed")])
    monkeypatch.setattr(
        skill, "_summarize", lambda *a, **kw: ("受注の実績。2099-12-31完了。失注。", 0)
    )
    out = fixtures.run(skill, query=query)
    assert "2026-08-15 時点の正式状態: 提案" in out.answer
    assert "根拠日（確認日）: 2026-09-01" in out.answer
    assert "https://example.test/source#1" in out.answer
    assert not any(word in out.answer for word in ("受注", "実績", "完了", "失注", "2099-12-31"))
    assert out.case_state_answer == out.direct_answer == out.answer


@pytest.mark.parametrize(
    "query", ["提案書", "事例を一覧で", "JTB と HIS の過去の提案書や事例ってある？"]
)
def test_non_status_question_keeps_fanout_search_answer(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    monkeypatch.setenv("SEARCH_TIMELINE_STATUS", "1")
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", fixtures.ME)
    skill, _ = fixtures.make([row(state="proposed")])
    monkeypatch.setattr(skill, "_summarize", lambda *a, **kw: ("検索で見つかった提案資料です。", 0))
    out = fixtures.run(skill, query=query)
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "")
    baseline = fixtures.run(skill, query=query)
    assert out.answer == baseline.answer
    assert out.answer
    assert out.case_state_answer is None
    assert out.direct_answer_kind != "case_state"
