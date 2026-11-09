"""架空の投稿のみで一覧取得・期間・本人・返信・面ガードを通す。"""

from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from teamagent.adapters.slack_user_reader import SlackUserReader
from teamagent.skills.base import SkillContext
from teamagent.skills.slack_search.listing import list_query, wants_list
from teamagent.skills.slack_search.schema import SlackSearchInput
from teamagent.skills.slack_search.skill import SlackSearchSkill, reset_name_cache
from teamagent.skills.slack_summary.period import JST, resolve_period
from teamagent.skills.slack_summary.schema import SlackSummaryInput
from teamagent.skills.slack_summary.skill import SlackSummarySkill


def _ts(day: str) -> str:
    return f"{datetime.fromisoformat(day).replace(tzinfo=JST).timestamp():.6f}"


def _match(ts: str, **extra: Any) -> dict[str, Any]:
    return {
        "ts": ts,
        "user": "UFAKE001",
        "text": "架空の定期投稿 https://example.invalid/report",
        "permalink": f"https://example.slack.com/archives/CFAKE001/p{ts.replace('.', '')}",
        "channel": {"id": "CFAKE001", "name": "fiction", "is_private": False, "is_mpim": False},
        **extra,
    }


def _setup(
    rows: list[dict[str, Any]], request: str = "", origin: str = "DFAKE001"
) -> tuple[Any, ...]:
    reset_name_cache()
    client = MagicMock()

    async def search(**params: Any) -> dict[str, Any]:
        selected = rows
        if "from:UFAKE001" in params["query"]:
            selected = [m for m in rows if m["user"] == "UFAKE001"]
        start = (params.get("page", 1) - 1) * params["count"]
        return {
            "ok": True,
            "messages": {
                "matches": selected[start : start + params["count"]],
                "total": len(selected),
            },
        }

    client.search_messages = AsyncMock(side_effect=search)
    client.users_info = AsyncMock(
        side_effect=lambda user: {
            "ok": True,
            "user": {
                "profile": {
                    "display_name": {
                        "UFAKE001": "架空太郎",
                        "UFAKE002": "架空花子",
                        "UFAKE003": "架空次郎",
                    }.get(user, "架空ユーザー")
                }
            },
        }
    )
    reader = SlackUserReader("xoxp-fiction", client=client)
    store = MagicMock()
    store.get.return_value = SimpleNamespace(access_token="xoxp-fiction")
    bedrock = MagicMock()
    ctx = SkillContext(
        metadata={
            "user_email": "fiction@example.invalid",
            "channel_id": origin,
            "verified_slack_user_id": "UFAKE001",
            "request_text": request,
        }
    )
    return client, reader, store, bedrock, ctx


def _search(
    rows: list[dict[str, Any]], request: str, query: str = "in:#fiction"
) -> tuple[Any, Any, Any]:
    client, reader, store, bedrock, ctx = _setup(rows, request)
    skill = SlackSearchSkill(store, reader_factory=lambda token: reader, bedrock=bedrock)
    out = skill.run(SlackSearchInput(query=query, focus="一覧"), ctx)
    return out, client, bedrock


def test_slack_month_list_pages_and_exact_jst_boundaries() -> None:
    start, end = resolve_period("2026年10月")
    rows = (
        [_match(f"{float(end):.6f}")]
        + [_match(f"{float(start) + i:.6f}") for i in range(101)]
        + [_match(f"{float(start) - 0.000001:.6f}")]
    )
    out, api, llm = _search(rows, "2026年10月の投稿を全部一覧")
    assert out.error == "" and out.match_count == out.total_hits == 101
    assert api.search_messages.await_count == 2
    query = api.search_messages.await_args.kwargs["query"]
    assert "in:#fiction" in query and "after:2026-09-30" in query and "before:2026-11-02" in query
    assert "2026-10-01 00:00" in out.message and "2026-11-01 00:00 未満" in out.message
    assert (
        len([line for line in out.message.splitlines() if "https://example.slack.com" in line])
        == 101
    )
    llm.converse.assert_not_called()


@pytest.mark.parametrize("month", [False, True])
def test_slack_list_cap_reports_exact_remaining_count(month: bool) -> None:
    start = float(_ts("2026-10-01"))
    rows = [_match(f"{start + i:.6f}") for i in range(503)]
    if month:
        rows += [_match(_ts("2026-09-30")), _match(_ts("2026-11-01"))]
    out, api, _ = _search(rows, ("2026年10月の" if month else "") + "投稿を全部一覧")
    assert out.match_count == 500 and out.total_hits == 503
    assert "ほか 3 件" in out.message
    assert api.search_messages.await_count == (6 if month else 5)


def test_slack_self_uses_verified_id_and_keeps_topic() -> None:
    out, api, _ = _search(
        [
            _match(_ts("2026-10-01")),
            _match(_ts("2026-10-02"), user="UFAKE002"),
        ],
        "自分が毎週投稿している定期投稿のURLがほしい",
        query="定期投稿 from:UFAKE002",
    )
    query = api.search_messages.await_args.kwargs["query"]
    assert "from:UFAKE001" in query and "from:UFAKE002" not in query
    assert "定期投稿" in query and "has:link" in query
    assert out.match_count == 1 and "検索期間:" in out.message


def test_slack_applicants_read_all_reply_pages_without_mistaking_root_for_reply() -> None:
    root = _ts("2026-10-01")
    client, reader, store, llm, ctx = _setup([_match(root)], "募集された案件で誰が応募したか")
    client.conversations_replies = AsyncMock(
        side_effect=[
            {
                "ok": True,
                "messages": [
                    {"ts": root, "user": "UFAKE001", "text": "募集します"},
                    {"ts": f"{float(root) + 1:.6f}", "user": "UFAKE002", "text": "応募します"},
                ],
                "has_more": True,
                "response_metadata": {"next_cursor": "next"},
            },
            {
                "ok": True,
                "messages": [
                    {
                        "ts": f"{float(root) + 2:.6f}",
                        "user": "UFAKE003",
                        "text": "今回は応募しません",
                    },
                ],
            },
        ]
    )
    out = SlackSearchSkill(store, reader_factory=lambda token: reader, bedrock=llm).run(
        SlackSearchInput(query="in:#fiction 募集"),
        ctx,
    )
    assert "返信者: 架空花子: 応募します、架空次郎: 今回は応募しません" in out.message
    assert "返信者: 架空太郎" not in out.message
    assert client.conversations_replies.await_count == 2
    assert client.conversations_replies.await_args.kwargs["cursor"] == "next"
    llm.converse.assert_not_called()


def test_slack_summary_lists_all_history_and_retains_cross_channel_guard() -> None:
    rows = [_match(_ts("2018-01-01")), _match(_ts("2026-10-01"))]
    client, reader, store, llm, ctx = _setup(rows, "過去まで遡って全部一覧")
    skill = SlackSummarySkill(store, reader_factory=lambda token: reader, bedrock=llm)
    out = skill.run(SlackSummaryInput(channel_id="CFAKE001", period="過去全部"), ctx)
    assert out.message_count == 2 and not out.error
    assert "過去すべて" in out.message and "2018-01-01" in out.message
    assert "after:" not in client.search_messages.await_args.kwargs["query"]
    llm.converse.assert_not_called()
    client.search_messages.reset_mock()
    ctx.metadata["channel_id"] = "COTHER01"
    out = skill.run(SlackSummaryInput(channel_id="CFAKE001", focus="全部一覧"), ctx)
    assert out.error == "cross_channel_blocked"
    client.search_messages.assert_not_called()


def test_slack_list_channel_hides_private_bodies_and_reply_reads() -> None:
    row = _match(_ts("2026-10-01"))
    row["channel"]["is_private"] = True
    client, reader, store, llm, ctx = _setup([row], "誰が応募したか一覧", "CORIGIN1")
    out = SlackSearchSkill(store, reader_factory=lambda token: reader, bedrock=llm).run(
        SlackSearchInput(query="募集"),
        ctx,
    )
    assert not out.matches and out.hidden_count == 1 and out.total_hits == 0
    assert "架空の定期投稿" not in out.message and "fiction" not in out.message
    client.conversations_replies.assert_not_called()


def test_slack_second_page_failure_is_not_a_successful_partial_list() -> None:
    client, reader, store, llm, ctx = _setup([], "全部一覧")
    client.search_messages = AsyncMock(
        side_effect=[
            {"ok": True, "messages": {"matches": [_match(_ts("2026-10-01"))], "total": 101}},
            {"ok": False, "error": "ratelimited"},
        ]
    )
    out = SlackSearchSkill(store, reader_factory=lambda token: reader, bedrock=llm).run(
        SlackSearchInput(query="in:#fiction"),
        ctx,
    )
    assert out.error == "search_failed" and not out.matches
    assert "見つかりません" not in out.message


@pytest.mark.parametrize(
    "text", ["全部", "一覧", "過去まで遡って", "誰が応募したか", "URLがほしい"]
)
def test_slack_list_classification(text: str) -> None:
    assert wants_list(text)


def test_slack_summary_only_request_still_uses_summary() -> None:
    assert not wants_list("今週の議論を要約して")
    client, reader, store, llm, ctx = _setup([], "今週の議論を要約して")
    client.conversations_replies = AsyncMock(
        return_value={
            "ok": True,
            "messages": [
                {"ts": _ts("2026-10-01"), "user": "UFAKE001", "text": "架空の議論"},
            ],
        }
    )
    llm.converse.return_value = SimpleNamespace(
        text="架空の要約", usage=SimpleNamespace(cost_usd=0)
    )
    out = SlackSummarySkill(store, reader_factory=lambda token: reader, bedrock=llm).run(
        SlackSummaryInput(channel_id="CFAKE001", thread_ts=_ts("2026-10-01"), scope="thread"),
        ctx,
    )
    assert "架空の要約" in out.message
    llm.converse.assert_called_once()
    client.search_messages.assert_not_called()


def test_slack_natural_query_and_invalid_self_id() -> None:
    query, bounds = list_query("2026年10月の投稿を全部一覧", "2026年10月の投稿を全部一覧")
    assert re.fullmatch(r"after:2026-09-30 before:2026-11-02", query)
    assert bounds == resolve_period("2026年10月")
    with pytest.raises(ValueError, match="Slack ID"):
        list_query("定期投稿", "自分が投稿したURL")


def test_slack_modifiers_with_list_words_and_explicit_dates_are_preserved() -> None:
    query, bounds = list_query(
        "in:#一覧 from:UFAKE002 after:2026-09-30 before:2026-11-01",
        "全部一覧",
        user_id="UFAKE001",
    )
    assert bounds is None
    assert "in:#一覧" in query and "from:UFAKE002" in query
    assert "after:2026-09-30" in query and "before:2026-11-01" in query
    query, _ = list_query(
        "自分が毎週投稿している定期投稿のURLがほしい", "自分が投稿したURL", user_id="UFAKE001"
    )
    assert query == "定期投稿 from:UFAKE001 has:link"


def test_slack_report_url_beyond_excerpt_is_preserved() -> None:
    link = "https://example.invalid/late-report"
    out, _, _ = _search(
        [_match(_ts("2026-10-01"), text="架空の説明" * 100 + f" <{link}|架空のレポート>")],
        "レポートのリンクを全部",
    )
    assert link in out.message


def test_slack_summary_month_list_does_not_use_history_or_bedrock() -> None:
    start, _ = resolve_period("2026年10月")
    rows = [_match(f"{float(start) + i:.6f}") for i in range(101)]
    client, reader, store, llm, ctx = _setup(rows, "10月の投稿を全部一覧")
    out = SlackSummarySkill(store, reader_factory=lambda token: reader, bedrock=llm).run(
        SlackSummaryInput(channel_id="CFAKE001", period="2026年10月"),
        ctx,
    )
    assert out.message_count == 101 and not out.truncated and not out.error
    assert "2026-10-01 00:00" in out.message and "2026-11-01 00:00 未満" in out.message
    assert client.search_messages.await_count == 2
    client.conversations_history.assert_not_called()
    llm.converse.assert_not_called()


def test_slack_missing_matches_cannot_claim_exhaustive() -> None:
    client, reader, store, llm, ctx = _setup([_match(_ts("2026-10-01"))], "全部一覧")
    client.search_messages = AsyncMock(
        return_value={
            "ok": True,
            "messages": {"matches": [_match(_ts("2026-10-01"))], "total": 10},
        }
    )
    out = SlackSearchSkill(store, reader_factory=lambda token: reader, bedrock=llm).run(
        SlackSearchInput(query="in:#fiction"),
        ctx,
    )
    assert out.error == "search_failed" and not out.matches


def test_slack_all_history_over_api_page_cap_still_reports_remaining_count() -> None:
    client, reader, store, llm, ctx = _setup(
        [_match(f"{float(_ts('2020-01-01')) + i:.6f}") for i in range(10_001)],
        "過去全部を一覧",
    )
    out = SlackSummarySkill(store, reader_factory=lambda token: reader, bedrock=llm).run(
        SlackSummaryInput(channel_id="CFAKE001", period="過去全部"),
        ctx,
    )
    assert out.message_count == 500 and out.truncated and not out.error
    assert "ほか 9501 件" in out.message
    assert client.search_messages.await_count == 5
