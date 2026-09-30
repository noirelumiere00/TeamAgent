"""oauth_connect の生存確認（OAUTH_CONNECT_LIVENESS_PROBE・既定 OFF）のテスト。

F0（2026-09-29）: 保存行のスコープだけで「連携済み」と答えると、refresh token が失効
（invalid_grant）した人が「連携」と送っても再連携リンクを手に入れられない（行き止まり）。
フラグ ON のときだけ、連携済みと判定した人のトークンで refresh を 1 回試す。

フェイクは token endpoint の HTTP 応答だけ（本物の Credentials.refresh と google-auth の
エラー処理を通し、本番と同じ形の RefreshError を分類器に届ける）。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import pytest
from structlog.testing import capture_logs

from teamagent.adapters.google_oauth_flow import WORKSPACE_SCOPES
from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.skills.base import SkillContext
from teamagent.skills.oauth_connect.schema import OAuthConnectInput
from teamagent.skills.oauth_connect.skill import OAuthConnectSkill

_EMAIL = "taro@vectorinc.co.jp"
_REFRESH = "1//secret-refresh-token-for-test"
_CAL_EVENTS = "https://www.googleapis.com/auth/calendar.events"
_OAUTH_ENV = {
    "OAUTH_REDIRECT_URI": "https://connect.example.com/oauth2/callback",
    "OAUTH_STATE_SECRET": "test-state-secret-0123456789",
    "CONNECT_GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
    "CONNECT_GOOGLE_CLIENT_SECRET": "test-secret",
}
_SLACK_ENV = {
    "SLACK_OAUTH_REDIRECT_URI": "https://connect.example.com/slack/oauth/callback",
    "CONNECT_SLACK_CLIENT_ID": "123456789.987654321",
    "SLACK_OAUTH_STATE_SECRET": "test-slack-state-secret-0123456789",
}
_UID = "U0123456789"
_TEAM = "T0123456789"

_CONNECTED_GOOGLE_ONLY = f"✅ *{_EMAIL}* は既に Google を連携済みです。追加の操作は不要です。そのまま話しかけてください。"
_TOKEN_DEAD_TEXT = (
    "連携の期限が切れています。パスワードの変更などで無効になりました。許可し直すと元どおり使えます"
)
_SCOPE_MISSING_TEXT = (
    "一部の権限が許可されていないため *再連携* が必要です。"
    "表示される画面ですべての項目にチェックを入れて許可してください"
)
_OLD_UPGRADE_TEXT = "機能追加により必要な権限が増えたため *再連携* が必要です"
_DEFAULT_DESC = "メールの読み取り・下書き作成、カレンダー等"


class _Resp:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self.headers = {"content-type": "application/json; charset=utf-8"}
        self.data = json.dumps(payload).encode()


class _TokenEndpoint:
    """oauth2.googleapis.com/token の代わり（呼ばれた回数と送られた form を記録）。"""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self.payload = payload
        self.calls: list[dict[str, list[str]]] = []

    def __call__(self, url: str, method: str = "GET", body: Any = None, **_k: Any) -> _Resp:
        self.calls.append(parse_qs(body.decode("utf-8")) if isinstance(body, bytes) else {})
        return _Resp(self.status, self.payload)


def _alive(scopes: tuple[str, ...] = WORKSPACE_SCOPES) -> _TokenEndpoint:
    return _TokenEndpoint(
        200,
        {
            "access_token": "ya29.test",
            "expires_in": 3599,
            "token_type": "Bearer",
            "scope": " ".join(scopes),
        },
    )


def _error(status: int, error: str, desc: str) -> _TokenEndpoint:
    return _TokenEndpoint(status, {"error": error, "error_description": desc})


class _TokenStore:
    """RdsTokenStore と同じ面（scopes() は復号なし・get() は復号した OAuthToken）。"""

    def __init__(self, scopes: tuple[str, ...] | None, *, get_error: Exception | None = None):
        self._scopes = scopes
        self._get_error = get_error
        self.get_calls = 0

    def scopes(self, _email: str) -> tuple[str, ...] | None:
        return self._scopes

    def has(self, _email: str) -> bool:
        return self._scopes is not None

    def get(self, _email: str) -> OAuthToken | None:
        self.get_calls += 1
        if self._get_error is not None:
            raise self._get_error
        if self._scopes is None:
            return None
        return OAuthToken(refresh_token=_REFRESH, scopes=self._scopes)


class _HasOnlyTokenStore:
    """scopes() を持たないストア（旧テストダブル等・has() へのフォールバック経路）。"""

    def __init__(self, *, with_get: bool = True) -> None:
        self.get_calls = 0
        if with_get:
            self.get = self._get

    def has(self, _email: str) -> bool:
        return True

    def _get(self, _email: str) -> OAuthToken | None:
        self.get_calls += 1
        return OAuthToken(refresh_token=_REFRESH, scopes=WORKSPACE_SCOPES)


class _SlackStore:
    def slack_user_id(self, _email: str) -> str | None:
        return _UID

    def has(self, _email: str) -> bool:
        return True


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="req-live-1",
        user_id="U1",
        metadata={
            "user_email": _EMAIL,
            "verified_slack_user_id": _UID,
            "verified_slack_team_id": _TEAM,
        },
    )


@pytest.fixture
def google_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SLACK_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.delenv("USE_OAUTH_START_LINKS", raising=False)


def _probe_on(monkeypatch: pytest.MonkeyPatch, value: str = "1") -> None:
    monkeypatch.setenv("OAUTH_CONNECT_LIVENESS_PROBE", value)


def _run(store: Any, endpoint: _TokenEndpoint, **kw: Any) -> Any:
    skill = OAuthConnectSkill(google_store=store, google_liveness_request=endpoint, **kw)
    return skill.run(OAuthConnectInput(), _ctx())


# ── フラグ OFF（既定）＝今と同じ ─────────────────────────────────────────────


@pytest.mark.parametrize("flag", [None, "", "0", "false", "off"])
def test_flag_off_never_probes_and_keeps_the_connected_message_bytes(
    monkeypatch: pytest.MonkeyPatch, google_only: None, flag: str | None
) -> None:
    if flag is None:
        monkeypatch.delenv("OAUTH_CONNECT_LIVENESS_PROBE", raising=False)
    else:
        monkeypatch.setenv("OAUTH_CONNECT_LIVENESS_PROBE", flag)
    store = _TokenStore(WORKSPACE_SCOPES)
    endpoint = _error(400, "invalid_grant", "Token has been expired or revoked.")
    with capture_logs() as logs:
        out = _run(store, endpoint)
    assert endpoint.calls == [], "フラグ OFF で token endpoint を呼んではいけない"
    assert store.get_calls == 0, "フラグ OFF で refresh token を復号してはいけない"
    assert out.url is None
    assert out.message == _CONNECTED_GOOGLE_ONLY  # 失効していても従来どおり（＝今と同じ）
    assert not [e for e in logs if e["event"] == "oauth_connect_liveness"]


def test_flag_off_keeps_the_scope_upgrade_wording(
    monkeypatch: pytest.MonkeyPatch, google_only: None
) -> None:
    monkeypatch.delenv("OAUTH_CONNECT_LIVENESS_PROBE", raising=False)
    old = tuple(s for s in WORKSPACE_SCOPES if s != _CAL_EVENTS)
    out = _run(_TokenStore(old), _alive())
    assert out.url is not None
    assert (
        "*Google を連携*（機能追加により必要な権限が増えたため *再連携* が必要です"
        "（カレンダー登録・日程提案など））\n"
    ) in out.message
    assert _SCOPE_MISSING_TEXT not in out.message


def test_description_is_unchanged_by_the_flag() -> None:
    """tool の説明（LLM が読むプロンプト）はフラグで変えない。"""
    desc = OAuthConnectSkill.description
    assert "生存" not in desc and "期限が切れ" not in desc


# ── フラグ ON ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("flag", ["1", "true", "on", "YES"])
def test_alive_token_stays_connected_after_one_refresh(
    monkeypatch: pytest.MonkeyPatch, google_only: None, flag: str
) -> None:
    _probe_on(monkeypatch, flag)
    store = _TokenStore(WORKSPACE_SCOPES)
    endpoint = _alive()
    with capture_logs() as logs:
        out = _run(store, endpoint)
    assert out.url is None
    assert out.message == _CONNECTED_GOOGLE_ONLY
    assert len(endpoint.calls) == 1
    assert endpoint.calls[0]["refresh_token"] == [_REFRESH]
    events = [e for e in logs if e["event"] == "oauth_connect_liveness"]
    assert len(events) == 1
    assert events[0]["result"] == "alive"
    assert events[0]["missing_count"] == 0


def test_revoked_token_gets_a_relink_with_the_expired_explanation(
    monkeypatch: pytest.MonkeyPatch, google_only: None
) -> None:
    _probe_on(monkeypatch)
    endpoint = _error(400, "invalid_grant", "Token has been expired or revoked.")
    with capture_logs() as logs:
        out = _run(_TokenStore(WORKSPACE_SCOPES), endpoint)
    assert out.url is not None and "accounts.google.com" in out.url
    assert out.url in out.message
    assert f"*Google を連携*（{_TOKEN_DEAD_TEXT}）\n" in out.message
    assert "連携済みです" not in out.message
    event = next(e for e in logs if e["event"] == "oauth_connect_liveness")
    assert event["result"] == "token_dead"
    assert event["reason"] == "invalid_grant"


@pytest.mark.parametrize(
    "endpoint",
    [
        _error(400, "invalid_scope", "Bad Request"),
        _alive(tuple(s for s in WORKSPACE_SCOPES if s != _CAL_EVENTS)),
    ],
    ids=["invalid_scope", "granted_subset"],
)
def test_partly_granted_token_gets_a_relink_with_the_scope_explanation(
    monkeypatch: pytest.MonkeyPatch, google_only: None, endpoint: _TokenEndpoint
) -> None:
    """保存行は全部の範囲に見えても、Google 側で一部しか許可されていない人（既存の 23 人の型）。"""
    _probe_on(monkeypatch)
    with capture_logs() as logs:
        out = _run(_TokenStore(WORKSPACE_SCOPES), endpoint)
    assert out.url is not None
    assert f"*Google を連携*（{_SCOPE_MISSING_TEXT}）\n" in out.message
    assert _OLD_UPGRADE_TEXT not in out.message
    event = next(e for e in logs if e["event"] == "oauth_connect_liveness")
    assert event["result"] == "scope_missing"


def test_granted_subset_logs_short_scope_names_only(
    monkeypatch: pytest.MonkeyPatch, google_only: None
) -> None:
    _probe_on(monkeypatch)
    endpoint = _alive(tuple(s for s in WORKSPACE_SCOPES if s != _CAL_EVENTS))
    with capture_logs() as logs:
        _run(_TokenStore(WORKSPACE_SCOPES), endpoint)
    event = next(e for e in logs if e["event"] == "oauth_connect_liveness")
    assert event["missing"] == ["calendar.events"]
    assert event["missing_count"] == 1


@pytest.mark.parametrize(
    "endpoint",
    [
        _error(401, "invalid_client", "Unauthorized"),
        _error(400, "admin_policy_enforced", "blocked"),
    ],
    ids=["invalid_client", "admin_policy_enforced"],
)
def test_undecidable_refresh_errors_issue_a_link_without_blaming_the_user(
    monkeypatch: pytest.MonkeyPatch, google_only: None, endpoint: _TokenEndpoint
) -> None:
    """判定不能はリンクを出す（安全側）。ただし「期限切れ」「権限不足」とは言わない。"""
    _probe_on(monkeypatch)
    with capture_logs() as logs:
        out = _run(_TokenStore(WORKSPACE_SCOPES), endpoint)
    assert out.url is not None
    assert f"*Google を連携*（{_DEFAULT_DESC}）\n" in out.message
    assert _TOKEN_DEAD_TEXT not in out.message
    assert _SCOPE_MISSING_TEXT not in out.message
    event = next(e for e in logs if e["event"] == "oauth_connect_liveness")
    assert event["result"] == "unknown"


def test_network_failure_issues_a_link(monkeypatch: pytest.MonkeyPatch, google_only: None) -> None:
    from google.auth import exceptions as gexc

    _probe_on(monkeypatch)

    def _down(*_a: Any, **_k: Any) -> Any:
        raise gexc.TransportError("connection reset by peer")

    skill = OAuthConnectSkill(
        google_store=_TokenStore(WORKSPACE_SCOPES), google_liveness_request=_down
    )
    out = skill.run(OAuthConnectInput(), _ctx())
    assert out.url is not None
    assert f"*Google を連携*（{_DEFAULT_DESC}）\n" in out.message


def test_token_decrypt_failure_issues_a_link(
    monkeypatch: pytest.MonkeyPatch, google_only: None
) -> None:
    _probe_on(monkeypatch)
    store = _TokenStore(WORKSPACE_SCOPES, get_error=RuntimeError("kms down"))
    endpoint = _alive()
    with capture_logs() as logs:
        out = _run(store, endpoint)
    assert out.url is not None
    assert endpoint.calls == []
    event = next(e for e in logs if e["event"] == "oauth_connect_liveness")
    assert event["result"] == "unknown"
    assert event["reason"] == "store_RuntimeError"


def test_stored_scope_shortfall_uses_the_general_wording_without_probing(
    monkeypatch: pytest.MonkeyPatch, google_only: None
) -> None:
    """保存行の時点で足りない人は、確かめるまでもなくリンク（文言は一般化した方）。"""
    _probe_on(monkeypatch)
    store = _TokenStore(tuple(s for s in WORKSPACE_SCOPES if s != _CAL_EVENTS))
    endpoint = _alive()
    out = _run(store, endpoint)
    assert out.url is not None
    assert f"*Google を連携*（{_SCOPE_MISSING_TEXT}）\n" in out.message
    assert endpoint.calls == []
    assert store.get_calls == 0


def test_no_row_issues_a_first_time_link_without_probing(
    monkeypatch: pytest.MonkeyPatch, google_only: None
) -> None:
    _probe_on(monkeypatch)
    store = _TokenStore(None)
    endpoint = _alive()
    out = _run(store, endpoint)
    assert out.url is not None
    assert f"*Google を連携*（{_DEFAULT_DESC}）\n" in out.message
    assert endpoint.calls == []


def test_has_only_store_is_probed_when_the_flag_is_on(
    monkeypatch: pytest.MonkeyPatch, google_only: None
) -> None:
    """scopes() を持たないストア（has() へのフォールバック）でも、ON なら確かめてから答える。"""
    _probe_on(monkeypatch)
    store = _HasOnlyTokenStore()
    endpoint = _error(400, "invalid_grant", "Token has been expired or revoked.")
    with capture_logs() as logs:
        out = _run(store, endpoint)
    assert len(endpoint.calls) == 1
    assert store.get_calls == 1
    assert out.url is not None
    assert f"*Google を連携*（{_TOKEN_DEAD_TEXT}）\n" in out.message
    assert "連携済みです" not in out.message
    event = next(e for e in logs if e["event"] == "oauth_connect_liveness")
    assert event["result"] == "token_dead"


def test_has_only_store_stays_connected_when_alive_or_flag_off(
    monkeypatch: pytest.MonkeyPatch, google_only: None
) -> None:
    _probe_on(monkeypatch)
    endpoint = _alive()
    assert _run(_HasOnlyTokenStore(), endpoint).message == _CONNECTED_GOOGLE_ONLY
    assert len(endpoint.calls) == 1

    monkeypatch.delenv("OAUTH_CONNECT_LIVENESS_PROBE", raising=False)
    store = _HasOnlyTokenStore()
    dead = _error(400, "invalid_grant", "Token has been expired or revoked.")
    assert _run(store, dead).message == _CONNECTED_GOOGLE_ONLY  # OFF＝今と同じ
    assert dead.calls == []
    assert store.get_calls == 0


def test_store_without_get_issues_a_link_as_undecidable(
    monkeypatch: pytest.MonkeyPatch, google_only: None
) -> None:
    """復号済みトークンを取れないストアは判定不能＝リンク（安全側・責める文言にしない）。"""
    _probe_on(monkeypatch)
    endpoint = _alive()
    with capture_logs() as logs:
        out = _run(_HasOnlyTokenStore(with_get=False), endpoint)
    assert endpoint.calls == []
    assert out.url is not None
    assert f"*Google を連携*（{_DEFAULT_DESC}）\n" in out.message
    event = next(e for e in logs if e["event"] == "oauth_connect_liveness")
    assert event["result"] == "unknown"
    assert event["reason"] == "store_no_get"


def test_dead_google_with_connected_slack_links_only_google(
    monkeypatch: pytest.MonkeyPatch, google_only: None
) -> None:
    _probe_on(monkeypatch)
    for k, v in _SLACK_ENV.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(
        google_store=_TokenStore(WORKSPACE_SCOPES),
        slack_store=_SlackStore(),
        google_liveness_request=_error(400, "invalid_grant", "Token has been revoked."),
    )
    out = skill.run(OAuthConnectInput(), _ctx())
    assert out.url is not None
    assert out.slack_url is None
    assert "（Slack は連携済みのため省略しています）" in out.message
    assert _TOKEN_DEAD_TEXT in out.message


def test_logs_never_carry_the_refresh_token_or_the_error_text(
    monkeypatch: pytest.MonkeyPatch, google_only: None
) -> None:
    _probe_on(monkeypatch)
    with capture_logs() as logs:
        _run(
            _TokenStore(WORKSPACE_SCOPES),
            _error(400, "invalid_grant", "Token has been expired or revoked."),
        )
    dumped = repr(logs)
    assert _REFRESH not in dumped
    assert "expired or revoked" not in dumped
    liveness = next(e for e in logs if e["event"] == "oauth_connect_liveness")
    assert _EMAIL not in repr(liveness)


def test_start_links_still_apply_to_the_relink(
    monkeypatch: pytest.MonkeyPatch, google_only: None
) -> None:
    """path 形式リンク（#376）の守りは生存確認の再連携リンクにもそのまま効く。"""
    _probe_on(monkeypatch)
    monkeypatch.setenv("USE_OAUTH_START_LINKS", "1")
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example.com")
    out = _run(
        _TokenStore(WORKSPACE_SCOPES),
        _error(400, "invalid_grant", "Token has been expired or revoked."),
    )
    assert out.url is not None
    assert out.url.startswith("https://connect.example.com/oauth2/start/")
    assert "?" not in out.url
