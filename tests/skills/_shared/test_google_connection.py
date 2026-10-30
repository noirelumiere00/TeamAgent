"""Google と同じ例外を Skill の API 境界へ注入する。外部 I/O は行わない。"""

from __future__ import annotations

import datetime as dt
import json
from importlib import import_module
from types import SimpleNamespace
from typing import Any, cast

import httplib2  # type: ignore[import-untyped]
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from teamagent.adapters.google_oauth_flow import WORKSPACE_SCOPES
from teamagent.adapters.oauth_token_store import InMemoryTokenStore, OAuthToken
from teamagent.skills._shared import google_connection as connection
from teamagent.skills.base import SkillContext

EMAIL = "user@example.com"
LINK = "https://connect.example.com/oauth2/start/signed-state"
MESSAGE = (
    f"Google の連携が切れています。こちらから 1 クリックで再連携できます: <{LINK}> "
    "再連携後に元の依頼を続けられます。"
)
NOW = dt.datetime(2026, 10, 30, 9, tzinfo=dt.timezone(dt.timedelta(hours=9)))


def failure(kind: str) -> Any:
    if kind in {"invalid_grant", "invalid_client", "invalid_scope"}:
        return RefreshError(f"{kind}: sensitive diagnostic", {"error": kind})  # type: ignore[no-untyped-call]
    status = 401 if kind == "401" else 403
    return HttpError(
        httplib2.Response({"status": str(status)}),
        json.dumps(
            {"error": {"code": status, "message": "private detail", "errors": [{"reason": kind}]}}
        ).encode(),
        uri="https://www.googleapis.com/private-document",
    )


class FailingGoogle:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            raise self.exc

        return call


def ctx(**changes: Any) -> SkillContext:
    return SkillContext(
        metadata={
            "user_email": EMAIL,
            "channel_id": "D1",
            "thread_ts": "100.1",
            "verified_slack_team_id": "T1",
            "identity_verified": True,
            **changes,
        }
    )


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    connection._NOTICES.clear()
    monkeypatch.setenv("USE_OAUTH_START_LINKS", "1")
    monkeypatch.setenv("CONNECT_BASE_URL", "https://connect.example.com")
    monkeypatch.setenv("OAUTH_REDIRECT_URI", "https://connect.example.com/oauth2/callback")
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "test-google-connection-secret-" + "x" * 32)
    monkeypatch.delenv("SLACK_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.setattr(
        "teamagent.adapters.google_oauth_flow.OAuthConsentFlow.authorization_url",
        lambda *a, **k: ("https://accounts.google.com/test", "signed-state"),
    )


def store(connected: bool = True) -> InMemoryTokenStore:
    result = InMemoryTokenStore()
    if connected:
        result.put(EMAIL, OAuthToken(refresh_token="fake", scopes=WORKSPACE_SCOPES))
    return result


MAILS = [
    ("mail_summary", "MailSummarySkill", "MailSummaryInput"),
    ("mail_followup", "MailFollowupSkill", "MailFollowupInput"),
    ("mail_reply", "MailReplySkill", "MailReplyInput"),
    ("mail_to_internal_context", "MailToInternalContextSkill", "MailInternalContextInput"),
    ("mail_constraints", "MailConstraintsSkill", "MailConstraintsInput"),
]


@pytest.mark.parametrize(("name", "cls", "schema"), MAILS)
@pytest.mark.parametrize(
    "kind", ["invalid_grant", "401", "insufficientPermissions", "invalid_scope"]
)
def test_mail_api_failures(name: str, cls: str, schema: str, kind: str) -> None:
    module = import_module(f"teamagent.skills.{name}.skill")
    inputs = import_module(f"teamagent.skills.{name}.schema")
    kwargs: dict[str, Any] = {"gmail": FailingGoogle(failure(kind))}
    if name == "mail_constraints":
        kwargs["consent_emails"] = {EMAIL}
    output = getattr(module, cls)(**kwargs).run(getattr(inputs, schema)(client_name="花王"), ctx())
    assert output.message == MESSAGE
    assert output.error == "reauth_needed"
    assert "\n" not in output.message


@pytest.mark.parametrize(("name", "cls", "schema"), MAILS)
def test_mail_not_connected(name: str, cls: str, schema: str) -> None:
    module = import_module(f"teamagent.skills.{name}.skill")
    inputs = import_module(f"teamagent.skills.{name}.schema")
    kwargs: dict[str, Any] = {"token_store": store(False)}
    if name == "mail_constraints":
        kwargs["consent_emails"] = {EMAIL}
    output = getattr(module, cls)(**kwargs).run(getattr(inputs, schema)(client_name="花王"), ctx())
    assert output.message == MESSAGE


@pytest.mark.parametrize(
    "mode",
    [
        "free",
        "agenda",
        "event",
        "workspace_calendar",
        "workspace_people",
        "digest",
        "meeting",
        "draft",
        "schedule",
    ],
)
@pytest.mark.parametrize("kind", ["missing", "invalid_grant", "401", "insufficientPermissions"])
def test_other_tools(monkeypatch: pytest.MonkeyPatch, mode: str, kind: str) -> None:
    from teamagent.adapters.gcalendar_client import GCalendarClient
    from teamagent.adapters.gmail_client import GmailClient
    from teamagent.adapters.gpeople_client import GPeopleClient
    from teamagent.skills.calendar_event.schema import CalendarEventInput
    from teamagent.skills.calendar_event.skill import CalendarEventSkill
    from teamagent.skills.calendar_freebusy.schema import CalendarFreeBusyInput
    from teamagent.skills.calendar_freebusy.skill import CalendarFreeBusySkill
    from teamagent.skills.mail_draft.schema import MailDraftInput
    from teamagent.skills.mail_draft.skill import MailDraftSkill
    from teamagent.skills.meeting_prep.schema import MeetingPrepInput
    from teamagent.skills.meeting_prep.skill import MeetingPrepSkill
    from teamagent.skills.morning_digest.draft_token import encode_draft_token
    from teamagent.skills.morning_digest.schema import MorningDigestInput
    from teamagent.skills.morning_digest.skill import MorningDigestSkill
    from teamagent.skills.schedule_propose.schema import ScheduleProposeInput
    from teamagent.skills.schedule_propose.skill import ScheduleProposeSkill
    from teamagent.skills.workspace_search.schema import WorkspaceSearchInput
    from teamagent.skills.workspace_search.skill import WorkspaceSearchSkill

    tokens = store(kind != "missing")
    action_token = encode_draft_token("thread", EMAIL)
    assert action_token
    output: Any
    api = FailingGoogle(failure(kind))
    for adapter in (GCalendarClient, GPeopleClient, GmailClient):
        monkeypatch.setattr(adapter, "from_user_token", lambda *a, **kw: api)
    if mode in {"free", "agenda"}:
        output = CalendarFreeBusySkill(token_store=tokens, now_factory=lambda: NOW).run(
            CalendarFreeBusyInput(
                mode="free" if mode == "free" else "agenda", relative_day="today"
            ),
            ctx(),
        )
    elif mode == "event":
        output = CalendarEventSkill(token_store=tokens, now_factory=lambda: NOW).run(
            CalendarEventInput(title="会議", start="2026-11-01T10:00:00+09:00"), ctx()
        )
    elif mode.startswith("workspace_"):
        output = WorkspaceSearchSkill(token_store=tokens).run(
            WorkspaceSearchInput(service=mode.removeprefix("workspace_"), query="花王"), ctx()
        )
    elif mode == "digest":
        output = MorningDigestSkill(token_store=tokens).run(MorningDigestInput(), ctx())
    elif mode == "meeting":
        output = MeetingPrepSkill(
            calendar_factory=lambda email: None if kind == "missing" else api
        ).run(MeetingPrepInput(), ctx())
    elif mode == "draft":
        output = MailDraftSkill(token_store=tokens).run(
            MailDraftInput(draft_token=action_token), ctx()
        )
    else:
        output = ScheduleProposeSkill(token_store=tokens).run(
            ScheduleProposeInput(schedule_token=action_token), ctx()
        )
    assert output.message == MESSAGE
    assert output.error == "reauth_needed"


def test_dedup_across_tools_and_fresh_contexts() -> None:
    from teamagent.skills.calendar_freebusy.schema import CalendarFreeBusyInput
    from teamagent.skills.calendar_freebusy.skill import CalendarFreeBusySkill
    from teamagent.skills.mail_summary.schema import MailSummaryInput
    from teamagent.skills.mail_summary.skill import MailSummarySkill

    assert (
        MailSummarySkill(token_store=store(False))
        .run(MailSummaryInput(client_name="花王"), ctx())
        .message
        == MESSAGE
    )
    assert (
        CalendarFreeBusySkill(token_store=store(False))
        .run(
            CalendarFreeBusyInput(relative_day="today"),
            ctx(_google_connection_session="new-session"),
        )
        .message
        == ""
    )
    assert connection.reconnect_message(ctx(thread_ts="other")) == MESSAGE
    assert connection.reconnect_message(ctx(user_email="other@example.com")) == MESSAGE
    assert connection.reconnect_message(ctx(verified_slack_team_id="T2")) == MESSAGE


@pytest.mark.parametrize("kind", ["invalid_client", "rateLimitExceeded", "domainPolicy"])
def test_non_reconnect_errors_do_not_issue_link(kind: str) -> None:
    from teamagent.skills.mail_summary.schema import MailSummaryInput
    from teamagent.skills.mail_summary.skill import MailSummarySkill

    output = MailSummarySkill(gmail=cast(Any, FailingGoogle(failure(kind)))).run(
        MailSummaryInput(client_name="花王"), ctx()
    )
    assert output.error == "gmail_api_failed"
    assert "1 クリック" not in output.message
    assert not connection._NOTICES


def test_identity_failure_is_not_reconnect() -> None:
    from teamagent.skills.mail_summary.schema import MailSummaryInput
    from teamagent.skills.mail_summary.skill import MailSummarySkill

    with pytest.raises(PermissionError):
        MailSummarySkill(token_store=store(False)).run(
            MailSummaryInput(client_name="花王"), ctx(user_email=None)
        )


@pytest.mark.parametrize(
    "kind", ["missing", "saved_scope", "invalid_grant", "invalid_scope", "401"]
)
def test_oauth_connect_same_guidance(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    from teamagent.skills.oauth_connect.schema import OAuthConnectInput
    from teamagent.skills.oauth_connect.skill import OAuthConnectSkill

    monkeypatch.setenv("OAUTH_CONNECT_LIVENESS_PROBE", "1")
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_ID", "fake-client")
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_SECRET", "fake-secret")
    tokens = store(kind != "missing")
    if kind == "saved_scope":
        tokens.put(EMAIL, OAuthToken(refresh_token="fake", scopes=("openid",)))

    def request(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            status=401 if kind == "401" else 400,
            data=json.dumps({"error": "invalid_grant" if kind == "401" else kind}).encode(),
            headers={},
        )

    output = OAuthConnectSkill(google_store=tokens, google_liveness_request=request).run(
        OAuthConnectInput(), ctx()
    )
    assert output.message == MESSAGE
    assert output.url == LINK
    assert output.slack_url is None


def test_durable_notice_claim_across_process_cache_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMAGENT_HMAC_STATE_TABLE", "fake-table")
    monkeypatch.setenv("TEAMAGENT_HMAC_STATE_SCOPE", "fake-scope")
    claims: set[str] = set()

    def consume(key: str, **kwargs: Any) -> bool:
        assert kwargs == {"record_prefix": "GOOGLE_RECONNECT#"}
        if key in claims:
            return False
        claims.add(key)
        return True

    monkeypatch.setattr("teamagent.adapters.google_oauth_flow.consume_state_once", consume)
    assert connection.reconnect_message(ctx()) == MESSAGE
    connection._NOTICES.clear()
    assert connection.reconnect_message(ctx()) == ""
    assert connection.reconnect_message(ctx(thread_ts="different")) == MESSAGE


def test_parallel_tools_only_one_notice() -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: connection.reconnect_message(ctx()), range(4)))
    assert results.count(MESSAGE) == 1
    assert results.count("") == 3


def test_link_failure_has_no_diagnostics_or_user_work(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("secret=private-scope-401")

    monkeypatch.setattr(
        "teamagent.adapters.google_oauth_flow.OAuthConsentFlow.authorization_url", broken
    )
    assert (
        connection.reconnect_message(ctx())
        == "Google の連携リンクを準備できませんでした。復旧をお待ちください。"
    )
    assert not connection._NOTICES


@pytest.mark.parametrize("api", ["get_message", "get_thread", "create_draft"])
@pytest.mark.parametrize("kind", ["invalid_grant", "401", "insufficientPermissions"])
def test_reply_late_api_failures(monkeypatch: pytest.MonkeyPatch, api: str, kind: str) -> None:
    from teamagent.skills.mail_reply.schema import MailReplyInput
    from teamagent.skills.mail_reply.skill import MailReplySkill
    from tests.skills.mail_reply.test_mail_reply import FakeBedrock, FakeGmail, _inbound

    gmail = FakeGmail([_inbound()])
    monkeypatch.setattr(gmail, api, FailingGoogle(failure(kind)).call, raising=False)
    output = MailReplySkill(gmail=cast(Any, gmail), bedrock=FakeBedrock(), thread_context=True).run(
        MailReplyInput(client_name="花王", target_message_id="m0"), ctx()
    )
    assert output.message == MESSAGE
    assert output.error == "reauth_needed"
    if api != "get_thread":
        assert not output.created
