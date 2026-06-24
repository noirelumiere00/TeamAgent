"""mail_reply Skill のオフラインテスト（課金0・外部I/O無し・実送信なし）。

fake GmailClient(create_draft 記録) / fake Bedrock / InMemoryTokenStore を注入し、
返信ドラフトの起草→Gmail 下書き保存（送信しない）と死守ライン・異常系を検証する。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import pytest

from teamagent.adapters.oauth_token_store import InMemoryTokenStore
from teamagent.skills.base import SkillContext
from teamagent.skills.mail_reply.schema import MailReplyInput
from teamagent.skills.mail_reply.skill import MailReplySkill, _reply_subject

OWNER = "s-komata@vectorinc.co.jp"


def _payload(text: str) -> dict[str, Any]:
    data = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
    return {"mimeType": "text/plain", "body": {"data": data}}


@dataclass
class _Ref:
    id: str
    thread_id: str = "th-1"


@dataclass
class _Msg:
    headers: dict[str, str]
    payload: dict[str, Any]
    thread_id: str = "th-1"
    internal_date_ms: int | None = 1_700_000_000_000
    id: str = "m0"


@dataclass
class _Draft:
    id: str
    message_id: str = "msg-x"
    thread_id: str | None = "th-1"


class FakeGmail:
    def __init__(self, msgs: list[_Msg], *, draft_raises: bool = False) -> None:
        self._msgs = msgs
        self._draft_raises = draft_raises
        self.last_query: str | None = None
        self.create_draft_calls: list[dict[str, Any]] = []

    def list_messages(
        self, query: str | None, request_id: str, *, max_results: int = 50, **kw: Any
    ) -> tuple[list[_Ref], None]:
        self.last_query = query
        n = min(len(self._msgs), max_results)
        return ([_Ref(id=f"m{i}") for i in range(n)], None)

    def get_message(self, msg_id: str, request_id: str, **kw: Any) -> _Msg:
        idx = int(msg_id[1:]) if msg_id[1:].isdigit() else 0
        return self._msgs[idx]

    def create_draft(
        self, *, to: str, subject: str, body_text: str, request_id: str, **kw: Any
    ) -> _Draft:
        if self._draft_raises:
            raise RuntimeError("403 insufficient scope")
        self.create_draft_calls.append({"to": to, "subject": subject, "body": body_text, **kw})
        return _Draft(id="draft-123")


@dataclass
class _Usage:
    cost_usd: float


@dataclass
class _Resp:
    text: str
    usage: _Usage


class FakeBedrock:
    def __init__(self) -> None:
        self.last_messages: Any = None
        self.last_system: str | None = None

    def converse(
        self, *, messages: Any, request_id: str, system: str | None = None, **kw: Any
    ) -> _Resp:
        self.last_messages = messages
        self.last_system = system
        return _Resp(
            text="田中様\n\nお世話になっております。見積を再提示いたします。\n\nよろしくお願いいたします。",
            usage=_Usage(cost_usd=0.004),
        )


def _ctx(user_email: str | None = OWNER) -> SkillContext:
    return SkillContext(request_id="r", user_id="U1", metadata={"user_email": user_email})


def _inbound() -> _Msg:
    return _Msg(
        headers={
            "From": "田中 <tanaka@moribuild.co.jp>",
            "To": OWNER,
            "Subject": "見積のご相談",
            "Message-ID": "<abc123@moribuild.co.jp>",
        },
        payload=_payload("お世話になります。見積を再提示いただけますか。"),
    )


def test_g1_requires_user_email() -> None:
    skill = MailReplySkill(gmail=FakeGmail([_inbound()]), bedrock=FakeBedrock())
    with pytest.raises(PermissionError):
        skill.run(MailReplyInput(client_name="森ビル"), _ctx(user_email=None))


def test_g2_unconnected_fails_closed() -> None:
    skill = MailReplySkill(token_store=InMemoryTokenStore(), bedrock=FakeBedrock())
    with pytest.raises(PermissionError):
        skill.run(MailReplyInput(client_name="森ビル"), _ctx())


def test_happy_path_creates_draft_does_not_send() -> None:
    gmail = FakeGmail([_inbound()])
    bedrock = FakeBedrock()
    skill = MailReplySkill(gmail=gmail, bedrock=bedrock)
    out = skill.run(MailReplyInput(client_name="森ビル"), _ctx())

    assert out.created is True
    assert out.gmail_draft_id == "draft-123"
    assert out.draft_body  # AI 起草本文
    assert out.draft_subject == "Re: 見積のご相談"
    assert out.to_display == "tanaka@moribuild.co.jp"  # 返信先（本人の取引相手）
    assert "送信" in out.note  # 「送信していない」旨
    # create_draft が 1 回・正しい宛先/件名/スレッドで呼ばれた（= 送信は呼ばれない）
    assert len(gmail.create_draft_calls) == 1
    call = gmail.create_draft_calls[0]
    assert call["to"] == "tanaka@moribuild.co.jp"
    assert call["subject"] == "Re: 見積のご相談"
    assert call["thread_id"] == "th-1"
    assert call["in_reply_to_message_id"] == "<abc123@moribuild.co.jp>"
    # G5: 受信のみ・client+期間で絞る
    assert '"森ビル"' in (gmail.last_query or "")
    assert "-in:sent" in (gmail.last_query or "")


def test_instructions_passed_to_model() -> None:
    gmail = FakeGmail([_inbound()])
    bedrock = FakeBedrock()
    skill = MailReplySkill(gmail=gmail, bedrock=bedrock)
    skill.run(MailReplyInput(client_name="森ビル", instructions="前向きに、来週訪問を提案"), _ctx())
    assert "来週訪問を提案" in str(bedrock.last_messages)


def test_no_target_returns_not_created() -> None:
    skill = MailReplySkill(gmail=FakeGmail([]), bedrock=FakeBedrock())
    out = skill.run(MailReplyInput(client_name="森ビル"), _ctx())
    assert out.created is False
    assert out.gmail_draft_id == ""
    assert "見つかりません" in out.note


def test_draft_failure_becomes_permission_error() -> None:
    # gmail.modify 未認可(readonly のみ connect)等で create_draft が 403 → 再連携案内。
    gmail = FakeGmail([_inbound()], draft_raises=True)
    skill = MailReplySkill(gmail=gmail, bedrock=FakeBedrock())
    with pytest.raises(PermissionError):
        skill.run(MailReplyInput(client_name="森ビル"), _ctx())


def test_reply_subject_dedupes_re() -> None:
    assert _reply_subject("見積の件") == "Re: 見積の件"
    assert _reply_subject("Re: 見積の件") == "Re: 見積の件"
    assert _reply_subject("RE: x") == "RE: x"
    assert _reply_subject("") == "Re:"


def _inbound_multi() -> _Msg:
    return _Msg(
        headers={
            "From": "田中 <tanaka@moribuild.co.jp>",
            "To": f"{OWNER}, sato@moribuild.co.jp",
            "Cc": "ueda@partner.co.jp",
            "Subject": "見積のご相談",
            "Message-ID": "<abc123@moribuild.co.jp>",
        },
        payload=_payload("お世話になります。見積を再提示いただけますか。"),
    )


def test_reply_all_cc_includes_other_recipients() -> None:
    gmail = FakeGmail([_inbound_multi()])
    skill = MailReplySkill(gmail=gmail, bedrock=FakeBedrock())
    skill.run(MailReplyInput(client_name="森ビル"), _ctx())
    call = gmail.create_draft_calls[0]
    assert call["to"] == "tanaka@moribuild.co.jp"
    cc = call.get("cc") or ""
    assert "sato@moribuild.co.jp" in cc
    assert "ueda@partner.co.jp" in cc
    assert OWNER not in cc  # 本人除外
    assert "tanaka@moribuild.co.jp" not in cc  # 主宛先除外
