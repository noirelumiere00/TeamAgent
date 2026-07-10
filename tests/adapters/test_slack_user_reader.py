"""SlackUserReader の単体テスト（実 Slack を叩かない・AsyncMock 注入）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from teamagent.adapters.slack_user_reader import SlackUserReader, _run_sync


def _client(**methods: AsyncMock) -> MagicMock:
    c = MagicMock()
    for name, mock in methods.items():
        setattr(c, name, mock)
    return c


def test_empty_xoxp_raises() -> None:
    with pytest.raises(ValueError, match="xoxp"):
        SlackUserReader("")
    with pytest.raises(ValueError):
        SlackUserReader.from_user_token("   ")


def test_read_thread_maps_messages() -> None:
    resp = {
        "messages": [
            {"ts": "1.1", "user": "U1", "text": "親", "thread_ts": "1.1", "reply_count": 2},
            {"ts": "1.2", "user": "U2", "text": "返信"},
        ]
    }
    client = _client(conversations_replies=AsyncMock(return_value=resp))
    reader = SlackUserReader("xoxp-x", client=client)
    msgs = reader.read_thread("C1", "1.1", "req")
    assert [m.text for m in msgs] == ["親", "返信"]
    assert msgs[0].user == "U1"
    client.conversations_replies.assert_awaited_once()


def test_read_thread_empty_args_no_call() -> None:
    client = _client(conversations_replies=AsyncMock(return_value={"messages": []}))
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.read_thread("", "1.1", "req") == []
    assert reader.read_thread("C1", "", "req") == []
    client.conversations_replies.assert_not_awaited()


def test_read_thread_failopen_on_exception() -> None:
    client = _client(conversations_replies=AsyncMock(side_effect=RuntimeError("boom")))
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.read_thread("C1", "1.1", "req") == []  # 例外は握って空返し


def test_search_maps_matches() -> None:
    resp = {
        "messages": {
            "matches": [
                {
                    "ts": "9.9",
                    "text": "○○社の件",
                    "channel": {"id": "C9", "name": "proj-oo"},
                    "user": "U3",
                    "permalink": "https://x/p",
                },
                {"text": "channel 欠損でも落ちない"},
            ]
        }
    }
    client = _client(search_messages=AsyncMock(return_value=resp))
    reader = SlackUserReader("xoxp-x", client=client)
    hits = reader.search("○○社", "req")
    assert hits[0].channel_id == "C9"
    assert hits[0].channel_name == "proj-oo"
    assert hits[0].text == "○○社の件"
    assert hits[1].channel_id == ""  # 欠損は空文字にフォールバック


def test_search_empty_query_no_call() -> None:
    client = _client(search_messages=AsyncMock(return_value={"messages": {"matches": []}}))
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.search("  ", "req") == []
    client.search_messages.assert_not_awaited()


def test_search_failopen_on_exception() -> None:
    client = _client(search_messages=AsyncMock(side_effect=RuntimeError("429")))
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.search("q", "req") == []


def test_run_sync_without_running_loop() -> None:
    async def _coro() -> int:
        return 42

    assert _run_sync(lambda: _coro()) == 42


@pytest.mark.asyncio
async def test_read_thread_works_inside_running_loop() -> None:
    # 実行中ループがあっても _run_sync が別スレッドへ退避して同期呼び出しできる。
    resp = {"messages": [{"ts": "1.1", "user": "U1", "text": "hi"}]}
    client = _client(conversations_replies=AsyncMock(return_value=resp))
    reader = SlackUserReader("xoxp-x", client=client)
    msgs = reader.read_thread("C1", "1.1", "req")
    assert [m.text for m in msgs] == ["hi"]
