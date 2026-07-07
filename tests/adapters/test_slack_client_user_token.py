"""SlackClient.from_user_token（本人 xoxp 経路）の最小テスト（課金0）。

要件B: 各営業「本人」の Slack User Token(xoxp) で動く経路が、共有 Bot Token(xoxb)
の ``from_env`` とは別に構築でき、渡した xoxp が AsyncWebClient / 内部 token に
そのまま流れることを確認する。実 API は叩かない（ダミー client 注入）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from teamagent.adapters.slack_client import SlackClient


def test_from_user_token_injected_client_is_used() -> None:
    """注入した AsyncWebClient がそのまま使われ、xoxp が token 概念として保持される。"""
    fake = AsyncMock()
    client = SlackClient.from_user_token("xoxp-user-123", client=fake)

    # 注入した本人 client がそのまま採用される（新規 AsyncWebClient を作らない）。
    assert client._client is fake
    # __init__ の token 概念(bot_token)に xoxp がそのまま入る（同型経路）。
    assert client._bot_token == "xoxp-user-123"


def test_from_user_token_empty_fail_closed() -> None:
    """xoxp が空なら未認可として弾く（本人トークン欠落での誤起動を防ぐ）。"""
    with pytest.raises(ValueError, match="xoxp"):
        SlackClient.from_user_token("")


def test_from_user_token_distinct_from_bot_token_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """from_env(xoxb 共有) と from_user_token(xoxp 本人) が別経路で別トークンを持つ。"""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-shared")
    bot = SlackClient.from_env()
    user = SlackClient.from_user_token("xoxp-user-123", client=AsyncMock())

    assert bot._bot_token == "xoxb-shared"
    assert user._bot_token == "xoxp-user-123"
    assert bot._bot_token != user._bot_token
