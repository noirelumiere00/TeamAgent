"""毎朝の配信が本人の設定（digest_preferences）どおりに変わるか — runner 側。

scripts/ は package でないため importlib でロードする（既存テストと同流儀）。
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from teamagent.skills.morning_digest import calendar_window as calwin
from teamagent.skills.morning_digest.preferences import DigestPreferences
from teamagent.skills.morning_digest.schema import (
    CalendarEventItem,
    MailDigestItem,
    MorningDigestInput,
    MorningDigestOutput,
    SlackUnreadItem,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"


def _load() -> Any:
    name = "run_morning_digest_prefs_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()

USER = "komata@vectorinc.co.jp"
MON = _dt.date(2026, 10, 5)
TUE = MON + _dt.timedelta(days=1)


@pytest.fixture(autouse=True)
def _freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        calwin, "now_jst", lambda: _dt.datetime(2026, 10, 5, 8, 0, tzinfo=calwin.JST)
    )


class _PrefStore:
    """本番ストアと同じ口（get は (dict|None, version)・障害は例外）。"""

    def __init__(self, row: dict[str, Any] | None = None, *, fail: bool = False) -> None:
        self.row, self.fail = row, fail
        self.reads: list[str] = []

    def get(self, email: str, *, request_id: str) -> tuple[dict[str, Any] | None, int]:
        self.reads.append(email)
        if self.fail:
            raise RuntimeError('relation "digest_preferences" does not exist')
        return self.row, 0 if self.row is None else 1


class _Skill:
    def __init__(self, digest: MorningDigestOutput | None = None) -> None:
        self.inputs: list[Any] = []
        self.digest = digest or MorningDigestOutput(user_email_masked="k***@vectorinc.co.jp")

    def run(self, inp: Any, ctx: Any) -> MorningDigestOutput:
        self.inputs.append(inp)
        return self.digest


class _Claims:
    def __init__(self) -> None:
        self.claims: list[str] = []

    def claim(self, email: str, day: _dt.date, *, origin: str, request_id: str) -> bool:
        self.claims.append(email)
        return True

    def release(self, email: str, day: _dt.date, *, request_id: str) -> bool:
        return True


def _patch_delivery(monkeypatch: pytest.MonkeyPatch) -> list[list[dict[str, Any]]]:
    sent: list[list[dict[str, Any]]] = []

    async def _fake(email: str, text: str, blocks: Any) -> tuple[bool, str | None]:
        sent.append(blocks)
        return (True, "D001")

    monkeypatch.setattr(mod, "_deliver_to_slack", _fake)
    return sent


# --- 止める / 休む / 曜日 --------------------------------------------------


@pytest.mark.parametrize(
    ("row", "day", "reason"),
    [
        ({"delivery": False}, MON, "pref_off"),
        ({"paused_until": "2026-10-06"}, TUE, "pref_paused"),
        ({"weekdays": [0]}, TUE, "pref_weekday"),
    ],
)
def test_user_who_opted_out_is_skipped_before_claim_and_skill(
    monkeypatch: pytest.MonkeyPatch, row: dict[str, Any], day: _dt.date, reason: str
) -> None:
    """止めた人には送らない。配信権も取らず、skill（Gmail 走査・下書き・Bedrock）も走らせない。

    変異: _process_user の skip_reason 分岐を外すと配信されて赤。
    """
    sent = _patch_delivery(monkeypatch)
    skill, claims, outcomes = _Skill(), _Claims(), []
    result = mod._process_user(
        skill,
        MorningDigestInput(),
        USER,
        store=claims,
        day=day,
        sink=outcomes,
        prefs_store=_PrefStore(row),
    )
    assert result == "skipped"
    assert [o.reason for o in outcomes] == [reason]
    assert skill.inputs == [] and claims.claims == [] and sent == []


def test_other_days_are_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _patch_delivery(monkeypatch)
    result = mod._process_user(
        _Skill(), MorningDigestInput(), USER, day=MON, prefs_store=_PrefStore({"weekdays": [0]})
    )
    assert result == "delivered" and len(sent) == 1


def test_unreadable_preferences_fall_back_to_normal_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """表が無い・DB 障害 → 既定で配信（止める側に倒すと全員に届かない）。"""
    sent = _patch_delivery(monkeypatch)
    store = _PrefStore(fail=True)
    result = mod._process_user(_Skill(), MorningDigestInput(), USER, day=MON, prefs_store=store)
    assert result == "delivered" and len(sent) == 1 and store.reads == [USER]


def test_admin_report_counts_opted_out_users_without_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_delivery(monkeypatch)
    outcomes: list[Any] = []
    for row in ({"delivery": False}, {"weekdays": [1]}, None):
        mod._process_user(
            _Skill(),
            MorningDigestInput(),
            USER,
            day=MON,
            sink=outcomes,
            prefs_store=_PrefStore(row),
        )
    text, _problem = mod._format_admin_report(outcomes, day=MON, users=3)
    assert "・本人設定で停止 2" in text.split("\n")[0]


# --- 下書き ------------------------------------------------------------------


def test_auto_drafts_off_sets_max_drafts_zero_for_that_user_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_delivery(monkeypatch)
    shared = MorningDigestInput(max_drafts=5)
    skill = _Skill()
    mod._process_user(skill, shared, USER, day=MON, prefs_store=_PrefStore({"auto_drafts": False}))
    mod._process_user(skill, shared, USER, day=MON, prefs_store=_PrefStore(None))
    assert [i.max_drafts for i in skill.inputs] == [0, 5]
    assert shared.max_drafts == 5  # 共有の入力は書き換えない


# --- 欄と件数（compact 描画）-------------------------------------------------


def _mail(i: int, *, high: bool) -> MailDigestItem:
    return MailDigestItem(
        counterpart_masked=f"u{i}***@ex.com",
        counterpart_display=f"担当{i}",
        subject_scrubbed=f"件名{i}",
        subject_display=f"件名{i}",
        importance="high" if high else "medium",
        to_self=high,
        is_unread=not high,
        summary="要約",
    )


def _digest(n_high: int = 3, n_unread: int = 3, n_slack: int = 2, n_cal: int = 3) -> Any:
    return MorningDigestOutput(
        user_email_masked="k***@vectorinc.co.jp",
        mail_digest=[_mail(i, high=True) for i in range(n_high)]
        + [_mail(100 + i, high=False) for i in range(n_unread)],
        calendar_events=[
            CalendarEventItem(summary_display=f"予定{i}", start_at="2026-10-05T11:00:00+09:00")
            for i in range(n_cal)
        ],
        calendar_date="2026-10-05",
        slack_unread_scanned=True,
        slack_unread=[
            SlackUnreadItem(
                channel_id="C08CHAN0001",
                channel_kind="channel",
                channel_name_display=f"ch-{i}",
                excerpt_display="確認お願いします",
                occurred_at="2026-10-04T09:00:00+09:00",
                permalink=f"https://vector.slack.com/archives/C08CHAN0001/p{i}",
            )
            for i in range(n_slack)
        ],
        ack_all_token="tok",
    )


def _dump(blocks: list[dict[str, Any]]) -> str:
    return json.dumps(blocks, ensure_ascii=False)


def test_default_preferences_render_byte_identical() -> None:
    d = _digest()
    assert mod._format_block_kit_compact(d, USER) == mod._format_block_kit_compact(
        d, USER, DigestPreferences()
    )


def test_hidden_sections_disappear_with_their_header_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """変異: show_slack / show_cal の分岐を外すと、消したはずの欄が出て赤。"""
    prefs = DigestPreferences(hidden_sections=("slack", "calendar"))
    text, blocks = mod._format_block_kit_compact(_digest(), USER, prefs)
    body = _dump(blocks)
    assert "Slack 返信漏れ" not in body and "の予定" not in body
    assert "💬" not in blocks[0]["text"]["text"] and "📅" not in blocks[0]["text"]["text"]
    assert "Slack" not in text and "予定" not in text
    assert "要返信" in body and "未確認" in body
    assert "あなたの設定で表示を変えています" in body


def test_limits_cap_each_section(monkeypatch: pytest.MonkeyPatch) -> None:
    prefs = DigestPreferences(limits={"reply": 1, "unread": 2, "calendar": 1})
    _text, blocks = mod._format_block_kit_compact(_digest(), USER, prefs)
    body = _dump(blocks)
    assert "*件名0*" in body and "*件名1*" not in body  # 要返信は 1 件だけ
    assert "〈他2件〉" in body  # 要返信の残り
    assert "*件名100*" in body and "*件名101*" in body and "*件名102*" not in body
    assert "予定0" in body and "予定1" not in body


def test_ack_all_is_withheld_when_hidden_sections_have_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """見ていない項目まで「全部確認した」で消さない。

    変異: hidden_has_items の判定を外すと、ボタンが残って赤。
    """
    monkeypatch.setenv("MORNING_DIGEST_ACK_BUTTON", "1")
    shown = _dump(mod._format_block_kit_compact(_digest(), USER)[1])
    assert "全部確認した" in shown
    hidden = DigestPreferences(hidden_sections=("unread",))
    assert "全部確認した" not in _dump(mod._format_block_kit_compact(_digest(), USER, hidden)[1])
    # 消した欄が空なら残す
    empty = _digest(n_unread=0)
    assert "全部確認した" in _dump(mod._format_block_kit_compact(empty, USER, hidden)[1])


def test_hiding_both_mail_sections_drops_the_no_mail_line() -> None:
    prefs = DigestPreferences(hidden_sections=("reply", "unread"))
    body = _dump(mod._format_block_kit_compact(_digest(0, 0), USER, prefs)[1])
    assert "新着なし" not in body


def test_hidden_unread_with_items_never_says_no_new_mail() -> None:
    """未確認だけ消した人に、未確認が残っているのに「新着なし」と書かない。"""
    prefs = DigestPreferences(hidden_sections=("unread",))
    body = _dump(mod._format_block_kit_compact(_digest(n_high=0, n_unread=2), USER, prefs)[1])
    assert "新着なし" not in body


def test_layout_prefs_force_compact_even_when_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MORNING_DIGEST_COMPACT", raising=False)
    sent = _patch_delivery(monkeypatch)
    mod._process_user(
        _Skill(_digest()),
        MorningDigestInput(),
        USER,
        day=MON,
        prefs_store=_PrefStore({"hidden_sections": ["slack"]}),
    )
    assert "Slack 返信漏れ" not in _dump(sent[0])


# --- 直前リマインド --------------------------------------------------------


class _Scheduler:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def schedule_reminder(self, **kw: Any) -> bool:
        self.calls.append(kw)
        return True


def _patch_scheduler(monkeypatch: pytest.MonkeyPatch) -> _Scheduler:
    import teamagent.adapters.scheduler_client as sc

    sched = _Scheduler()
    monkeypatch.setattr(sc.SchedulerClient, "from_env", classmethod(lambda cls: sched))
    return sched


def _events_digest() -> MorningDigestOutput:
    return MorningDigestOutput(
        user_email_masked="k***@vectorinc.co.jp",
        calendar_events=[
            CalendarEventItem(
                summary_display="10:30 タスク確認（公式LINE）",
                start_at="2026-10-05T10:30:00+09:00",
            ),
            CalendarEventItem(
                summary_display="クライアント定例", start_at="2026-10-05T14:00:00+09:00"
            ),
        ],
    )


def test_skip_words_drop_matching_reminders(monkeypatch: pytest.MonkeyPatch) -> None:
    """「タスク」を含む予定だけ登録しない（依頼の発端のケース）。

    変異: reminder_allowed の確認を外すと 2 件登録されて赤。
    """
    sched = _patch_scheduler(monkeypatch)
    prefs = DigestPreferences(reminder_skip_keywords=("ﾀｽｸ",))
    assert mod._schedule_event_reminders(_events_digest(), "D0", prefs) == 1
    assert [c["title"] for c in sched.calls] == ["クライアント定例"]


def test_reminders_off_registers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    sched = _patch_scheduler(monkeypatch)
    prefs = DigestPreferences(reminders=False)
    assert mod._schedule_event_reminders(_events_digest(), "D0", prefs) == 0
    assert sched.calls == []


def test_lead_minutes_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMINDER_LEAD_MINUTES", "5")
    sched = _patch_scheduler(monkeypatch)
    prefs = DigestPreferences(reminder_lead_minutes=15)
    mod._schedule_event_reminders(_events_digest(), "D0", prefs)
    fires = sorted(c["fire_at"] for c in sched.calls)
    assert fires[0] == _dt.datetime(2026, 10, 5, 10, 15, tzinfo=calwin.JST)


def test_default_prefs_keep_every_reminder(monkeypatch: pytest.MonkeyPatch) -> None:
    sched = _patch_scheduler(monkeypatch)
    assert mod._schedule_event_reminders(_events_digest(), "D0") == 2
    assert len(sched.calls) == 2


def test_holiday_path_respects_reminders_off() -> None:
    class _NeverSkill:
        def collect_calendar_events(self, *_a: Any) -> Any:
            raise AssertionError("リマインドを止めた人の予定は取りに行かない")

    state, n = mod._register_holiday_reminders(
        _NeverSkill(), MorningDigestInput(), USER, DigestPreferences(reminders=False)
    )
    assert (state, n) == ("skipped", 0)


# --- 読み込み口 --------------------------------------------------------------


def test_store_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MORNING_DIGEST_PREFERENCES", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/db")
    assert mod._preferences_store() is None
    monkeypatch.setenv("MORNING_DIGEST_PREFERENCES", "true")
    assert mod._preferences_store() is not None
    monkeypatch.delenv("DATABASE_URL")
    assert mod._preferences_store() is None


def test_planner_does_not_reserve_for_opted_out_users(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MORNING_DIGEST_PERSONALIZED", "1")
    monkeypatch.setenv("MORNING_DIGEST_DATE", MON.isoformat())
    import teamagent.adapters.scheduler_client as sc

    class _S:
        def schedule_digest(self, **kw: Any) -> bool:
            raise AssertionError("止めた人の配信予約は作らない")

    monkeypatch.setattr(sc.SchedulerClient, "from_env", classmethod(lambda cls: _S()))
    monkeypatch.setattr(mod, "_build_token_store", lambda: object())
    monkeypatch.setattr(mod, "_holiday_calendar", lambda: None)
    monkeypatch.setattr(mod, "_preferences_store", lambda: _PrefStore({"delivery": False}))
    monkeypatch.setattr(
        mod,
        "_read_only_calendar",
        lambda *_a: (_ for _ in ()).throw(AssertionError("カレンダーも読まない")),
    )
    assert mod.run_planner([USER]) == 0
