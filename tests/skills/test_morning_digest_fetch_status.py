"""F0: 朝ダイジェストの取得状態（mail_fetch / calendar_fetch / thread_ok / draft_mode）のテスト。

本番の失敗の形を再現するため、**本物の GmailClient / GCalendarClient** に Gmail / Calendar API の
service の形をしたフェイクを注入する。フェイクが返すのは Gmail API の実 JSON（本物の
``_message_from_resp`` を通る）と、execute() で投げる **本物の** 例外:
  - google-auth の RefreshError（``google.oauth2._client._handle_error_response`` で作る）
  - googleapiclient の HttpError（本物の httplib2.Response と Google API のエラー JSON）
認証情報の組み立て失敗は、フェイクを注入せず **実関数 build_user_credentials** を通す。

日付に依存する部分（予定の窓・calendar_date）は calendar_window.now_jst を固定する
（書いた日だけ緑になる罠の対策）。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httplib2
import pytest
from google.auth import exceptions as gauth_exc
from google.oauth2 import _client as google_oauth_client
from googleapiclient.errors import HttpError
from structlog.testing import capture_logs

from teamagent.adapters.gcalendar_client import GCalendarClient
from teamagent.adapters.gmail_client import GmailClient
from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest import calendar_window as calwin
from teamagent.skills.morning_digest.schema import MorningDigestInput
from teamagent.skills.morning_digest.skill import MorningDigestSkill

JST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime(2026, 10, 1, 9, 30, tzinfo=JST)
ME = "owner@vectorinc.co.jp"


@pytest.fixture(autouse=True)
def _freeze_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(calwin, "now_jst", lambda: NOW)
    monkeypatch.delenv("DRAFT_ON_DEMAND_ONLY", raising=False)
    monkeypatch.delenv("MORNING_DIGEST_BRIEF", raising=False)
    monkeypatch.delenv("MORNING_DIGEST_ACK_FILTER", raising=False)


# ── 本物の例外 ───────────────────────────────────────────────────────────────


def refresh_error(error: str, description: str = "x") -> gauth_exc.RefreshError:
    try:
        google_oauth_client._handle_error_response(
            {"error": error, "error_description": description}, False
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


# ── Gmail / Calendar API の service の形をしたフェイク ────────────────────────


class _Req:
    def __init__(self, fn: Callable[[], Any]) -> None:
        self._fn = fn

    def execute(self) -> Any:
        return self._fn()


def _result(value: Any) -> Any:
    if isinstance(value, BaseException):
        raise value
    return value


def gmail_json(mid: str, tid: str, subject: str, *, minute: int = 0) -> dict[str, Any]:
    """Gmail API の messages.get / threads.get の 1 メッセージ（本物の形）。"""
    body = base64.urlsafe_b64encode("本文です。ご確認ください。".encode()).decode().rstrip("=")
    internal = int((NOW - dt.timedelta(hours=2, minutes=minute)).timestamp() * 1000)
    return {
        "id": mid,
        "threadId": tid,
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "本文です",
        "internalDate": str(internal),
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "取引先 太郎 <taro@client.example>"},
                {"name": "To", "value": ME},
                {"name": "Subject", "value": subject},
            ],
            "body": {"data": body},
        },
    }


class FakeGmailService:
    """users().messages().list/get と users().threads().get の形だけを持つ。"""

    def __init__(
        self,
        *,
        refs: list[tuple[str, str]] | BaseException,
        threads: dict[str, Any],
        messages: dict[str, Any],
    ) -> None:
        self._refs = refs
        self._threads = threads
        self._messages = messages

    def users(self) -> FakeGmailService:
        return self

    def messages(self) -> _Messages:
        return _Messages(self)

    def threads(self) -> _Threads:
        return _Threads(self)


class _Messages:
    def __init__(self, svc: FakeGmailService) -> None:
        self._svc = svc

    def list(self, **_: Any) -> _Req:
        def run() -> Any:
            refs = _result(self._svc._refs)
            return {"messages": [{"id": m, "threadId": t} for m, t in refs]}

        return _Req(run)

    def get(self, *, id: str, **_: Any) -> _Req:
        return _Req(lambda: _result(self._svc._messages[id]))


class _Threads:
    def __init__(self, svc: FakeGmailService) -> None:
        self._svc = svc

    def get(self, *, id: str, **_: Any) -> _Req:
        return _Req(lambda: _result(self._svc._threads[id]))


class FakeCalendarService:
    def __init__(self, result: Any) -> None:
        self._result = result

    def events(self) -> FakeCalendarService:
        return self

    def list(self, **_: Any) -> _Req:
        return _Req(lambda: _result(self._result))


def calendar_ok() -> dict[str, Any]:
    return {
        "items": [
            {
                "id": "e1",
                "summary": "定例",
                "start": {"dateTime": "2026-10-01T14:00:00+09:00"},
                "end": {"dateTime": "2026-10-01T15:00:00+09:00"},
            }
        ]
    }


class FakeBedrock:
    """triage の応答（id を複写した JSON 配列）。本文の中身には依存しない。"""

    def converse(self, *, messages: list[Any], **_: Any) -> Any:
        text = messages[0]["content"][0]["text"]
        ids = re.findall(r"<<<MAIL id=(\S+) ", text)
        payload = [{"id": i, "importance": "medium", "summary": "要約"} for i in ids]
        return SimpleNamespace(text=json.dumps(payload), usage=SimpleNamespace(cost_usd=0.0))


class FakeTokenStore:
    def __init__(self, token: OAuthToken) -> None:
        self._token = token

    def get(self, email: str) -> OAuthToken:
        return self._token


def run_skill(
    *,
    gmail_service: FakeGmailService | None,
    calendar_result: Any = None,
    token: OAuthToken | None = None,
    skill_input: MorningDigestInput | None = None,
) -> Any:
    skill = MorningDigestSkill(
        token_store=FakeTokenStore(token or OAuthToken(refresh_token="rt")),
        gmail=GmailClient(credentials=None, service=gmail_service) if gmail_service else None,
        gcalendar=(
            GCalendarClient(credentials=None, service=FakeCalendarService(calendar_result))
            if calendar_result is not None
            else None
        ),
        bedrock=FakeBedrock(),
    )
    ctx = SkillContext(request_id="rq-f0", metadata={"user_email": ME})
    return skill.run(skill_input or MorningDigestInput(), ctx)


def two_thread_service(**overrides: Any) -> FakeGmailService:
    threads: dict[str, Any] = {
        "t1": {"messages": [gmail_json("m1", "t1", "件名A")]},
        "t2": {"messages": [gmail_json("m2", "t2", "件名B", minute=5)]},
    }
    messages: dict[str, Any] = {
        "m1": gmail_json("m1", "t1", "件名A"),
        "m2": gmail_json("m2", "t2", "件名B"),
    }
    threads.update(overrides.get("threads", {}))
    messages.update(overrides.get("messages", {}))
    return FakeGmailService(
        refs=overrides.get("refs", [("m1", "t1"), ("m2", "t2")]), threads=threads, messages=messages
    )


# ── 正常 ───────────────────────────────────────────────────────────────────


def test_normal_day_marks_ok_and_thread_ok() -> None:
    out = run_skill(gmail_service=two_thread_service(), calendar_result=calendar_ok())
    assert out.mail_fetch == "ok"
    assert out.calendar_fetch == "ok"
    assert out.mail_threads_failed == 0
    assert len(out.mail_digest) == 2
    assert all(item.thread_ok for item in out.mail_digest)
    assert out.mail_fetch_detail == "" and out.calendar_fetch_detail == ""
    assert len(out.calendar_events) == 1


# ── 取得失敗の分類（本物の例外が本物のクライアントを通る）───────────────────


def test_expired_token_on_both_marks_token_expired_and_logs_without_pii() -> None:
    expired = refresh_error("invalid_grant", "Token has been expired or revoked.")
    with capture_logs() as logs:
        out = run_skill(gmail_service=two_thread_service(refs=expired), calendar_result=expired)
    assert out.mail_fetch == "token_expired"
    assert out.calendar_fetch == "token_expired"
    assert out.mail_fetch_detail == "RefreshError:invalid_grant"
    assert out.mail_digest == [] and out.calendar_events == []
    # 既存の errors の契約（型名）は変えない。
    assert out.errors == ["mail: RefreshError", "calendar: RefreshError"]
    events = [e for e in logs if e["event"] == "morning_digest_fetch_failed"]
    assert {(e["section"], e["reason"], e["log_level"]) for e in events} == {
        ("mail", "token_expired", "warning"),
        ("calendar", "token_expired", "warning"),
    }
    blob = json.dumps(logs, ensure_ascii=False, default=str)
    assert ME not in blob and "owner" not in blob


def test_invalid_client_is_temporary_and_logged_at_error_level() -> None:
    """secret 不正は全員に同時に起きる＝本人の再連携では直らない＝temporary（ERROR で警報へ）。"""
    bad_client = refresh_error("invalid_client", "Unauthorized")
    with capture_logs() as logs:
        out = run_skill(
            gmail_service=two_thread_service(refs=bad_client), calendar_result=bad_client
        )
    assert out.mail_fetch == "temporary"
    assert out.calendar_fetch == "temporary"
    assert out.calendar_fetch_detail == "RefreshError:invalid_client"
    levels = {e["log_level"] for e in logs if e["event"] == "morning_digest_fetch_failed"}
    assert levels == {"error"}


def test_calendar_scope_403_is_scope_missing_while_mail_is_ok() -> None:
    scope = http_error(403, "insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT")
    out = run_skill(gmail_service=two_thread_service(), calendar_result=scope)
    assert out.mail_fetch == "ok"
    assert out.calendar_fetch == "scope_missing"
    assert out.calendar_fetch_detail == "HttpError:403"


def test_gmail_5xx_is_temporary() -> None:
    out = run_skill(
        gmail_service=two_thread_service(refs=http_error(503, "backendError")),
        calendar_result=calendar_ok(),
    )
    assert out.mail_fetch == "temporary"
    assert out.mail_fetch_detail == "HttpError:503"
    assert out.calendar_fetch == "ok"


# ── スレッド単位（thread_ok と読めなかった数）──────────────────────────────


def test_thread_get_failure_falls_back_to_message_and_marks_thread_not_ok() -> None:
    svc = two_thread_service(threads={"t2": http_error(500, "backendError")})
    out = run_skill(gmail_service=svc, calendar_result=calendar_ok())
    assert out.mail_fetch == "ok"
    assert out.mail_threads_failed == 0
    by_subject = {item.subject_display: item.thread_ok for item in out.mail_digest}
    assert by_subject == {"件名A": True, "件名B": False}


def test_some_threads_unreadable_counts_them_but_stays_ok() -> None:
    err = http_error(500, "backendError")
    svc = two_thread_service(threads={"t2": err}, messages={"m2": err})
    out = run_skill(gmail_service=svc, calendar_result=calendar_ok())
    assert out.mail_fetch == "ok"
    assert out.mail_threads_failed == 1
    assert [item.subject_display for item in out.mail_digest] == ["件名A"]


def test_all_threads_unreadable_is_not_new_mail_none() -> None:
    """一覧は取れたが 1 通も読めなかった＝「新着なし」ではなく temporary。"""
    err = http_error(503, "backendError")
    svc = two_thread_service(threads={"t1": err, "t2": err}, messages={"m1": err, "m2": err})
    with capture_logs() as logs:
        out = run_skill(gmail_service=svc, calendar_result=calendar_ok())
    assert out.mail_digest == []
    assert out.mail_fetch == "temporary"
    assert out.mail_fetch_detail == "threads_all_failed"
    assert out.mail_threads_failed == 2
    assert any(
        e["event"] == "morning_digest_mail_threads_failed" and e["failed"] == 2 for e in logs
    )


def test_empty_inbox_is_ok() -> None:
    """一覧が 0 件は本当に「新着なし」（取れなかったのとは別）。"""
    out = run_skill(gmail_service=two_thread_service(refs=[]), calendar_result={"items": []})
    assert out.mail_fetch == "ok" and out.mail_threads_failed == 0
    assert out.calendar_fetch == "ok"


# ── 認証情報の組み立て失敗（実関数 build_user_credentials を通す）────────────


def test_empty_refresh_token_marks_both_token_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_SECRET", "dummy")
    out = run_skill(gmail_service=None, token=OAuthToken(refresh_token=""))
    assert out.mail_fetch == "token_expired"
    assert out.calendar_fetch == "token_expired"
    assert out.mail_fetch_detail == "MissingRefreshTokenError"


def test_missing_client_secret_marks_both_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-06-25 の回帰と同じ形。全員に起きるので再連携へは誘導しない。"""
    for name in (
        "CONNECT_GOOGLE_CLIENT_ID",
        "CONNECT_GOOGLE_CLIENT_SECRET",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    out = run_skill(gmail_service=None, token=OAuthToken(refresh_token="rt"))
    assert out.mail_fetch == "temporary"
    assert out.calendar_fetch == "temporary"
    assert out.mail_fetch_detail == "ValueError"


# ── 下書きの作り方（末尾の説明文の切り替え材料）───────────────────────────


@pytest.mark.parametrize(
    ("on_demand", "max_drafts", "mode", "limit"),
    [("false", 5, "auto", 5), ("true", 5, "on_demand", 0), ("false", 0, "off", 0)],
)
def test_draft_mode_follows_the_real_settings(
    monkeypatch: pytest.MonkeyPatch, on_demand: str, max_drafts: int, mode: str, limit: int
) -> None:
    monkeypatch.setenv("DRAFT_ON_DEMAND_ONLY", on_demand)
    out = run_skill(
        gmail_service=two_thread_service(refs=[]),
        calendar_result={"items": []},
        skill_input=MorningDigestInput(max_drafts=max_drafts),
    )
    assert out.draft_mode == mode
    assert out.draft_limit == limit


@pytest.mark.parametrize(
    ("on_demand", "skip_env", "expected"),
    [("false", None, True), ("false", "false", False), ("true", None, False)],
)
def test_draft_skip_internal_reaches_the_footer_input(
    monkeypatch: pytest.MonkeyPatch, on_demand: str, skip_env: str | None, expected: bool
) -> None:
    """社内だけのやり取りを外したか（#504）は、朝に自動で作るときだけ説明文の材料に載る。"""
    monkeypatch.setenv("DRAFT_ON_DEMAND_ONLY", on_demand)
    if skip_env is None:
        monkeypatch.delenv("MORNING_DIGEST_DRAFT_SKIP_INTERNAL", raising=False)
    else:
        monkeypatch.setenv("MORNING_DIGEST_DRAFT_SKIP_INTERNAL", skip_env)
    out = run_skill(
        gmail_service=two_thread_service(refs=[]),
        calendar_result={"items": []},
        skill_input=MorningDigestInput(max_drafts=5),
    )
    assert out.draft_skip_internal is expected
