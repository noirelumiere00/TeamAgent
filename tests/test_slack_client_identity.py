"""SlackClient.resolve_identity の単体テスト（外部I/O無し・fake AsyncWebClient）。

検証主眼: 外部/ゲスト/bot/削除済/別ワークスペース/email欠落 を fail-closed(None)、
正規ユーザは正規化済み email を返す、TTL キャッシュで再呼出ししない。
"""

from __future__ import annotations

from typing import Any

import pytest

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
    "team_id": "T123",
    "profile": {"email": "Taro@VectorInc.CO.JP", "real_name": "Taro"},
}


async def test_resolve_identity_member_normalizes_email() -> None:
    client, fake = _client(_MEMBER)
    ident = await client.resolve_identity("U12345")
    assert ident is not None
    assert ident.email == "taro@vectorinc.co.jp"  # 正規化済み
    assert ident.is_member is True
    assert ident.slack_user_id == "U12345"
    assert fake.calls == 1


@pytest.mark.parametrize(
    "flag",
    ["deleted", "is_bot", "is_restricted", "is_ultra_restricted", "is_stranger"],
)
async def test_resolve_identity_rejects_guest_bot_deleted(flag: str) -> None:
    user = {**_MEMBER, flag: True}
    client, _ = _client(user)
    assert await client.resolve_identity("U12345") is None


async def test_resolve_identity_rejects_foreign_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_TEAM_ID", "T_OWN")
    client, _ = _client({**_MEMBER, "team_id": "T_OTHER"})
    assert await client.resolve_identity("U12345") is None


async def test_resolve_identity_accepts_matching_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_TEAM_ID", "T_OWN")
    client, _ = _client({**_MEMBER, "team_id": "T_OWN"})
    assert (await client.resolve_identity("U12345")) is not None


async def test_resolve_identity_rejects_missing_email() -> None:
    client, _ = _client({"team_id": "T123", "profile": {"real_name": "NoEmail"}})
    assert await client.resolve_identity("U12345") is None


@pytest.mark.parametrize("bad", ["", "unknown", "x", "u12345", "12345", None])
async def test_resolve_identity_rejects_bad_user_id(bad: str | None) -> None:
    client, fake = _client(_MEMBER)
    assert await client.resolve_identity(bad) is None
    assert fake.calls == 0  # 形式不正は API 呼出し前に弾く


async def test_resolve_identity_caches() -> None:
    client, fake = _client(_MEMBER)
    a = await client.resolve_identity("U12345")
    b = await client.resolve_identity("U12345")
    assert a == b
    assert fake.calls == 1  # 2回目はキャッシュ命中


async def test_resolve_user_email_returns_email() -> None:
    client, _ = _client(_MEMBER)
    assert await client.resolve_user_email("U12345") == "taro@vectorinc.co.jp"
    client2, _ = _client({**_MEMBER, "is_restricted": True})
    assert await client2.resolve_user_email("U12345") is None


async def test_team_check_skipped_when_env_unset_warns_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SLACK_TEAM_ID 未設定は team 検証 skip（fail-open・仕様として明文化）＋WARN は1回だけ。

    多人数運用では tfvars の slack_team_id を必ず設定すること（CLAUDE.md §5-C5）。
    """
    monkeypatch.delenv("SLACK_TEAM_ID", raising=False)
    client, _ = _client({**_MEMBER, "team_id": "T_FOREIGN"})
    assert not client._team_check_warned
    ident = await client.resolve_identity("U12345")
    assert ident is not None  # 未設定＝他 team でも通る（fail-open。他ガードは別途有効）
    assert client._team_check_warned  # 警告済みフラグが立つ（2回目以降は出さない）
