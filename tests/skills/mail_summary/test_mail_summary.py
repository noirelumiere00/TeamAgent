"""mail_summary Skill のオフラインテスト（課金0・外部I/O無し）。"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import pytest

from teamagent.adapters.oauth_token_store import InMemoryTokenStore
from teamagent.skills.base import SkillContext
from teamagent.skills.mail_summary.schema import MailSummaryInput
from teamagent.skills.mail_summary.skill import MailSummarySkill, _mask_email

OWNER = "s-komata@vectorinc.co.jp"


def _payload(text: str) -> dict[str, Any]:
    data = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
    return {"mimeType": "text/plain", "body": {"data": data}}


@dataclass
class _Ref:
    id: str
    thread_id: str = "t"


@dataclass
class _Msg:
    headers: dict[str, str]
    payload: dict[str, Any]
    internal_date_ms: int | None = 1_700_000_000_000
    id: str = "m"
    thread_id: str = "t"


class FakeGmail:
    def __init__(self, msgs: list[_Msg]) -> None:
        self._msgs = msgs
        self.last_query: str | None = None

    def list_messages(
        self, query: str | None, request_id: str, *, max_results: int = 50, **kw: Any
    ) -> tuple[list[_Ref], None]:
        self.last_query = query
        n = min(len(self._msgs), max_results)
        return ([_Ref(id=f"m{i}") for i in range(n)], None)

    def get_message(self, msg_id: str, request_id: str, **kw: Any) -> _Msg:
        return self._msgs[int(msg_id[1:])]


@dataclass
class _Usage:
    cost_usd: float


@dataclass
class _Resp:
    text: str
    usage: _Usage


class FakeBedrock:
    def __init__(
        self, text: str = "・先方は提案内容に前向き\n・見積の再提示を依頼\n・期限は来週金曜"
    ) -> None:
        self._text = text
        self.last_messages: Any = None
        self.last_system: str | None = None

    def converse(
        self, *, messages: Any, request_id: str, system: str | None = None, **kw: Any
    ) -> _Resp:
        self.last_messages = messages
        self.last_system = system
        return _Resp(text=self._text, usage=_Usage(cost_usd=0.003))


class _BoomBedrock:
    def converse(self, **kw: Any) -> Any:
        raise RuntimeError("throttle")


def _ctx(user_email: str | None = OWNER) -> SkillContext:
    return SkillContext(request_id="r", user_id="U1", metadata={"user_email": user_email})


def _msg(sender: str, subject: str, body: str) -> _Msg:
    return _Msg(headers={"From": sender, "Subject": subject}, payload=_payload(body))


def test_g1_requires_user_email() -> None:
    skill = MailSummarySkill(gmail=FakeGmail([]), bedrock=FakeBedrock())
    with pytest.raises(PermissionError):
        skill.run(MailSummaryInput(client_name="森ビル"), _ctx(user_email=None))


def test_g2_unconnected_fails_closed() -> None:
    skill = MailSummarySkill(token_store=InMemoryTokenStore(), bedrock=FakeBedrock())
    with pytest.raises(PermissionError):
        skill.run(MailSummaryInput(client_name="森ビル"), _ctx())


def test_happy_path_summarizes_and_masks() -> None:
    msgs = [
        _msg(
            "田中 <tanaka@moribuild.co.jp>", "提案の件", "見積を再提示してほしい。期限は来週金曜。"
        ),
        _msg("佐藤 <sato@moribuild.co.jp>", "日程", "来週で調整したい。"),
    ]
    fake = FakeGmail(msgs)
    skill = MailSummarySkill(gmail=fake, bedrock=FakeBedrock())
    out = skill.run(MailSummaryInput(client_name="森ビル", lookback_days=14), _ctx())

    assert out.scanned_count == 2
    assert out.summary  # LLM 要約が返る
    assert out.total_cost_usd > 0.0
    assert out.inbox_owner_masked == "s***@vectorinc.co.jp"
    assert len(out.highlights) == 2
    for h in out.highlights:
        assert h.counterpart_masked.endswith("@moribuild.co.jp")
        assert "tanaka@" not in h.counterpart_masked and "sato@" not in h.counterpart_masked
    # G5: client + 期間で絞る
    assert '"森ビル"' in (fake.last_query or "")
    assert "newer_than:14d" in (fake.last_query or "")


def test_empty_inbox_friendly_no_cost() -> None:
    skill = MailSummarySkill(gmail=FakeGmail([]), bedrock=FakeBedrock())
    out = skill.run(MailSummaryInput(client_name="X社"), _ctx())
    assert out.scanned_count == 0
    assert out.total_cost_usd == 0.0
    assert "見つかりません" in out.summary


def test_llm_failure_is_graceful() -> None:
    msgs = [_msg("a@x.co.jp", "件名", "本文")]
    skill = MailSummarySkill(gmail=FakeGmail(msgs), bedrock=_BoomBedrock())
    out = skill.run(MailSummaryInput(client_name="X社"), _ctx())
    assert out.total_cost_usd == 0.0
    assert out.summary  # 失敗時も案内文を返す（落ちない）


def test_mask_email() -> None:
    assert _mask_email("tanaka@moribuild.co.jp") == "t***@moribuild.co.jp"
