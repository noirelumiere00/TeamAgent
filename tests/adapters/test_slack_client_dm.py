"""slack_client の DM ヘルパー（lookup_user_id_by_email / open_dm）のテスト。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from teamagent.adapters.slack_client import SlackClient


def _client() -> AsyncMock:
    return AsyncMock()


def test_lookup_user_id_by_email_ok() -> None:
    c = _client()
    c.users_lookupByEmail.return_value = {"ok": True, "user": {"id": "U123"}}
    slack = SlackClient(bot_token="x", client=c)
    assert asyncio.run(slack.lookup_user_id_by_email("a@b.jp", "r")) == "U123"
    c.users_lookupByEmail.assert_awaited_once_with(email="a@b.jp")


def test_lookup_user_id_by_email_missing_returns_none() -> None:
    c = _client()
    c.users_lookupByEmail.return_value = {"ok": True, "user": {}}
    slack = SlackClient(bot_token="x", client=c)
    assert asyncio.run(slack.lookup_user_id_by_email("a@b.jp", "r")) is None


def test_lookup_user_id_by_email_error_returns_none() -> None:
    c = _client()
    c.users_lookupByEmail.side_effect = RuntimeError("users_not_found")
    slack = SlackClient(bot_token="x", client=c)
    assert asyncio.run(slack.lookup_user_id_by_email("a@b.jp", "r")) is None


def test_open_dm_ok() -> None:
    c = _client()
    c.conversations_open.return_value = {"ok": True, "channel": {"id": "D999"}}
    slack = SlackClient(bot_token="x", client=c)
    assert asyncio.run(slack.open_dm("U123", "r")) == "D999"
    c.conversations_open.assert_awaited_once_with(users="U123")


def test_open_dm_error_returns_none() -> None:
    c = _client()
    c.conversations_open.side_effect = RuntimeError("cannot_dm_bot")
    slack = SlackClient(bot_token="x", client=c)
    assert asyncio.run(slack.open_dm("U123", "r")) is None
