"""期間・名前読取の本番形応答とページ失敗を再現する（外部接続なし）。"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from teamagent.adapters import slack_user_reader as mod
from teamagent.adapters.slack_user_reader import SlackUserReader


def _reader(**methods: Any) -> SlackUserReader:
    return SlackUserReader("xoxp-me", client=MagicMock(**methods))


def _page(messages: list[Any], cursor: str = "", **extra: Any) -> dict[str, Any]:
    return {"ok": True, "messages": messages, "response_metadata": {"next_cursor": cursor}, **extra}


def _message(ts: str, text: str = "本文", **extra: Any) -> dict[str, Any]:
    return {"ts": ts, "user": "U1", "text": text, **extra}


def _search(matches: list[Any], total: int | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "messages": {"matches": matches, "total": len(matches) if total is None else total},
    }


def _match(name: str = "proj-01", cid: str = "C1", **flags: Any) -> dict[str, Any]:
    return {
        "ts": "12.0",
        "text": "抜粋",
        "channel": {"id": cid, "name": name, "is_private": False, "is_mpim": False, **flags},
    }


def test_history_pages_are_bounded_filtered_sorted_and_deduplicated() -> None:
    api = AsyncMock(
        side_effect=[
            _page([_message("19.0"), _message("15.0")], "next"),
            _page([_message("15.0"), _message("10.0"), _message("9.0"), _message("20.0")]),
        ]
    )
    out = _reader(conversations_history=api).read_period_checked(
        "C1", "r", oldest="10", latest="20"
    )
    assert [m.ts for m in out.messages] == ["10.0", "15.0", "19.0"]
    assert not out.error and not out.truncated
    assert api.await_args_list[1].kwargs["cursor"] == "next"
    assert all(
        c.kwargs["oldest"] == "10" and c.kwargs["latest"] == "20" for c in api.await_args_list
    )


def test_replies_pages_use_same_period_and_thread() -> None:
    api = AsyncMock(side_effect=[_page([_message("12.0")], "c"), _page([_message("13.0")])])
    out = _reader(conversations_replies=api).read_period_checked(
        "C1", "r", oldest="10", latest="20", thread_ts="1.0"
    )
    assert len(out.messages) == 2
    assert api.await_args.kwargs["ts"] == "1.0"
    assert api.await_args.kwargs["cursor"] == "c"


@pytest.mark.parametrize("mode", ["cap", "page_cap", "missing_cursor", "cycle"])
def test_incomplete_pages_never_claim_complete(mode: str) -> None:
    api = AsyncMock(
        return_value=_page(
            [_message("12.0")], "c" if mode != "missing_cursor" else "", has_more=True
        )
    )
    out = _reader(conversations_history=api).read_period_checked(
        "C1",
        "r",
        oldest="10",
        latest="20",
        max_messages=1 if mode == "cap" else 1000,
        max_pages=1 if mode == "page_cap" else 10,
    )
    assert out.truncated
    assert api.await_count <= 2


@pytest.mark.parametrize(
    "code", ["ratelimited", "missing_scope", "not_in_channel", "channel_not_found"]
)
@pytest.mark.parametrize("exception", [True, False])
def test_api_errors_do_not_become_empty_period(code: str, exception: bool) -> None:
    payload = {"ok": False, "error": code}
    api = (
        AsyncMock(side_effect=SlackApiError("failure", payload))
        if exception
        else AsyncMock(return_value=payload)
    )
    out = _reader(conversations_history=api).read_period_checked(
        "C1", "r", oldest="10", latest="20"
    )
    assert out.error == code and not out.messages


def test_second_page_failure_does_not_return_successful_partial_history() -> None:
    api = AsyncMock(side_effect=[_page([_message("12.0")], "c"), TimeoutError()])
    out = _reader(conversations_history=api).read_period_checked(
        "C1", "r", oldest="10", latest="20"
    )
    assert out.error and not out.messages


@pytest.mark.parametrize("raw", [None, {}, [None], [{"text": "ts無し"}], [{"ts": "oops"}]])
def test_bad_response_is_failure(raw: Any) -> None:
    out = _reader(conversations_history=AsyncMock(return_value=_page(raw))).read_period_checked(
        "C1", "r", oldest="10", latest="20"
    )
    assert out.error == "bad_response"


def test_deadline_prevents_more_api_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod.time, "monotonic", lambda: 2)
    api = AsyncMock()
    out = _reader(conversations_history=api).read_period_checked(
        "C1", "r", oldest="10", latest="20", deadline=1
    )
    assert out.truncated
    api.assert_not_awaited()


@pytest.mark.parametrize(
    "name", ["#proj-01", "＃ｐｒｏｊ－０１", "PROJ-01チャンネル", "proj-01のチャンネル"]
)
def test_names_normalize_without_list_or_extra_scopes(name: str) -> None:
    api = AsyncMock(return_value=_search([_match()]))
    reader = _reader(search_messages=api)
    out = reader.resolve_channel_checked(name, "r")
    assert out.channel_id == "C1" and out.is_public
    assert api.await_args.kwargs["query"] == "in:#proj-01"
    reader._client.conversations_list.assert_not_called()  # type: ignore[attr-defined]


def test_unique_partial_name_is_resolved_but_ambiguous_names_are_not() -> None:
    api = AsyncMock(side_effect=[_search([]), _search([_match("proj-01案件決定")])])
    assert (
        _reader(search_messages=api).resolve_channel_checked("案件決定のチャンネル", "r").channel_id
        == "C1"
    )
    api = AsyncMock(side_effect=[_search([]), _search([_match("案件-a"), _match("案件-b", "C2")])])
    assert (
        _reader(search_messages=api).resolve_channel_checked("案件", "r").error
        == "ambiguous_channel"
    )


def test_truncated_partial_candidates_are_not_guessed() -> None:
    api = AsyncMock(side_effect=[_search([]), _search([_match("案件-a")], total=120)])
    assert (
        _reader(search_messages=api).resolve_channel_checked("案件", "r").error
        == "ambiguous_channel"
    )


@pytest.mark.parametrize("name", ["x in:#secret", 'foo"', "", "#x\nfrom:me"])
def test_search_operator_injection_is_rejected(name: str) -> None:
    api = AsyncMock()
    assert _reader(search_messages=api).resolve_channel_checked(name, "r").error == "bad_target"
    api.assert_not_awaited()


def test_period_search_pages_and_permalink_thread_parent() -> None:
    one = {**_match(), "permalink": "https://workspace.slack.com/archives/C1/p120?thread_ts=1.0"}
    api = AsyncMock(
        side_effect=[_search([one], total=101), _search([{**_match(), "ts": "13.0"}], total=101)]
    )
    out = _reader(search_messages=api).search_period_checked("C1", "r", oldest="10", latest="20")
    assert len(out.matches) == 2 and out.matches[0].thread_ts == "1.0"
    assert api.await_args.kwargs["page"] == 2
    assert api.await_args.kwargs["query"].startswith("in:C1 after:")
