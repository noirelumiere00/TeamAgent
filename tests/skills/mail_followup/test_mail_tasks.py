"""架空スレッドだけで未返信・ボール・JST 境界を検証する。"""

from __future__ import annotations

import base64
import datetime as dt
from dataclasses import replace
from typing import Any

import pytest

from teamagent.adapters.gmail_client import GmailMessage, GmailMessageRef
from teamagent.adapters.oauth_token_store import InMemoryTokenStore, OAuthToken
from teamagent.skills._shared import mail_tasks
from teamagent.skills.base import SkillContext
from teamagent.skills.mail_followup.schema import MailFollowupInput
from teamagent.skills.mail_followup.skill import MailFollowupSkill
from teamagent.skills.mail_summary.schema import MailSummaryInput
from teamagent.skills.mail_summary.skill import MailSummarySkill
from teamagent.skills.morning_digest.calendar_window import JST

OWNER = "user@own.example"
OTHER = "架空広告株式会社 山田 <yamada@outside.example>"
NOW = dt.datetime(2026, 11, 11, 12, tzinfo=JST)
CTX = SkillContext(request_id="fictional", user_id="U1", metadata={"user_email": OWNER})


def message(
    key: str,
    body: str = "見積の確認をお願いします。",
    *,
    sender: str = OTHER,
    to: str = OWNER,
    cc: str = "",
    at: dt.datetime = NOW,
    thread: str | None = None,
    headers: dict[str, str] | None = None,
    labels: tuple[str, ...] = (),
    mime: str = "text/plain",
) -> GmailMessage:
    return GmailMessage(
        id=key,
        thread_id=thread or key,
        label_ids=labels,
        snippet="",
        internal_date_ms=int(at.timestamp() * 1000),
        headers={
            "From": sender,
            "To": to,
            "Cc": cc,
            "Subject": f"架空案件 {key}",
            **(headers or {}),
        },
        payload={
            "mimeType": mime,
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()},
        },
    )


class Gmail:
    def __init__(self, messages: list[GmailMessage]) -> None:
        self.messages = messages
        self.queries: list[str] = []
        self.formats: list[str] = []

    def list_messages(
        self, query: str, request_id: str, **kwargs: Any
    ) -> tuple[list[GmailMessageRef], None]:
        self.queries.append(query)
        return [GmailMessageRef(m.id, m.thread_id) for m in reversed(self.messages)], None

    def get_thread(
        self, thread_id: str, request_id: str, *, format: str, user_id: str = "me"
    ) -> list[GmailMessage]:
        assert user_id == "me"
        self.formats.append(format)
        return [m for m in self.messages if m.thread_id == thread_id]


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mail_tasks, "now_jst", lambda: NOW)
    monkeypatch.setattr("teamagent.skills.mail_followup.skill.now_jst", lambda: NOW)
    monkeypatch.setattr("teamagent.skills.mail_summary.skill.now_jst", lambda: NOW)


@pytest.mark.parametrize("client", ["", "架空広告"])
def test_unreplied_requires_human_to_or_cc_request_and_no_later_reply(
    client: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAIL_EXCLUDE_BULK", "false")
    messages = [
        message("pending"),
        message("cc", to="colleague@own.example", cc=f"User <{OWNER.upper()}>"),
        message("bcc", to="somebody@own.example"),
        message("not-human", sender="no-reply@outside.example"),
        message("notification", sender="notifications@outside.example"),
        message("list", headers={"List-Unsubscribe": "<https://outside.example/unsubscribe>"}),
        message("auto", headers={"Auto-Submitted": "auto-generated"}),
        message("fyi", "本日の状況共有です。対応は不要です。"),
        message("no-request", "資料を送付しました。"),
        message("quoted", "ありがとうございました。\nOn Tue someone wrote:\n見積をお願いします。"),
        message("replied"),
        message(
            "reply",
            "送付しました。",
            sender=OWNER,
            thread="replied",
            at=NOW + dt.timedelta(minutes=1),
            labels=("SENT",),
        ),
        message("alias"),
        message(
            "alias-reply",
            "送付しました。",
            sender="alias@own.example",
            thread="alias",
            labels=("SENT",),
        ),
        message("draft-pending"),
        message("draft", sender=OWNER, thread="draft-pending", labels=("DRAFT",)),
        message(
            "html",
            "<p>納期を教えてください。</p><blockquote>過去の本文</blockquote>",
            mime="text/html",
        ),
    ]
    gmail = Gmail(messages)
    out = MailFollowupSkill(gmail=gmail).run(
        MailFollowupInput(client_name=client, period="今日"), CTX
    )
    assert {i.subject_scrubbed for i in out.items} == {
        f"架空案件 {key}" for key in ("pending", "cc", "draft-pending", "html")
    }
    assert set(gmail.formats) == {"full"}
    assert all("in:inbox" not in q for q in gmail.queries)  # アーカイブも対象
    assert all(i.mail_url and i.occurred_at_display and i.request_summary for i in out.items)
    pending = next(
        i
        for i in out.items
        if i.subject_scrubbed.endswith("pending") and "draft" not in i.subject_scrubbed
    )
    assert pending.counterpart_name == "架空広告株式会社 山田"
    assert "yamada@" not in out.model_dump_json()


def test_later_fyi_does_not_hide_an_unanswered_request_but_new_request_after_reply_returns() -> (
    None
):
    gmail = Gmail(
        [
            message("pending"),
            message("fyi", "資料を送付しました。", thread="pending"),
            message("done"),
            message("reply", sender=OWNER, thread="done", labels=("SENT",)),
            message("new", thread="done"),
        ]
    )
    out = MailFollowupSkill(gmail=gmail).run(MailFollowupInput(), CTX)
    assert {i.subject_scrubbed for i in out.items} == {"架空案件 pending", "架空案件 new"}


def test_ball_uses_last_sent_request_and_company_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", "own.example,subsidiary.example")
    gmail = Gmail(
        [
            message("self-wait"),
            message("external-wait"),
            message(
                "outgoing",
                "金曜までに見積をご提示ください。",
                sender=OWNER,
                to=OTHER,
                thread="external-wait",
                labels=("SENT",),
            ),
            message("colleague", sender="同僚 <staff@subsidiary.example>", to=OTHER, cc=OWNER),
            message("done"),
            message(
                "thanks",
                "ありがとうございました。\n> 依頼をお願いします。",
                sender=OWNER,
                to=OTHER,
                thread="done",
                labels=("SENT",),
            ),
            message("unrelated", to="not-user@own.example"),
            message("only-bcc", to="not-user@own.example", headers={"Bcc": OWNER}),
            message("draft", sender=OWNER, to=OTHER, thread="self-wait", labels=("DRAFT",)),
        ]
    )
    out = MailSummarySkill(gmail=gmail).run(MailSummaryInput(mode="ball", period="今週"), CTX)
    assert {i.subject_scrubbed for i in out.waiting_on_self} == {"架空案件 self-wait"}
    assert {i.subject_scrubbed for i in out.waiting_on_external} == {
        "架空案件 outgoing",
        "架空案件 colleague",
    }
    assert "相手待ち（相手にボール・社外）: 2件" in out.summary
    assert "自分待ち（自社にボール）: 1件" in out.summary
    assert "代理店のドメイン名寄せがない" in out.summary
    assert out.total_cost_usd == 0


@pytest.mark.parametrize(
    ("period", "start", "end"),
    [
        ("今日", "2026-11-11T00:00:00+09:00", NOW + dt.timedelta(milliseconds=1)),
        ("昨日", "2026-11-10T00:00:00+09:00", dt.datetime(2026, 11, 11, tzinfo=JST)),
        ("今週", "2026-11-09T00:00:00+09:00", NOW + dt.timedelta(milliseconds=1)),
        ("先週", "2026-11-02T00:00:00+09:00", dt.datetime(2026, 11, 9, tzinfo=JST)),
        ("今月", "2026-11-01T00:00:00+09:00", NOW + dt.timedelta(milliseconds=1)),
    ],
)
def test_period_boundaries_are_jst_calendar_and_applied_to_both_tools(
    period: Any, start: str, end: dt.datetime
) -> None:
    first = dt.datetime.fromisoformat(start)
    gmail = Gmail(
        [
            message("before", at=first - dt.timedelta(milliseconds=1)),
            message("start", at=first),
            message("end", at=end),
            message("last", at=end - dt.timedelta(milliseconds=1)),
        ]
    )
    followup = MailFollowupSkill(gmail=gmail).run(MailFollowupInput(period=period), CTX)
    ball = MailSummarySkill(gmail=gmail).run(MailSummaryInput(mode="ball", period=period), CTX)
    assert "直近14日" not in followup.note
    expected = {"架空案件 start", "架空案件 last"}
    assert {i.subject_scrubbed for i in followup.items} == expected
    assert {i.subject_scrubbed for i in ball.waiting_on_self} == expected
    assert all(f"after:{int(first.timestamp()) - 1}" in q for q in gmail.queries)


def test_only_request_sentence_is_returned_and_scrubbed() -> None:
    gmail = Gmail(
        [
            message(
                "brief",
                "秘密の経緯。見積をsecret@outside.exampleへ送ってください。\n" + "不要な本文" * 200,
            )
        ]
    )
    out = MailFollowupSkill(gmail=gmail).run(MailFollowupInput(), CTX)
    assert len(out.items) == 1
    assert out.items[0].request_summary.startswith("見積を")
    assert "secret@" not in out.model_dump_json()
    assert "秘密の経緯" not in out.model_dump_json()
    assert "不要な本文" not in out.model_dump_json()


@pytest.mark.parametrize(
    "skill,input",
    [
        (MailFollowupSkill, MailFollowupInput(period="今日")),
        (MailSummarySkill, MailSummaryInput(mode="ball")),
    ],
)
def test_own_google_token_only_and_readonly(
    skill: Any, input: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    own_token = OAuthToken(refresh_token="fictional-own")
    other_token = OAuthToken(refresh_token="fictional-other")
    store = InMemoryTokenStore({OWNER: own_token, "another@own.example": other_token})

    def resolve(token: OAuthToken, *, readonly: bool = True) -> Gmail:
        assert token == own_token
        assert readonly
        return Gmail([message("pending")])

    monkeypatch.setattr("teamagent.adapters.gmail_client.GmailClient.from_user_token", resolve)
    out = skill(token_store=store).run(input, CTX)
    assert out.connection == "live"
    assert (
        skill(token_store=store)
        .run(input, replace(CTX, metadata={"user_email": "unconnected@own.example"}))
        .error
        == "not_connected"
    )


@pytest.mark.parametrize(
    "body",
    [
        "ご依頼の件、承知しました。引き続きよろしくお願いします。",
        "資料を送付しました。何卒よろしくお願いいたします。",
        "資料を送付しました。ご不明な点がございましたらお知らせください。",
    ],
)
def test_acknowledgements_and_generic_closings_are_not_requests(body: str) -> None:
    gmail = Gmail([message("ack", body)])
    assert MailFollowupSkill(gmail=gmail).run(MailFollowupInput(), CTX).items == []
    out = MailSummarySkill(gmail=gmail).run(MailSummaryInput(mode="ball"), CTX)
    assert out.waiting_on_self == out.waiting_on_external == []
