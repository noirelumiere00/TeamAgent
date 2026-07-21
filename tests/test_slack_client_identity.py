"""SlackClient.resolve_identity の単体テスト（外部I/O無し・fake AsyncWebClient）。

検証主眼: 外部/ゲスト/bot/削除済/別ワークスペース/email欠落 を fail-closed(None)、
正規ユーザは正規化済み email を返す、TTL キャッシュで再呼出ししない。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import teamagent.adapters.slack_client as slack_client_module
from teamagent.adapters.slack_client import SlackClient


class _FakeWebClient:
    """users_info だけを差し替える最小スタブ。呼出し回数を数える。"""

    def __init__(self, user: dict[str, Any] | None) -> None:
        self._user = user
        self.calls = 0

    async def users_info(self, user: str) -> dict[str, Any]:
        self.calls += 1
        return {"user": self._user}


def _client(user: dict[str, Any] | None) -> tuple[SlackClient, _FakeWebClient]:
    fake = _FakeWebClient(user)
    return SlackClient(bot_token="x", client=fake), fake  # type: ignore[arg-type]


_MEMBER = {
    "team_id": "T0123456789",
    "profile": {"email": "Taro@VectorInc.CO.JP", "real_name": "Taro"},
}


@pytest.fixture(autouse=True)
def _production_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_TEAM_ID", "T0123456789")


async def test_resolve_identity_member_normalizes_email() -> None:
    client, fake = _client(_MEMBER)
    ident = await client.resolve_identity("U0123456789")
    assert ident is not None
    assert ident.email == "taro@vectorinc.co.jp"  # 正規化済み
    assert ident.is_member is True
    assert ident.slack_user_id == "U0123456789"
    assert fake.calls == 1


@pytest.mark.parametrize(
    "flag",
    ["deleted", "is_bot", "is_restricted", "is_ultra_restricted", "is_stranger"],
)
async def test_resolve_identity_rejects_guest_bot_deleted(flag: str) -> None:
    user = {**_MEMBER, flag: True}
    client, _ = _client(user)
    assert await client.resolve_identity("U0123456789") is None


async def test_resolve_identity_rejects_foreign_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_TEAM_ID", "T0123456789")
    client, _ = _client({**_MEMBER, "team_id": "T9876543210"})
    assert await client.resolve_identity("U0123456789") is None


async def test_resolve_identity_accepts_matching_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_TEAM_ID", "T0123456789")
    client, _ = _client({**_MEMBER, "team_id": "T0123456789"})
    assert (await client.resolve_identity("U0123456789")) is not None


async def test_resolve_identity_rejects_missing_email() -> None:
    client, _ = _client({"team_id": "T0123456789", "profile": {"real_name": "NoEmail"}})
    assert await client.resolve_identity("U0123456789") is None


@pytest.mark.parametrize(
    "bad",
    ["", "unknown", "x", "u0123456789", "W0123456789", "U12345", "12345", None],
)
async def test_resolve_identity_rejects_bad_user_id(bad: str | None) -> None:
    client, fake = _client(_MEMBER)
    assert await client.resolve_identity(bad) is None
    assert fake.calls == 0  # 形式不正は API 呼出し前に弾く


async def test_resolve_identity_caches() -> None:
    client, fake = _client(_MEMBER)
    a = await client.resolve_identity("U0123456789")
    b = await client.resolve_identity("U0123456789")
    assert a == b
    assert fake.calls == 1  # 2回目はキャッシュ命中


async def test_resolve_identity_revalidates_after_claim_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter((100.0, 161.0))
    monkeypatch.setattr(
        slack_client_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks)),
    )
    client, fake = _client(_MEMBER)
    assert await client.resolve_identity("U0123456789") is not None
    assert await client.resolve_identity("U0123456789") is not None
    assert fake.calls == 2


async def test_resolve_user_email_returns_email() -> None:
    client, _ = _client(_MEMBER)
    assert await client.resolve_user_email("U0123456789") == "taro@vectorinc.co.jp"
    client2, _ = _client({**_MEMBER, "is_restricted": True})
    assert await client2.resolve_user_email("U0123456789") is None


async def test_team_check_rejects_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SLACK_TEAM_ID 欠落は foreign team と同様に fail-closed。"""
    monkeypatch.delenv("SLACK_TEAM_ID", raising=False)
    client, _ = _client(_MEMBER)
    assert await client.resolve_identity("U0123456789") is None


async def test_team_check_rejects_malformed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_TEAM_ID", "T_BAD")
    client, _ = _client({**_MEMBER, "team_id": "T_BAD"})
    assert await client.resolve_identity("U0123456789") is None
