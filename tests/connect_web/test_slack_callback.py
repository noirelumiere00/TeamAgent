"""connect_web /slack/oauth/callback のテスト（実Slack0・実DB0・実KMS0）。

state検証(CSRF・nonce+TTL)→code交換→xoxp保存 の経路を、slack_exchange_fn/slack_store を
注入して検証する（Google 版 test_callback.py と対称）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from teamagent.adapters.oauth_token_store import SlackOAuthToken
from teamagent.adapters.slack_oauth_flow import make_state
from teamagent.connect_web.app import create_app

_SECRET = "unit-test-slack-state-secret"


class _FakeSlackStore:
    def __init__(self) -> None:
        self.puts: list[tuple[str, SlackOAuthToken]] = []

    def put(self, user_email: str, token: SlackOAuthToken) -> None:
        self.puts.append((user_email, token))


_UID = "U123"
_TEAM = "T456"


def bound_state(email: str, *, uid: str = _UID, team: str = _TEAM) -> str:
    """本人束縛タグ入りの state（発行側が実際に作る形）。"""
    return make_state(email, slack_user_id=uid, slack_team_id=team)


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    slack_exchange_fn: Any = None,
    slack_store: _FakeSlackStore | None = None,
    consumed: set[str] | None = None,
) -> tuple[TestClient, _FakeSlackStore, list[SlackOAuthToken]]:
    monkeypatch.setenv("SLACK_OAUTH_STATE_SECRET", _SECRET)
    monkeypatch.setenv("SLACK_TEAM_ID", _TEAM)
    st = slack_store or _FakeSlackStore()
    if slack_exchange_fn is None:

        def slack_exchange_fn(code: str) -> SlackOAuthToken:
            return SlackOAuthToken(
                access_token=f"xoxp-{code}",
                scopes=("search:read", "users:read"),
                slack_user_id=_UID,
                team_id=_TEAM,
            )

    # ワンタイム消費: 本番は DynamoDB。テストでは集合で「2回目は False」を再現する。
    seen = consumed if consumed is not None else set()

    def slack_state_consumer(key: str) -> bool:
        if key in seen:
            return False
        seen.add(key)
        return True

    revoked: list[SlackOAuthToken] = []

    app = create_app(
        slack_redirect_uri="https://example/slack/oauth/callback",
        slack_exchange_fn=slack_exchange_fn,
        slack_store=st,
        slack_state_consumer=slack_state_consumer,
        slack_revoke_fn=revoked.append,
    )
    return TestClient(app), st, revoked


def test_slack_callback_success_stores_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, revoked = _client(monkeypatch)  # revoked は成功時に空であることを固定
    state = bound_state("Owner@vectorinc.co.jp")  # SLACK_OAUTH_STATE_SECRET から署名
    r = client.get("/slack/oauth/callback", params={"code": "abc", "state": state})
    assert r.status_code == 200
    assert "Slack連携が完了" in r.text
    # email は正規化(lower)・token は exchange の戻り
    assert len(store.puts) == 1
    assert store.puts[0][0] == "owner@vectorinc.co.jp"
    assert store.puts[0][1].access_token == "xoxp-abc"
    assert revoked == []  # 成功経路では revoke しない


def test_slack_callback_rejects_tampered_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _revoked = _client(monkeypatch)
    r = client.get("/slack/oauth/callback", params={"code": "abc", "state": "garbage"})
    assert r.status_code == 400
    assert "検証に失敗" in r.text
    assert store.puts == []  # 保存されない


def test_slack_callback_missing_params(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _revoked = _client(monkeypatch)
    r = client.get("/slack/oauth/callback", params={"code": "abc"})  # state 欠如
    assert r.status_code == 400
    assert store.puts == []


def test_slack_callback_user_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _revoked = _client(monkeypatch)
    r = client.get("/slack/oauth/callback", params={"error": "access_denied"})
    assert r.status_code == 400
    assert "キャンセル" in r.text
    assert store.puts == []


def test_slack_callback_exchange_failure_returns_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(code: str) -> SlackOAuthToken:
        raise RuntimeError("oauth_v2_access down")

    client, store, _revoked = _client(monkeypatch, slack_exchange_fn=boom)
    state = bound_state("owner@vectorinc.co.jp")
    r = client.get("/slack/oauth/callback", params={"code": "abc", "state": state})
    assert r.status_code == 500
    assert "連携に失敗" in r.text
    assert store.puts == []


def test_slack_callback_does_not_leak_token_in_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功ページに xoxp を出さない（G8）。"""
    client, _, _revoked = _client(monkeypatch)
    state = make_state("owner@vectorinc.co.jp")
    r = client.get("/slack/oauth/callback", params={"code": "abc", "state": state})
    assert "xoxp-abc" not in r.text


# ── 本人照合の負テスト（この修正の核心・変異で赤くなることを証明する対象）──────────


def test_rejects_when_consenting_slack_account_differs(monkeypatch: pytest.MonkeyPatch) -> None:
    """リンクの宛先とは別の Slack アカウントで許可した → 403 で保存しない。

    配布時に1回リンクがズレるだけで、被害者宛メンション本文が別人の DM へ
    毎営業日配信される経路の入口。ここが本修正の本体。
    """

    def other_person(code: str) -> SlackOAuthToken:
        return SlackOAuthToken(
            access_token=f"xoxp-{code}",
            scopes=("search:read",),
            slack_user_id="U999OTHER",  # ← state に焼かれた _UID とは別人
            team_id=_TEAM,
        )

    client, store, revoked = _client(monkeypatch, slack_exchange_fn=other_person)
    r = client.get(
        "/slack/oauth/callback",
        params={"code": "abc", "state": bound_state("owner@vectorinc.co.jp")},
    )
    assert r.status_code == 403
    assert store.puts == []
    assert len(revoked) == 1  # 取得済み xoxp は握り潰さず revoke する


@pytest.mark.parametrize(
    ("uid", "team", "label"),
    [
        ("", _TEAM, "slack_user_id が空"),
        (_UID, "", "team_id が空"),
        ("", "", "両方が空"),
    ],
)
def test_rejects_when_identity_fields_are_empty(
    monkeypatch: pytest.MonkeyPatch, uid: str, team: str, label: str
) -> None:
    """Slack が id/team を返さなかったケースは「一致しない」＝拒否へ倒す。

    `if expected and observed and observed != expected` と書くと空文字で素通りする。
    実際 team 照合はこの形の fail-open だった（本修正で除去）。同じ穴を作らせない。
    """

    def blank(code: str) -> SlackOAuthToken:
        return SlackOAuthToken(
            access_token=f"xoxp-{code}", scopes=("search:read",), slack_user_id=uid, team_id=team
        )

    client, store, _revoked = _client(monkeypatch, slack_exchange_fn=blank)
    r = client.get(
        "/slack/oauth/callback",
        params={"code": "abc", "state": bound_state("owner@vectorinc.co.jp")},
    )
    assert r.status_code == 403, label
    assert store.puts == [], label


def test_rejects_unbound_legacy_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """本人束縛タグの無い state（旧形式）は 400。移行窓でも穴を開けない。"""
    client, store, _revoked = _client(monkeypatch)
    r = client.get(
        "/slack/oauth/callback",
        params={"code": "abc", "state": make_state("owner@vectorinc.co.jp")},
    )
    assert r.status_code == 400
    assert store.puts == []


def test_state_cannot_be_used_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """同じ state の二度使いは 2 回目で拒否（ワンタイム消費）。"""
    client, store, _revoked = _client(monkeypatch)
    state = bound_state("owner@vectorinc.co.jp")
    first = client.get("/slack/oauth/callback", params={"code": "abc", "state": state})
    second = client.get("/slack/oauth/callback", params={"code": "abc", "state": state})
    assert first.status_code == 200
    assert second.status_code == 400
    assert len(store.puts) == 1  # 2 回目は保存されない
