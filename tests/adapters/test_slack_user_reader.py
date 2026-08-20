"""SlackUserReader の単体テスト（実 Slack を叩かない・AsyncMock 注入）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.errors import SlackApiError

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


# ── read_thread_checked（fail-closed 用・error code を返す）────────────────────
# フェイクは本番の失敗モードを再現する: slack_sdk は ok:false で SlackApiError を投げ、
# .response["error"] に code が入る。


def test_read_thread_checked_maps_messages_on_success() -> None:
    resp = {
        "ok": True,
        "messages": [
            {"ts": "1.1", "user": "U1", "text": "親", "thread_ts": "1.1", "reply_count": 1},
            {"ts": "1.2", "user": "U2", "text": "返信"},
        ],
    }
    client = _client(conversations_replies=AsyncMock(return_value=resp))
    reader = SlackUserReader("xoxp-x", client=client)
    out = reader.read_thread_checked("C1", "1.1", "req")
    assert out.error == ""
    assert [m.text for m in out.messages] == ["親", "返信"]


@pytest.mark.parametrize("code", ["not_in_channel", "channel_not_found", "thread_not_found"])
def test_read_thread_checked_surfaces_slack_error_code(code: str) -> None:
    client = _client(
        conversations_replies=AsyncMock(
            side_effect=SlackApiError("failed", {"ok": False, "error": code})
        )
    )
    reader = SlackUserReader("xoxp-x", client=client)
    out = reader.read_thread_checked("C1", "1.1", "req")
    assert out.error == code
    assert out.messages == ()


def test_read_thread_checked_generic_exception_is_api_error() -> None:
    client = _client(conversations_replies=AsyncMock(side_effect=RuntimeError("boom")))
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.read_thread_checked("C1", "1.1", "req").error == "api_error"


def test_read_thread_checked_ok_false_without_raise() -> None:
    """slack_sdk が例外を投げず ok:false を素通りさせても error を拾う（防御的）。"""
    client = _client(
        conversations_replies=AsyncMock(return_value={"ok": False, "error": "not_in_channel"})
    )
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.read_thread_checked("C1", "1.1", "req").error == "not_in_channel"


def test_read_thread_checked_bad_target_no_call() -> None:
    client = _client(conversations_replies=AsyncMock(return_value={"messages": []}))
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.read_thread_checked("", "1.1", "req").error == "bad_target"
    assert reader.read_thread_checked("C1", "", "req").error == "bad_target"
    client.conversations_replies.assert_not_awaited()


def test_existing_read_thread_stays_fail_open_on_slack_api_error() -> None:
    """回帰: 既存 read_thread は fail-open のまま（slack_context / unreplied を壊さない）。"""
    client = _client(
        conversations_replies=AsyncMock(
            side_effect=SlackApiError("failed", {"ok": False, "error": "not_in_channel"})
        )
    )
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.read_thread("C1", "1.1", "req") == []


# ── 差出人の実名解決（users.info・24h TTL キャッシュ）──────────────────────


def _users_info(profile: dict[str, object] | None = None, **user: object) -> dict[str, object]:
    return {"ok": True, "user": {**user, "profile": profile or {}}}


def test_get_display_name_prefers_display_name() -> None:
    client = _client(
        users_info=AsyncMock(
            return_value=_users_info({"display_name": "こまた", "real_name": "小俣 慎悟"})
        )
    )
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.get_display_name("U12345678", "req") == "こまた"


def test_get_display_name_falls_back_through_real_name_and_handle() -> None:
    # display_name 空 → profile.real_name
    r1 = SlackUserReader(
        "xoxp-x",
        client=_client(
            users_info=AsyncMock(
                return_value=_users_info({"display_name": "", "real_name": "小俣"})
            )
        ),
    )
    assert r1.get_display_name("U12345678", "req") == "小俣"
    # profile が空 → user.real_name
    r2 = SlackUserReader(
        "xoxp-x",
        client=_client(users_info=AsyncMock(return_value=_users_info({}, real_name="佐藤"))),
    )
    assert r2.get_display_name("U12345678", "req") == "佐藤"
    # real_name も無い → user.name（ハンドル）
    r3 = SlackUserReader(
        "xoxp-x", client=_client(users_info=AsyncMock(return_value=_users_info({}, name="taro")))
    )
    assert r3.get_display_name("U12345678", "req") == "taro"


def test_get_display_name_returns_none_on_api_error() -> None:
    """失敗は None（呼び出し側を壊さない・架空の名前を作らない）。"""
    client = _client(
        users_info=AsyncMock(
            side_effect=SlackApiError("failed", {"ok": False, "error": "user_not_found"})
        )
    )
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.get_display_name("U12345678", "req") is None


def test_get_display_name_returns_none_when_no_name_fields() -> None:
    client = _client(users_info=AsyncMock(return_value=_users_info({})))
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.get_display_name("U12345678", "req") is None


def test_get_display_name_returns_none_on_ok_false_without_raise() -> None:
    client = _client(users_info=AsyncMock(return_value={"ok": False, "error": "user_not_found"}))
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.get_display_name("U12345678", "req") is None


def test_get_display_name_rejects_bad_ids_without_api_call() -> None:
    client = _client(users_info=AsyncMock(return_value=_users_info({"real_name": "誰か"})))
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.get_display_name("", "req") is None
    assert reader.get_display_name("   ", "req") is None
    assert reader.get_display_name("B0123456", "req") is None  # bot ID は対象外
    assert reader.get_display_name("not-an-id", "req") is None
    client.users_info.assert_not_awaited()


def test_get_display_name_caches_hit_for_24h() -> None:
    client = _client(users_info=AsyncMock(return_value=_users_info({"real_name": "小俣"})))
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.get_display_name("U12345678", "req") == "小俣"
    assert reader.get_display_name("U12345678", "req") == "小俣"
    assert client.users_info.await_count == 1  # 2 回目はキャッシュ
    # TTL は 24h。期限切れにすると再度叩く。
    name, expires = reader._name_cache["U12345678"]
    assert name == "小俣" and expires > 0
    reader._name_cache["U12345678"] = (name, -1.0)
    assert reader.get_display_name("U12345678", "req") == "小俣"
    assert client.users_info.await_count == 2


def test_get_display_name_caches_miss_but_retries_sooner() -> None:
    """失敗も一旦キャッシュ（連打しない）が、24h 焼き付けない。"""
    from teamagent.adapters.slack_user_reader import _DISPLAY_NAME_TTL, _DISPLAY_NAME_TTL_MISS

    client = _client(users_info=AsyncMock(side_effect=RuntimeError("boom")))
    reader = SlackUserReader("xoxp-x", client=client)
    assert reader.get_display_name("U12345678", "req") is None
    assert reader.get_display_name("U12345678", "req") is None
    assert client.users_info.await_count == 1
    assert _DISPLAY_NAME_TTL_MISS < _DISPLAY_NAME_TTL


def test_get_display_name_cache_is_per_instance() -> None:
    """トークン（＝人）が違えば結果を混ぜない。"""
    c1 = _client(users_info=AsyncMock(return_value=_users_info({"real_name": "A"})))
    c2 = _client(users_info=AsyncMock(return_value=_users_info({"real_name": "B"})))
    assert SlackUserReader("xoxp-1", client=c1).get_display_name("U12345678", "req") == "A"
    assert SlackUserReader("xoxp-2", client=c2).get_display_name("U12345678", "req") == "B"


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
