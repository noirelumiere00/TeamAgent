"""短文化でもSlackの根拠・弱い一致・非公開の制約を失わない。"""

import pytest

from teamagent.skills.search import fanout
from teamagent.skills.search.composite import SlackLookup
from teamagent.skills.search.result_guard import WEAK_RESULT_NOTICE
from tests.skills.search.test_case_slack_absence import slack_item
from tests.skills.search.test_fanout import META, _converse, _vault_hit, make, run
from tests.skills.search.test_fanout import setup as setup


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_compact_slack_only_answer_has_cited_body_without_inventory(monkeypatch, fanout_enabled):
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    skill, _ = make([])
    skill._alias_expansion = False
    skill._composite = True
    lookup = SlackLookup(
        status="ok", reason="ok", items=(slack_item("連携はアプリから開始できます。", "connect"),)
    )
    monkeypatch.setattr(skill, "_start_slack_lookup", lambda *a: object())
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: lookup)
    monkeypatch.setattr(fanout, "lookup_slack", lambda *a, **kw: lookup)
    skill._bedrock.converse.return_value = _converse("連携はアプリから開始できます。[S:slack0]")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = run(
        skill, query="Slack連携の始め方を教えて", meta={**META, "_search_compact_answer": True}
    )
    assert "連携はアプリから開始できます。" in out.answer
    assert "出典:" in out.answer
    assert "https://example.test/slack/connect" in out.answer
    assert "見つかりません" not in out.answer
    assert "💬" not in out.answer and "探した範囲" not in out.answer
    assert "[S:" not in out.answer


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_compact_keeps_code_generated_low_relevance_warning(monkeypatch, fanout_enabled):
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    skill, _ = make([_vault_hit(1, title="報告書", source_uri="gdrive://REPORT")])
    skill._weak_result_threshold = 0.99
    skill._result_guard = True
    skill._bedrock.converse.return_value = _converse("参考資料はあります。[S:1]")
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    out = run(skill, query="状況は？", meta={**META, "_search_compact_answer": True})
    assert WEAK_RESULT_NOTICE in out.answer
    assert "https://drive.google.com/file/d/REPORT/view" in out.answer
    assert len(out.answer) <= 2500
