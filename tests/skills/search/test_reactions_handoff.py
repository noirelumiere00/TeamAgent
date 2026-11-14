"""search に選ばれたリアクション依頼も既存の本人読取・面ガードを通す。"""

from datetime import datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.search.schema import SearchInput, SearchOutput
from teamagent.skills.search.skill import SearchSkill
from teamagent.skills.slack_summary.period import JST, extract_period, resolve_period
from teamagent.skills.slack_summary.reactions import reaction_channel, reaction_terms
from tests.skills.slack_summary.test_reactions import UID_A, UID_B, _names, _post
from tests.skills.slack_summary.test_slack_summary import ORIGIN_CH, OTHER_CH, _build

PRODUCTION_REQUEST = """指定チャンネル内の指定文字列を含んだメッセージを走査し、絵文字スタンプでリアクションをしたユーザー（マルチチャンネルゲスト）を「ユニーク」で取得し、列挙してください
対象チャンネル：#team-newstvショート動画パートナー
メッセージに含む文字列：制作パートナー募集のご案内"""
_REORDERED_REQUEST = "\n".join(reversed(PRODUCTION_REQUEST.splitlines()))
_SEND_FOOTER = "\n*使用して送信されました* <@U0B3HLF45JB|Claude>"
PRODUCTION_REQUESTS = [
    PRODUCTION_REQUEST,
    " ".join(PRODUCTION_REQUEST.splitlines()),
    _REORDERED_REQUEST,
    " ".join(_REORDERED_REQUEST.splitlines()),
    " ".join((PRODUCTION_REQUEST + _SEND_FOOTER).splitlines()),
    " ".join((_REORDERED_REQUEST + _SEND_FOOTER).splitlines()),
]
CHANNEL_NAME = "team-newstvショート動画パートナー"
NOW = datetime(2026, 11, 13, 17, tzinfo=JST)


@pytest.fixture
def setup_handoff(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    monkeypatch.setenv("SLACK_SUMMARY_NAMED_PERIOD_ENABLED", "1")
    monkeypatch.setattr(
        "teamagent.skills.slack_summary.skill.resolve_period",
        lambda period: resolve_period(period, now=NOW),
    )
    september = resolve_period("2026-09-01", now=NOW)[0]
    _, factory, bed, store = _build(
        history_messages=[
            _post("制作パートナー募集のご案内", ts=september),
            _post("制作パートナー募集のご案内 再掲", ts=str(float(september) + 100)),
            _post(
                "ユニークな制作パートナーの連絡",
                ts=str(float(september) + 200),
                reactions=[{"name": "eyes", "users": [UID_B], "count": 1}],
            ),
            _post("制作パートナー募集のご案内", ts=resolve_period("2026-01-01", now=NOW)[0]),
        ]
    )
    _names(factory._client)

    async def search(**kw: Any) -> dict[str, Any]:
        matches = []
        if kw["query"].startswith("in:#"):
            matches = [
                {
                    "ts": september,
                    "text": "場所の解決用",
                    "user": UID_A,
                    "channel": {"id": ORIGIN_CH, "name": CHANNEL_NAME, "is_private": False},
                }
            ]
        return {"ok": True, "messages": {"matches": matches, "total": len(matches)}}

    factory._client.search_messages = AsyncMock(side_effect=search)
    pg = MagicMock()
    skill = SearchSkill(
        bedrock=cast(Any, bed), pgvector=pg, slack_store=store, slack_reader_factory=factory
    )
    pg.reset_mock()
    ctx = SkillContext(
        request_id="req-fiction",
        metadata={
            "user_email": "me@example.test",
            "channel_id": "D0DM",
            "request_text": PRODUCTION_REQUEST,
        },
    )
    return skill, factory, bed, pg, ctx, store


@pytest.mark.parametrize("request_text", PRODUCTION_REQUESTS)
def test_production_request_handoff_returns_unique_guests_from_90_days(
    request_text: str,
    setup_handoff: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill, factory, bed, pg, ctx, store = setup_handoff
    ctx.metadata["request_text"] = request_text
    log = MagicMock()
    monkeypatch.setattr("teamagent.skills.search.skill.logger", log)
    out = skill.run(SearchInput(query="モデルが作った短い検索語"), ctx)
    assert reaction_terms(PRODUCTION_REQUEST) == ["制作パートナー募集のご案内"]
    assert out.answer.startswith("対象期間: 直近90日（2026-08-15 00:00")
    assert "架空メンバーB（ゲスト） — 2 投稿" in out.answer
    assert out.answer.endswith("ゲスト 1 人") and "架空メンバーA" not in out.answer
    assert "別の連絡" not in out.answer and "制作パートナー募集のご案内" not in out.answer
    assert out.found and not out.error and out.answer_format == "list" and out.hits == []
    assert "指定の語を含む投稿には無かったので" not in out.answer
    assert not bed.calls and not pg.mock_calls
    assert store.asked == ["me@example.test"] and factory.tokens == ["xoxp-personal-token-of-me"]
    params = factory._client.conversations_history.await_args.kwargs
    assert (params["oldest"], params["latest"]) == resolve_period("直近90日", now=NOW)
    assert params["channel"] == ORIGIN_CH
    log.info.assert_called_once_with(
        "search_handoff_reactions", request_id="req-fiction", has_channel=True
    )


@pytest.mark.parametrize(
    "request_text",
    [
        "スタンプラリーの事例を一覧で",
        "リアクション動画の事例を一覧で見せて",
        "#xxx の要約して",
        "スタンプを押した人の一覧",
        "#xxx のスタンプラリーの事例を一覧で",
        "#xxx のリアクション動画の事例を一覧で見せて",
        "#1 のスタンプを押した人の一覧",
        "チャンネル：123 スタンプを押した人の一覧",
        "https://example.invalid/page#xxx スタンプを押した人の一覧",
        "<https://example.invalid/page#xxx|リンク> スタンプを押した人の一覧",
    ],
)
def test_ordinary_search_stays_on_search(
    request_text: str,
    setup_handoff: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill, factory, _, _, ctx, _ = setup_handoff
    ctx.metadata["request_text"] = request_text
    normal = MagicMock(return_value=SearchOutput(answer="通常の検索結果", total_cost_usd=0))
    monkeypatch.setattr(skill, "_run_once", normal)
    monkeypatch.setattr(skill, "_not_found_recovery", False)
    assert skill.run(SearchInput(query=request_text), ctx).answer == "通常の検索結果"
    normal.assert_called_once()
    assert not factory.tokens


@pytest.mark.parametrize(
    "reference,channel_id,channel_name",
    [
        ("#" + CHANNEL_NAME, "", CHANNEL_NAME),
        (f"<#{ORIGIN_CH}>", ORIGIN_CH, ""),
        (f"<#{ORIGIN_CH}|{CHANNEL_NAME}>", ORIGIN_CH, ""),
        ("チャンネル：" + CHANNEL_NAME, "", CHANNEL_NAME),
        ("チャンネル " + CHANNEL_NAME, "", CHANNEL_NAME),
    ],
)
def test_channel_forms_handoff(
    reference: str, channel_id: str, channel_name: str, setup_handoff: tuple[Any, ...]
) -> None:
    from teamagent.skills.slack_summary.skill import SlackSummarySkill

    skill, factory, _, _, ctx, _ = setup_handoff
    ctx.metadata["request_text"] = f"{reference}\nスタンプを押した人を重複なしで一覧"
    with patch.object(
        SlackSummarySkill, "run", autospec=True, side_effect=SlackSummarySkill.run
    ) as run:
        out = skill.run(SearchInput(query="募集"), ctx)
    summary_input = run.call_args.args[1]
    assert summary_input.channel_id == channel_id and summary_input.channel_name == channel_name
    lookups = [
        c.kwargs["query"]
        for c in factory._client.search_messages.await_args_list
        if c.kwargs["query"].startswith("in:#")
    ]
    assert lookups == ([f"in:#{CHANNEL_NAME}"] if channel_name else [])
    assert out.answer.endswith("ゲスト 1 人・社員 1 人") and not out.error


@pytest.mark.parametrize(
    "surface,reference",
    [
        (OTHER_CH, f"<#{ORIGIN_CH}>"),
        (OTHER_CH, "#" + CHANNEL_NAME),
    ],
)
def test_handoff_cannot_read_another_channel_on_channel_surface(
    surface: str,
    reference: str,
    setup_handoff: tuple[Any, ...],
) -> None:
    skill, factory, _, pg, ctx, _ = setup_handoff
    ctx.metadata.update(channel_id=surface, request_text=reference + " スタンプを押した人の一覧")
    out = skill.run(SearchInput(query="募集"), ctx)
    assert out.error == "cross_channel_blocked" and not out.found
    assert not factory._client.conversations_history.called and not pg.mock_calls
    assert not factory._client.users_info.called


def test_handoff_requires_user_email_before_any_read(setup_handoff: tuple[Any, ...]) -> None:
    skill, factory, _, pg, ctx, store = setup_handoff
    del ctx.metadata["user_email"]
    with pytest.raises(PermissionError, match="user_email"):
        skill.run(SearchInput(query="募集"), ctx)
    assert not factory.tokens and not store.asked and not pg.mock_calls


def test_handoff_preserves_unconnected_and_feature_gate(
    setup_handoff: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill, factory, _, _, ctx, store = setup_handoff
    monkeypatch.delenv("SLACK_SUMMARY_NAMED_PERIOD_ENABLED", raising=False)
    assert skill.run(SearchInput(query="募集"), ctx).error == "feature_disabled"
    assert not factory.tokens
    monkeypatch.setenv("SLACK_SUMMARY_NAMED_PERIOD_ENABLED", "1")
    store._tok = None
    assert skill.run(SearchInput(query="募集"), ctx).error == "not_connected"
    assert not factory.tokens


def test_production_factory_connects_search_with_only_summary_enabled(
    setup_handoff: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.orchestrator import factory

    skill, _, _, _, _, store = setup_handoff
    skill._slack_store = None
    monkeypatch.setattr(
        factory, "_envflag", lambda name, default="false": name == "USE_SLACK_SUMMARY_TOOL"
    )
    monkeypatch.setattr(factory, "_build_search_skill", lambda: skill)
    monkeypatch.setattr(factory, "_build_slack_store", lambda: store)
    specs = {spec.name: spec for spec in factory.build_production_tools()}
    from teamagent.skills.slack_summary.skill import SlackSummarySkill

    search_skill = specs["search"].instantiate()
    summary_skill = specs["slack_summary"].instantiate()
    assert isinstance(search_skill, SearchSkill) and search_skill._slack_store is store
    assert isinstance(summary_skill, SlackSummarySkill) and summary_skill._slack_store is store


@pytest.mark.parametrize(
    "period",
    [
        "先週",
        "直近7日",
        "直近2週間",
        "10/1〜10/5",
        "10月1日〜5日",
        "2026-09-01",
        "2026-09-01〜2026-09-30",
        "先月",
        "全期間",
    ],
)
def test_explicit_period_from_request_is_preserved(
    period: str,
    setup_handoff: tuple[Any, ...],
) -> None:
    skill, factory, _, _, ctx, _ = setup_handoff
    ctx.metadata["request_text"] = f"<#{ORIGIN_CH}> {period} スタンプを押した人の一覧"
    assert extract_period(ctx.metadata["request_text"]) == period
    out = skill.run(SearchInput(query="募集"), ctx)
    assert not out.error and out.answer.startswith(f"対象期間: {period}")
    params = factory._client.conversations_history.await_args.kwargs
    assert (params["oldest"], params["latest"]) == resolve_period(period, now=NOW)


def test_channel_word_without_name_does_not_invent_target() -> None:
    assert reaction_channel("指定チャンネル内のスタンプを押した人の一覧") == {}
