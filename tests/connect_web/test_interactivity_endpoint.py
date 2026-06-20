"""connect_web /slack/interactivity のテスト（実Slack0・実DB0）。

署名検証 → block_actions 解析 → 状態更新 → response_url へ差し替え送信、を
fake slack_client / InMemory state_store / capture http_post を注入して検証する。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Any

from fastapi.testclient import TestClient

from teamagent import mail_action_ui as ui
from teamagent.adapters.mail_thread_state_store import (
    STATUS_DONE,
    InMemoryMailThreadStateStore,
)
from teamagent.connect_web.app import create_app, verify_slack_signature

SECRET = "test-signing-secret"
USER = "s-komata@vectorinc.co.jp"


class _FakeSlack:
    def __init__(self, email: str | None) -> None:
        self._email = email

    async def resolve_user_email(self, user_id: str | None, *, request_id: str = "-") -> str | None:
        return self._email


def _signed_post(client: TestClient, body: str, *, secret: str = SECRET) -> Any:
    ts = str(int(time.time()))
    base = f"v0:{ts}:{body}".encode()
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return client.post(
        "/slack/interactivity",
        content=body,
        headers={
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sig,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


def _done_body() -> str:
    payload = {
        "type": "block_actions",
        "user": {"id": "U1"},
        "channel": {"id": "D1"},
        "message": {"ts": "1716000000.0001"},
        "response_url": "https://hooks.slack.com/actions/x",
        "actions": [
            {
                "type": "button",
                "action_id": ui.ACTION_DONE,
                "value": ui.encode_value("thr-9", "見積の件", "t***@ex.com"),
            }
        ],
    }
    return urllib.parse.urlencode({"payload": json.dumps(payload, ensure_ascii=False)})


# ── 署名検証（純粋）─────────────────────────────────────────────────────────


def test_verify_signature_valid() -> None:
    body = b"payload=abc"
    ts = "1700000000"
    base = b"v0:" + ts.encode() + b":" + body
    sig = "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest()
    assert verify_slack_signature(SECRET, ts, sig, body, now=1700000010) is True


def test_verify_signature_bad() -> None:
    assert (
        verify_slack_signature(SECRET, "1700000000", "v0=deadbeef", b"x", now=1700000010) is False
    )


def test_verify_signature_stale_timestamp() -> None:
    body = b"x"
    ts = "1700000000"
    base = b"v0:" + ts.encode() + b":" + body
    sig = "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest()
    # now が 1 時間後 → スキュー超過で拒否
    assert verify_slack_signature(SECRET, ts, sig, body, now=1700003600) is False


def test_verify_signature_empty_secret() -> None:
    assert verify_slack_signature("", "1700000000", "v0=x", b"x", now=1700000010) is False


# ── エンドポイント ───────────────────────────────────────────────────────────


def test_bad_signature_rejected() -> None:
    app = create_app(signing_secret=SECRET, state_store=InMemoryMailThreadStateStore())
    client = TestClient(app)
    resp = client.post(
        "/slack/interactivity",
        content=_done_body(),
        headers={
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=bad",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert resp.status_code == 401


def test_done_action_writes_state_and_posts_update() -> None:
    store = InMemoryMailThreadStateStore()
    captured: list[tuple[str, dict[str, Any]]] = []

    def http_post(url: str, body: dict[str, Any]) -> None:
        captured.append((url, body))

    app = create_app(
        signing_secret=SECRET,
        state_store=store,
        slack_client=_FakeSlack(USER),
        http_post=http_post,
    )
    client = TestClient(app)
    resp = _signed_post(client, _done_body())

    assert resp.status_code == 200
    # BackgroundTask は TestClient 呼び出し内で実行される
    assert store.get(USER, "thr-9") is not None
    assert store.get(USER, "thr-9").status == STATUS_DONE
    assert captured, "response_url への差し替え送信が無い"
    url, body = captured[0]
    assert url == "https://hooks.slack.com/actions/x"
    assert body["replace_original"] is True
    assert "対応済み" in str(body["blocks"])


def test_unresolved_identity_writes_nothing() -> None:
    store = InMemoryMailThreadStateStore()
    captured: list[Any] = []
    app = create_app(
        signing_secret=SECRET,
        state_store=store,
        slack_client=_FakeSlack(None),  # 身元解決不可（ゲスト/外部等）
        http_post=lambda u, b: captured.append((u, b)),
    )
    client = TestClient(app)
    resp = _signed_post(client, _done_body())
    assert resp.status_code == 200
    assert store.get(USER, "thr-9") is None  # 状態は書かれない（fail-closed）
    assert captured == []
