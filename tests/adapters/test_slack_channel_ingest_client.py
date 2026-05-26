"""adapters/slack_channel_ingest_client.py のユニットテスト。

FakeAsyncWebClient で AsyncWebClient.conversations_*, users_info をモック。
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.adapters.slack_channel_ingest_client import (
    HistoryBatch,
    IngestChannelConfig,
    SlackChannelIngestClient,
    SlackChannelMember,
    SlackMessage,
    _message_from_raw,
    collect_thread_participants,
    format_thread_as_document,
)


# -----------------------------------------------------------
# Fake AsyncWebClient（slack_sdk.web.async_client.AsyncWebClient 互換の最低限）
# -----------------------------------------------------------
class FakeAsyncWebClient:
    def __init__(
        self,
        *,
        history_resp: dict[str, Any] | None = None,
        replies_resp: dict[str, Any] | None = None,
        members_resp: dict[str, Any] | None = None,
        users_info_responses: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._history = history_resp or {"messages": [], "has_more": False}
        self._replies = replies_resp or {"messages": [], "has_more": False}
        self._members = members_resp or {"members": []}
        self._users_info = users_info_responses or {}
        self.last_history_kwargs: dict[str, Any] = {}
        self.last_replies_kwargs: dict[str, Any] = {}
        self.last_members_kwargs: dict[str, Any] = {}
        self.users_info_calls: list[str] = []

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self.last_history_kwargs = kwargs
        return self._history

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.last_replies_kwargs = kwargs
        return self._replies

    async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        self.last_members_kwargs = kwargs
        return self._members

    async def users_info(self, *, user: str) -> dict[str, Any]:
        self.users_info_calls.append(user)
        return self._users_info.get(user, {"user": {}})


# -----------------------------------------------------------
# list_channel_history
# -----------------------------------------------------------
def test_list_channel_history_maps_messages_and_cursor() -> None:
    fake = FakeAsyncWebClient(
        history_resp={
            "messages": [
                {
                    "ts": "1700000001.000001",
                    "user": "U001",
                    "text": "親メッセージ",
                    "thread_ts": "1700000001.000001",
                    "reply_count": 3,
                },
                {
                    "ts": "1700000000.000002",
                    "user": "U002",
                    "text": "単発投稿",
                },
            ],
            "has_more": True,
            "response_metadata": {"next_cursor": "PAGE2"},
        }
    )
    client = SlackChannelIngestClient(bot_token="xoxb-fake", client=fake)
    batch = client.list_channel_history(
        channel_id="C0XYZ",
        request_id="r",
        oldest=1699999999.0,
        limit=100,
    )
    assert isinstance(batch, HistoryBatch)
    assert len(batch.messages) == 2
    assert batch.messages[0].is_thread_parent is True
    assert batch.messages[1].is_top_level is True
    assert batch.has_more is True
    assert batch.next_cursor == "PAGE2"
    # kwargs 検証
    kw = fake.last_history_kwargs
    assert kw["channel"] == "C0XYZ"
    assert kw["limit"] == 100
    assert kw["oldest"] == "1699999999.0"


def test_list_channel_history_handles_empty() -> None:
    fake = FakeAsyncWebClient()
    client = SlackChannelIngestClient(bot_token="xoxb-fake", client=fake)
    batch = client.list_channel_history(channel_id="C0", request_id="r")
    assert batch.messages == ()
    assert batch.next_cursor is None
    assert batch.has_more is False


def test_list_channel_history_treats_empty_cursor_as_none() -> None:
    """next_cursor='' は最終ページのサインとして None に正規化。"""
    fake = FakeAsyncWebClient(
        history_resp={
            "messages": [],
            "has_more": False,
            "response_metadata": {"next_cursor": ""},
        }
    )
    client = SlackChannelIngestClient(bot_token="xoxb-fake", client=fake)
    batch = client.list_channel_history(channel_id="C0", request_id="r")
    assert batch.next_cursor is None


# -----------------------------------------------------------
# list_thread_replies
# -----------------------------------------------------------
def test_list_thread_replies_maps_messages() -> None:
    fake = FakeAsyncWebClient(
        replies_resp={
            "messages": [
                {"ts": "1700000001.000001", "user": "U001", "text": "親"},
                {"ts": "1700000002.000001", "user": "U002", "text": "リプライ"},
                {"ts": "1700000003.000001", "user": "U003", "text": "別リプライ"},
            ],
            "has_more": False,
        }
    )
    client = SlackChannelIngestClient(bot_token="xoxb-fake", client=fake)
    batch = client.list_thread_replies(
        channel_id="C0", thread_ts="1700000001.000001", request_id="r"
    )
    assert len(batch.messages) == 3
    assert fake.last_replies_kwargs["ts"] == "1700000001.000001"


# -----------------------------------------------------------
# list_channel_members + get_user_emails
# -----------------------------------------------------------
def test_list_channel_members_returns_ids() -> None:
    fake = FakeAsyncWebClient(
        members_resp={
            "members": ["U001", "U002", "U003"],
            "response_metadata": {"next_cursor": "PAGE2"},
        }
    )
    client = SlackChannelIngestClient(bot_token="xoxb-fake", client=fake)
    ids, cursor = client.list_channel_members(channel_id="C0", request_id="r")
    assert ids == ["U001", "U002", "U003"]
    assert cursor == "PAGE2"


def test_get_user_emails_resolves_profile_email() -> None:
    fake = FakeAsyncWebClient(
        users_info_responses={
            "U001": {
                "user": {
                    "id": "U001",
                    "profile": {"email": "alice@x.jp", "display_name": "Alice"},
                    "is_bot": False,
                    "deleted": False,
                }
            },
            "U002": {
                "user": {
                    "id": "U002",
                    "profile": {"display_name": "Bot"},  # email なし
                    "is_bot": True,
                }
            },
        }
    )
    client = SlackChannelIngestClient(bot_token="xoxb-fake", client=fake)
    members = client.get_user_emails(["U001", "U002"], request_id="r")
    assert len(members) == 2
    assert isinstance(members[0], SlackChannelMember)
    assert members[0].email == "alice@x.jp"
    assert members[0].is_bot is False
    assert members[1].email is None
    assert members[1].is_bot is True
    assert fake.users_info_calls == ["U001", "U002"]


# -----------------------------------------------------------
# from_env
# -----------------------------------------------------------
def test_from_env_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        SlackChannelIngestClient.from_env()


def test_from_env_constructs_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-" + "f" * 30)
    client = SlackChannelIngestClient.from_env()
    assert client._bot_token.startswith("xoxb-")


# -----------------------------------------------------------
# SlackMessage プロパティ
# -----------------------------------------------------------
def test_slack_message_is_thread_parent() -> None:
    m = SlackMessage(ts="1700.000001", user="U1", text="x", thread_ts="1700.000001", reply_count=2)
    assert m.is_thread_parent is True
    assert m.is_top_level is True


def test_slack_message_in_thread_is_not_top_level() -> None:
    m = SlackMessage(
        ts="1700.000002",
        user="U2",
        text="reply",
        thread_ts="1700.000001",  # 別 ts
        reply_count=0,
    )
    assert m.is_thread_parent is False
    assert m.is_top_level is False  # parent ではなくスレッド内 reply


def test_slack_message_single_post_is_top_level() -> None:
    m = SlackMessage(ts="1700.000003", user="U3", text="single")
    assert m.is_thread_parent is False
    assert m.is_top_level is True


# -----------------------------------------------------------
# format_thread_as_document / collect_thread_participants
# -----------------------------------------------------------
def test_format_thread_concatenates_parent_and_replies() -> None:
    parent = SlackMessage(
        ts="1700000001.000001",
        user="U001",
        text="提案 A はどうしますか？",
        thread_ts="1700000001.000001",
        reply_count=2,
    )
    replies = [
        # parent と同じ ts は除外される
        SlackMessage(ts="1700000001.000001", user="U001", text="提案 A はどうしますか？"),
        SlackMessage(ts="1700000002.000001", user="U002", text="A 案で進めましょう"),
        SlackMessage(ts="1700000003.000001", user="U003", text="承知しました"),
    ]
    text = format_thread_as_document(parent, replies)
    assert "<U001>: 提案 A はどうしますか？" in text
    assert "<U002>: A 案で進めましょう" in text
    assert "<U003>: 承知しました" in text
    # parent と同じ ts の重複行が出ないこと
    assert text.count("提案 A はどうしますか？") == 1


def test_collect_thread_participants_dedups() -> None:
    parent = SlackMessage(ts="1700.000001", user="U001", text="x")
    replies = [
        SlackMessage(ts="1700.000002", user="U002", text="a"),
        SlackMessage(ts="1700.000003", user="U001", text="b"),  # parent と同じ
        SlackMessage(ts="1700.000004", user=None, text="c"),  # bot メッセージ
        SlackMessage(ts="1700.000005", user="U003", text="d"),
    ]
    parts = collect_thread_participants(parent, replies)
    assert parts == ["U001", "U002", "U003"]


# -----------------------------------------------------------
# _message_from_raw（防御的パース）
# -----------------------------------------------------------
def test_message_from_raw_handles_missing_optional_fields() -> None:
    raw = {"ts": "1700.000001", "text": "hi"}  # user / thread_ts / files なし
    m = _message_from_raw(raw)
    assert m.ts == "1700.000001"
    assert m.user is None
    assert m.thread_ts is None
    assert m.files == ()


def test_message_from_raw_preserves_files() -> None:
    raw = {
        "ts": "1700.0001",
        "user": "U1",
        "text": "資料",
        "files": [{"id": "F1", "name": "a.pdf"}],
    }
    m = _message_from_raw(raw)
    assert m.files == ({"id": "F1", "name": "a.pdf"},)


# -----------------------------------------------------------
# IngestChannelConfig（設定型）
# -----------------------------------------------------------
def test_ingest_channel_config_defaults() -> None:
    c = IngestChannelConfig(
        channel_id="C0",
        channel_name="#proj-knowledge",
        description="test",
    )
    assert c.include_files is True
    assert c.oldest_days == 90
    assert c.extra_acl_emails == ()
    assert c.extra_metadata == {}
