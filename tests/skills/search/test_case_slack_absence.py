"""不在の事例に無関係なSlack候補を添えない（#637の経路も固定する）。"""

from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout
from teamagent.skills.search.composite import SlackItem, SlackLookup
from teamagent.skills.search.expand import ExpansionResult
from teamagent.skills.search.schema import SearchInput, SlackHitOut
from tests.skills.search.test_fanout import META, _vault_hit, make


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", META["user_email"])
    monkeypatch.setenv("USE_COMPOSITE_SEARCH", "0")
    monkeypatch.setenv("SEARCH_ANSWER_SOURCE_LINKS", "1")
    monkeypatch.setattr(fanout, "build_expansion_planner", lambda: MagicMock(bedrock=MagicMock()))
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), None, (), "empty", 1, 0.0)
    )


def slack_item(text: str, name: str) -> SlackItem:
    return SlackItem(
        hit=SlackHitOut(
            channel="#sales",
            posted_on="2026-10-19",
            excerpt=text,
            permalink=f"https://example.test/slack/{name}",
        ),
        text=text,
    )


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("query", ["宅配の事例", "宅配事例ある？"])
@pytest.mark.parametrize("related", [False, True])
def test_absent_delivery_cases_never_publish_unrelated_slack_candidates(
    monkeypatch: pytest.MonkeyPatch, fanout_enabled: bool, query: str, related: bool
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", "1" if fanout_enabled else "0")
    hits = [_vault_hit(title="HARIASの実績", source_uri="https://example.test/harias")]
    skill, _ = make(hits)
    items = [
        slack_item("肩甲骨はがしの施策紹介", "scapula"),
        slack_item("HARIASの広告施策", "harias"),
    ]
    if related:
        items.append(slack_item("宅配サービスのショート動画事例", "delivery"))
    lookup = SlackLookup(status="ok", reason="ok", items=tuple(items))
    skill._composite = True
    monkeypatch.setattr(skill, "_start_slack_lookup", lambda *a: object())
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: lookup)
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: lookup)
    summary = MagicMock(return_value=("宅配サービスの事例があります。", 0.0))
    monkeypatch.setattr(skill, "_summarize", summary)
    out = skill.run(
        SearchInput(query=query),
        SkillContext(
            metadata={
                **META,
                "_search_direct_answer": True,
                "_search_direct_answer_kinds": ("not_found",),
            }
        ),
    )

    assert "HARIAS" not in out.model_dump_json()
    assert "scapula" not in out.model_dump_json()
    assert "harias" not in out.model_dump_json()
    assert "肩甲骨" not in out.answer
    assert bool(out.found) is related
    assert len(out.slack_hits) == int(related)
    if related:
        assert "https://example.test/slack/delivery" in out.answer
        if fanout_enabled:
            summary.assert_not_called()
        else:
            summary.assert_called_once()
    else:
        assert "事例は見つかりませんでした" in out.answer
        summary.assert_not_called()
