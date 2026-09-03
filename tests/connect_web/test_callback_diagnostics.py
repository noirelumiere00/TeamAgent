"""connect-web の連携失敗ページに診断行（``診断: CONNECT-…``）が出ることのテスト。

Google（/oauth2/callback）と Slack（/slack/oauth/callback）の全失敗経路を TestClient で踏み、
- 失敗ページに期待コードの診断行と転送案内が出る
- 診断行に state / code / token / 素のメールが出ない
- 成功ページには診断行が出ない
- S01（署名不一致）と S02（期限切れ）が出し分けられる
を固定する。実 Google / 実 Slack / 実 DB / 実 KMS は 0。
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import UniqueViolation

from teamagent.adapters import google_oauth_flow, slack_oauth_flow
from teamagent.adapters.oauth_token_store import OAuthToken, SlackOAuthToken
from teamagent.connect_diagnostics import ConnectDiag, admin_forward_hint
from teamagent.connect_web.app import _request_id_of, create_app

_GSECRET = "unit-test-state-secret"
_SSECRET = "unit-test-slack-state-secret"
_EMAIL = "owner@vectorinc.co.jp"
_UID = "U123"
_TEAM = "T456"
_DIAG_RE = re.compile(r"診断: (CONNECT-[A-Z]\d\d[a-c]?) (\d{4}-\d\d-\d\d \d\d:\d\d JST) ([^<\s]+)")


def _diag(text: str) -> tuple[str, str, str]:
    m = _DIAG_RE.search(text)
    assert m, "診断行が無い"
    return m.group(1), m.group(2), m.group(3)


def _assert_no_secrets(text: str, *, state: str, code: str = "abc") -> None:
    assert state not in text
    assert f"rt-{code}" not in text and f"xoxp-{code}" not in text
    assert "owner@" not in _DIAG_RE.search(text).group(0)  # type: ignore[union-attr]


# ───────────────────────── Google ─────────────────────────


class _Store:
    def __init__(self, *, fail: bool = False) -> None:
        self.puts: list[tuple[str, OAuthToken]] = []
        self._fail = fail

    def put(self, user_email: str, token: OAuthToken) -> None:
        if self._fail:
            raise RuntimeError("db down")
        self.puts.append((user_email, token))


def _google_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exchange_fn: Any = None,
    store: Any = None,
    verifier: Any = None,
    state_consumer: Any = None,
    client_id: str | None = "test-client.apps.googleusercontent.com",
) -> TestClient:
    monkeypatch.setenv("OAUTH_STATE_SECRET", _GSECRET)
    if client_id is None:
        monkeypatch.delenv("CONNECT_GOOGLE_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    else:
        monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_ID", client_id)

    def _exchange(code: str) -> OAuthToken:
        return OAuthToken(refresh_token=f"rt-{code}", scopes=("a",), id_token=f"id-{code}")

    def _verifier(token: str, client_id: str) -> dict[str, Any]:
        return {"email": _EMAIL, "email_verified": True}

    seen: set[str] = set()

    def _consume(state: str) -> bool:
        if state in seen:
            return False
        seen.add(state)
        return True

    app = create_app(
        redirect_uri="https://example/oauth2/callback",
        exchange_fn=exchange_fn or _exchange,
        store=store if store is not None else _Store(),
        google_state_consumer=state_consumer or _consume,
        oauth_id_token_verifier=verifier or _verifier,
    )
    return TestClient(app)


def _gstate(**kw: Any) -> str:
    return google_oauth_flow.make_state(_EMAIL, **kw)


def test_google_success_page_has_no_diag(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _google_client(monkeypatch)
    r = client.get("/oauth2/callback", params={"code": "abc", "state": _gstate()})
    assert r.status_code == 200
    assert "診断:" not in r.text


def test_google_user_denied_is_s05(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _google_client(monkeypatch)
    r = client.get("/oauth2/callback", params={"error": "access_denied"})
    assert r.status_code == 400
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.S05.value
    assert subject == "-"
    assert admin_forward_hint() in r.text


def test_google_missing_params_is_s01(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _google_client(monkeypatch)
    r = client.get("/oauth2/callback", params={"code": "abc"})
    assert r.status_code == 400
    assert _diag(r.text)[0] == ConnectDiag.S01.value


def test_google_tampered_state_is_s01_without_email(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _google_client(monkeypatch)
    state = _gstate()
    tampered = state[:-6] + ("AAAAAA" if not state.endswith("AAAAAA") else "BBBBBB")
    r = client.get("/oauth2/callback", params={"code": "abc", "state": tampered})
    assert r.status_code == 400
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.S01.value
    assert subject == "-"  # 署名未検証の email は出さない
    assert "改変" in r.text or "文字が変わった" in r.text
    _assert_no_secrets(r.text, state=tampered)


def test_google_garbage_state_is_s01(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _google_client(monkeypatch)
    r = client.get("/oauth2/callback", params={"code": "abc", "state": "garbage"})
    assert r.status_code == 400
    assert _diag(r.text)[0] == ConnectDiag.S01.value


def test_google_expired_state_is_s02_with_masked_email(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _google_client(monkeypatch)
    state = _gstate(now=1, nonce="old")
    r = client.get("/oauth2/callback", params={"code": "abc", "state": state})
    assert r.status_code == 400
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.S02.value
    assert subject == "o***@vectorinc.co.jp"
    assert "30 分" in r.text
    _assert_no_secrets(r.text, state=state)


def test_google_reused_state_is_s03(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _google_client(monkeypatch)
    state = _gstate()
    assert client.get("/oauth2/callback", params={"code": "a", "state": state}).status_code == 200
    r = client.get("/oauth2/callback", params={"code": "b", "state": state})
    assert r.status_code == 400
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.S03.value
    assert subject == "o***@vectorinc.co.jp"


def test_google_account_mismatch_is_s04(monkeypatch: pytest.MonkeyPatch) -> None:
    def other(token: str, client_id: str) -> dict[str, Any]:
        return {"email": "someone.else@gmail.com", "email_verified": True}

    client = _google_client(monkeypatch, verifier=other)
    state = _gstate()
    r = client.get("/oauth2/callback", params={"code": "abc", "state": state})
    assert r.status_code == 403
    assert _diag(r.text)[0] == ConnectDiag.S04.value
    _assert_no_secrets(r.text, state=state)


def _raise_runtime(_: str) -> bool:
    raise RuntimeError("state store unconfigured")


def _raise_other(_: str) -> bool:
    raise TimeoutError("dynamodb throttled")


def _exchange_boom(code: str) -> OAuthToken:
    raise RuntimeError("token endpoint down")


def _exchange_no_id_token(code: str) -> OAuthToken:
    return OAuthToken(refresh_token=f"rt-{code}", scopes=("a",), id_token=None)


def _verifier_boom(token: str, client_id: str) -> dict[str, Any]:
    raise ValueError("bad id_token")


@pytest.mark.parametrize(
    ("label", "kwargs", "status"),
    [
        ("state_store_unconfigured", {"state_consumer": _raise_runtime}, 500),
        ("state_consume_failed", {"state_consumer": _raise_other}, 503),
        ("exchange_failed", {"exchange_fn": _exchange_boom}, 500),
        ("id_token_missing", {"exchange_fn": _exchange_no_id_token}, 403),
        ("client_id_missing", {"client_id": None}, 500),
        ("id_token_invalid", {"verifier": _verifier_boom}, 403),
        ("store_failed", {"store": _Store(fail=True)}, 500),
    ],
)
def test_google_server_side_failures_are_s06(
    monkeypatch: pytest.MonkeyPatch, label: str, kwargs: dict[str, Any], status: int
) -> None:
    client = _google_client(monkeypatch, **kwargs)
    state = _gstate()
    r = client.get("/oauth2/callback", params={"code": "abc", "state": state})
    assert r.status_code == status, label
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.S06.value, label
    assert subject == "o***@vectorinc.co.jp", label
    _assert_no_secrets(r.text, state=state)


def test_request_id_from_alb_trace_header_is_in_diag(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _google_client(monkeypatch)
    r = client.get(
        "/oauth2/callback",
        params={"code": "abc", "state": "garbage"},
        headers={"X-Amzn-Trace-Id": "Root=1-68b7c0de-0123456789abcdef01234567"},
    )
    assert "1-68b7c0de-0123456789abcdef01234567" in r.text
    line = _DIAG_RE.search(r.text)
    assert line is not None
    assert r.text[line.end() :].startswith(" 1-68b7c0de-0123456789abcdef01234567")


def test_diag_line_is_html_escaped_and_request_id_sanitised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _google_client(monkeypatch)
    r = client.get(
        "/oauth2/callback",
        params={"code": "abc", "state": "garbage"},
        headers={"X-Request-Id": "<script>alert(1)</script>"},
    )
    assert "<script>" not in r.text
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.S01.value
    assert subject == "-"


class _Headers:
    def __init__(self, **h: str) -> None:
        self._h = {k.lower().replace("_", "-"): v for k, v in h.items()}

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._h.get(key.lower(), default)


class _Req:
    def __init__(self, **h: str) -> None:
        self.headers = _Headers(**h)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, None),
        ({"x_amzn_trace_id": "Root=1-abc-def"}, "1-abc-def"),
        ({"x_amzn_trace_id": "Self=1-x;Root=1-abc-def;Parent=zz"}, "1-abc-def"),
        ({"x_request_id": "req-1234"}, "req-1234"),
        ({"x_request_id": "bad value with spaces"}, None),
        ({"x_request_id": "a" * 200}, None),
    ],
)
def test_request_id_of(headers: dict[str, str], expected: str | None) -> None:
    assert _request_id_of(_Req(**headers)) == expected  # type: ignore[arg-type]


# ───────────────────────── Slack ─────────────────────────


class _SlackStore:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.puts: list[tuple[str, SlackOAuthToken]] = []
        self._fail = fail

    def put(self, user_email: str, token: SlackOAuthToken) -> None:
        if self._fail is not None:
            raise self._fail
        self.puts.append((user_email, token))


def _slack_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    slack_exchange_fn: Any = None,
    slack_store: Any = None,
    slack_state_consumer: Any = None,
) -> TestClient:
    monkeypatch.setenv("SLACK_OAUTH_STATE_SECRET", _SSECRET)
    monkeypatch.setenv("SLACK_TEAM_ID", _TEAM)

    def _exchange(code: str) -> SlackOAuthToken:
        return SlackOAuthToken(
            access_token=f"xoxp-{code}",
            scopes=("search:read",),
            slack_user_id=_UID,
            team_id=_TEAM,
        )

    seen: set[str] = set()

    def _consume(key: str) -> bool:
        if key in seen:
            return False
        seen.add(key)
        return True

    app = create_app(
        slack_redirect_uri="https://example/slack/oauth/callback",
        slack_exchange_fn=slack_exchange_fn or _exchange,
        slack_store=slack_store if slack_store is not None else _SlackStore(),
        slack_state_consumer=slack_state_consumer or _consume,
        slack_revoke_fn=lambda _t: None,
    )
    return TestClient(app)


def _sstate(*, bound: bool = True, **kw: Any) -> str:
    if bound:
        return slack_oauth_flow.make_state(_EMAIL, slack_user_id=_UID, slack_team_id=_TEAM, **kw)
    return slack_oauth_flow.make_state(_EMAIL, **kw)


def test_slack_success_page_has_no_diag(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _slack_client(monkeypatch)
    r = client.get("/slack/oauth/callback", params={"code": "abc", "state": _sstate()})
    assert r.status_code == 200
    assert "診断:" not in r.text


def test_slack_user_denied_is_s05(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _slack_client(monkeypatch)
    r = client.get("/slack/oauth/callback", params={"error": "access_denied"})
    assert r.status_code == 400
    assert _diag(r.text)[0] == ConnectDiag.S05.value


@pytest.mark.parametrize(
    ("label", "params"),
    [
        ("missing_params", {"code": "abc"}),
        ("garbage", {"code": "abc", "state": "garbage"}),
        ("expired", {"code": "abc", "state": None}),  # None → 期限切れ state を後で入れる
        ("unbound", {"code": "abc", "state": "UNBOUND"}),
    ],
)
def test_slack_state_problems_are_t01(
    monkeypatch: pytest.MonkeyPatch, label: str, params: dict[str, Any]
) -> None:
    client = _slack_client(monkeypatch)
    p = dict(params)
    if p.get("state") is None and "state" in p:
        p["state"] = _sstate(now=1, nonce="old")
    elif p.get("state") == "UNBOUND":
        p["state"] = _sstate(bound=False)
    r = client.get("/slack/oauth/callback", params=p)
    assert r.status_code == 400, label
    assert _diag(r.text)[0] == ConnectDiag.T01.value, label
    if isinstance(p.get("state"), str) and len(p["state"]) > 20:
        _assert_no_secrets(r.text, state=p["state"])


def test_slack_reused_state_is_t01(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _slack_client(monkeypatch)
    state = _sstate()
    assert (
        client.get("/slack/oauth/callback", params={"code": "a", "state": state}).status_code == 200
    )
    r = client.get("/slack/oauth/callback", params={"code": "b", "state": state})
    assert r.status_code == 400
    assert _diag(r.text)[0] == ConnectDiag.T01.value


def _slack_other_person(code: str) -> SlackOAuthToken:
    return SlackOAuthToken(
        access_token=f"xoxp-{code}", scopes=("search:read",), slack_user_id="U999", team_id=_TEAM
    )


def _slack_other_team(code: str) -> SlackOAuthToken:
    return SlackOAuthToken(
        access_token=f"xoxp-{code}", scopes=("search:read",), slack_user_id=_UID, team_id="T999"
    )


def _slack_blank_identity(code: str) -> SlackOAuthToken:
    return SlackOAuthToken(
        access_token=f"xoxp-{code}", scopes=("search:read",), slack_user_id="", team_id=""
    )


@pytest.mark.parametrize(
    ("label", "kwargs", "status", "expect_uid"),
    [
        ("identity_mismatch", {"slack_exchange_fn": _slack_other_person}, 403, "U999"),
        ("team_mismatch", {"slack_exchange_fn": _slack_other_team}, 403, _UID),
        ("identity_missing", {"slack_exchange_fn": _slack_blank_identity}, 403, None),
        ("uid_collision", {"slack_store": _SlackStore(fail=UniqueViolation())}, 409, _UID),
    ],
)
def test_slack_identity_problems_are_t02(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    kwargs: dict[str, Any],
    status: int,
    expect_uid: str | None,
) -> None:
    client = _slack_client(monkeypatch, **kwargs)
    state = _sstate()
    r = client.get("/slack/oauth/callback", params={"code": "abc", "state": state})
    assert r.status_code == status, label
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.T02.value, label
    assert subject == "o***@vectorinc.co.jp", label
    if expect_uid:
        assert f"o***@vectorinc.co.jp {expect_uid}" in r.text, label
    _assert_no_secrets(r.text, state=state)


def _slack_exchange_boom(code: str) -> SlackOAuthToken:
    raise RuntimeError("oauth_v2_access down")


@pytest.mark.parametrize(
    ("label", "kwargs", "status"),
    [
        ("state_store_unconfigured", {"slack_state_consumer": _raise_runtime}, 500),
        ("state_consume_failed", {"slack_state_consumer": _raise_other}, 503),
        ("exchange_failed", {"slack_exchange_fn": _slack_exchange_boom}, 500),
        ("store_failed", {"slack_store": _SlackStore(fail=RuntimeError("db down"))}, 500),
    ],
)
def test_slack_server_side_failures_are_s06(
    monkeypatch: pytest.MonkeyPatch, label: str, kwargs: dict[str, Any], status: int
) -> None:
    client = _slack_client(monkeypatch, **kwargs)
    state = _sstate()
    r = client.get("/slack/oauth/callback", params={"code": "abc", "state": state})
    assert r.status_code == status, label
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.S06.value, label
    assert subject == "o***@vectorinc.co.jp", label
    _assert_no_secrets(r.text, state=state)


# ───────────────────────── ログ ↔ 診断行の突合キー ─────────────────────────


def test_failure_warning_log_carries_request_id_and_diag(monkeypatch: pytest.MonkeyPatch) -> None:
    """診断行の request_id と同じ値が connect-web の warning ログに request_id= で出る（M1）。

    ALB/API GW のアクセスログには突合先が無いため、connect-web 自身の warning を
    `request_id` で引けることが運用の前提（docs/runbooks/connect_diagnostics.md）。
    """
    from structlog.testing import capture_logs

    client = _google_client(monkeypatch)
    state = _gstate(now=1, nonce="old")
    with capture_logs() as logs:
        r = client.get(
            "/oauth2/callback",
            params={"code": "abc", "state": state},
            headers={"X-Amzn-Trace-Id": "Root=1-68b7c0de-0123456789abcdef01234567"},
        )
    assert r.status_code == 400
    rows = [x for x in logs if x.get("event") == "connect_callback_bad_state"]
    assert len(rows) == 1
    assert rows[0]["request_id"] == "1-68b7c0de-0123456789abcdef01234567"
    assert rows[0]["diag"] == ConnectDiag.S02.value
    assert rows[0]["state_reason"] == "expired"
    assert "1-68b7c0de-0123456789abcdef01234567" in r.text


def test_slack_failure_warning_log_carries_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from structlog.testing import capture_logs

    client = _slack_client(monkeypatch, slack_exchange_fn=_slack_other_person)
    with capture_logs() as logs:
        r = client.get(
            "/slack/oauth/callback",
            params={"code": "abc", "state": _sstate()},
            headers={"X-Request-Id": "req-slack-1"},
        )
    assert r.status_code == 403
    rows = [x for x in logs if x.get("event") == "connect_slack_callback_identity_mismatch"]
    assert len(rows) == 1
    assert rows[0]["request_id"] == "req-slack-1"
    assert rows[0]["diag"] == ConnectDiag.T02.value


def test_success_log_carries_request_id_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from structlog.testing import capture_logs

    client = _google_client(monkeypatch)
    with capture_logs() as logs:
        r = client.get(
            "/oauth2/callback",
            params={"code": "abc", "state": _gstate()},
            headers={"X-Request-Id": "req-ok-1"},
        )
    assert r.status_code == 200
    rows = [x for x in logs if x.get("event") == "connect_callback_ok"]
    assert rows and rows[0]["request_id"] == "req-ok-1"
