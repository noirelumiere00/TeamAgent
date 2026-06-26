"""SlackClient のユニットテスト。

AsyncWebClient をモックして post_message が正しい kwargs を組み立てるか検証。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from teamagent.adapters.slack_client import SlackClient, SlackPostResult


@pytest.fixture
def fake_web_client() -> AsyncMock:
    """AsyncWebClient をモックして、ok=True と固定 ts を返す。"""
    mock = AsyncMock()
    mock.chat_postMessage.return_value = {"ok": True, "ts": "1716000000.000100"}
    mock.chat_update.return_value = {"ok": True, "ts": "1716000000.000100"}
    mock.users_profile_get.return_value = {
        "profile": {
            "real_name": "Shogo Komata",
            "email": "s-komata@vectorinc.co.jp",
        }
    }
    return mock


async def test_post_message_basic(fake_web_client: AsyncMock) -> None:
    """post_message が channel/text を WebClient に正しく渡すこと。"""
    client = SlackClient(bot_token="xoxb-test", client=fake_web_client)
    result = await client.post_message(
        channel="C123",
        text="hello",
        request_id="req-1",
    )

    assert isinstance(result, SlackPostResult)
    assert result.ok is True
    assert result.ts == "1716000000.000100"
    assert result.channel == "C123"

    call_kwargs: dict[str, Any] = fake_web_client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "C123"
    assert call_kwargs["text"] == "hello"
    assert "thread_ts" not in call_kwargs
    assert "blocks" not in call_kwargs


async def test_post_message_with_thread_and_blocks(fake_web_client: AsyncMock) -> None:
    """thread_ts と blocks が指定されたら WebClient にそのまま渡る。"""
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
    client = SlackClient(bot_token="xoxb-test", client=fake_web_client)

    await client.post_message(
        channel="C456",
        text="fallback",
        request_id="req-2",
        thread_ts="1700000000.000001",
        blocks=blocks,
    )

    call_kwargs: dict[str, Any] = fake_web_client.chat_postMessage.call_args.kwargs
    assert call_kwargs["thread_ts"] == "1700000000.000001"
    assert call_kwargs["blocks"] == blocks


async def test_get_user_profile(fake_web_client: AsyncMock) -> None:
    """get_user_profile が profile dict を返すこと。"""
    client = SlackClient(bot_token="xoxb-test", client=fake_web_client)
    profile = await client.get_user_profile(user_id="U999", request_id="req-3")

    assert profile["real_name"] == "Shogo Komata"
    assert profile["email"] == "s-komata@vectorinc.co.jp"
    fake_web_client.users_profile_get.assert_awaited_once_with(user="U999")


def test_from_env_raises_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """SLACK_BOT_TOKEN が無い場合に from_env が RuntimeError を投げること。"""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        SlackClient.from_env()


# -------------------------
# update_message（受付→結果の書き換え：段階表示の土台）
# -------------------------
async def test_update_message_basic(fake_web_client: AsyncMock) -> None:
    """update_message が chat.update に channel/ts/text を渡し、ok=True を返す。"""
    client = SlackClient(bot_token="xoxb-test", client=fake_web_client)
    result = await client.update_message(
        channel="C123",
        ts="1716000000.000100",
        text="✅ 完了しました",
        request_id="req-up-1",
    )
    assert result.ok is True
    assert result.ts == "1716000000.000100"
    call_kwargs = fake_web_client.chat_update.call_args.kwargs
    assert call_kwargs["channel"] == "C123"
    assert call_kwargs["ts"] == "1716000000.000100"
    assert call_kwargs["text"] == "✅ 完了しました"
    assert "blocks" not in call_kwargs


async def test_update_message_with_blocks(fake_web_client: AsyncMock) -> None:
    """blocks 指定時は chat.update にそのまま渡る。"""
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*done*"}}]
    client = SlackClient(bot_token="xoxb-test", client=fake_web_client)
    await client.update_message(
        channel="C123",
        ts="1716000000.000100",
        text="fallback",
        request_id="req-up-2",
        blocks=blocks,
    )
    call_kwargs = fake_web_client.chat_update.call_args.kwargs
    assert call_kwargs["blocks"] == blocks


async def test_update_message_empty_ts_is_noop(fake_web_client: AsyncMock) -> None:
    """ts が空なら chat.update を呼ばず ok=False を返す（呼び側でフォールバック可）。"""
    client = SlackClient(bot_token="xoxb-test", client=fake_web_client)
    result = await client.update_message(
        channel="C123",
        ts="",
        text="x",
        request_id="req-up-3",
    )
    assert result.ok is False
    fake_web_client.chat_update.assert_not_called()


async def test_update_message_swallows_errors(fake_web_client: AsyncMock) -> None:
    """chat.update が例外を投げても上位に伝播せず ok=False を返す（graceful）。"""
    fake_web_client.chat_update.side_effect = RuntimeError("slack down")
    client = SlackClient(bot_token="xoxb-test", client=fake_web_client)
    result = await client.update_message(
        channel="C123",
        ts="1716000000.000100",
        text="x",
        request_id="req-up-4",
    )
    assert result.ok is False
