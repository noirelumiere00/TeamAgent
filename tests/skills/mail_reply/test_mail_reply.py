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


def _candidate(
    sender: str,
    subject: str,
    body: str,
    *,
    extra_headers: dict[str, str] | None = None,
) -> _Msg:
    headers = {
        "From": sender,
        "To": OWNER,
        "Subject": subject,
        "Message-ID": "<candidate@example.com>",
    }
    headers.update(extra_headers or {})
    return _Msg(headers=headers, payload=_payload(body))


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


def test_skips_bulk_noreply_and_daily_then_replies_to_personal_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAIL_EXCLUDE_BULK", raising=False)
    monkeypatch.delenv("MAIL_EXCLUDE_SUBJECT_KEYWORDS", raising=False)
    msgs = [
        _candidate(
            "配信 <news@example.com>",
            "ニュースレター",
            "配信本文",
            extra_headers={"List-Unsubscribe": "<mailto:unsubscribe@example.com>"},
        ),
        _candidate("通知 <noreply@example.com>", "自動通知", "自動通知本文"),
        _candidate("営業企画 <sales@example.com>", "営業日報", "日報本文"),
        _candidate("田中 <tanaka@example.com>", "個別相談", "通常の個人メール本文"),
    ]
    gmail = FakeGmail(msgs)

    out = MailReplySkill(gmail=gmail, bedrock=FakeBedrock()).run(
        MailReplyInput(client_name="Example"),
        _ctx(),
    )

    assert out.created is True
    assert out.to_display == "tanaka@example.com"
    assert out.draft_subject == "Re: 個別相談"
    assert gmail.create_draft_calls[0]["to"] == "tanaka@example.com"


def test_all_excluded_candidates_return_no_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAIL_EXCLUDE_BULK", raising=False)
    monkeypatch.delenv("MAIL_EXCLUDE_SUBJECT_KEYWORDS", raising=False)
    msgs = [
        _candidate(
            "配信 <news@example.com>",
            "ニュースレター",
            "配信本文",
            extra_headers={"Precedence": "bulk"},
        ),
        _candidate("通知 <no-reply@example.com>", "自動通知", "自動通知本文"),
        _candidate("営業企画 <sales@example.com>", "営業日報", "日報本文"),
    ]
    gmail = FakeGmail(msgs)

    out = MailReplySkill(gmail=gmail, bedrock=FakeBedrock()).run(
        MailReplyInput(client_name="Example"),
        _ctx(),
    )

    assert out.created is False
    assert "見つかりません" in out.note
    assert gmail.create_draft_calls == []


def test_explicit_bulk_target_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """本人が target_message_id で指したメールは、一斉配信でも除外しない。

    除外は「どれに返信するか自動で選ぶ」ときの事故防止であって、指を差された
    ものまで落とすと「対象が見つかりません」しか返せなくなる（＝依頼が実行不能）。
    """
    monkeypatch.delenv("MAIL_EXCLUDE_BULK", raising=False)
    msg = _candidate(
        "配信 <news@example.com>",
        "ニュースレター",
        "配信本文",
        extra_headers={"List-Id": "newsletter.example.com"},
    )
    gmail = FakeGmail([msg])

    out = MailReplySkill(gmail=gmail, bedrock=FakeBedrock()).run(
        MailReplyInput(client_name="Example", target_message_id="m0"),
        _ctx(),
    )

    assert out.created is True
    assert len(gmail.create_draft_calls) == 1


def test_bulk_is_still_excluded_when_target_is_auto_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自動選択のときは従来どおり一斉配信を飛ばす（上の明示指定と対になる保証）。"""
    monkeypatch.delenv("MAIL_EXCLUDE_BULK", raising=False)
    msg = _candidate(
        "配信 <news@example.com>",
        "ニュースレター",
        "配信本文",
        extra_headers={"List-Id": "newsletter.example.com"},
    )
    gmail = FakeGmail([msg])

    out = MailReplySkill(gmail=gmail, bedrock=FakeBedrock()).run(
        MailReplyInput(client_name="Example"),
        _ctx(),
    )

    assert out.created is False
    assert gmail.create_draft_calls == []


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


class _FakeSlackProvider:
    """deal_provider 契約の fake（fetch 引数を記録し bullets/cost を返す）。"""

    def __init__(self, bullets: list[str], cost: float = 0.003) -> None:
        self._bullets = bullets
        self._cost = cost
        self.calls: list[tuple[str, str, dict]] = []

    def fetch(self, client_hint: str, requester: str, ctx: Any) -> Any:
        from teamagent.skills._shared.slack_context import SlackContextResult

        self.calls.append((client_hint, requester, dict(ctx.metadata)))
        return SlackContextResult(bullets=self._bullets, cost_usd=self._cost)


def test_slack_context_injected_when_gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_SLACK_CONTEXT", "1")
    gmail = FakeGmail([_inbound()])
    bedrock = FakeBedrock()
    prov = _FakeSlackProvider(["○○社は金曜納期で合意", "予算は300万"])
    skill = MailReplySkill(gmail=gmail, bedrock=bedrock, deal_provider=prov)
    ctx = SkillContext(
        request_id="r",
        user_id="U1",
        metadata={"user_email": OWNER, "channel_id": "C1", "thread_ts": "1.1"},
    )
    out = skill.run(MailReplyInput(client_name="森ビル"), ctx)

    msg = str(bedrock.last_messages)
    assert "社内Slackの関連文脈" in msg  # セクションが注入される
    assert "金曜納期" in msg
    assert out.total_cost_usd == pytest.approx(0.007)  # draft 0.004 + slack 0.003
    # provider に client_name と現スレッド channel/thread が渡る
    assert prov.calls[0][0] == "森ビル"
    assert prov.calls[0][2]["channel_id"] == "C1"
    assert prov.calls[0][2]["thread_ts"] == "1.1"
    # 送信は呼ばれない（create_draft のみ）
    assert len(gmail.create_draft_calls) == 1


def test_slack_context_skipped_when_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_SLACK_CONTEXT", raising=False)
    gmail = FakeGmail([_inbound()])
    bedrock = FakeBedrock()
    prov = _FakeSlackProvider(["出てはいけない"])
    skill = MailReplySkill(gmail=gmail, bedrock=bedrock, deal_provider=prov)
    skill.run(MailReplyInput(client_name="森ビル"), _ctx())

    assert "社内Slackの関連文脈" not in str(bedrock.last_messages)
    assert prov.calls == []  # gate OFF なら provider は呼ばれない


# ── 要修正2(G5 バイパス): 生 client_name をフレーズに直挿ししない ─────────────


# 空文字は本 Skill の schema が min_length=1 で弾く（ガードの担当は断片のみ）。
@pytest.mark.parametrize("bad", ["今日のメール", "返信必要", "今週の空き時間", "の"])
def test_guard_blocks_request_fragments_without_touching_gmail(bad: str) -> None:
    """依頼文の断片で受信箱を漁らない（mail_summary / mail_followup と同じ規律）。"""
    fake = FakeGmail([_inbound()])
    skill = MailReplySkill(gmail=fake, bedrock=FakeBedrock())

    out = skill.run(MailReplyInput(client_name=bad), _ctx())

    assert fake.last_query is None, "受信箱を検索してはいけない"
    assert fake.create_draft_calls == [], "下書きを作ってはいけない"
    assert out.created is False
    assert "連携は正常です" in out.note


def test_guard_refuses_gmail_operator_injection_in_the_query() -> None:
    """``"`` でフレーズを閉じて ``from:`` を継ぎ足す注入を、クエリを組む前に落とす。"""
    fake = FakeGmail([_inbound()])
    skill = MailReplySkill(gmail=fake, bedrock=FakeBedrock())

    out = skill.run(MailReplyInput(client_name='x" OR from:ceo@example.com "'), _ctx())

    assert fake.last_query is None
    assert out.created is False
    assert "ceo@example.com" not in out.note  # エコーも PII マスク後


def test_query_is_phrase_quoted_for_a_real_client_name() -> None:
    fake = FakeGmail([_inbound()])
    MailReplySkill(gmail=fake, bedrock=FakeBedrock()).run(
        MailReplyInput(client_name="森ビル", lookback_days=7), _ctx()
    )
    assert fake.last_query == '"森ビル" newer_than:7d -in:sent in:inbox'


def test_explicit_target_message_id_bypasses_the_guard() -> None:
    """本人が返信先を指名しているなら client_name の検査で止めない（検索もしない）。"""
    fake = FakeGmail([_inbound()])
    out = MailReplySkill(gmail=fake, bedrock=FakeBedrock()).run(
        MailReplyInput(client_name="今日のメール", target_message_id="m0"), _ctx()
    )
    assert fake.last_query is None  # 指名時はそもそも検索しない
    assert out.created is True


def test_output_client_name_is_scrubbed() -> None:
    fake = FakeGmail([_inbound()])
    out = MailReplySkill(gmail=fake, bedrock=FakeBedrock()).run(
        MailReplyInput(client_name="tanaka@example.com 森ビル"), _ctx()
    )
    assert "tanaka@example.com" not in out.client_name
