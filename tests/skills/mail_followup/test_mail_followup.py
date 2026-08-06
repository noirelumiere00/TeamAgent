"""mail_followup Skill のオフラインテスト（課金0・外部I/O無し）。

fake GmailClient / InMemoryTokenStore を注入し、死守ライン（G1 本人限定 / G2 連携必須 /
G3 マスク / G5 クエリ限定）と放置日数・並び順・返信済み除外を検証する。実 Gmail 不要。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from teamagent.adapters.oauth_token_store import InMemoryTokenStore
from teamagent.skills.base import SkillContext
from teamagent.skills.mail_followup.schema import MailFollowupInput
from teamagent.skills.mail_followup.skill import (
    MailFollowupSkill,
    _hash_id,
    _idle_days,
    _mask_email,
)

OWNER = "s-komata@vectorinc.co.jp"
NOW_MS = 1_700_000_000_000  # 固定 now（テスト決定性）
MS_PER_DAY = 86_400_000


# ── fakes ─────────────────────────────────────────────────────────────────


@dataclass
class _Ref:
    id: str
    thread_id: str = "t"


@dataclass
class _Msg:
    headers: dict[str, str]
    internal_date_ms: int | None
    id: str = ""
    thread_id: str = ""
    label_ids: tuple[str, ...] = ()


class FakeGmail:
    """list_messages / get_thread(metadata) だけを持つ最小 fake。"""

    def __init__(self, msgs: list[_Msg], *, refs: list[_Ref] | None = None) -> None:
        for i, msg in enumerate(msgs):
            if not msg.id:
                msg.id = f"m{i}"
            if not msg.thread_id:
                msg.thread_id = f"t{i}"
        self._msgs = msgs
        self._refs = (
            list(refs)
            if refs is not None
            else [_Ref(id=msg.id, thread_id=msg.thread_id) for msg in msgs]
        )
        self.last_query: str | None = None
        self.last_format: str | None = None
        self.thread_calls: list[str] = []
        self.get_message_calls = 0

    def list_messages(
        self,
        query: str | None,
        request_id: str,
        *,
        label_ids: Any = None,
        max_results: int = 50,
        **kw: Any,
    ) -> tuple[list[_Ref], None]:
        self.last_query = query
        return (self._refs[:max_results], None)

    def get_thread(
        self,
        thread_id: str,
        request_id: str,
        *,
        format: str = "full",
        user_id: str = "me",
    ) -> list[_Msg]:
        self.last_format = format
        self.thread_calls.append(thread_id)
        return [msg for msg in self._msgs if msg.thread_id == thread_id]

    def get_message(
        self, msg_id: str, request_id: str, *, format: str = "full", user_id: str = "me"
    ) -> _Msg:
        self.get_message_calls += 1
        self.last_format = format
        return next(msg for msg in self._msgs if msg.id == msg_id)


def _ctx(user_email: str | None = OWNER) -> SkillContext:
    return SkillContext(request_id="r-test", user_id="U1", metadata={"user_email": user_email})


def _msg(
    sender: str,
    subject: str,
    days_ago: int,
    *,
    thread_id: str = "",
    msg_id: str = "",
    label_ids: tuple[str, ...] = (),
    extra_headers: dict[str, str] | None = None,
) -> _Msg:
    headers = {"From": sender, "Subject": subject}
    headers.update(extra_headers or {})
    return _Msg(
        headers=headers,
        internal_date_ms=NOW_MS - days_ago * MS_PER_DAY,
        id=msg_id,
        thread_id=thread_id,
        label_ids=label_ids,
    )


# ── G1 / G2 fail-closed ────────────────────────────────────────────────────


def test_g1_requires_user_email() -> None:
    skill = MailFollowupSkill(gmail=FakeGmail([]), now_ms=NOW_MS)
    with pytest.raises(PermissionError):
        skill.run(MailFollowupInput(client_name="森ビル"), _ctx(user_email=None))


def test_g1_blank_user_email_fails_closed() -> None:
    skill = MailFollowupSkill(gmail=FakeGmail([]), now_ms=NOW_MS)
    with pytest.raises(PermissionError):
        skill.run(MailFollowupInput(client_name="森ビル"), _ctx(user_email="   "))


def test_g2_unconnected_fails_closed() -> None:
    # gmail 未注入 + 空 TokenStore → 本人トークン無し → fail-closed。
    skill = MailFollowupSkill(token_store=InMemoryTokenStore(), now_ms=NOW_MS)
    with pytest.raises(PermissionError):
        skill.run(MailFollowupInput(client_name="森ビル"), _ctx())


def test_g2_no_token_store_fails_closed() -> None:
    skill = MailFollowupSkill(now_ms=NOW_MS)  # token_store も gmail も無し
    with pytest.raises(PermissionError):
        skill.run(MailFollowupInput(client_name="森ビル"), _ctx())


# ── happy path / 並び順 / マスク / 正直ラベル ──────────────────────────────


def test_happy_path_sorted_and_masked() -> None:
    msgs = [
        _msg("田中 <tanaka@moribuild.co.jp>", "ご提案の件", days_ago=2),
        _msg("佐藤 <sato@moribuild.co.jp>", "請求書送付", days_ago=10),
        _msg("鈴木 <suzuki@moribuild.co.jp>", "日程調整", days_ago=5),
    ]
    skill = MailFollowupSkill(gmail=FakeGmail(msgs), now_ms=NOW_MS)
    out = skill.run(MailFollowupInput(client_name="森ビル", lookback_days=30), _ctx())

    assert out.scanned_count == 3
    assert out.total_cost_usd == 0.0
    assert out.inbox_owner_masked == "s***@vectorinc.co.jp"
    assert out.note  # 正直な但し書きが入る
    # 放置日数が大きい順
    assert [it.idle_days for it in out.items] == [10, 5, 2]
    # 相手アドレスはマスクされ、生アドレスは出ない
    for it in out.items:
        assert it.counterpart_masked.endswith("@moribuild.co.jp")
        assert "@moribuild.co.jp" in it.counterpart_masked
        assert "tanaka@" not in it.counterpart_masked
        assert it.evidence_ref and "@" not in it.evidence_ref


def test_bulk_noreply_and_daily_subject_are_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAIL_EXCLUDE_BULK", raising=False)
    monkeypatch.delenv("MAIL_EXCLUDE_SUBJECT_KEYWORDS", raising=False)
    msgs = [
        _msg(
            "配信 <news@example.com>",
            "ニュースレター",
            days_ago=5,
            extra_headers={"List-Id": "newsletter.example.com"},
        ),
        _msg("通知 <noreply@example.com>", "自動通知", days_ago=4),
        _msg("営業企画 <sales@example.com>", "営業日報", days_ago=3),
        _msg("田中 <tanaka@example.com>", "個別相談", days_ago=2),
    ]

    out = MailFollowupSkill(gmail=FakeGmail(msgs), now_ms=NOW_MS).run(
        MailFollowupInput(client_name="Example"),
        _ctx(),
    )

    assert [item.subject_scrubbed for item in out.items] == ["個別相談"]


def test_personal_mail_is_kept_by_bulk_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAIL_EXCLUDE_BULK", raising=False)
    monkeypatch.delenv("MAIL_EXCLUDE_SUBJECT_KEYWORDS", raising=False)
    msg = _msg("田中 <tanaka@example.com>", "個別のご相談", days_ago=1)

    out = MailFollowupSkill(gmail=FakeGmail([msg]), now_ms=NOW_MS).run(
        MailFollowupInput(client_name="Example"),
        _ctx(),
    )

    assert len(out.items) == 1
    assert out.items[0].subject_scrubbed == "個別のご相談"


def test_idle_days_filter() -> None:
    msgs = [
        _msg("a@x.co.jp", "件名A", days_ago=2),
        _msg("b@x.co.jp", "件名B", days_ago=9),
    ]
    skill = MailFollowupSkill(gmail=FakeGmail(msgs), now_ms=NOW_MS)
    out = skill.run(MailFollowupInput(client_name="X社", idle_days=5, lookback_days=30), _ctx())
    assert [it.idle_days for it in out.items] == [9]


def test_query_is_client_scoped_and_thread_read_is_metadata_only() -> None:
    fake = FakeGmail([_msg("a@x.co.jp", "件名", days_ago=1)])
    skill = MailFollowupSkill(gmail=fake, now_ms=NOW_MS)
    skill.run(MailFollowupInput(client_name="INPEX", lookback_days=7), _ctx())
    # G5: client 名 + 期間 + 受信限定がクエリに入る
    assert '"INPEX"' in (fake.last_query or "")
    assert "newer_than:7d" in (fake.last_query or "")
    assert "-in:sent" in (fake.last_query or "")
    # G6 構造的: 本文は読まない（metadata のみ）
    assert fake.last_format == "metadata"
    assert fake.get_message_calls == 0


def test_self_reply_last_is_excluded() -> None:
    msgs = [
        _msg(
            "client@example.com",
            "ご確認",
            days_ago=3,
            thread_id="thread-replied",
            msg_id="incoming",
            label_ids=("INBOX",),
        ),
        _msg(
            OWNER,
            "Re: ご確認",
            days_ago=1,
            thread_id="thread-replied",
            msg_id="reply",
        ),
    ]
    fake = FakeGmail(msgs, refs=[_Ref(id="incoming", thread_id="thread-replied")])

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="Example"),
        _ctx(),
    )

    assert out.items == []
    assert fake.thread_calls == ["thread-replied"]
    assert "返信が最後" in out.note


def test_last_external_message_is_used_after_chronological_sort() -> None:
    # threads.get の戻り順に依存せず internal_date_ms で末尾を決める。
    msgs = [
        _msg(
            "client@example.com",
            "追加のお願い",
            days_ago=2,
            thread_id="thread-open",
            msg_id="latest-external",
            label_ids=("INBOX",),
        ),
        _msg(
            OWNER,
            "Re: 最初のお願い",
            days_ago=5,
            thread_id="thread-open",
            msg_id="older-reply",
            label_ids=("SENT",),
        ),
    ]
    fake = FakeGmail(msgs, refs=[_Ref(id="latest-external", thread_id="thread-open")])

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="Example"),
        _ctx(),
    )

    assert len(out.items) == 1
    assert out.items[0].subject_scrubbed == "追加のお願い"
    assert out.items[0].idle_days == 2
    assert out.items[0].evidence_ref == _hash_id("latest-external")


def test_duplicate_message_refs_fetch_each_thread_once() -> None:
    msgs = [
        _msg(
            "a@example.com",
            "古い受信",
            days_ago=5,
            thread_id="same-thread",
            msg_id="old",
        ),
        _msg(
            "a@example.com",
            "新しい受信",
            days_ago=2,
            thread_id="same-thread",
            msg_id="new",
        ),
        _msg(
            "b@example.com",
            "別スレッド",
            days_ago=1,
            thread_id="other-thread",
            msg_id="other",
        ),
    ]
    refs = [
        _Ref(id="new", thread_id="same-thread"),
        _Ref(id="old", thread_id="same-thread"),
        _Ref(id="other", thread_id="other-thread"),
    ]
    fake = FakeGmail(msgs, refs=refs)

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="Example"),
        _ctx(),
    )

    assert out.scanned_count == 3
    assert len(out.items) == 2
    assert fake.thread_calls == ["same-thread", "other-thread"]


def test_sent_label_identifies_send_as_alias_reply() -> None:
    msgs = [
        _msg(
            "client@example.com",
            "ご確認",
            days_ago=3,
            thread_id="thread-alias",
            msg_id="incoming",
        ),
        _msg(
            "sales-alias@vectorinc.co.jp",
            "Re: ご確認",
            days_ago=1,
            thread_id="thread-alias",
            msg_id="alias-reply",
            label_ids=("SENT",),
        ),
    ]
    fake = FakeGmail(msgs, refs=[_Ref(id="incoming", thread_id="thread-alias")])

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="Example"),
        _ctx(),
    )

    assert out.items == []


def test_latest_draft_does_not_count_as_a_sent_reply() -> None:
    msgs = [
        _msg(
            "client@example.com",
            "ご確認",
            days_ago=3,
            thread_id="thread-draft",
            msg_id="incoming",
            label_ids=("INBOX",),
        ),
        _msg(
            OWNER,
            "Re: ご確認",
            days_ago=1,
            thread_id="thread-draft",
            msg_id="draft",
            label_ids=("DRAFT",),
        ),
    ]
    fake = FakeGmail(msgs, refs=[_Ref(id="incoming", thread_id="thread-draft")])

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="Example"),
        _ctx(),
    )

    assert len(out.items) == 1
    assert out.items[0].subject_scrubbed == "ご確認"
    assert out.items[0].evidence_ref == _hash_id("incoming")


def test_subject_is_scrubbed_and_truncated() -> None:
    long_subject = "重要 " + "x" * 200
    msgs = [_msg("a@x.co.jp", long_subject, days_ago=1)]
    skill = MailFollowupSkill(gmail=FakeGmail(msgs), now_ms=NOW_MS)
    out = skill.run(MailFollowupInput(client_name="X社"), _ctx())
    assert len(out.items[0].subject_scrubbed) <= 80


# ── 純粋関数 ────────────────────────────────────────────────────────────────


def test_mask_email() -> None:
    assert _mask_email("tanaka@moribuild.co.jp") == "t***@moribuild.co.jp"
    assert _mask_email("garbage") == "***"


def test_hash_id_is_not_raw() -> None:
    h = _hash_id("18f0a1b2c3")
    assert h != "18f0a1b2c3"
    assert len(h) == 12


def test_idle_days_clamps_negative() -> None:
    assert _idle_days(NOW_MS + MS_PER_DAY, NOW_MS) == 0  # 未来日時は 0 に丸める
    assert _idle_days(None, NOW_MS) == 0
    assert _idle_days(NOW_MS - 3 * MS_PER_DAY, NOW_MS) == 3


# ── レビュー指摘の回帰テスト ────────────────────────────────────────────────


def test_idle_days_widens_scan_window() -> None:
    """idle_days > lookback_days のとき走査窓を広げる（広げないと post-filter で全滅・誤答）。"""
    fake = FakeGmail([])
    skill = MailFollowupSkill(gmail=fake, now_ms=NOW_MS)
    skill.run(MailFollowupInput(client_name="森ビル", idle_days=30, lookback_days=14), _ctx())
    assert "newer_than:33d" in (fake.last_query or "")  # min(90, max(14, 30+3))


def test_credential_error_becomes_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """認証情報の解決失敗(ValueError)は PermissionError に変換（dispatch が連携案内に出せる）。"""
    from teamagent.adapters import gmail_client as gc
    from teamagent.adapters.oauth_token_store import OAuthToken

    def _boom(token: Any, *, readonly: bool = True) -> Any:
        raise ValueError("GOOGLE_CLIENT_ID 未設定")

    monkeypatch.setattr(gc.GmailClient, "from_user_token", staticmethod(_boom))
    store = InMemoryTokenStore({OWNER: OAuthToken(refresh_token="x")})
    skill = MailFollowupSkill(token_store=store, now_ms=NOW_MS)
    with pytest.raises(PermissionError):
        skill.run(MailFollowupInput(client_name="森ビル"), _ctx())
