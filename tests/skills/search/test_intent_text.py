"""署名検証済みの元質問は意図に、短縮queryは検索語に使う。外部接続なし。"""

from unittest.mock import MagicMock

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search import fanout
from teamagent.skills.search.answer_mode import intent_text
from teamagent.skills.search.composite import SlackLookup
from teamagent.skills.search.schema import SearchInput
from tests.skills.search.test_case_list_composed import hit
from tests.skills.search.test_fanout import META, make
from tests.skills.search.test_fanout import setup as setup

REQUEST = "飲料系の事例５件出して"


@pytest.mark.parametrize("original", [None, "", "  ", [REQUEST]])
def test_missing_or_invalid_request_uses_query(original):
    assert (
        intent_text(SearchInput(query="飲料"), SkillContext(metadata={"request_text": original}))
        == "飲料"
    )


@pytest.mark.parametrize("fanout_enabled", [False, True])
def test_short_query_keeps_original_count_industry_and_material_links(monkeypatch, fanout_enabled):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    skill, _ = make([*(hit(i) for i in range(1, 8)), hit(99, industry="化粧品", score=1)])
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: SlackLookup("ok", "ok"))
    embed = MagicMock(wraps=skill._embed)
    retrieve = MagicMock(wraps=skill._retrieve)
    summary = MagicMock(return_value=("{}", 0.0))
    monkeypatch.setattr(skill, "_embed", embed)
    monkeypatch.setattr(skill, "_retrieve", retrieve)
    monkeypatch.setattr(skill, "_summarize", summary)
    out = skill.run(
        SearchInput(query="飲料", top_k=5),
        SkillContext(
            metadata={
                **META,
                "request_text": REQUEST,
            }
        ),
    )
    assert out.answer_format == "list" and out.found
    assert out.answer.count("|資料を見る>") == 5
    assert "FILE99" not in out.answer and "見つかったのは" not in out.answer
    assert "？" not in out.answer and out.suggested_next is None
    embed.assert_called_once_with("飲料")
    assert retrieve.call_args.args[1].query == "飲料"
    assert retrieve.call_args.args[1].filter_industry == "飲料"
    assert retrieve.call_args.args[1].top_k >= 5
    summary.assert_called_once()
    assert summary.call_args.args[0] == REQUEST
    assert summary.call_args.kwargs["case_points"] is True
    assert len(summary.call_args.args[1]) == 5


@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("channel", ["D0FAKE", "C0FAKE"])
def test_absent_request_keeps_legacy_summary_even_on_channel(monkeypatch, fanout_enabled, channel):
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    skill, _ = make([hit(1)])
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: SlackLookup("ok", "ok"))
    summary = MagicMock(return_value=("従来の要約です。", 0.0))
    monkeypatch.setattr(skill, "_summarize", summary)
    out = skill.run(
        SearchInput(query="飲料"), SkillContext(metadata={**META, "channel_id": channel})
    )
    assert out.answer_format is None
    assert "従来の要約です。" in out.answer
    assert summary.call_args.args[0] == "飲料"
    assert not summary.call_args.kwargs.get("case_points")


@pytest.mark.parametrize("filter_client", [None, "架空別社"])
def test_request_client_is_used_in_retrieval_conditions(monkeypatch, filter_client):
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    skill, pg = make([hit(1, client_name="架空飲料社")])
    pg.list_client_names.return_value = ["架空飲料社", "架空別社"]
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: SlackLookup("ok", "ok"))
    monkeypatch.setattr(skill, "_summarize", lambda *a, **kw: ("{}", 0.0))
    pool = MagicMock(wraps=skill._pool_search)
    monkeypatch.setattr(skill, "_pool_search", pool)
    out = skill.run(
        SearchInput(query="飲料", filter_client=filter_client),
        SkillContext(
            metadata={
                **META,
                "request_text": "架空飲料社の事例を5件出して",
            }
        ),
    )
    assert out.found
    assert any(
        call.kwargs.get("metadata_contains", {}).get("__client__") == "架空飲料社"
        for call in pool.call_args_list
        if call.kwargs.get("metadata_contains")
    )


def test_summary_receives_original_numeric_question(monkeypatch):
    monkeypatch.setenv("USE_SEARCH_FANOUT", "0")
    skill, _ = make([hit(1)])
    monkeypatch.setattr(skill, "_collect_slack_lookup", lambda *a: SlackLookup("ok", "ok"))
    summary = MagicMock(return_value=("架空の集計結果です。", 0.0))
    monkeypatch.setattr(skill, "_summarize", summary)
    request = "飲料の投稿の再生数合計はいくつ？"
    skill.run(SearchInput(query="飲料"), SkillContext(metadata={**META, "request_text": request}))
    assert summary.call_args.args[0] == request
