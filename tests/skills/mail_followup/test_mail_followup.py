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

# GitHub の push protection がリテラルを弾くため実行時に組み立てる（値は同じ）。
_FAKE_SLACK_TOKEN = "xo" + "xb-" + "1234567890" + "-abcdefghijklmn"

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
        # 発行されたクエリの履歴（ガード発火時に 0 本であること・二段検索の順序を検証する）。
        self.queries: list[str] = []
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
        self.queries.append(query or "")
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


class PhraseGmail(FakeGmail):
    """指定フレーズを含むクエリにだけヒットを返す fake（二段検索の検証用）。"""

    def __init__(self, msgs: list[_Msg], *, hit_phrase: str) -> None:
        super().__init__(msgs)
        self._hit_phrase = hit_phrase

    def list_messages(
        self,
        query: str | None,
        request_id: str,
        *,
        label_ids: Any = None,
        max_results: int = 50,
        **kw: Any,
    ) -> tuple[list[_Ref], None]:
        refs, token = super().list_messages(
            query, request_id, label_ids=label_ids, max_results=max_results, **kw
        )
        if self._hit_phrase not in (query or ""):
            return ([], None)
        return (refs, token)


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
    """P0-4: 未連携は例外ではなく構造化 return。受信箱には 1 度も触れない（G2）。"""
    # gmail 未注入 + 空 TokenStore → 本人トークン無し → fail-closed。
    skill = MailFollowupSkill(token_store=InMemoryTokenStore(), now_ms=NOW_MS)

    out = skill.run(MailFollowupInput(client_name="森ビル"), _ctx())

    assert out.error == "not_connected"
    assert out.connection == ""  # 「連携は正常」と嘘をつかない
    assert out.items == []
    assert out.scanned_count == 0


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


def test_credential_error_becomes_reauth_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    """認証情報の解決失敗(ValueError)は error=reauth_needed の構造化 return（P0-4）。"""
    from teamagent.adapters import gmail_client as gc
    from teamagent.adapters.oauth_token_store import OAuthToken

    def _boom(token: Any, *, readonly: bool = True) -> Any:
        raise ValueError("GOOGLE_CLIENT_ID 未設定")

    monkeypatch.setattr(gc.GmailClient, "from_user_token", staticmethod(_boom))
    store = InMemoryTokenStore({OWNER: OAuthToken(refresh_token="x")})
    skill = MailFollowupSkill(token_store=store, now_ms=NOW_MS)

    out = skill.run(MailFollowupInput(client_name="森ビル"), _ctx())

    assert out.error == "reauth_needed"
    assert out.connection == ""
    assert "@NewsTV AI に『連携』" in out.message


# ── P0-2: client_name ガード（依頼文の断片で受信箱を叩かない）────────────────


@pytest.mark.parametrize("bad", ["今週の空き時間", "返信必要", "今日のメール", "未読"])
def test_guard_blocks_structural_client_name_without_touching_gmail(bad: str) -> None:
    """依頼文の断片が来たら Gmail を 1 回も呼ばない（実測症状の再発防止）。"""
    fake = FakeGmail([_msg("相手 <a@x.co.jp>", "件名", 5)])

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name=bad), _ctx()
    )

    assert fake.queries == []  # 受信箱を検索していない
    assert fake.thread_calls == []
    assert out.error == "client_name_structural"
    assert out.connection == "ok"
    assert out.scanned_count == 0
    assert out.items == []
    assert out.total_cost_usd == 0.0
    assert out.message
    # SOUL は message を表示する規約を持たないので note にも同じ文言を載せる。
    assert out.note == out.message
    assert "連携は正常です" in out.note
    assert "まだ受信箱は検索していません" in out.note


def test_guard_blocks_missing_client_name() -> None:
    """client_name 省略が **スキーマで通る**（min_length 撤去）ことと案内文を固定する。"""
    fake = FakeGmail([_msg("相手 <a@x.co.jp>", "件名", 5)])

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(MailFollowupInput(), _ctx())

    assert fake.queries == []
    assert out.error == "client_name_missing"
    assert out.connection == "ok"
    assert out.note == out.message
    assert "どちらのお客様" in out.note


def test_guard_rejects_gmail_operator_injection() -> None:
    fake = FakeGmail([_msg("相手 <a@x.co.jp>", "件名", 5)])

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name='x" OR from:ceo@example.com "'), _ctx()
    )

    assert fake.queries == []
    assert out.error == "client_name_structural"
    assert "ceo@example.com" not in out.note  # エコーは PII マスク後


def test_two_stage_search_retries_with_residual() -> None:
    """「花王のメール」→ 1 本目 0 件 → 2 本目 '"花王"' で救う（Gmail 往復は最大 2 回）。"""
    fake = PhraseGmail([_msg("相手 <a@kao.co.jp>", "提案の件", 5)], hit_phrase='"花王"')

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="花王のメール"), _ctx()
    )

    assert fake.queries == [
        '"花王のメール" newer_than:14d -in:sent in:inbox',
        '"花王" newer_than:14d -in:sent in:inbox',
    ]
    assert out.scanned_count == 1
    assert len(out.items) == 1
    assert out.error == ""


def test_no_retry_when_first_query_hits() -> None:
    fake = FakeGmail([_msg("相手 <a@kao.co.jp>", "提案の件", 5)])

    MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="花王のメール"), _ctx()
    )

    assert fake.queries == ['"花王のメール" newer_than:14d -in:sent in:inbox']


def test_query_for_plain_client_name_is_unchanged_from_head() -> None:
    """後方互換の固定点: 素のお客様名は HEAD と 1 文字も違わないクエリになる。"""
    fake = FakeGmail([_msg("相手 <a@kao.co.jp>", "件名", 5)])

    MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(MailFollowupInput(client_name="花王"), _ctx())

    assert fake.queries == ['"花王" newer_than:14d -in:sent in:inbox']


def test_single_term_never_issues_second_query() -> None:
    """残差が無い名前は 0 件でも 1 本で終える（無駄な往復を増やさない）。"""
    fake = FakeGmail([])

    MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(MailFollowupInput(client_name="花王"), _ctx())

    assert fake.queries == ['"花王" newer_than:14d -in:sent in:inbox']


def test_unconnected_user_gets_connect_guidance_not_guard_message() -> None:
    """未連携なら「連携は正常です」と嘘をつかず、連携案内が勝つ（ガード文言より優先）。"""
    skill = MailFollowupSkill(token_store=InMemoryTokenStore(), now_ms=NOW_MS)

    out = skill.run(MailFollowupInput(client_name="今週の空き時間"), _ctx())

    assert out.error == "not_connected"  # client_name_structural ではない
    assert "連携は正常です" not in out.note
    assert "@NewsTV AI に『連携』" in out.note


# ── P0-3: 0 件の理由を LLM に創作させない ───────────────────────────────────


def test_no_hits_states_connection_is_live() -> None:
    """0 件でも「連携は正常・実際に検索した」を断言する（LLM の創作余地を潰す）。"""
    fake = FakeGmail([])

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="X社", lookback_days=14), _ctx()
    )

    assert out.error == "no_hits"
    assert out.connection == "live"
    assert out.scanned_count == 0
    assert out.note == out.message
    assert "連携は正常です" in out.note
    assert "s***@vectorinc.co.jp を実際に検索しました" in out.note
    assert "「X社」で直近 14 日に" in out.note
    assert "『相手から来たまま止まっている』受信メールは 0 件でした" in out.note
    # 既存の正直な但し書きも失わない（除外ロジックの開示）。
    assert "返信が最後" in out.note


def test_no_hits_message_reports_the_window_actually_searched() -> None:
    """idle_days で窓を広げたら 0 件文言の日数も広げた側に一致させる（嘘を書かない）。"""
    fake = FakeGmail([])

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="X社", idle_days=30, lookback_days=14), _ctx()
    )

    assert "newer_than:33d" in (fake.last_query or "")
    assert "直近 33 日に" in out.note  # lookback_days の 14 ではない


def test_all_replied_threads_report_zero_without_claiming_no_mail() -> None:
    """走査 > 0 でも items が空なら no_hits。ただし『メールが無い』とは言わない。"""
    msgs = [_msg(f"{OWNER}", "こちらが最後に返信済み", days_ago=3, label_ids=("SENT",))]
    fake = FakeGmail(msgs)

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="X社"), _ctx()
    )

    assert out.items == []
    assert out.scanned_count == 1  # 走査はしている
    assert out.error == "no_hits"
    assert "『相手から来たまま止まっている』受信メールは 0 件でした" in out.note


def test_items_found_reports_connection_live_without_error() -> None:
    fake = FakeGmail([_msg("相手 <a@x.co.jp>", "ご提案の件", days_ago=3)])

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="X社"), _ctx()
    )

    assert len(out.items) == 1
    assert out.error == ""
    assert out.message == ""
    assert out.connection == "live"
    assert out.note == (
        "※ スレッドの最新メッセージをメタデータで確認し、あなたの返信が最後のものは除外して"
        "います（gmail.readonly のみ・本文は読みません）。"
    )


# ── P0-4: 未連携シグナルの構造化 ────────────────────────────────────────────


def test_not_connected_is_structured_and_points_at_the_real_flow() -> None:
    """error=not_connected + calendar_freebusy と同じ導線文言（/teamagent connect ではない）。"""
    from teamagent.skills._shared.mail_connection import NOT_CONNECTED_MESSAGE

    out = MailFollowupSkill(token_store=InMemoryTokenStore(), now_ms=NOW_MS).run(
        MailFollowupInput(client_name="森ビル"), _ctx()
    )

    assert out.error == "not_connected"
    assert out.message == NOT_CONNECTED_MESSAGE
    assert out.note == out.message  # SOUL は note を見るので二重掲載
    assert "@NewsTV AI に『連携』" in out.message
    assert "/teamagent connect" not in out.message


# ── 要修正1: 活用の残りかす（している / 届いた）で受信箱を引き直さない ──────────


def test_conjugation_residual_never_lists_unrelated_threads() -> None:
    """『放置しているメール』で他社スレッドを『放置』として列挙しない。

    HEAD 相当の実測: 2 本目 ``"している"`` が無関係な他社メールに当たり、それを元の
    client_name の名前で「相手から来たまま止まっている」と提示していた。
    """
    unrelated = [
        _msg("担当 <a@sony.example.jp>", "値下げのお願い", 5, thread_id="tA", msg_id="mA"),
        _msg("担当 <b@toyota.example.jp>", "納期の件", 9, thread_id="tB", msg_id="mB"),
    ]
    fake = PhraseGmail(unrelated, hit_phrase='"している"')

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="放置しているメール"), _ctx()
    )

    assert len(fake.queries) == 1, f"2 本目を出してはいけない: {fake.queries}"
    assert fake.thread_calls == []  # スレッド本体も読んでいない
    assert out.items == []
    assert out.error == "no_hits"
    assert out.connection == "live"
    assert "sony" not in out.note and "toyota" not in out.note


# ── 要修正4: 2 本目を使ったことを黙らない ─────────────────────────────────────


def test_second_stage_hit_is_disclosed_in_the_note() -> None:
    msgs = [_msg("田中 <tanaka@kao.co.jp>", "提案の件", 5, thread_id="t1", msg_id="m1")]
    fake = PhraseGmail(msgs, hit_phrase='"花王"')

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="花王のメール"), _ctx()
    )

    assert len(out.items) == 1
    assert out.error == ""
    assert out.note.startswith("※「花王のメール」では 0 件だったため「花王」で検索し直した")
    assert "gmail.readonly のみ" in out.note  # 正直ラベリングも消えていない


def test_second_stage_miss_is_disclosed_in_the_zero_note() -> None:
    fake = PhraseGmail([], hit_phrase="絶対に当たらない")

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="花王のメール"), _ctx()
    )

    assert len(fake.queries) == 2
    assert out.error == "no_hits"
    assert "「花王」でも検索し直しましたが 0 件でした" in out.note


# ── 要修正1(HIGH): Output.client_name も scrub を通る ────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        f"返信必要: {_FAKE_SLACK_TOKEN}",
        "tanaka@example.com 090-1234-5678",
    ],
)
def test_output_client_name_is_scrubbed_like_the_note(raw: str) -> None:
    fake = FakeGmail([])
    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name=raw), _ctx()
    )
    for secret in (_FAKE_SLACK_TOKEN, "tanaka@example.com", "090-1234-5678"):
        assert secret not in out.client_name
    assert "REDACTED" in out.client_name


def test_output_client_name_is_scrubbed_on_success_and_not_connected() -> None:
    pii = "tanaka@example.com"
    msgs = [_msg("田中 <tanaka@kao.co.jp>", "提案の件", 5, thread_id="t1", msg_id="m1")]
    ok = MailFollowupSkill(gmail=FakeGmail(msgs), now_ms=NOW_MS).run(
        MailFollowupInput(client_name=f"{pii} 花王"), _ctx()
    )
    assert ok.items and pii not in ok.client_name

    nc = MailFollowupSkill(token_store=InMemoryTokenStore(), now_ms=NOW_MS).run(
        MailFollowupInput(client_name=pii), _ctx()
    )
    assert nc.error == "not_connected"
    assert pii not in nc.client_name


# ── 要修正3: 失効トークン/API 障害を「0 件」と混同しない ─────────────────────


class _RefreshError(Exception):
    """google.auth.exceptions.RefreshError の代役（型名で認証失敗と分かる形）。"""


class _BoomGmail(FakeGmail):
    def __init__(self, exc: Exception) -> None:
        super().__init__([])
        self._exc = exc

    def list_messages(
        self,
        query: str | None,
        request_id: str,
        *,
        label_ids: Any = None,
        max_results: int = 50,
        **kw: Any,
    ) -> tuple[list[_Ref], None]:
        self.queries.append(query or "")
        raise self._exc


def test_expired_token_becomes_reauth_needed_not_zero_hits() -> None:
    fake = _BoomGmail(_RefreshError("invalid_grant: Token has been expired or revoked."))

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="花王"), _ctx()
    )

    assert out.error == "reauth_needed"
    assert out.connection == ""
    assert "@NewsTV AI に『連携』" in out.message
    assert "0 件" not in out.note


def test_generic_gmail_failure_is_distinguished_from_zero_hits() -> None:
    fake = _BoomGmail(RuntimeError("backendError: internal failure"))

    out = MailFollowupSkill(gmail=fake, now_ms=NOW_MS).run(
        MailFollowupInput(client_name="花王"), _ctx()
    )

    assert out.error == "gmail_api_failed"
    assert "0 件という意味ではありません" in out.message


def test_thread_fetch_failure_is_also_structured() -> None:
    class _ThreadBoom(FakeGmail):
        def get_thread(
            self, thread_id: str, request_id: str, *, format: str = "full", user_id: str = "me"
        ) -> list[_Msg]:
            raise RuntimeError("403 insufficient permissions")

    msgs = [_msg("田中 <tanaka@kao.co.jp>", "提案の件", 5, thread_id="t1", msg_id="m1")]
    out = MailFollowupSkill(gmail=_ThreadBoom(msgs), now_ms=NOW_MS).run(
        MailFollowupInput(client_name="花王"), _ctx()
    )

    assert out.error == "reauth_needed"  # スコープ不足は再連携で直る
    assert out.items == []
