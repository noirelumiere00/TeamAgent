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
_MAIL_SECRET = "calendar-action-test-secret-" + "m" * 32
_MAIL_NEXT_SECRET = "calendar-action-next-secret-" + "n" * 32
_ROTATION_NOW = 2_000_000_000


@pytest.fixture(autouse=True)
def _hmac_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
        "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
        "MAIL_ACTION_HMAC_PREVIOUS_SECRET_VALID_UNTIL",
        "MAIL_ACTION_TTL_S",
        "REPORT_LINK_HMAC_SECRET",
        "DATABASE_URL",
        "SLACK_BOT_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_SECRET)


def _token(**kw: Any) -> str:
    token = encode_event_token(
        start_iso=kw.get("start", "2026-07-15T14:00:00+09:00"),
        end_iso=kw.get("end", "2026-07-15T15:00:00+09:00"),
        title=kw.get("title", "◯◯様 定例"),
        owner_email=kw.get("owner", ME),
        now=kw.get("now"),
        ttl_s=kw.get("ttl_s"),
    )
    assert token is not None
    return token


# ── event_token 単体 ────────────────────────────────────────────────────────


def test_token_roundtrip() -> None:
    p = decode_event_token(_token(), ME)
    assert p is not None
    assert p.start_iso == "2026-07-15T14:00:00+09:00"
    assert p.title == "◯◯様 定例"


def test_token_rejects_other_owner_and_tamper_and_expiry() -> None:
    t = _token()
    assert decode_event_token(t, "other@vectorinc.co.jp") is None  # 所有者不一致
    # 署名改竄: base64 末尾は非有意ビットがあり末尾差し替えだと 1/256 で素通りする（フレーク）。
    # 本文中央の 1 文字を必ず別値に差し替える＝決定的に HMAC 不一致にする。
    body, sig = t.split(".", 1)
    flip = "A" if body[5] != "A" else "B"
    assert decode_event_token(body[:5] + flip + body[6:] + "." + sig, ME) is None
    old = _token(now=1_000_000)  # 発行が大昔＝失効
    assert decode_event_token(old, ME) is None


def test_event_token_expiry_is_exclusive_and_ttl_is_bounded() -> None:
    token = _token(now=1000, ttl_s=60)
    assert decode_event_token(token, ME, now=1059) is not None
    assert decode_event_token(token, ME, now=1060) is None
    assert (
        encode_event_token(
            start_iso="2026-07-15T14:00:00+09:00",
            end_iso="2026-07-15T15:00:00+09:00",
            title="meeting",
            owner_email=ME,
            now=1000,
            ttl_s=60 * 60 * 24 + 1,
        )
        is None
    )


def test_invalid_configured_mail_ttl_suppresses_event_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAIL_ACTION_TTL_S", " 3600")
    assert (
        encode_event_token(
            start_iso="2026-07-15T14:00:00+09:00",
            end_iso="2026-07-15T15:00:00+09:00",
            title="meeting",
            owner_email=ME,
            ttl_s=60,
        )
        is None
    )


def test_token_fail_closed_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    t = _token()
    monkeypatch.delenv("MAIL_ACTION_HMAC_SECRET", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert decode_event_token(t, ME) is None
    assert (
        encode_event_token(
            start_iso="2026-07-15T14:00:00+09:00",
            end_iso="2026-07-15T15:00:00+09:00",
            title="meeting",
            owner_email=ME,
        )
        is None
    )


def test_event_token_accepts_previous_only_during_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _token(now=_ROTATION_NOW)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_NEXT_SECRET)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET", _MAIL_SECRET)
    monkeypatch.setenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT", str(_ROTATION_NOW))
    assert decode_event_token(old, ME, now=_ROTATION_NOW) is not None

    new = _token(now=_ROTATION_NOW)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_SECRET)
    monkeypatch.delenv("MAIL_ACTION_HMAC_PREVIOUS_SECRET")
    monkeypatch.delenv("MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT")
    assert decode_event_token(new, ME, now=_ROTATION_NOW) is None


def test_stable_event_id_is_base32hex_and_deterministic() -> None:
    eid = stable_event_id("2026-07-15T14:00:00+09:00", "2026-07-15T15:00:00+09:00", ME)
    assert eid == stable_event_id("2026-07-15T14:00:00+09:00", "2026-07-15T15:00:00+09:00", ME)
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
    assert call["event_id"] == stable_event_id(
        "2026-07-15T14:00:00+09:00", "2026-07-15T15:00:00+09:00", ME
    )  # 冪等キー（安定フィールド由来）
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


# ── F1 回帰: triage → event_token 発行の結合（fake Bedrock で全経路を通す） ──


def test_triage_to_event_token_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM が meeting_* を返したら MailDigestItem に event_token が発行される（結合）。

    _triage_batch_call の出力組み立てがホワイトリスト方式のため、新キーの追加漏れが
    あると全ゲート ON でも機能が沈黙する（レビュー F1）。この結合テストが唯一それを捕まえる。
    """
    import base64 as _b64
    import datetime as _dt
    import json as _json

    from teamagent.skills.morning_digest.schema import MorningDigestInput
    from teamagent.skills.morning_digest.skill import MorningDigestSkill

    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", _MAIL_SECRET)
    future = (
        (_dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9))) + _dt.timedelta(days=2))
        .replace(minute=0, second=0, microsecond=0)
        .isoformat()
    )
    triage_json = _json.dumps(
        [
            {
                "importance": "high",
                "summary": "定例の確定連絡",
                "deadline": None,
                "ask": "",
                "next_step": "",
                "meeting_start": future,
                "meeting_end": None,
                "meeting_title": "◯◯様 定例",
                "scheduling_request": True,
            }
        ]
    )

    class _Resp:
        def __init__(self, text: str) -> None:
            self.text = text
            self.usage = type("U", (), {"cost_usd": 0.001})()

    class _Bedrock:
        def converse(self, **kw: Any) -> _Resp:
            return _Resp(triage_json)

    class _Msg:
        def __init__(self) -> None:
            self.headers = {"From": "c@x.com", "To": ME, "Subject": "定例の件"}
            self.payload = {
                "mimeType": "text/plain",
                "body": {"data": _b64.urlsafe_b64encode("7/15 14:00 確定です".encode()).decode()},
            }
            self.internal_date_ms = 1000
            self.thread_id = "T1"
            self.id = "m1"
            self.label_ids = ()

    class _Gmail:
        def list_messages(self, q: str, rid: str, max_results: int = 30) -> Any:
            ref = type("R", (), {"id": "m1", "thread_id": "T1"})()
            return ([ref], None)

        def get_thread(self, tid: str, rid: str, **_: Any) -> list[Any]:
            return [_Msg()]

        def list_drafts(self, rid: str, **_: Any) -> list[Any]:
            return []

    class _Tokens:
        def get(self, e: str) -> Any:
            return object()

    skill = MorningDigestSkill(token_store=_Tokens(), gmail=_Gmail(), bedrock=_Bedrock())
    skill._draft_on_demand_only = True  # 下書き生成はスキップ（このテストの対象外）
    skill._gcalendar = object()  # calendar 収集は失敗して errors に入るだけでよい
    out = skill.run(
        MorningDigestInput(max_drafts=0),
        SkillContext(request_id="r", metadata={"user_email": ME}),
    )
    item = out.mail_digest[0]
    assert item.meeting_start == future  # triage → item へ伝播（F1）
    assert item.meeting_end  # +1h 補完
    assert item.scheduling_request is True  # Task4 用フラグも伝播
    assert item.event_token  # To 本人×日時確定 → token 発行
    p = decode_event_token(item.event_token, ME)
    assert p is not None and p.start_iso == future

    def _event_token_boom(**kw: Any) -> str:
        raise RuntimeError("event token helper failed")

    monkeypatch.setattr(
        "teamagent.skills.morning_digest.skill.encode_event_token", _event_token_boom
    )
    contained = skill.run(
        MorningDigestInput(max_drafts=0),
        SkillContext(request_id="r-token-exception", metadata={"user_email": ME}),
    ).mail_digest[0]
    assert contained.event_token == ""  # digest survives and the unsafe action is omitted

    # 現Terraformのように DB URL が主鍵へ誤配線されても、呼出元は action token/button を出さない。
    legacy_db = "postgresql://teamagent:do-not-sign@db.internal:5432/teamagent"
    monkeypatch.setenv("DATABASE_URL", legacy_db)
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", legacy_db)
    invalid = skill.run(
        MorningDigestInput(max_drafts=0),
        SkillContext(request_id="r-invalid-key", metadata={"user_email": ME}),
    ).mail_digest[0]
    assert invalid.draft_token == ""
    assert invalid.event_token == ""


def test_meeting_iso_rejects_date_only_and_past() -> None:
    from teamagent.skills.morning_digest.skill import _meeting_iso

    assert _meeting_iso("2026-07-15") is None  # 日付のみ＝深夜0時に化けるので不採用
    assert _meeting_iso("2026-13-45T10:00:00+09:00") is None  # 不正
    assert _meeting_iso("2026-07-15T14:00:00") == "2026-07-15T14:00:00+09:00"  # naive→JST


def test_stable_event_id_survives_token_reissue() -> None:
    """F3 回帰: 翌日再発行された token（失効時刻が違う）でも同一日時なら同一 id。"""
    a = stable_event_id("2026-07-15T14:00:00+09:00", "2026-07-15T15:00:00+09:00", ME)
    b = stable_event_id("2026-07-15T14:00:00+09:00", "2026-07-15T15:00:00+09:00", ME)
    c = stable_event_id("2026-07-16T14:00:00+09:00", "2026-07-16T15:00:00+09:00", ME)
    assert a == b and a != c
    import re

    assert re.fullmatch(r"[a-v0-9]{5,1024}", a)
