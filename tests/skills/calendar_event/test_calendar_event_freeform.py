"""calendar_event の自由文経路（freeform）テスト。

背景（2026-08-18 本番 QA）: DM の「カレンダーに追加して」に対し bot は
「このツールはボタン専用です」と**正しく断った**。設計の穴なので入口を足した。

検証主眼:
  - ボタン経路は 1 バイトも変わらない（回帰）。event_token があれば自由文引数は無視。
  - 自由文の入力検証（ISO 必須・所要 8h 以内・過去1日〜未来366日）が fail-closed。
  - **招待は物理的に不可**（入力にも insert_event 呼び出しにも attendees が存在しない）。
  - **他人のカレンダーは指定不可**（calendar_id 相当の引数が存在しない・primary 固定）。
  - 登録後の message に htmlLink（原本リンク）が載る。
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

import pytest

from teamagent.adapters.gcalendar_client import DuplicateEventError, InsertedEvent
from teamagent.skills.base import SkillContext
from teamagent.skills.calendar_event.schema import CalendarEventInput
from teamagent.skills.calendar_event.skill import CalendarEventSkill
from teamagent.skills.morning_digest.event_token import encode_event_token

ME = "me@vectorinc.co.jp"
_CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"
_MAIL_SECRET = "calendar-freeform-test-secret-" + "m" * 32
_JST = _dt.timezone(_dt.timedelta(hours=9))
NOW = _dt.datetime(2026, 8, 18, 10, 0, tzinfo=_JST)


@pytest.fixture(autouse=True)
def _hmac_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MAIL_ACTION_TTL_S", "DATABASE_URL", "SLACK_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_SECRET)


@dataclass
class _Tok:
    scopes: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.modify", _CAL_SCOPE)


class _Store:
    def __init__(self, tok: Any) -> None:
        self._tok = tok

    def get(self, email: str) -> Any:
        return self._tok


@dataclass
class _FakeGCal:
    calls: list[dict[str, Any]] = field(default_factory=list)
    raise_dup: bool = False

    def insert_event(self, request_id: str, **kw: Any) -> InsertedEvent:
        self.calls.append(kw)
        if self.raise_dup:
            raise DuplicateEventError(kw.get("event_id", ""))
        return InsertedEvent(
            event_id=str(kw.get("event_id") or "x"),
            html_link="https://calendar.google.com/event?eid=abc",
            summary=str(kw.get("summary", "")),
            start=str(kw.get("start_iso", "")),
            end=str(kw.get("end_iso", "")),
            status="confirmed",
        )


def _skill(gcal: _FakeGCal | None = None, tok: Any = "default") -> tuple[Any, _FakeGCal]:
    gcal = gcal or _FakeGCal()
    token = _Tok() if tok == "default" else tok
    skill = CalendarEventSkill(
        token_store=_Store(token),
        gcalendar_factory=lambda _t: gcal,
        now_factory=lambda: NOW,
    )
    return skill, gcal


def _run(skill: Any, **kw: Any) -> Any:
    return skill.run(
        CalendarEventInput(**kw),
        SkillContext(request_id="r", metadata={"user_email": ME}),
    )


# ── 正常系 ────────────────────────────────────────────────────────────────


def test_freeform_creates_event_on_own_calendar() -> None:
    skill, gcal = _skill()

    out = _run(skill, title="A社と打合せ", start="2026-08-20T15:00:00+09:00")

    assert out.created and not out.error
    call = gcal.calls[0]
    assert call["summary"] == "A社と打合せ"
    assert call["start_iso"] == "2026-08-20T15:00:00+09:00"
    # end 省略時は 60 分
    assert call["end_iso"] == "2026-08-20T16:00:00+09:00"
    # 招待・他人のカレンダーは **引数として存在しない**（構造的な封鎖）
    assert "attendees" not in call
    assert "calendar_id" not in call and "calendarId" not in call


def test_freeform_message_carries_the_original_link() -> None:
    """出典 URL 方針: 登録した予定の原本リンクを message 本文に必ず載せる。"""
    skill, _ = _skill()

    out = _run(skill, title="A社と打合せ", start="2026-08-20T15:00:00+09:00")

    assert "https://calendar.google.com/event?eid=abc" in out.message
    assert out.event_url == "https://calendar.google.com/event?eid=abc"


def test_freeform_accepts_naive_datetime_as_jst() -> None:
    skill, gcal = _skill()

    out = _run(skill, title="打合せ", start="2026-08-20 15:00", end="2026-08-20 16:30")

    assert out.created
    assert gcal.calls[0]["start_iso"] == "2026-08-20T15:00:00+09:00"
    assert gcal.calls[0]["end_iso"] == "2026-08-20T16:30:00+09:00"


def test_freeform_passes_location_through() -> None:
    skill, gcal = _skill()

    _run(skill, title="打合せ", start="2026-08-20T15:00+09:00", location="本社 3F 会議室")

    assert gcal.calls[0]["location"] == "本社 3F 会議室"


def test_freeform_double_send_is_idempotent() -> None:
    skill, _ = _skill(_FakeGCal(raise_dup=True))

    out = _run(skill, title="打合せ", start="2026-08-20T15:00+09:00")

    assert out.already and not out.created


def test_freeform_same_slot_different_title_gets_distinct_id() -> None:
    """同じ枠でも別件は登録できる（冪等キーにタイトルを混ぜている）。"""
    skill, gcal = _skill()

    _run(skill, title="A社と打合せ", start="2026-08-20T15:00+09:00")
    _run(skill, title="B社と打合せ", start="2026-08-20T15:00+09:00")

    assert gcal.calls[0]["event_id"] != gcal.calls[1]["event_id"]


# ── ガード（fail-closed）───────────────────────────────────────────────────


def test_missing_user_email_is_fail_closed() -> None:
    skill, gcal = _skill()

    with pytest.raises(PermissionError):
        skill.run(
            CalendarEventInput(title="打合せ", start="2026-08-20T15:00+09:00"),
            SkillContext(request_id="r", metadata={}),
        )
    assert gcal.calls == []


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"title": "", "start": "2026-08-20T15:00+09:00"}, "no_input"),
        ({"title": "打合せ", "start": ""}, "no_input"),
        ({"title": "打合せ", "start": "来週あたり"}, "bad_datetime"),
        ({"title": "打合せ", "start": "2026-08-20"}, "bad_datetime"),  # 日付だけ＝時刻未確定
        (
            {"title": "打合せ", "start": "2026-08-20T15:00+09:00", "end": "とりあえず"},
            "bad_datetime",
        ),
        # 所要ガード（0 分以下 / 8 時間超）
        (
            {
                "title": "打合せ",
                "start": "2026-08-20T15:00+09:00",
                "end": "2026-08-20T15:00+09:00",
            },
            "bad_duration",
        ),
        (
            {
                "title": "打合せ",
                "start": "2026-08-20T15:00+09:00",
                "end": "2026-08-20T14:00+09:00",
            },
            "bad_duration",
        ),
        (
            {
                "title": "合宿",
                "start": "2026-08-20T09:00+09:00",
                "end": "2026-08-20T18:00+09:00",
            },
            "bad_duration",
        ),
        # 日付レンジ（過去 1 日より前 / 未来 366 日より先）
        ({"title": "打合せ", "start": "2026-08-16T15:00+09:00"}, "out_of_range"),
        ({"title": "打合せ", "start": "2028-01-01T15:00+09:00"}, "out_of_range"),
        # LLM が年を取り違えたケース
        ({"title": "打合せ", "start": "2126-08-20T15:00+09:00"}, "out_of_range"),
    ],
)
def test_freeform_guards_reject_before_touching_the_api(kwargs: dict[str, str], error: str) -> None:
    skill, gcal = _skill()

    out = _run(skill, **kwargs)

    assert out.error == error and not out.created
    assert gcal.calls == []  # Google API に 1 度も触れていない


def test_freeform_allows_exactly_eight_hours() -> None:
    """境界: 8 時間ちょうどは通す（8 時間 1 分から拒否）。"""
    skill, _ = _skill()

    out = _run(
        skill, title="終日研修", start="2026-08-20T09:00+09:00", end="2026-08-20T17:00+09:00"
    )

    assert out.created


def test_freeform_not_connected_is_fail_closed() -> None:
    skill, gcal = _skill(tok=None)

    out = _run(skill, title="打合せ", start="2026-08-20T15:00+09:00")

    assert out.error == "not_connected" and gcal.calls == []


def test_freeform_old_scope_asks_for_reauth() -> None:
    skill, gcal = _skill(tok=_Tok(scopes=("https://www.googleapis.com/auth/gmail.modify",)))

    out = _run(skill, title="打合せ", start="2026-08-20T15:00+09:00")

    assert out.error == "reauth_needed" and gcal.calls == []


# ── ボタン経路の回帰（自由文の追加で壊れていない）────────────────────────────


def _button_token() -> str:
    token = encode_event_token(
        start_iso="2026-08-20T14:00:00+09:00",
        end_iso="2026-08-20T15:00:00+09:00",
        title="◯◯様 定例",
        owner_email=ME,
    )
    assert token is not None
    return token


def test_button_path_still_works() -> None:
    skill, gcal = _skill()

    out = _run(skill, event_token=_button_token())

    assert out.created
    assert gcal.calls[0]["summary"] == "◯◯様 定例"
    assert gcal.calls[0]["start_iso"] == "2026-08-20T14:00:00+09:00"


def test_signed_token_wins_over_free_text_arguments() -> None:
    """署名済みの日時が LLM 由来の値に上書きされる経路を作らない。"""
    skill, gcal = _skill()

    out = _run(
        skill,
        event_token=_button_token(),
        title="乗っ取りタイトル",
        start="2027-01-01T00:00+09:00",
        location="乗っ取り会議室",
    )

    assert out.created
    call = gcal.calls[0]
    assert call["summary"] == "◯◯様 定例"
    assert call["start_iso"] == "2026-08-20T14:00:00+09:00"
    assert call["location"] == ""


def test_button_path_ignores_freeform_date_range_guard() -> None:
    """署名済みトークンは（過去日でも）従来どおり通る＝ボタン経路の挙動は不変。"""
    token = encode_event_token(
        start_iso="2020-01-01T10:00:00+09:00",
        end_iso="2020-01-01T11:00:00+09:00",
        title="過去の予定",
        owner_email=ME,
    )
    assert token is not None
    skill, gcal = _skill()

    out = _run(skill, event_token=token)

    assert out.created and len(gcal.calls) == 1


def test_invalid_token_is_not_retried_as_freeform() -> None:
    """壊れたトークンは fail-closed。自由文引数へフォールバックして登録しない。"""
    skill, gcal = _skill()

    out = _run(skill, event_token="garbage.token", title="打合せ", start="2026-08-20T15:00+09:00")

    assert out.error == "expired" and not out.created
    assert gcal.calls == []


# ── 入力スキーマそのものが招待を作れない ────────────────────────────────────


def test_input_schema_has_no_attendee_or_calendar_field() -> None:
    """引数が無い＝どんな指示があっても招待も他人カレンダー指定も作れない。"""
    fields = set(CalendarEventInput.model_fields)

    assert fields == {"event_token", "title", "start", "end", "location"}
    for forbidden in ("attendees", "guests", "invitees", "calendar_id", "calendarId", "email"):
        assert forbidden not in fields


def test_extra_attendee_argument_is_rejected_by_schema() -> None:
    """attendees を渡そうとしても Pydantic が無視/拒否する（値が通らない）。"""
    parsed = CalendarEventInput.model_validate(
        {"title": "打合せ", "start": "2026-08-20T15:00+09:00", "attendees": ["x@example.com"]}
    )

    assert not hasattr(parsed, "attendees")
