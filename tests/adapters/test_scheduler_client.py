"""SchedulerClient＋runner リマインド登録＋Lambda handler（v0.3 Task5）のテスト。

外部 I/O 無し（boto3/Slack/urllib すべてフェイク）。検証主眼:
at() 式と DELETE・冪等（Conflict→成功）・fail-open・payload に PII が無いこと・
runner の対象選定（終日/過去/直近すぎ skip・flag OFF で不発）・handler の通知文。
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.scheduler_client import SchedulerClient, reminder_schedule_name

_JST = _dt.timezone(_dt.timedelta(hours=9))
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class _FakeBoto:
    def __init__(self, raise_name: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raise = raise_name

    def create_schedule(self, **kw: Any) -> None:
        self.calls.append(kw)
        if self._raise:
            exc = type(self._raise, (Exception,), {})()
            raise exc


def _client(fake: _FakeBoto) -> SchedulerClient:
    return SchedulerClient(
        group_name="teamagent-dev-reminders",
        queue_arn="arn:aws:sqs:ap-northeast-1:1:teamagent-dev-reminders.fifo",
        role_arn="arn:aws:iam::1:role/rem-scheduler",
        client=fake,
    )


def test_schedule_reminder_builds_one_time_delete_schedule() -> None:
    fake = _FakeBoto()
    ok = _client(fake).schedule_reminder(
        channel="D123",
        start_iso="2026-07-15T14:00:00+09:00",
        fire_at=_dt.datetime(2026, 7, 15, 13, 55, tzinfo=_JST),
        url="https://meet/x",
        request_id="r1",
    )
    assert ok
    call = fake.calls[0]
    assert call["ScheduleExpression"] == "at(2026-07-15T13:55:00)"
    assert call["ScheduleExpressionTimezone"] == "Asia/Tokyo"
    assert call["ActionAfterCompletion"] == "DELETE"  # 発火後に自動削除（指示書どおり）
    assert call["Name"] == reminder_schedule_name("D123", "2026-07-15T14:00:00+09:00")
    payload = json.loads(call["Target"]["Input"])
    assert payload == {"v": 1, "channel": "D123", "start_hm": "14:00", "url": "https://meet/x"}
    # PII（予定タイトル・email）が payload に無いこと（G3）。
    assert "summary" not in payload and "email" not in payload
    assert call["Target"]["SqsParameters"]["MessageGroupId"] == "D123"


def test_conflict_is_idempotent_success() -> None:
    fake = _FakeBoto(raise_name="ConflictException")
    ok = _client(fake).schedule_reminder(
        channel="D123",
        start_iso="2026-07-15T14:00:00+09:00",
        fire_at=_dt.datetime(2026, 7, 15, 13, 55, tzinfo=_JST),
        url="",
        request_id="r1",
    )
    assert ok  # 既存＝同じ予定に登録済み → 冪等成功（朝バッチ再実行で二重登録しない）


def test_other_failure_is_fail_open() -> None:
    fake = _FakeBoto(raise_name="AccessDeniedException")
    ok = _client(fake).schedule_reminder(
        channel="D123",
        start_iso="2026-07-15T14:00:00+09:00",
        fire_at=_dt.datetime(2026, 7, 15, 13, 55, tzinfo=_JST),
        url="",
        request_id="r1",
    )
    assert not ok  # 失敗は False（呼び出し側はダイジェスト配信を止めない）


def test_missing_env_raises_value_error() -> None:
    with pytest.raises(ValueError):
        SchedulerClient(group_name="", queue_arn="x", role_arn="y")


# ── runner の対象選定（_schedule_event_reminders） ──────────────────────────


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location(
        "run_md_reminders_under_test", PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_md_reminders_under_test"] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


def _digest(events: list[dict[str, str]]) -> Any:
    from teamagent.skills.morning_digest.schema import CalendarEventItem, MorningDigestOutput

    return MorningDigestOutput(
        user_email_masked="m***@x",
        calendar_events=[CalendarEventItem(**e) for e in events],
    )


def test_runner_schedules_only_future_timed_events(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduled: list[dict[str, Any]] = []

    class _FakeScheduler:
        @classmethod
        def from_env(cls) -> Any:
            return cls()

        def schedule_reminder(self, **kw: Any) -> bool:
            scheduled.append(kw)
            return True

    import teamagent.adapters.scheduler_client as sc_mod

    monkeypatch.setattr(sc_mod, "SchedulerClient", _FakeScheduler)

    now = _dt.datetime.now(tz=_JST)
    future = (now + _dt.timedelta(hours=3)).replace(microsecond=0)
    soon = (now + _dt.timedelta(minutes=3)).replace(microsecond=0)  # lead 5分に間に合わない
    past = (now - _dt.timedelta(hours=1)).replace(microsecond=0)
    d = _digest(
        [
            {"summary_scrubbed": "a", "start_at": future.isoformat(), "meeting_url": "https://m/1"},
            {"summary_scrubbed": "b", "start_at": soon.isoformat()},
            {"summary_scrubbed": "c", "start_at": past.isoformat()},
            {"summary_scrubbed": "d", "start_at": "2026-07-20"},  # 終日
        ]
    )
    n = runner._schedule_event_reminders(d, "D_IM")
    assert n == 1 and len(scheduled) == 1
    kw = scheduled[0]
    assert kw["channel"] == "D_IM" and kw["url"] == "https://m/1"
    assert kw["fire_at"] == future - _dt.timedelta(minutes=5)  # 既定 5 分前


def test_runner_reminders_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MORNING_DIGEST_REMINDERS", raising=False)
    assert runner._reminders_enabled() is False
    monkeypatch.setenv("MORNING_DIGEST_REMINDERS", "1")
    assert runner._reminders_enabled() is True


# ── Lambda handler ──────────────────────────────────────────────────────────


def _load_handler() -> Any:
    spec = importlib.util.spec_from_file_location(
        "reminder_notify_under_test",
        PROJECT_ROOT / "infra" / "terraform" / "lambda" / "reminder_notify" / "handler.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["reminder_notify_under_test"] = module
    spec.loader.exec_module(module)
    return module


def test_lambda_handler_posts_reminder(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _load_handler()
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")  # secretsmanager を迂回（テスト用経路）
    posted: list[tuple[str, str]] = []

    def _fake_post(channel: str, text: str) -> None:
        posted.append((channel, text))

    monkeypatch.setattr(h, "_post_message", _fake_post)
    event = {
        "Records": [
            {"body": json.dumps({"v": 1, "channel": "D1", "start_hm": "14:00", "url": "https://m"})}
        ]
    }
    out = h.handler(event, None)
    assert out["ok"] and posted == [
        ("D1", "🔔 まもなく予定があります（14:00〜）\n<https://m|開く>")
    ]


def test_lambda_handler_skips_invalid_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _load_handler()
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    called: list[Any] = []
    monkeypatch.setattr(h, "_post_message", lambda *a: called.append(a))
    out = h.handler({"Records": [{"body": json.dumps({"v": 1})}]}, None)  # channel 無し
    assert out["ok"] and called == []  # リトライしても直らない payload は skip（DLQ を汚さない）
