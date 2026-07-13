"""progress_notify（v0.3.1 Task7 Phase B）の単体テスト。

固定する仕様:
  - ENABLE_PROGRESS_NOTIFY 既定 OFF では一切送信しない（AC-7.3）
  - 送信失敗はツール実行を阻害しない＝例外を投げず None を返す（AC-7.4）
  - ツール完了後に削除される（AC-7.5）
  - channel_id が無い場合は slack_user_id 宛 DM フォールバック、どちらも無ければスキップ（AC-7.6）
  - 内部情報（ツール名等）がメッセージに出ない（AC-7.7）＝文言は _PROGRESS_MESSAGES のみ
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.mcp_gateway import progress_notify as pn


class _FakeResult:
    def __init__(self, ok: bool, ts: str) -> None:
        self.ok = ok
        self.ts = ts


class _FakeSlack:
    """SlackClient のダブル。post_message/open_dm/delete_message を記録する。"""

    def __init__(
        self, *, post_ok: bool = True, post_ts: str = "111.222", dm: str | None = "D999"
    ) -> None:
        self._post_ok = post_ok
        self._post_ts = post_ts
        self._dm = dm
        self.posted: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self.opened: list[str] = []

    @classmethod
    def _install(cls, monkeypatch: pytest.MonkeyPatch, inst: _FakeSlack) -> None:
        monkeypatch.setattr(pn.SlackClient, "from_env", classmethod(lambda c: inst))

    async def post_message(
        self, *, channel: str, text: str, request_id: str, thread_ts: str | None = None
    ) -> _FakeResult:
        self.posted.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return _FakeResult(self._post_ok, self._post_ts if self._post_ok else "")

    async def open_dm(self, user_id: str, request_id: str) -> str | None:
        self.opened.append(user_id)
        return self._dm

    async def delete_message(self, channel: str, ts: str, request_id: str) -> bool:
        self.deleted.append({"channel": channel, "ts": ts})
        return True


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_PROGRESS_NOTIFY", "true")


@pytest.mark.asyncio
async def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENABLE_PROGRESS_NOTIFY", raising=False)
    h = await pn.send_progress("search", {"channel_id": "C1"}, request_id="r")
    assert h is None


@pytest.mark.asyncio
async def test_send_to_channel_and_clear(on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSlack()
    _FakeSlack._install(monkeypatch, fake)
    h = await pn.send_progress("search", {"channel_id": "C1", "thread_ts": "T9"}, request_id="r")
    assert h is not None and h.channel == "C1" and h.ts == "111.222"
    assert fake.posted[0]["channel"] == "C1" and fake.posted[0]["thread_ts"] == "T9"
    assert "検索しています" in fake.posted[0]["text"]  # 利用者向け文言・ツール名は出ない
    assert "search" not in fake.posted[0]["text"]
    await pn.clear_progress(h, request_id="r")
    assert fake.deleted == [{"channel": "C1", "ts": "111.222"}]


@pytest.mark.asyncio
async def test_dm_fallback_when_no_channel(on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSlack(dm="D42")
    _FakeSlack._install(monkeypatch, fake)
    h = await pn.send_progress("video_analysis", {"slack_user_id": "U7"}, request_id="r")
    assert h is not None and h.channel == "D42"
    assert fake.opened == ["U7"]  # DM を開いた
    assert fake.posted[0]["channel"] == "D42"


@pytest.mark.asyncio
async def test_skip_when_no_destination(on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSlack()
    _FakeSlack._install(monkeypatch, fake)
    h = await pn.send_progress("search", {}, request_id="r")  # channel_id も slack_user_id も無い
    assert h is None
    assert fake.posted == []


@pytest.mark.asyncio
async def test_unknown_tool_skipped(on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSlack()
    _FakeSlack._install(monkeypatch, fake)
    h = await pn.send_progress("some_fast_tool", {"channel_id": "C1"}, request_id="r")
    assert h is None  # _PROGRESS_MESSAGES に無いツールは進捗を出さない（点滅回避）
    assert fake.posted == []


@pytest.mark.asyncio
async def test_send_failure_is_failopen(on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom(_FakeSlack):
        async def post_message(self, **kw: Any) -> _FakeResult:
            raise RuntimeError("network")

    fake = _Boom()
    _FakeSlack._install(monkeypatch, fake)
    h = await pn.send_progress("search", {"channel_id": "C1"}, request_id="r")
    assert h is None  # 例外を投げず None（ツール実行を阻害しない）


@pytest.mark.asyncio
async def test_post_not_ok_returns_none(on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSlack(post_ok=False)
    _FakeSlack._install(monkeypatch, fake)
    h = await pn.send_progress("search", {"channel_id": "C1"}, request_id="r")
    assert h is None


@pytest.mark.asyncio
async def test_clear_none_is_noop() -> None:
    await pn.clear_progress(None, request_id="r")  # 例外を投げない


@pytest.mark.asyncio
async def test_clear_failure_is_failopen(on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomDel(_FakeSlack):
        async def delete_message(self, channel: str, ts: str, request_id: str) -> bool:
            raise RuntimeError("boom")

    fake = _BoomDel()
    handle = pn.ProgressHandle(client=fake, channel="C1", ts="1.2")
    await pn.clear_progress(handle, request_id="r")  # 例外を投げない


@pytest.mark.asyncio
async def test_send_hang_is_bounded_by_timeout(on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Slack がハングしても _PROGRESS_TIMEOUT_S で打ち切り None を返す（本処理を遅延させない）。"""
    import asyncio

    class _Hang(_FakeSlack):
        async def post_message(self, **kw: Any) -> _FakeResult:
            await asyncio.sleep(30)  # ハングを模す
            return _FakeResult(True, "x")

    monkeypatch.setattr(pn, "_PROGRESS_TIMEOUT_S", 0.05)
    fake = _Hang()
    _FakeSlack._install(monkeypatch, fake)
    h = await pn.send_progress("search", {"channel_id": "C1"}, request_id="r")
    assert h is None  # タイムアウトで打ち切り（30秒待たない）


@pytest.mark.asyncio
async def test_privacy_sensitive_tools_not_in_progress_set() -> None:
    """メール/カルテ等の個人機微ツールは進捗対象に入れない（チャンネル露見の防止）。"""
    for t in ("mail_summary", "mail_draft", "mail_reply", "clientkarte", "morning_digest"):
        assert t not in pn._PROGRESS_MESSAGES
