"""slack_search でも原文から本人読取のリアクション一覧へ受け渡す。"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.skills.search.skill import SearchSkill
from teamagent.skills.slack_search.schema import SlackSearchInput
from teamagent.skills.slack_search.skill import SlackSearchSkill
from teamagent.skills.slack_summary.period import resolve_period
from teamagent.skills.slack_summary.schema import SlackSummaryOutput
from tests.skills.search.test_reactions_handoff import NOW, PRODUCTION_REQUESTS
from tests.skills.search.test_reactions_handoff import setup_handoff as setup_handoff
from tests.skills.slack_summary.test_slack_summary import ORIGIN_CH, OTHER_CH


@pytest.mark.parametrize("request_text", PRODUCTION_REQUESTS)
def test_production_request_handoff_filters_unique_guests(
    request_text: str,
    setup_handoff: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, factory, bed, pg, ctx, store = setup_handoff
    ctx.metadata["request_text"] = request_text
    skill = SlackSearchSkill(slack_store=store, reader_factory=factory, bedrock=bed, pgvector=pg)
    log = MagicMock()
    monkeypatch.setattr("teamagent.skills.slack_search.skill.logger", log)
    out = skill.run(SlackSearchInput(query="モデルが作った短い検索語", focus="別の語"), ctx)
    assert out.message.startswith("対象期間: 直近90日（2026-08-15 00:00")
    assert "架空メンバーB（ゲスト） — 2 投稿" in out.message
    assert out.message.endswith("ゲスト 1 人") and "架空メンバーA" not in out.message
    assert "指定の語を含む投稿には無かったので" not in out.message
    assert out.match_count == 2 and not out.error and not out.truncated
    assert out.matches == [] and out.summary == "" and out.total_hits == out.hidden_count == 0
    assert not bed.calls and not pg.mock_calls
    assert store.asked == ["me@example.test"] and factory.tokens == ["xoxp-personal-token-of-me"]
    params = factory._client.conversations_history.await_args.kwargs
    assert (params["oldest"], params["latest"]) == resolve_period("直近90日", now=NOW)
    assert params["channel"] == ORIGIN_CH
    log.info.assert_called_once_with(
        "slack_search_handoff_reactions", request_id="req-fiction", has_channel=True
    )


@pytest.mark.parametrize(
    "request_text",
    [
        "#xxx で〇〇の話を探して",
        "スタンプラリーの投稿を探して",
        "#xxx のスタンプラリーの投稿を一覧で探して",
        "#xxx のリアクション動画を一覧で探して",
        "スタンプを押した人の一覧",
    ],
)
def test_ordinary_slack_search_does_not_handoff(
    request_text: str, setup_handoff: tuple[Any, ...]
) -> None:
    _, factory, bed, pg, ctx, store = setup_handoff
    ctx.metadata["request_text"] = request_text
    skill = SlackSearchSkill(slack_store=store, reader_factory=factory, bedrock=bed, pgvector=pg)
    out = skill.run(SlackSearchInput(query="通常の検索語"), ctx)
    assert not out.error
    factory._client.search_messages.assert_awaited_once()
    assert factory._client.search_messages.await_args.kwargs["query"].startswith("通常の検索語")
    assert not factory._client.conversations_history.called and not bed.calls


def test_handoff_preserves_surface_guard(setup_handoff: tuple[Any, ...]) -> None:
    _, factory, _, pg, ctx, store = setup_handoff
    ctx.metadata.update(
        channel_id=OTHER_CH, request_text=f"<#{ORIGIN_CH}> スタンプを押した人の一覧"
    )
    out = SlackSearchSkill(slack_store=store, reader_factory=factory).run(
        SlackSearchInput(query="募集"), ctx
    )
    assert out.error == "cross_channel_blocked" and out.match_count == 0
    assert not factory.tokens and not pg.mock_calls
    assert not factory._client.conversations_history.called


def test_handoff_requires_user_email_before_any_read(setup_handoff: tuple[Any, ...]) -> None:
    _, factory, _, _, ctx, store = setup_handoff
    del ctx.metadata["user_email"]
    with pytest.raises(PermissionError, match="user_email"):
        SlackSearchSkill(slack_store=store, reader_factory=factory).run(
            SlackSearchInput(query="募集"), ctx
        )
    assert not factory.tokens and not store.asked


def test_handoff_preserves_feature_gate_and_unconnected(
    setup_handoff: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory, _, _, ctx, store = setup_handoff
    skill = SlackSearchSkill(slack_store=store, reader_factory=factory)
    monkeypatch.delenv("SLACK_SUMMARY_NAMED_PERIOD_ENABLED", raising=False)
    assert skill.run(SlackSearchInput(query="募集"), ctx).error == "feature_disabled"
    monkeypatch.setenv("SLACK_SUMMARY_NAMED_PERIOD_ENABLED", "1")
    store._tok = None
    assert skill.run(SlackSearchInput(query="募集"), ctx).error == "not_connected"
    assert not factory.tokens


def test_handoff_copies_partial_output(
    setup_handoff: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, factory, _, _, ctx, store = setup_handoff
    monkeypatch.setattr(
        "teamagent.skills.slack_summary.skill.SlackSummarySkill.run",
        lambda self, input, ctx: SlackSummaryOutput(
            message="取得できた一部", message_count=2, truncated=True, total_cost_usd=0.01
        ),
    )
    out = SlackSearchSkill(slack_store=store, reader_factory=factory).run(
        SlackSearchInput(query="募集"), ctx
    )
    assert out.message == "取得できた一部" and out.match_count == 2
    assert out.truncated and out.total_cost_usd == 0.01 and not out.error
    assert not factory.tokens


def test_production_factory_connects_slack_search_with_search_enabled(
    setup_handoff: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    from teamagent.orchestrator import factory

    skill, _, _, _, _, store = setup_handoff
    monkeypatch.setattr(
        factory, "_envflag", lambda name, default="false": name == "USE_SLACK_SEARCH_TOOL"
    )
    monkeypatch.setattr(factory, "_build_search_skill", lambda: skill)
    monkeypatch.setattr(factory, "_build_slack_store", lambda: store)
    specs = {spec.name: spec for spec in factory.build_production_tools()}
    search_skill = specs["search"].instantiate()
    slack_search_skill = specs["slack_search"].instantiate()
    assert isinstance(search_skill, SearchSkill) and search_skill._slack_store is store
    assert (
        isinstance(slack_search_skill, SlackSearchSkill)
        and slack_search_skill._slack_store is store
    )
