"""朝ダイジェストの祝日スキップ（F0・PR-0c）の runner テスト。

受け入れ条件（mailauto_plan.md §3 の 0c）:
  - 2026-10-12（スポーツの日）で skill.run を呼ばず DM も送らず、予定リマインドは登録する
  - 10/13 の走査範囲が 4 日
  - 会社休日を足せる・表の範囲外なら配信を続けて警告・期限の 60 日前に警告
  - フラグ OFF（既定）なら今と同じ（祝日も配信・走査 3 日・表を見ない）

フェイクは本番の失敗の形に寄せる:
  - 予定は本物の ``CalendarEvent`` を、窓で絞らずに返す（本番の events.list も窓に
    重なる予定を返す）。窓の外の予定を落とすのは skill 側の本物の ``_collect_calendar``。
  - Gmail の検索式は本物の ``skill.run`` が組み立てたものを記録して確かめる。
  - 日付は ``MORNING_DIGEST_DATE``（＝``_digest_day``）と ``calendar_window.now_jst`` の
    **両方**を固定する（書いた日だけ緑の罠・#401）。リマインドの「もう過ぎた予定」の判定も
    同じ now_jst を見るので、9:00 の予定が登録されないことで時計の固定まで確かめる。
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.gcalendar_client import CalendarEvent
from teamagent.digest_user_ref import user_ref
from teamagent.observability.logging_config import (
    configure_logging as _real_configure_logging,
)
from teamagent.skills.morning_digest import calendar_window as calwin
from teamagent.skills.morning_digest import delivery_calendar as dc
from teamagent.skills.morning_digest.skill import MorningDigestSkill

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"
TF_PATH = PROJECT_ROOT / "infra" / "terraform" / "morning_digest_schedule.tf"
SKILLMOD = "teamagent.skills.morning_digest.skill"


def _load() -> Any:
    name = "run_morning_digest_holiday_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()

U1 = "komata@vectorinc.co.jp"
U2 = "unlinked@vectorinc.co.jp"
U3 = "linked2@vectorinc.co.jp"
_D = _dt.date
_JST = calwin.JST

_ENV_TO_CLEAR = (
    "MORNING_DIGEST_HOLIDAY_SKIP",
    "MORNING_DIGEST_EXTRA_SKIP_DATES",
    "MORNING_DIGEST_REMINDERS",
    "MORNING_DIGEST_PERSONALIZED",
    "MORNING_DIGEST_USERS",
    "MORNING_DIGEST_USER_REF",
    "MORNING_DIGEST_MODE",
    "MORNING_DIGEST_COMPACT",
    "MORNING_DIGEST_BRIEF",
    "MORNING_DIGEST_MAX_DRAFTS",
    "MORNING_DIGEST_SLACK_UNREAD",
    "MORNING_DIGEST_CONCURRENCY",
    "MORNING_DIGEST_ACK_FILTER",
    "DRAFT_ON_DEMAND_ONLY",
    "REMINDER_LEAD_MINUTES",
    "USE_SLACK_CONTEXT",
    "DIGEST_USER_REF_PEPPER",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_TO_CLEAR:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "argv", ["run_morning_digest_fargate.py"])


def _freeze(monkeypatch: pytest.MonkeyPatch, day: _dt.date, hh: int = 9, mm: int = 30) -> None:
    """対象日（_digest_day）と壁時計（now_jst）を同じ日に固定する。"""
    monkeypatch.setenv("MORNING_DIGEST_DATE", day.isoformat())
    monkeypatch.setattr(
        calwin, "now_jst", lambda: _dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=_JST)
    )


# ── fakes ───────────────────────────────────────────────────────────────


class _Log:
    """runner の structlog を置き換える記録係（イベント名と付随キーだけ見る）。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def _rec(self, level: str, event: str, **kw: Any) -> None:
        self.events.append((level, event, kw))

    def info(self, event: str, **kw: Any) -> None:
        self._rec("info", event, **kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._rec("warning", event, **kw)

    def error(self, event: str, **kw: Any) -> None:
        self._rec("error", event, **kw)

    def named(self, event: str) -> list[dict[str, Any]]:
        return [kw for _lvl, ev, kw in self.events if ev == event]


class _Token:
    """本人の OAuthToken の代役（誰の token かだけを持つ）。"""

    def __init__(self, email: str) -> None:
        self.user_email = email


class _TokenStore:
    def __init__(self, linked: set[str]) -> None:
        self._linked = linked

    def get(self, email: str) -> Any:
        return _Token(email) if email in self._linked else None  # None = 未連携


class _GCal:
    """本物の events.list と同じく **窓で絞らない**（窓外を落とすのは skill 側）。"""

    def __init__(self, events: list[CalendarEvent]) -> None:
        self.events = events
        self.calls = 0

    def list_events(self, request_id: str, **kwargs: Any) -> list[CalendarEvent]:
        self.calls += 1
        return list(self.events)


class _CalRequest:
    """googleapiclient の HttpRequest の代役。本番の失敗はここ（execute）から出る。"""

    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


class _CalEvents:
    def __init__(self, service: _CalService) -> None:
        self._service = service

    def list(self, **params: Any) -> _CalRequest:
        self._service.calls.append(params)
        return _CalRequest(self._service.outcome)


class _CalService:
    """Calendar API v3 の service の形（``events().list(**p).execute()``）だけを偽る。

    ``GCalendarClient`` 本体（policy の封鎖・``extract_events``）は本物を通す。
    本番の失敗の形:
      - 失効した refresh token → ``execute()`` の中で AuthorizedHttp の refresh が失敗し、
        ``google.auth.exceptions.RefreshError``（invalid_grant / invalid_client）がそのまま出る
      - 権限不足・Google 側の障害 → ``googleapiclient.errors.HttpError``（403 / 503）
    ``GCalendarClient.list_events`` はどちらも握らずに上へ投げる。
    """

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def events(self) -> _CalEvents:
        return _CalEvents(self)


def _calendar_api_json(day: _dt.date) -> dict[str, Any]:
    """events.list の実レスポンスの形（``_holiday_events`` と同じ 4 件を API の JSON で）。"""
    d = day.isoformat()
    nxt = (day + _dt.timedelta(days=1)).isoformat()

    def _timed(event_id: str, summary: str, start: str, end: str, **extra: Any) -> dict[str, Any]:
        return {
            "kind": "calendar#event",
            "id": event_id,
            "status": "confirmed",
            "summary": summary,
            "start": {"dateTime": start, "timeZone": "Asia/Tokyo"},
            "end": {"dateTime": end, "timeZone": "Asia/Tokyo"},
            **extra,
        }

    return {
        "kind": "calendar#events",
        "summary": "primary",
        "timeZone": "Asia/Tokyo",
        "items": [
            _timed("e1", "朝会", f"{d}T09:00:00+09:00", f"{d}T09:30:00+09:00"),
            _timed(
                "e2",
                "定例",
                f"{d}T10:00:00+09:00",
                f"{d}T11:00:00+09:00",
                hangoutLink="https://meet.google.com/abc-defg-hij",
            ),
            {
                "kind": "calendar#event",
                "id": "e3",
                "status": "confirmed",
                "summary": "休日",
                "start": {"date": d},
                "end": {"date": nxt},
            },
            _timed("e4", "翌日", f"{nxt}T10:00:00+09:00", f"{nxt}T11:00:00+09:00"),
        ],
    }


def _google_http_error(status: int, reason: str, message: str, status_text: str) -> Any:
    """本物の ``googleapiclient.errors.HttpError``（Google API のエラー JSON つき）。"""
    import httplib2
    from googleapiclient.errors import HttpError

    resp = httplib2.Response(
        {"status": str(status), "content-type": "application/json; charset=UTF-8"}
    )
    body = json.dumps(
        {
            "error": {
                "code": status,
                "message": message,
                "errors": [{"message": message, "domain": "global", "reason": reason}],
                "status": status_text,
            }
        }
    ).encode()
    return HttpError(
        resp,
        body,
        uri="https://www.googleapis.com/calendar/v3/calendars/primary/events?alt=json",
    )


def _refresh_error(error: str, description: str) -> Any:
    """本物の ``google.auth.exceptions.RefreshError``（oauth2 の token 応答つき）。"""
    from google.auth.exceptions import RefreshError

    return RefreshError(
        f"{error}: {description}", {"error": error, "error_description": description}
    )


class _Gmail:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def list_messages(
        self, query: str, request_id: str, max_results: int = 30
    ) -> tuple[list[Any], None]:
        self.queries.append(query)
        return ([], None)

    def list_drafts(self, request_id: str, **_: Any) -> list[Any]:
        return []


class _NoBedrock:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"Bedrock must not be called in this test: {name}")


class _World:
    """1 回の main() 実行ぶんの観測点。"""

    def __init__(self, events: list[CalendarEvent], linked: set[str]) -> None:
        self.gcal = _GCal(events)
        self.gmail = _Gmail()
        self.store = _TokenStore(linked)
        self.run_calls: list[str] = []
        self.run_inputs: list[Any] = []
        self.calendar_only_calls: list[str] = []
        self.delivered: list[str] = []
        self.reminders: list[dict[str, Any]] = []
        self.digest_reservations: list[dict[str, Any]] = []
        self.posted: list[Any] = []
        self.im_channel = "D0HOLIDAY"
        #: users.lookupByEmail を本番の失敗の形で落とす人（email → 例外）
        self.slack_lookup_errors: dict[str, BaseException] = {}
        self.slack_lookups: list[str] = []
        self.log = _Log()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    users: list[str],
    events: list[CalendarEvent] | None = None,
    linked: set[str] | None = None,
) -> _World:
    w = _World(events or [], linked if linked is not None else set(users))

    class _SpySkill(MorningDigestSkill):
        def run(self, input: Any, ctx: Any) -> Any:
            w.run_calls.append(ctx.metadata["user_email"])
            w.run_inputs.append(input)
            return super().run(input, ctx)

        def collect_calendar_events(self, input: Any, ctx: Any) -> Any:
            w.calendar_only_calls.append(ctx.metadata["user_email"])
            return super().collect_calendar_events(input, ctx)

    def _factory(token_store: Any = None, **_: Any) -> _SpySkill:
        return _SpySkill(token_store=w.store, gmail=w.gmail, gcalendar=w.gcal, bedrock=_NoBedrock())

    monkeypatch.setattr(mod, "_resolve_target_users", lambda: list(users))
    monkeypatch.setattr(mod, "_build_token_store", lambda: w.store)
    monkeypatch.setattr(f"{SKILLMOD}.MorningDigestSkill", _factory)
    monkeypatch.setattr(
        "teamagent.orchestrator.factory._build_slack_context_provider", lambda: None
    )
    monkeypatch.setattr(mod, "logger", w.log)

    async def _deliver(email: str, text: str, blocks: Any) -> tuple[bool, str | None]:
        w.delivered.append(email)
        return (True, "D0DIGEST")

    monkeypatch.setattr(mod, "_deliver_to_slack", _deliver)

    class _AsyncClient:
        async def users_lookupByEmail(self, *, email: str) -> dict[str, Any]:  # noqa: N802
            w.slack_lookups.append(email)
            if email in w.slack_lookup_errors:
                raise w.slack_lookup_errors[email]
            return {"ok": True, "user": {"id": "U" + email.split("@")[0].upper()}}

        async def conversations_open(self, *, users: str) -> dict[str, Any]:
            return {"ok": True, "channel": {"id": w.im_channel}}

    class _Slack:
        def __init__(self) -> None:
            self._client = _AsyncClient()

        async def post_message(self, **kw: Any) -> Any:
            w.posted.append(kw)
            raise AssertionError("no DM may be posted on a holiday")

    import teamagent.adapters.slack_client as slack_mod

    monkeypatch.setattr(slack_mod.SlackClient, "from_env", classmethod(lambda cls, **kw: _Slack()))

    class _Scheduler:
        def schedule_reminder(self, **kw: Any) -> bool:
            w.reminders.append(kw)
            return True

        def schedule_digest(self, **kw: Any) -> bool:
            w.digest_reservations.append(kw)
            return True

    import teamagent.adapters.scheduler_client as sched_mod

    monkeypatch.setattr(
        sched_mod.SchedulerClient, "from_env", classmethod(lambda cls: _Scheduler())
    )
    return w


def _ev(event_id: str, summary: str, start: str, end: str, **kw: Any) -> CalendarEvent:
    return CalendarEvent(
        event_id=event_id, summary=summary, start=start, end=end, attendees=(), **kw
    )


def _holiday_events(day: _dt.date) -> list[CalendarEvent]:
    d = day.isoformat()
    nxt = (day + _dt.timedelta(days=1)).isoformat()
    return [
        # 9:30 の実行時点でもう始まっている → リマインドは登録しない
        _ev("e1", "朝会", f"{d}T09:00:00+09:00", f"{d}T09:30:00+09:00"),
        # これだけが登録される（開始 5 分前 = 9:55）
        _ev(
            "e2",
            "定例",
            f"{d}T10:00:00+09:00",
            f"{d}T11:00:00+09:00",
            meeting_url="https://meet.google.com/abc-defg-hij",
        ),
        # 終日は対象外
        _ev("e3", "休日", d, nxt, all_day=True),
        # 翌日の予定（events.list は返すが skill の窓で落ちる）
        _ev("e4", "翌日", f"{nxt}T10:00:00+09:00", f"{nxt}T11:00:00+09:00"),
    ]


# ── 祝日: skill.run を呼ばず・DM を送らず・リマインドだけ登録 ────────────────


@pytest.mark.parametrize(
    ("day", "holiday"),
    [
        (_D(2026, 10, 12), "スポーツの日"),
        (_D(2026, 11, 3), "文化の日"),
        (_D(2027, 1, 1), "元日"),
    ],
)
def test_holiday_skips_the_digest_but_registers_reminders(
    monkeypatch: pytest.MonkeyPatch, day: _dt.date, holiday: str
) -> None:
    """変異: 祝日の早期 return を消す → skill.run と DM が走って赤。
    リマインドの経路を消す → 登録 0 件で赤。"""
    _freeze(monkeypatch, day)
    monkeypatch.setenv("MORNING_DIGEST_HOLIDAY_SKIP", "true")
    monkeypatch.setenv("MORNING_DIGEST_REMINDERS", "1")
    w = _install(monkeypatch, [U1, U2], _holiday_events(day), linked={U1})

    assert mod.main() == 0

    assert w.run_calls == []  # skill.run を呼ばない
    assert w.delivered == [] and w.posted == []  # DM は 1 通も送らない
    assert w.gmail.queries == []  # メールにも触れない
    assert w.calendar_only_calls == [U1, U2]
    assert len(w.reminders) == 1
    rem = w.reminders[0]
    assert rem["channel"] == "D0HOLIDAY"
    assert rem["start_iso"] == f"{day.isoformat()}T10:00:00+09:00"
    assert rem["fire_at"] == _dt.datetime(day.year, day.month, day.day, 9, 55, tzinfo=_JST)
    assert rem["url"] == "https://meet.google.com/abc-defg-hij"
    (skip,) = w.log.named("morning_digest_holiday_skip")
    assert skip["reason"] == dc.SKIP_JP_HOLIDAY and skip["holiday"] == holiday
    assert (skip["users"], skip["reminded"], skip["reminders"], skip["skipped"]) == (2, 1, 1, 1)
    # 件数だけ。メールアドレスも予定のタイトルもログに出さない。
    dumped = json.dumps(w.log.events, ensure_ascii=False, default=str)
    assert "komata" not in dumped and "定例" not in dumped


def test_holiday_without_reminders_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """リマインドが OFF の環境では、祝日は予定も読まない（何もせず終わる）。"""
    _freeze(monkeypatch, _D(2026, 10, 12))
    monkeypatch.setenv("MORNING_DIGEST_HOLIDAY_SKIP", "1")
    w = _install(monkeypatch, [U1], _holiday_events(_D(2026, 10, 12)))

    assert mod.main() == 0
    assert w.run_calls == [] and w.delivered == []
    assert w.calendar_only_calls == [] and w.gcal.calls == 0 and w.reminders == []
    (skip,) = w.log.named("morning_digest_holiday_skip")
    assert skip["reminders_enabled"] is False


def test_holiday_reminders_go_only_to_a_one_to_one_dm(monkeypatch: pytest.MonkeyPatch) -> None:
    """conversations.open が DM 以外（C/G）を返したらリマインドを向けない。

    変異: ``_open_dm_channel`` の D 始まりの確認を外す → チャンネルに登録されて赤。"""
    _freeze(monkeypatch, _D(2026, 10, 12))
    monkeypatch.setenv("MORNING_DIGEST_HOLIDAY_SKIP", "1")
    monkeypatch.setenv("MORNING_DIGEST_REMINDERS", "1")
    w = _install(monkeypatch, [U1], _holiday_events(_D(2026, 10, 12)))
    w.im_channel = "C0PUBLIC"

    assert mod.main() == 0
    assert w.reminders == []
    (skip,) = w.log.named("morning_digest_holiday_skip")
    assert skip["errors"] == 1


# ── 祝日: 1 人の失敗で全体を落とさない（本番の失敗の形） ─────────────────────


def _per_user_calendar(
    monkeypatch: pytest.MonkeyPatch, w: _World, outcomes: dict[str, Any]
) -> dict[str, _CalService]:
    """人ごとの Calendar を本物の ``GCalendarClient`` で組む（偽物は service だけ）。

    skill に gcalendar を注入しない＝本番と同じ ``_gcal_for(token)`` →
    ``GCalendarClient.from_user_token`` → ``list_events`` → ``extract_events`` を通す。
    差し替えるのは refresh token から credentials を作る所だけ。
    """
    from teamagent.adapters.gcalendar_client import GCalendarClient

    services = {email: _CalService(outcome) for email, outcome in outcomes.items()}

    def _from_user_token(cls: type[GCalendarClient], token: Any) -> GCalendarClient:
        return cls(service=services[token.user_email], scopes=cls.SCOPES_READONLY)

    monkeypatch.setattr(GCalendarClient, "from_user_token", classmethod(_from_user_token))
    w.gcal = None  # type: ignore[assignment]
    return services


def _assert_no_pii(text: str) -> None:
    for leaked in (U1, U3, "komata", "linked2", "定例", "朝会"):
        assert leaked not in text


@pytest.mark.parametrize(
    "failure",
    [
        _refresh_error("invalid_grant", "Token has been expired or revoked."),
        _refresh_error("invalid_client", "The OAuth client was not found."),
        _google_http_error(
            403,
            "insufficientPermissions",
            "Request had insufficient authentication scopes.",
            "PERMISSION_DENIED",
        ),
        _google_http_error(503, "backendError", "Backend Error", "UNAVAILABLE"),
    ],
    ids=["refresh_invalid_grant", "refresh_invalid_client", "http_403", "http_503"],
)
def test_one_users_calendar_failure_does_not_stop_the_others_reminders(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: BaseException
) -> None:
    """先頭の人の予定取得が本番の失敗の形で落ちても、その人だけ error に数え、
    後ろの人のリマインドは登録し、タスクは 0 で終わる。

    変異: ``_register_holiday_reminders`` の予定取得の ``except Exception`` を外す →
    例外が ``_run_holiday`` を抜けて後ろの人が登録されず赤。error を skipped に数える → 赤。
    WARN 行のメールアドレスのマスクを外す → 赤。"""
    day = _D(2026, 10, 12)
    _freeze(monkeypatch, day)
    monkeypatch.setenv("MORNING_DIGEST_HOLIDAY_SKIP", "true")
    monkeypatch.setenv("MORNING_DIGEST_REMINDERS", "1")
    w = _install(monkeypatch, [U1, U3], linked={U1, U3})
    services = _per_user_calendar(monkeypatch, w, {U1: failure, U3: _calendar_api_json(day)})

    assert mod.main() == 0

    assert w.run_calls == [] and w.delivered == [] and w.posted == []
    assert w.calendar_only_calls == [U1, U3]  # 失敗する人が先＝後ろの人まで届くかを見る
    assert len(services[U1].calls) == 1 and len(services[U3].calls) == 1
    assert w.slack_lookups == [U3]  # 予定を取れなかった人の DM は開かない
    (rem,) = w.reminders
    assert rem["channel"] == "D0HOLIDAY"
    assert rem["start_iso"] == "2026-10-12T10:00:00+09:00"
    assert rem["fire_at"] == _dt.datetime(2026, 10, 12, 9, 55, tzinfo=_JST)
    assert rem["url"] == "https://meet.google.com/abc-defg-hij"
    (skip,) = w.log.named("morning_digest_holiday_skip")
    assert (skip["users"], skip["reminded"], skip["reminders"]) == (2, 1, 1)
    assert (skip["skipped"], skip["errors"]) == (0, 1)
    err = capsys.readouterr().err
    assert "k***@vectorinc.co.jp 祝日の予定取得失敗" in err
    assert type(failure).__name__ in err
    _assert_no_pii(err)
    _assert_no_pii(json.dumps(w.log.events, ensure_ascii=False, default=str))


def test_one_users_slack_lookup_failure_does_not_stop_the_others_reminders(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """users.lookupByEmail が本番の失敗の形（slack_sdk の ``SlackApiError``・
    users_not_found）で落ちても、その人だけ error に数え、後ろの人のリマインドは登録する。

    変異: DM を開けなかった人を skipped に数える → 赤。WARN 行のマスクを外す → 赤。"""
    from slack_sdk.errors import SlackApiError

    day = _D(2026, 10, 12)
    _freeze(monkeypatch, day)
    monkeypatch.setenv("MORNING_DIGEST_HOLIDAY_SKIP", "true")
    monkeypatch.setenv("MORNING_DIGEST_REMINDERS", "1")
    w = _install(monkeypatch, [U1, U3], linked={U1, U3})
    _per_user_calendar(monkeypatch, w, {U1: _calendar_api_json(day), U3: _calendar_api_json(day)})
    w.slack_lookup_errors[U1] = SlackApiError(
        "The request to the Slack API failed. (url: https://slack.com/api/users.lookupByEmail)",
        {"ok": False, "error": "users_not_found"},
    )

    assert mod.main() == 0

    assert w.delivered == [] and w.posted == []
    assert w.slack_lookups == [U1, U3]
    (rem,) = w.reminders  # U3 の 10:00 の予定だけ
    assert rem["channel"] == "D0HOLIDAY"
    assert rem["start_iso"] == "2026-10-12T10:00:00+09:00"
    (skip,) = w.log.named("morning_digest_holiday_skip")
    assert (skip["users"], skip["reminded"], skip["reminders"]) == (2, 1, 1)
    assert (skip["skipped"], skip["errors"]) == (0, 1)
    err = capsys.readouterr().err
    assert "lookupByEmail 失敗 k***@vectorinc.co.jp SlackApiError" in err
    _assert_no_pii(err)
    _assert_no_pii(json.dumps(w.log.events, ensure_ascii=False, default=str))


def test_company_holiday_is_skipped_like_a_national_holiday(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """会社休日（年末 12/29）も同じ扱い。理由は company_holiday。"""
    _freeze(monkeypatch, _D(2026, 12, 29))
    monkeypatch.setenv("MORNING_DIGEST_HOLIDAY_SKIP", "1")
    monkeypatch.setenv("MORNING_DIGEST_EXTRA_SKIP_DATES", "2026-12-29,2026-12-30,2026-12-31")
    w = _install(monkeypatch, [U1])

    assert mod.main() == 0
    assert w.run_calls == [] and w.delivered == []
    (skip,) = w.log.named("morning_digest_holiday_skip")
    assert skip["reason"] == dc.SKIP_COMPANY_HOLIDAY and skip["holiday"] == ""


def test_scheduled_single_run_on_a_holiday_does_not_deliver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """個人別配信の予約発火（single）も祝日は送らない（予約が残っていた場合の保険）。"""
    _freeze(monkeypatch, _D(2026, 10, 12), 8, 0)
    monkeypatch.setenv("MORNING_DIGEST_HOLIDAY_SKIP", "1")
    monkeypatch.setenv("MORNING_DIGEST_USER_REF", user_ref(U1))
    w = _install(monkeypatch, [U1, U2])

    assert mod.main() == 0
    assert w.run_calls == [] and w.delivered == []
    (skip,) = w.log.named("morning_digest_holiday_skip")
    assert skip["users"] == 1


# ── フラグ OFF（既定）: 今と同じ ─────────────────────────────────────────


def test_flag_off_delivers_on_a_holiday_exactly_as_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF なら 10/12 も配信し、走査は 3 日・入力は既定のまま・表を見ない。"""
    from teamagent.skills.morning_digest.schema import MorningDigestInput

    _freeze(monkeypatch, _D(2026, 10, 12))
    monkeypatch.setenv("MORNING_DIGEST_EXTRA_SKIP_DATES", "2026-10-12")  # OFF なら読まない
    w = _install(monkeypatch, [U1])

    assert mod.main() == 0
    assert w.run_calls == [U1] and w.delivered == [U1]
    assert w.calendar_only_calls == []
    assert w.run_inputs == [MorningDigestInput(max_drafts=3)]
    assert w.gmail.queries and "newer_than:3d " in w.gmail.queries[0]
    # 祝日・走査範囲・表の鮮度のイベントを 1 つも出さない（F0 PR-0a の実行結果の 1 行 run_done は別物）
    assert [e for e in w.log.events if e[1] != "morning_digest_run_done"] == []


def test_flag_off_never_warns_about_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF なら表を使わない＝範囲外の日でも警告しない。

    変異: ``_holiday_calendar`` が OFF でもカレンダーを返す → 警告が出て赤。"""
    _freeze(monkeypatch, _D(2028, 1, 10))
    w = _install(monkeypatch, [U1])

    assert mod.main() == 0
    assert w.delivered == [U1]
    assert w.log.named("jp_holiday_table_stale") == []


# ── 祝日明けの走査範囲 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("day", "extra", "days"),
    [
        (_D(2026, 10, 13), "", 4),  # スポーツの日の明け（前の配信日 10/9 金）
        (_D(2026, 11, 4), "", 3),  # 文化の日の明け（前の配信日 11/2 月 → 2 日 → 最低 3 日）
        (_D(2026, 10, 19), "", 3),  # ふだんの月曜は今と同じ
        (_D(2027, 1, 4), "", 4),  # 年始（前の配信日 12/31）
        (_D(2027, 1, 4), "2026-12-29,2026-12-30,2026-12-31", 7),  # 会社休日つき（12/28 から）
    ],
)
def test_day_after_a_holiday_scans_back_to_the_previous_delivery_day(
    monkeypatch: pytest.MonkeyPatch, day: _dt.date, extra: str, days: int
) -> None:
    """本物の skill.run が組み立てる Gmail の検索式で確かめる。

    変異: 走査範囲の拡張を消す → 10/13 が 3 日のままで赤。"""
    _freeze(monkeypatch, day)
    monkeypatch.setenv("MORNING_DIGEST_HOLIDAY_SKIP", "1")
    if extra:
        monkeypatch.setenv("MORNING_DIGEST_EXTRA_SKIP_DATES", extra)
    w = _install(monkeypatch, [U1])

    assert mod.main() == 0
    assert w.run_calls == [U1] and w.delivered == [U1]
    assert len(w.gmail.queries) == 1
    assert f"newer_than:{days}d " in w.gmail.queries[0]
    (lb,) = w.log.named("morning_digest_lookback")
    assert lb["lookback_days"] == days and lb["day"] == day.isoformat()


def test_day_after_a_holiday_keeps_three_days_when_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze(monkeypatch, _D(2026, 10, 13))
    w = _install(monkeypatch, [U1])

    assert mod.main() == 0
    assert "newer_than:3d " in w.gmail.queries[0]


# ── 表の範囲外・期限 ────────────────────────────────────────────────────


def test_out_of_range_day_keeps_delivering_and_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """2028-01-10 は実際には成人の日だが表の範囲外。配信を止めずに警告する。

    変異: 範囲外を祝日扱いにする（止める側に倒す）→ 配信されず赤。"""
    _freeze(monkeypatch, _D(2028, 1, 10))
    monkeypatch.setenv("MORNING_DIGEST_HOLIDAY_SKIP", "1")
    w = _install(monkeypatch, [U1])

    assert mod.main() == 0
    assert w.run_calls == [U1] and w.delivered == [U1]
    (stale,) = w.log.named("jp_holiday_table_stale")
    assert stale["state"] == "expired" and stale["coverage_end"] == "2027-12-31"


@pytest.mark.parametrize(
    ("day", "warned"),
    [
        (_D(2027, 10, 29), False),  # 期限まで 63 日
        (_D(2027, 11, 1), True),  # 期限の 60 日前（月曜）
        (_D(2027, 12, 30), True),
    ],
)
def test_table_expiry_is_warned_from_sixty_days_before(
    monkeypatch: pytest.MonkeyPatch, day: _dt.date, warned: bool
) -> None:
    """期限の 60 日前から毎朝警告する（配信は続ける）。"""
    _freeze(monkeypatch, day)
    monkeypatch.setenv("MORNING_DIGEST_HOLIDAY_SKIP", "1")
    w = _install(monkeypatch, [U1])

    assert mod.main() == 0
    assert w.delivered == [U1]
    stale = w.log.named("jp_holiday_table_stale")
    assert bool(stale) is warned
    if warned:
        assert stale[0]["state"] == "expiring"
        assert stale[0]["days_left"] == (_D(2027, 12, 31) - day).days


def test_stale_warning_is_also_raised_on_a_holiday(monkeypatch: pytest.MonkeyPatch) -> None:
    """祝日の実行でも鮮度の警告は出す（2027-11-03 文化の日・期限まで 58 日）。"""
    _freeze(monkeypatch, _D(2027, 11, 3))
    monkeypatch.setenv("MORNING_DIGEST_HOLIDAY_SKIP", "1")
    w = _install(monkeypatch, [U1])

    assert mod.main() == 0
    assert w.delivered == []
    assert [s["state"] for s in w.log.named("jp_holiday_table_stale")] == ["expiring"]


# ── planner（個人別配信の 04:00） ───────────────────────────────────────


class _PlanEv:
    def __init__(self, start: str) -> None:
        self.start = start
        self.all_day = False


class _PlanCal:
    def __init__(self, start: str) -> None:
        self._start = start

    def list_events(self, request_id: str, **kwargs: Any) -> list[_PlanEv]:
        return [_PlanEv(self._start)]


class _NoticeStore:
    def __init__(self) -> None:
        self.claims: list[str] = []

    def claim(self, email: str, day: _dt.date, *, kind: str, request_id: str) -> bool:
        self.claims.append(email)
        return True


@pytest.mark.parametrize(("skip_on", "reserved"), [(True, 0), (False, 1)])
def test_planner_makes_no_reservation_on_a_holiday(
    monkeypatch: pytest.MonkeyPatch, skip_on: bool, reserved: int
) -> None:
    """祝日は配信予約も、月曜の未連携のお知らせ（DM）も作らない。OFF なら今と同じ。

    変異: planner の祝日の確認を消す → 10/12 に予約が作られ、お知らせも届いて赤。"""
    _freeze(monkeypatch, _D(2026, 10, 12), 4, 0)
    monkeypatch.setenv("MORNING_DIGEST_PERSONALIZED", "1")
    monkeypatch.setenv("MORNING_DIGEST_BRIEF", "1")
    if skip_on:
        monkeypatch.setenv("MORNING_DIGEST_HOLIDAY_SKIP", "1")
    w = _install(monkeypatch, [U1, U2])
    notices = _NoticeStore()
    monkeypatch.setattr(mod, "_notice_store", lambda: notices)

    class _Reserve:  # planner の予約印（0031）。祝日でない日は印を付けて予約する
        def reserve(self, *_: Any, **__: Any) -> bool:
            return True

        def release(self, *_: Any, **__: Any) -> bool:
            return True

    monkeypatch.setattr(mod, "_delivery_store", lambda: _Reserve())
    cals = {U1: _PlanCal("2026-10-12T09:00:00+09:00"), U2: None}  # 8:30 に送る予約
    monkeypatch.setattr(mod, "_read_only_calendar", lambda store, email: cals[email])

    assert mod.run_planner([U1, U2]) == 0
    assert len(w.digest_reservations) == reserved
    # U2 は未連携・10/12 は月曜: OFF なら週 1 回のお知らせ DM が出る、ON なら出ない。
    assert w.delivered == ([] if skip_on else [U2])


# ── terraform との対（イベント名・env 名・既定値） ───────────────────────


def _tf_block(header: str) -> str:
    """``resource "kind" "name"`` / ``variable "name"`` の 1 ブロックを取り出す。"""
    text = TF_PATH.read_text(encoding="utf-8")
    start = text.index(header + " {")
    end = text.index("\n}\n", start)
    return text[start : end + 3]


def _tf_resource(kind: str, name: str) -> str:
    return _tf_block(f'resource "{kind}" "{name}"')


def _tf_variable(name: str) -> str:
    return _tf_block(f'variable "{name}"')


def test_the_metric_filter_matches_the_event_the_runner_actually_emits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """tf の pattern のイベント名を、本物の JSON ログ出力（STRUCTLOG_FORMAT=json）で確かめる。

    変異: runner のイベント名を変える → pattern と一致せず赤（警報が永久に鳴らない事故）。"""
    import structlog

    from teamagent.observability import logging_config

    block = _tf_resource("aws_cloudwatch_log_metric_filter", "morning_digest_holiday_table_stale")
    m = re.search(r'\$\.event = \\"([a-z_]+)\\"', block)
    assert m is not None
    tf_event = m.group(1)
    assert "aws_cloudwatch_log_group.morning_digest.name" in block
    alarm = _tf_resource("aws_cloudwatch_metric_alarm", "morning_digest_holiday_table_stale")
    assert 'metric_name         = "MorningDigestHolidayTableStale"' in alarm
    assert "threshold           = 1" in alarm
    assert "alarm_actions      = [aws_sns_topic.alarms.arn]" in alarm

    monkeypatch.setenv("STRUCTLOG_FORMAT", "json")
    logging_config._reset_for_tests()
    try:
        # tests/scripts/conftest.py が configure_logging を no-op にしているので、読み込み時に
        # 掴んだ本物を呼ぶ（前後で _reset_for_tests するので後続のテストは汚さない）。
        _real_configure_logging(force=True)
        monkeypatch.setattr(mod, "logger", structlog.get_logger("holiday_contract"))
        capsys.readouterr()
        assert mod._warn_if_holiday_table_stale(_D(2027, 11, 1)) is True
        out = capsys.readouterr().out
    finally:
        logging_config._reset_for_tests()
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert [rec["event"] for rec in lines] == [tf_event]
    assert lines[0]["level"] == "warning" and lines[0]["days_left"] == 60


def test_terraform_passes_the_flags_with_safe_defaults() -> None:
    """env 名はコードが読む名前と同じ・既定は OFF／空（TD に足しても今と同じ）。"""
    text = TF_PATH.read_text(encoding="utf-8")
    assert (
        f'{{ name = "{dc.HOLIDAY_SKIP_ENV}", value = var.morning_digest_holiday_skip ? "true" : "false" }}'
        in text
    )
    assert (
        f'{{ name = "{dc.EXTRA_SKIP_DATES_ENV}", value = var.morning_digest_extra_skip_dates }}'
        in text
    )
    skip_var = _tf_variable("morning_digest_holiday_skip")
    assert "default     = false" in skip_var and "activation 版 tfvars" in skip_var
    extra_var = _tf_variable("morning_digest_extra_skip_dates")
    assert 'default     = ""' in extra_var and "activation 版 tfvars" in extra_var
