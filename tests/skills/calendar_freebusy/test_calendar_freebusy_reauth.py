"""F0: 空き時間照会・予定一覧（calendar_freebusy）の失効を reauth_needed に分けるテスト。

本物の GCalendarClient に Calendar API の service の形をしたフェイクを注入し、execute() で
**本物の** RefreshError / HttpError を投げる（本番の失敗の形）。認証情報の組み立ては
フェイクの factory ではなく実関数 GCalendarClient.from_user_token → build_user_credentials を通す。

固定する仕様:
  - 失効（invalid_grant・refresh token 空）→ error=reauth_needed＋「連携」への誘導文
  - 権限不足（403 のスコープ不足）→ error=reauth_needed＋「すべての項目にチェック」
  - それ以外（invalid_client・5xx・secret 欠落）→ 従来どおり freebusy_failed / agenda_failed
    （再連携では直らないので誘導しない）
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from typing import Any

import httplib2
import pytest
from google.auth import exceptions as gauth_exc
from google.oauth2 import _client as google_oauth_client
from googleapiclient.errors import HttpError

from teamagent.adapters.gcalendar_client import GCalendarClient
from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.skills.base import SkillContext
from teamagent.skills.calendar_freebusy.schema import CalendarFreeBusyInput
from teamagent.skills.calendar_freebusy.skill import CalendarFreeBusySkill

JST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime(2026, 10, 1, 9, 0, tzinfo=JST)


def refresh_error(error: str) -> gauth_exc.RefreshError:
    try:
        google_oauth_client._handle_error_response(
            {"error": error, "error_description": "x"}, False
        )
    except gauth_exc.RefreshError as exc:
        return exc
    raise AssertionError("RefreshError が作れなかった")


def http_error(status: int, reason: str, details_reason: str = "") -> HttpError:
    error: dict[str, Any] = {
        "code": status,
        "message": "m",
        "errors": [{"message": "m", "domain": "global", "reason": reason}],
    }
    if details_reason:
        error["details"] = [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": details_reason}
        ]
    resp = httplib2.Response({"status": str(status), "content-type": "application/json"})
    return HttpError(resp, json.dumps({"error": error}).encode(), uri="https://x/y")


class _Req:
    def __init__(self, fn: Callable[[], Any]) -> None:
        self._fn = fn

    def execute(self) -> Any:
        return self._fn()


def _raise(exc: BaseException) -> Callable[[], Any]:
    def run() -> Any:
        raise exc

    return run


class FakeCalendarService:
    """freebusy().query() と events().list() が同じ例外を投げる。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def freebusy(self) -> FakeCalendarService:
        return self

    def events(self) -> FakeCalendarService:
        return self

    def query(self, **_: Any) -> _Req:
        return _Req(_raise(self._exc))

    def list(self, **_: Any) -> _Req:
        return _Req(_raise(self._exc))


class _Store:
    def __init__(self, token: OAuthToken) -> None:
        self._token = token

    def get(self, email: str) -> OAuthToken:
        return self._token


def _run(
    mode: str,
    *,
    exc: BaseException | None = None,
    token: OAuthToken | None = None,
) -> Any:
    factory = (
        (lambda tok: GCalendarClient(credentials=None, service=FakeCalendarService(exc)))
        if exc is not None
        else None  # None＝実関数 GCalendarClient.from_user_token を通す
    )
    skill = CalendarFreeBusySkill(
        token_store=_Store(token or OAuthToken(refresh_token="rt")),
        gcalendar_factory=factory,
        now_factory=lambda: NOW,
    )
    ctx = SkillContext(request_id="rq", metadata={"user_email": "me@vectorinc.co.jp"})
    return skill.run(CalendarFreeBusyInput(mode=mode, relative_day="today"), ctx)


@pytest.mark.parametrize("mode", ["free", "agenda"])
def test_expired_token_returns_reauth_needed(mode: str) -> None:
    out = _run(mode, exc=refresh_error("invalid_grant"))
    assert out.error == "reauth_needed"
    assert "連携が切れている" in out.message
    assert "@Aico に『連携』" in out.message
    assert "時間をおいて" not in out.message


@pytest.mark.parametrize("mode", ["free", "agenda"])
def test_scope_missing_returns_reauth_needed_with_check_all_guidance(mode: str) -> None:
    exc = http_error(403, "insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
    out = _run(mode, exc=exc)
    assert out.error == "reauth_needed"
    assert "権限が足りない" in out.message
    assert "すべての項目にチェック" in out.message


@pytest.mark.parametrize(
    ("mode", "fallback"), [("free", "freebusy_failed"), ("agenda", "agenda_failed")]
)
@pytest.mark.parametrize(
    "exc",
    [refresh_error("invalid_client"), http_error(503, "backendError"), TimeoutError("t")],
)
def test_other_failures_keep_the_old_error_code(
    mode: str, fallback: str, exc: BaseException
) -> None:
    """再連携では直らない失敗は従来どおり（連携へ誘導しない）。"""
    out = _run(mode, exc=exc)
    assert out.error == fallback
    assert "連携" not in out.message


def test_empty_refresh_token_through_the_real_builder_is_reauth_needed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_SECRET", "dummy")
    out = _run("free", token=OAuthToken(refresh_token=""))
    assert out.error == "reauth_needed"


def test_missing_client_secret_through_the_real_builder_is_not_reauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """以前は例外のまま MCP の汎用エラーへ落ちていた。今は構造化して返し、誘導はしない。"""
    for name in (
        "CONNECT_GOOGLE_CLIENT_ID",
        "CONNECT_GOOGLE_CLIENT_SECRET",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    out = _run("agenda", token=OAuthToken(refresh_token="rt"))
    assert out.error == "agenda_failed"
    assert "連携" not in out.message
