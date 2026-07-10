"""calendar_event Skill＋event_token（v0.3 Task3）のテスト（外部I/O無し）。

検証主眼: トークンの署名/所有者/失効 fail-closed・冪等（連打→登録済み）・
旧スコープの事前 reauth 案内・登録は insert のみ（confirmed・招待なし）・G3。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from teamagent.adapters.gcalendar_client import DuplicateEventError, InsertedEvent
from teamagent.skills.base import SkillContext
from teamagent.skills.calendar_event.schema import CalendarEventInput
from teamagent.skills.calendar_event.skill import CalendarEventSkill
from teamagent.skills.morning_digest.event_token import (
    decode_event_token,
    encode_event_token,
    stable_event_id,
)

ME = "me@vectorinc.co.jp"
_CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"


@pytest.fixture(autouse=True)
def _hmac_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "test-secret")


def _token(**kw: Any) -> str:
    return encode_event_token(
        start_iso=kw.get("start", "2026-07-15T14:00:00+09:00"),
        end_iso=kw.get("end", "2026-07-15T15:00:00+09:00"),
        title=kw.get("title", "◯◯様 定例"),
        owner_email=kw.get("owner", ME),
        now=kw.get("now"),
    )


# ── event_token 単体 ────────────────────────────────────────────────────────


def test_token_roundtrip() -> None:
    p = decode_event_token(_token(), ME)
    assert p is not None
    assert p.start_iso == "2026-07-15T14:00:00+09:00"
    assert p.title == "◯◯様 定例"


def test_token_rejects_other_owner_and_tamper_and_expiry() -> None:
    t = _token()
    assert decode_event_token(t, "other@vectorinc.co.jp") is None  # 所有者不一致
    assert decode_event_token(t[:-2] + "xx", ME) is None  # 署名改竄
    old = _token(now=1_000_000)  # 発行が大昔＝失効
    assert decode_event_token(old, ME) is None


def test_token_fail_closed_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    t = _token()
    monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert decode_event_token(t, ME) is None


def test_stable_event_id_is_base32hex_and_deterministic() -> None:
    t = _token()
    eid = stable_event_id(t)
    assert eid == stable_event_id(t)  # 決定的＝連打で同一 id
    import re

    assert re.fullmatch(r"[a-v0-9]{5,1024}", eid)


# ── skill ───────────────────────────────────────────────────────────────────


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
    raise_exc: Exception | None = None

    def insert_event(self, request_id: str, **kw: Any) -> InsertedEvent:
        self.calls.append(kw)
        if self.raise_dup:
            raise DuplicateEventError(kw.get("event_id", ""))
        if self.raise_exc:
            raise self.raise_exc
        return InsertedEvent(
            event_id=str(kw.get("event_id") or "x"),
            html_link="https://calendar.google.com/event?eid=abc",
            summary=str(kw.get("summary", "")),
            start=str(kw.get("start_iso", "")),
            end=str(kw.get("end_iso", "")),
            status="confirmed",
        )


def _skill(
    gcal: _FakeGCal | None = None, tok: Any = "default"
) -> tuple[CalendarEventSkill, _FakeGCal]:
    gcal = gcal or _FakeGCal()
    token = _Tok() if tok == "default" else tok
    return (
        CalendarEventSkill(token_store=_Store(token), gcalendar_factory=lambda _t: gcal),
        gcal,
    )


def _run(skill: CalendarEventSkill, token: str) -> Any:
    return skill.run(
        CalendarEventInput(event_token=token),
        SkillContext(request_id="r", metadata={"user_email": ME}),
    )


def test_happy_path_inserts_with_idempotent_id() -> None:
    skill, gcal = _skill()
    t = _token()
    out = _run(skill, t)
    assert out.created and out.event_url.startswith("https://calendar.google.com/")
    call = gcal.calls[0]
    assert call["event_id"] == stable_event_id(t)  # 冪等キー
    assert call["summary"] == "◯◯様 定例"
    assert call["start_iso"] == "2026-07-15T14:00:00+09:00"
    assert "attendees" not in call  # 招待は API 面ごと存在しない


def test_invalid_token_fail_closed() -> None:
    skill, gcal = _skill()
    out = _run(skill, "garbage.token")
    assert not out.created and out.error == "expired"
    assert gcal.calls == []  # API に到達しない


def test_double_click_returns_already() -> None:
    skill, _ = _skill(_FakeGCal(raise_dup=True))
    out = _run(skill, _token())
    assert out.already and not out.created
    assert "登録済み" in out.message


def test_old_scope_token_gets_reauth_message() -> None:
    old_tok = _Tok(scopes=("https://www.googleapis.com/auth/calendar.readonly",))
    skill, gcal = _skill(tok=old_tok)
    out = _run(skill, _token())
    assert out.error == "reauth_needed" and "再連携" in out.message
    assert gcal.calls == []  # 403 を本番で踏ませない（事前検知）


def test_not_connected() -> None:
    skill, _ = _skill(tok=None)
    out = _run(skill, _token())
    assert out.error == "not_connected"


def test_unknown_scopes_row_falls_through_to_api() -> None:
    # scopes 未記録の古い行（空 tuple）は事前拒否せず API 判定に委ねる。
    skill, gcal = _skill(tok=_Tok(scopes=()))
    out = _run(skill, _token())
    assert out.created and len(gcal.calls) == 1


def test_insert_failure_is_contained() -> None:
    skill, _ = _skill(_FakeGCal(raise_exc=RuntimeError("api down")))
    out = _run(skill, _token())
    assert out.error == "insert_failed" and not out.created
