"""adapters/google_liveness.probe のテスト（実 Google 0・実 DB 0）。

フェイクは token endpoint の **HTTP 応答だけ**を差し替え、本物の
``build_user_credentials`` → ``google.oauth2.credentials.Credentials.refresh`` →
``google.oauth2.reauth.refresh_grant`` → ``_client._handle_error_response`` を通す。
これで本番と同じ形の ``google.auth.exceptions.RefreshError``（``"invalid_grant: …"`` と
応答の dict）が分類器に届く（汎用 Exception で分類器を素通りさせない）。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import parse_qs

import pytest

from teamagent.adapters import google_liveness
from teamagent.adapters.google_oauth_flow import WORKSPACE_SCOPES
from teamagent.adapters.oauth_token_store import OAuthToken

_CAL_EVENTS = "https://www.googleapis.com/auth/calendar.events"


class _Resp:
    """google.auth.transport.Response と同じ形（status / headers / data）。"""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self.headers = {"content-type": "application/json; charset=utf-8"}
        self.data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()


class _TokenEndpoint:
    """oauth2.googleapis.com/token の代わり（google-auth の Request と同じ呼び出し形）。"""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: Any = None,
        headers: Any = None,
        timeout: Any = None,
        **kwargs: Any,
    ) -> _Resp:
        form = parse_qs(body.decode("utf-8")) if isinstance(body, bytes) else {}
        self.calls.append({"url": url, "method": method, "form": form, "timeout": timeout})
        return _Resp(self.status, self.payload)


def _ok(scope: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "access_token": "ya29.test-access-token",
        "expires_in": 3599,
        "token_type": "Bearer",
    }
    if scope is not None:
        payload["scope"] = scope
    return payload


def _err(error: str, desc: str) -> dict[str, str]:
    return {"error": error, "error_description": desc}


@pytest.fixture(autouse=True)
def _client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_SECRET", "test-secret")


def _token(scopes: tuple[str, ...] = WORKSPACE_SCOPES) -> OAuthToken:
    return OAuthToken(refresh_token="1//test-refresh-token", scopes=scopes)


def test_alive_when_refresh_succeeds_with_all_scopes() -> None:
    endpoint = _TokenEndpoint(200, _ok(" ".join(WORKSPACE_SCOPES)))
    result = google_liveness.probe(_token(), request=endpoint)
    assert result.status == "alive"
    assert result.missing_scopes == ()
    assert result.granted_count == len(WORKSPACE_SCOPES)
    # 本番の各アダプタと同じ組み立て: 保存した範囲を scope に付けた refresh_token grant を 1 回。
    assert len(endpoint.calls) == 1
    call = endpoint.calls[0]
    assert call["url"] == "https://oauth2.googleapis.com/token"
    assert call["method"] == "POST"
    assert call["form"]["grant_type"] == ["refresh_token"]
    assert call["form"]["refresh_token"] == ["1//test-refresh-token"]
    assert call["form"]["client_id"] == ["test-client.apps.googleusercontent.com"]
    assert call["form"]["scope"][0].split() == list(WORKSPACE_SCOPES)


def test_invalid_grant_is_token_dead() -> None:
    endpoint = _TokenEndpoint(400, _err("invalid_grant", "Token has been expired or revoked."))
    result = google_liveness.probe(_token(), request=endpoint)
    assert result.status == "token_dead"
    assert result.reason == "invalid_grant"
    assert len(endpoint.calls) == 1  # 400 は再試行しない（連携の返事を遅らせない）


def test_invalid_scope_is_scope_missing() -> None:
    """保存した範囲の一部が実は許可されていない（Google は refresh を invalid_scope で断る）。"""
    endpoint = _TokenEndpoint(400, _err("invalid_scope", "Bad Request"))
    result = google_liveness.probe(_token(), request=endpoint)
    assert result.status == "scope_missing"
    assert result.reason == "invalid_scope"


def test_granted_subset_in_refresh_response_is_scope_missing() -> None:
    granted = " ".join(s for s in WORKSPACE_SCOPES if s != _CAL_EVENTS)
    endpoint = _TokenEndpoint(200, _ok(granted))
    result = google_liveness.probe(_token(), request=endpoint)
    assert result.status == "scope_missing"
    assert result.reason == "granted_subset"
    assert result.missing_scopes == (_CAL_EVENTS,)
    assert result.granted_count == len(WORKSPACE_SCOPES) - 1


def test_email_alias_in_refresh_response_counts_as_userinfo_email() -> None:
    granted = " ".join("email" if s.endswith("/userinfo.email") else s for s in WORKSPACE_SCOPES)
    result = google_liveness.probe(_token(), request=_TokenEndpoint(200, _ok(granted)))
    assert result.status == "alive"


def test_refresh_response_without_scope_is_alive() -> None:
    """応答に scope が無い＝範囲の根拠が無い。生きていることだけは確か（不足と決めつけない）。"""
    result = google_liveness.probe(_token(), request=_TokenEndpoint(200, _ok(None)))
    assert result.status == "alive"
    assert result.granted_count is None


@pytest.mark.parametrize("scope", ["", "   "], ids=["empty", "blank"])
def test_refresh_response_with_empty_scope_is_alive(scope: str) -> None:
    """応答の scope が空＝範囲の根拠が無い（exchange() が空を要求範囲に戻すのと同じ判断）。

    本物の Credentials.refresh は ``"scope": ""`` を ``granted_scopes == []`` にする。これを
    「全部未許可」と読むと、生きている人に誤った「一部の権限が許可されていない」文言と
    missing_count=10 のログが出る。
    """
    endpoint = _TokenEndpoint(200, _ok(scope))
    result = google_liveness.probe(_token(), request=endpoint)
    assert result.status == "alive"
    assert result.reason == "refreshed"
    assert result.missing_scopes == ()
    assert len(endpoint.calls) == 1


@pytest.mark.parametrize(
    ("status", "payload", "reason"),
    [
        # クライアントの secret 不正は全員に同時に起きる。再連携では直らないので失効扱いにしない。
        (401, _err("invalid_client", "Unauthorized"), "invalid_client"),
        (400, _err("admin_policy_enforced", "blocked by admin"), "admin_policy_enforced"),
        (400, _err("unauthorized_client", "Unauthorized"), "unauthorized_client"),
    ],
)
def test_non_reauth_refresh_errors_are_unknown(
    status: int, payload: dict[str, str], reason: str
) -> None:
    result = google_liveness.probe(_token(), request=_TokenEndpoint(status, payload))
    assert result.status == "unknown"
    assert result.reason == reason


def test_transport_error_is_unknown() -> None:
    from google.auth import exceptions as gexc

    def _down(*_a: Any, **_k: Any) -> Any:
        raise gexc.TransportError("connection reset")

    result = google_liveness.probe(_token(), request=_down)
    assert result.status == "unknown"
    assert result.reason == "TransportError"


def test_timeout_bounds_the_whole_probe() -> None:
    """token endpoint が返ってこなくても、上限秒で unknown を返す（連携の返事を止めない）。"""
    release = threading.Event()

    def _hang(*_a: Any, **_k: Any) -> Any:
        release.wait(5)
        raise RuntimeError("released")

    started = time.monotonic()
    try:
        result = google_liveness.probe(_token(), request=_hang, timeout=0.2)
    finally:
        release.set()
    assert result.status == "unknown"
    assert result.reason == "timeout"
    assert time.monotonic() - started < 2.0


def test_empty_refresh_token_is_token_dead_without_calling_google() -> None:
    endpoint = _TokenEndpoint(200, _ok(" ".join(WORKSPACE_SCOPES)))
    result = google_liveness.probe(
        OAuthToken(refresh_token="", scopes=WORKSPACE_SCOPES), request=endpoint
    )
    assert result.status == "token_dead"
    assert result.reason == "refresh_token_empty"
    assert endpoint.calls == []


def test_no_row_is_unknown() -> None:
    assert google_liveness.probe(None).status == "unknown"


def test_missing_client_config_is_unknown_without_calling_google(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "CONNECT_GOOGLE_CLIENT_ID",
        "CONNECT_GOOGLE_CLIENT_SECRET",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    endpoint = _TokenEndpoint(200, _ok(" ".join(WORKSPACE_SCOPES)))
    result = google_liveness.probe(_token(), request=endpoint)
    assert result.status == "unknown"
    assert result.reason == "credentials_ValueError"
    assert endpoint.calls == []


def test_classifier_reads_invalid_grant_from_a_non_json_body() -> None:
    """応答が JSON でない（文字列の RefreshError）ときも invalid_grant を拾う。"""
    endpoint = _TokenEndpoint(400, b"invalid_grant")
    result = google_liveness.probe(_token(), request=endpoint)
    assert result.status == "token_dead"


def test_result_carries_no_token_or_error_text() -> None:
    """結果（＝ログに出す値）に token・例外の文面を入れない。"""
    endpoint = _TokenEndpoint(400, _err("invalid_grant", "Token has been expired or revoked."))
    result = google_liveness.probe(_token(), request=endpoint)
    dumped = repr(result)
    assert "1//test-refresh-token" not in dumped
    assert "expired or revoked" not in dumped


def test_default_request_caps_each_http_call_at_the_timeout() -> None:
    """既定のトランスポート（requests）は 1 回の HTTP を上限秒で区切る（既定 120 秒にしない）。"""

    class _RawResponse:
        """requests.Response の必要な面だけ（status_code / headers / content）。"""

        def __init__(self) -> None:
            self.status_code = 400
            self.headers = {"content-type": "application/json"}
            self.content = json.dumps(
                _err("invalid_grant", "Token has been expired or revoked.")
            ).encode()

    class _Session:
        def __init__(self) -> None:
            self.timeouts: list[Any] = []

        def request(self, method: str, url: str, **kwargs: Any) -> _RawResponse:
            self.timeouts.append(kwargs.get("timeout"))
            return _RawResponse()

        def close(self) -> None:  # google.auth.transport.requests.Request.__del__ が呼ぶ
            return None

    session = _Session()
    request = google_liveness._default_request(3.0, session=session)
    result = google_liveness.probe(_token(), request=request, timeout=3.0)
    assert result.status == "token_dead"
    assert session.timeouts == [3.0]
