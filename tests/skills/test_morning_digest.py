"""morning_digest Skill のテスト（課金 0・外部依存をすべて mock）。

検証観点（G1-G7 + 機能）:
  - G1: user_email 未指定/空は PermissionError（本人受信箱限定）
  - G2: 未連携（token store get=None）は PermissionError
  - 重要度分類: Bedrock triage 戻りで importance / summary が反映される
  - importance="high" の上位 max_drafts 件で has_draft=True
  - DLP マスク: counterpart は ***@domain 形式・件名は scrub 適用
  - 部分失敗（calendar/slack）は errors リストに残り mail は影響なし
  - draft 件数の上限が input.max_drafts
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest.schema import MorningDigestInput
from teamagent.skills.morning_digest.skill import MorningDigestSkill

# ─────────────────────────────────────────────────────────────
# テスト用 fakes（軽量）
# ─────────────────────────────────────────────────────────────


@dataclass
class _FakeRef:
    id: str


@dataclass
class _FakeMsg:
    headers: dict[str, str]
    payload: dict[str, Any]
    internal_date_ms: int | None = None
    thread_id: str = "thr-1"


class _FakeGmail:
    def __init__(self, msgs: list[_FakeMsg]):
        self._msgs = msgs
        self.created_drafts: list[dict[str, Any]] = []

    def list_messages(
        self, query: str, request_id: str, max_results: int = 30
    ) -> tuple[list[_FakeRef], None]:
        return ([_FakeRef(id=f"m{i}") for i in range(len(self._msgs))], None)

    def get_message(self, msg_id: str, request_id: str) -> _FakeMsg:
        idx = int(msg_id.lstrip("m"))
        return self._msgs[idx]

    def create_draft(
        self,
        *,
        to: str,
        subject: str,
        body_text: str,
        request_id: str,
        thread_id: str | None = None,
        cc: str | None = None,
        in_reply_to_message_id: str | None = None,
        user_id: str = "me",
    ) -> Any:
        self.created_drafts.append(
            dict(
                to=to,
                subject=subject,
                body_text=body_text,
                thread_id=thread_id,
                in_reply_to_message_id=in_reply_to_message_id,
            )
        )
        return type(
            "D",
            (),
            {"id": f"draft-{len(self.created_drafts)}", "message_id": "", "thread_id": thread_id},
        )()


class _FakeGCal:
    def __init__(self, events: list[Any]):
        self._events = events

    def list_events(self, request_id: str, **kwargs: Any) -> list[Any]:
        return self._events


@dataclass
class _FakeCalEvent:
    # 実 CalendarEvent と同じ属性名（start/end/location/hangout_link）。
    summary: str = ""
    start: str = ""
    end: str = ""
    location: str = ""
    hangout_link: str = ""
    attendees: tuple[str, ...] = ()


class _FakeTokenStore:
    def __init__(self, tokens: dict[str, Any]):
        self._tokens = tokens

    def get(self, user_email: str) -> Any:
        return self._tokens.get(user_email.lower())

    def put(self, user_email: str, token: Any) -> None:
        self._tokens[user_email.lower()] = token

    def has(self, user_email: str) -> bool:
        return user_email.lower() in self._tokens


class _FakeBedrockResp:
    def __init__(self, text: str, cost: float = 0.001):
        self.text = text
        self.usage = type("U", (), {"cost_usd": cost})()


class _FakeBedrock:
    """triage と draft で異なる応答を返す。順番に消費する。"""

    def __init__(self, triage_json: str, draft_text: str = "下書きです。"):
        self._triage_json = triage_json
        self._draft_text = draft_text
        self.call_count = 0

    def converse(self, **kwargs: Any) -> _FakeBedrockResp:
        self.call_count += 1
        system = kwargs.get("system", "")
        # triage のシステムプロンプトには "分類規則" を含めてある
        if "分類規則" in str(system):
            return _FakeBedrockResp(self._triage_json)
        return _FakeBedrockResp(self._draft_text)


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def fake_msgs() -> list[_FakeMsg]:
    return [
        _FakeMsg(
            headers={
                "From": "alice@x.com",
                "To": "me@vectorinc.co.jp",
                "Subject": "Re: 契約書",
                "Message-ID": "<a1>",
            },
            payload={"body": {"data": "Y29udHJhY3QgdXJnZW50"}},  # base64 "contract urgent"
            internal_date_ms=1718681400000,
        ),
        _FakeMsg(
            headers={
                "From": "bob@x.com",
                "To": "me@vectorinc.co.jp",
                "Subject": "FYI: 業界ニュース",
                "Message-ID": "<a2>",
            },
            payload={"body": {"data": "ZnlpIG5ld3NsZXR0ZXI="}},  # "fyi newsletter"
            internal_date_ms=1718681500000,
        ),
        _FakeMsg(
            headers={
                "From": "carol@x.com",
                "To": "me@vectorinc.co.jp",
                "Subject": "確認のお願い",
                "Message-ID": "<a3>",
            },
            payload={"body": {"data": "Y2hlY2sgcGxlYXNl"}},  # "check please"
            internal_date_ms=1718681600000,
        ),
    ]


@pytest.fixture
def triage_json() -> str:
    return (
        '[{"importance": "high", "summary": "契約書の差し戻し対応依頼"},'
        ' {"importance": "low", "summary": "業界ニュースの共有"},'
        ' {"importance": "medium", "summary": "資料確認の依頼"}]'
    )


# ─────────────────────────────────────────────────────────────
# テスト
# ─────────────────────────────────────────────────────────────


def test_fail_closed_when_user_email_missing(fake_msgs, triage_json) -> None:
    skill = MorningDigestSkill(
        token_store=_FakeTokenStore({"me@vectorinc.co.jp": object()}),
        gmail=_FakeGmail(fake_msgs),
        gcalendar=_FakeGCal([]),
        bedrock=_FakeBedrock(triage_json),
    )
    ctx = SkillContext(request_id="req-1", metadata={})  # user_email 無し
    with pytest.raises(PermissionError, match="user_email"):
        skill.run(MorningDigestInput(), ctx)


def test_fail_closed_when_user_not_connected(fake_msgs, triage_json) -> None:
    skill = MorningDigestSkill(
        token_store=_FakeTokenStore({}),  # 連携済ゼロ
        gmail=_FakeGmail(fake_msgs),
        gcalendar=_FakeGCal([]),
        bedrock=_FakeBedrock(triage_json),
    )
    ctx = SkillContext(request_id="req-2", metadata={"user_email": "me@vectorinc.co.jp"})
    with pytest.raises(PermissionError, match="連携"):
        skill.run(MorningDigestInput(), ctx)


def test_basic_digest_with_triage_and_sort(fake_msgs, triage_json) -> None:
    skill = MorningDigestSkill(
        token_store=_FakeTokenStore({"me@vectorinc.co.jp": object()}),
        gmail=_FakeGmail(fake_msgs),
        gcalendar=_FakeGCal([]),
        bedrock=_FakeBedrock(triage_json),
    )
    ctx = SkillContext(request_id="req-3", metadata={"user_email": "me@vectorinc.co.jp"})
    out = skill.run(MorningDigestInput(max_drafts=1), ctx)

    # 3 件取得
    assert len(out.mail_digest) == 3
    # importance="high" → "medium" → "low" でソート
    assert out.mail_digest[0].importance == "high"
    assert out.mail_digest[1].importance == "medium"
    assert out.mail_digest[2].importance == "low"
    # 高優先度の summary が反映
    assert "契約書" in out.mail_digest[0].summary
    # DLP マスクされた相手
    assert out.mail_digest[0].counterpart_masked.endswith("@x.com")
    assert "***" in out.mail_digest[0].counterpart_masked


def test_drafts_created_only_for_high_importance(fake_msgs, triage_json) -> None:
    fake_gmail = _FakeGmail(fake_msgs)
    skill = MorningDigestSkill(
        token_store=_FakeTokenStore({"me@vectorinc.co.jp": object()}),
        gmail=fake_gmail,
        gcalendar=_FakeGCal([]),
        bedrock=_FakeBedrock(triage_json),
    )
    ctx = SkillContext(request_id="req-4", metadata={"user_email": "me@vectorinc.co.jp"})
    out = skill.run(MorningDigestInput(max_drafts=3), ctx)

    # high 重要度は 1 件しかないので draft も 1 件
    assert out.drafts_created == 1
    assert len(fake_gmail.created_drafts) == 1
    # 下書きの宛先は high の相手 (alice@x.com)
    assert "alice@x.com" == fake_gmail.created_drafts[0]["to"]
    # Re: prefix
    assert fake_gmail.created_drafts[0]["subject"].startswith("Re:")


def test_max_drafts_zero_creates_no_drafts(fake_msgs, triage_json) -> None:
    fake_gmail = _FakeGmail(fake_msgs)
    skill = MorningDigestSkill(
        token_store=_FakeTokenStore({"me@vectorinc.co.jp": object()}),
        gmail=fake_gmail,
        gcalendar=_FakeGCal([]),
        bedrock=_FakeBedrock(triage_json),
    )
    ctx = SkillContext(request_id="req-5", metadata={"user_email": "me@vectorinc.co.jp"})
    out = skill.run(MorningDigestInput(max_drafts=0), ctx)

    assert out.drafts_created == 0
    assert len(fake_gmail.created_drafts) == 0


def test_calendar_partial_failure_is_recorded_in_errors(fake_msgs, triage_json) -> None:
    class _ExplodingGCal:
        def list_events(self, request_id: str, **kwargs: Any) -> list[Any]:
            raise RuntimeError("calendar api down")

    skill = MorningDigestSkill(
        token_store=_FakeTokenStore({"me@vectorinc.co.jp": object()}),
        gmail=_FakeGmail(fake_msgs),
        gcalendar=_ExplodingGCal(),
        bedrock=_FakeBedrock(triage_json),
    )
    ctx = SkillContext(request_id="req-6", metadata={"user_email": "me@vectorinc.co.jp"})
    out = skill.run(MorningDigestInput(max_drafts=0), ctx)

    # mail は成功して digest にある
    assert len(out.mail_digest) == 3
    # calendar はエラー記録あり
    assert any("calendar" in e for e in out.errors)
    # calendar_events は空
    assert out.calendar_events == []


def test_calendar_events_collected(fake_msgs, triage_json) -> None:
    events = [
        _FakeCalEvent(
            summary="営業 MTG",
            start="2026-06-18T10:00:00+09:00",
            end="2026-06-18T11:00:00+09:00",
            location="本社 13F 会議室A",
            hangout_link="https://meet.google.com/abc-defg-hij",
        ),
    ]
    skill = MorningDigestSkill(
        token_store=_FakeTokenStore({"me@vectorinc.co.jp": object()}),
        gmail=_FakeGmail(fake_msgs),
        gcalendar=_FakeGCal(events),
        bedrock=_FakeBedrock(triage_json),
    )
    ctx = SkillContext(request_id="req-7", metadata={"user_email": "me@vectorinc.co.jp"})
    out = skill.run(MorningDigestInput(max_drafts=0), ctx)

    assert len(out.calendar_events) == 1
    ev0 = out.calendar_events[0]
    assert ev0.summary_scrubbed == "営業 MTG"
    # 旧コードの start_at/location 取りこぼしバグの回帰防止：時刻・会議室・会議URLが入る
    assert ev0.start_at == "2026-06-18T10:00:00+09:00"
    assert ev0.location_scrubbed == "本社 13F 会議室A"
    assert ev0.meeting_url == "https://meet.google.com/abc-defg-hij"


def test_user_email_masked_in_output(fake_msgs, triage_json) -> None:
    skill = MorningDigestSkill(
        token_store=_FakeTokenStore({"shogo@vectorinc.co.jp": object()}),
        gmail=_FakeGmail(fake_msgs),
        gcalendar=_FakeGCal([]),
        bedrock=_FakeBedrock(triage_json),
    )
    ctx = SkillContext(request_id="req-8", metadata={"user_email": "shogo@vectorinc.co.jp"})
    out = skill.run(MorningDigestInput(max_drafts=0), ctx)

    # 生 email が出ない・マスク後
    assert out.user_email_masked == "s***@vectorinc.co.jp"
    assert "shogo@vectorinc.co.jp" not in out.user_email_masked


def test_has_draft_painted_only_for_high(fake_msgs, triage_json) -> None:
    skill = MorningDigestSkill(
        token_store=_FakeTokenStore({"me@vectorinc.co.jp": object()}),
        gmail=_FakeGmail(fake_msgs),
        gcalendar=_FakeGCal([]),
        bedrock=_FakeBedrock(triage_json),
    )
    ctx = SkillContext(request_id="req-9", metadata={"user_email": "me@vectorinc.co.jp"})
    out = skill.run(MorningDigestInput(max_drafts=5), ctx)

    # high のみ has_draft=True
    high_count = sum(1 for m in out.mail_digest if m.has_draft)
    assert high_count == 1
    # high 重要度のものに has_draft=True
    for m in out.mail_digest:
        if m.importance == "high":
            assert m.has_draft is True
        else:
            assert m.has_draft is False
