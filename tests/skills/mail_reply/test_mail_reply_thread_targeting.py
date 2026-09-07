"""mail_reply のスレッド取り違え防止（G8）のオフラインテスト（課金0・外部I/O無し・実送信なし）。

2026-09-07 本番実測の再現: 同じ話題（日本教育財団）のスレッドが 2 本あり、利用者が
【日本教育財団様_PR関連のご提案について】（ベクトル徳野）を指したのに、Aico は最新の
別スレッド（石川さん・クオラス経由）へ下書きを作った。ここでは:

* 件名/差出人/日付の手がかりで **正しい 1 件** に絞れること（最新でなくても）
* 手がかりが無く 2 件以上なら **下書きを作らずに** 候補を返すこと
* 候補 1 件なら従来どおり作ること
* ``discard_draft_id`` で直前の誤下書きを削除してから作り直すこと
* 送信 API を **一切** 呼ばないこと

を固定する。変異テスト（件名の絞り込みを外す／曖昧時に作ってしまう）で赤くなることを
PR 本文の変異表で証明する。
"""

from __future__ import annotations

import base64
import datetime as _dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from teamagent.adapters.gmail_client import DraftNotOwnedError
from teamagent.skills.base import SkillContext
from teamagent.skills.mail_reply.schema import MailReplyInput
from teamagent.skills.mail_reply.skill import (
    ERROR_AMBIGUOUS,
    ERROR_THREAD_NOT_FOUND,
    MailReplySkill,
)
from teamagent.skills.mail_reply.targeting import (
    ThreadCandidateMeta,
    build_search_query,
    filter_by_hints,
    gmail_after_clause,
    group_newest_per_thread,
    normalize_for_match,
    parse_received_after_ms,
    sender_matches,
    subject_matches,
)

OWNER = "s-komata@vectorinc.co.jp"
JST = _dt.timezone(_dt.timedelta(hours=9))

TH_ISHIKAWA = "th-ishikawa"
TH_TOKUNO = "th-tokuno"
SUBJECT_TOKUNO = "【日本教育財団様_PR関連のご提案について】"
SUBJECT_ISHIKAWA = "日本教育財団 PR関連のご相談（クオラス経由）"


def _ms(y: int, m: int, d: int, hh: int = 9, mm: int = 0) -> int:
    return int(_dt.datetime(y, m, d, hh, mm, tzinfo=JST).timestamp() * 1000)


def _payload(text: str) -> dict[str, Any]:
    data = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
    return {"mimeType": "text/plain", "body": {"data": data}}


@dataclass
class _Ref:
    id: str
    thread_id: str


@dataclass
class _Msg:
    id: str
    thread_id: str
    headers: dict[str, str]
    payload: dict[str, Any]
    snippet: str = ""
    internal_date_ms: int | None = None
    label_ids: tuple[str, ...] = ()


@dataclass
class _Draft:
    id: str
    message_id: str = "msg-x"
    thread_id: str | None = None


@dataclass
class FakeGmail:
    """create_draft / delete_draft を記録し、呼ばれた全メソッド名を ``calls`` に残す fake。

    送信系メソッド（send_*）は **存在しない**。万一 skill が呼べば AttributeError で赤くなる。
    ``msgs`` は newest-first（list_messages の並び）。
    """

    msgs: list[_Msg]
    list_hook: Callable[[str], list[str] | None] | None = None
    not_owned: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    get_formats: list[str] = field(default_factory=list)
    create_draft_calls: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def _by_id(self, msg_id: str) -> _Msg:
        for m in self.msgs:
            if m.id == msg_id:
                return m
        raise KeyError(msg_id)

    def list_messages(
        self, query: str | None, request_id: str, *, max_results: int = 50, **kw: Any
    ) -> tuple[list[_Ref], None]:
        self.calls.append("list_messages")
        self.queries.append(query or "")
        ids: list[str] | None = None
        if self.list_hook is not None:
            ids = self.list_hook(query or "")
        if ids is None:
            ids = [m.id for m in self.msgs]
        refs = [_Ref(id=i, thread_id=self._by_id(i).thread_id) for i in ids[:max_results]]
        return (refs, None)

    def get_message(self, msg_id: str, request_id: str, *, format: str = "full", **kw: Any) -> _Msg:
        self.calls.append("get_message")
        self.get_formats.append(format)
        return self._by_id(msg_id)

    def get_thread(self, thread_id: str, request_id: str, **kw: Any) -> list[_Msg]:
        self.calls.append("get_thread")
        # Gmail の threads.get は時系列（古い→新しい）。newest-first を反転する。
        return [m for m in reversed(self.msgs) if m.thread_id == thread_id]

    def create_draft(
        self, *, to: str, subject: str, body_text: str, request_id: str, **kw: Any
    ) -> _Draft:
        self.calls.append("create_draft")
        self.create_draft_calls.append({"to": to, "subject": subject, "body": body_text, **kw})
        return _Draft(id="draft-new", thread_id=kw.get("thread_id"))

    def delete_draft(self, draft_id: str, request_id: str, **kw: Any) -> _Draft:
        self.calls.append("delete_draft")
        if draft_id in self.not_owned:
            raise DraftNotOwnedError("not owned")
        if draft_id == "draft-missing":
            raise RuntimeError("404 draft not found")
        self.deleted.append(draft_id)
        return _Draft(id=draft_id)


@dataclass
class _Usage:
    cost_usd: float


@dataclass
class _Resp:
    text: str
    usage: _Usage


class FakeBedrock:
    def __init__(self) -> None:
        self.calls = 0

    def converse(self, *, messages: Any, request_id: str, **kw: Any) -> _Resp:
        self.calls += 1
        return _Resp(text="徳野様\n\nお世話になっております。", usage=_Usage(cost_usd=0.004))


def _ctx() -> SkillContext:
    return SkillContext(request_id="r", user_id="U1", metadata={"user_email": OWNER})


def _ishikawa(msg_id: str = "m-ishikawa") -> _Msg:
    return _Msg(
        id=msg_id,
        thread_id=TH_ISHIKAWA,
        headers={
            "From": "石川 花子 <ishikawa@quorus.co.jp>",
            "To": OWNER,
            "Subject": SUBJECT_ISHIKAWA,
            "Message-ID": "<ishikawa-1@quorus.co.jp>",
        },
        payload=_payload("クオラスの石川です。日本教育財団の PR の件でご相談です。"),
        snippet="クオラスの石川です。日本教育財団の PR の件でご相談です。",
        internal_date_ms=_ms(2026, 9, 6, 20, 0),
    )


def _tokuno(msg_id: str = "m-tokuno") -> _Msg:
    return _Msg(
        id=msg_id,
        thread_id=TH_TOKUNO,
        headers={
            "From": "徳野 太郎 <tokuno@vectorinc.co.jp>",
            "To": OWNER,
            "Subject": SUBJECT_TOKUNO,
            "Message-ID": "<tokuno-1@vectorinc.co.jp>",
        },
        payload=_payload(
            "徳野です。日本教育財団様向けの PR ご提案について、ご確認をお願いします。"
        ),
        snippet="徳野です。日本教育財団様向けの PR ご提案について、ご確認をお願いします。",
        internal_date_ms=_ms(2026, 9, 5, 18, 20),
    )


def _two_threads() -> list[_Msg]:
    """本番の再現: 石川（最新・別スレッド）と徳野（利用者が意図した方・古い）。newest-first。"""
    return [_ishikawa(), _tokuno()]


def _skill(gmail: FakeGmail, bedrock: FakeBedrock | None = None) -> MailReplySkill:
    return MailReplySkill(gmail=gmail, bedrock=bedrock or FakeBedrock())


# ── 一意化: 手がかりで正しい 1 件に絞る ─────────────────────────────────────


def test_subject_hint_picks_the_named_thread_even_if_not_newest() -> None:
    """【件名】で指されたら、最新でなくてもそのスレッドに作る（本番事故の再現→修正）。"""
    gmail = FakeGmail(_two_threads())
    out = _skill(gmail).run(
        MailReplyInput(client_name="日本教育財団", subject_contains=SUBJECT_TOKUNO), _ctx()
    )

    assert out.created is True
    assert out.error == ""
    assert out.thread_id == TH_TOKUNO
    assert out.to_display == "tokuno@vectorinc.co.jp"
    assert len(gmail.create_draft_calls) == 1
    assert gmail.create_draft_calls[0]["thread_id"] == TH_TOKUNO
    assert gmail.create_draft_calls[0]["in_reply_to_message_id"] == "<tokuno-1@vectorinc.co.jp>"
    # Gmail 側にも subject: 演算子を渡している（括弧は落として被演算子に）
    assert 'subject:"日本教育財団様_PR関連のご提案について"' in gmail.queries[0]
    assert '"日本教育財団"' in gmail.queries[0]


def test_from_hint_company_plus_surname_matches_the_sender() -> None:
    """「ベクトル徳野」のように会社＋姓が区切り無しでも、差出人の姓で当てる。"""
    gmail = FakeGmail(_two_threads())
    out = _skill(gmail).run(
        MailReplyInput(client_name="日本教育財団", from_contains="ベクトル徳野"), _ctx()
    )
    assert out.created is True
    assert out.thread_id == TH_TOKUNO
    assert gmail.create_draft_calls[0]["to"] == "tokuno@vectorinc.co.jp"


def test_received_after_filters_out_older_thread() -> None:
    gmail = FakeGmail(_two_threads())
    out = _skill(gmail).run(
        MailReplyInput(client_name="日本教育財団", received_after="2026-09-06"), _ctx()
    )
    assert out.created is True
    assert out.thread_id == TH_ISHIKAWA
    assert "after:2026/09/06" in gmail.queries[0]


def test_hints_alone_search_even_without_a_usable_client_name() -> None:
    """件名/差出人の手がかりがあれば、client_name が空・断片でも受信箱を引いて 1 件に絞る。"""
    gmail = FakeGmail(_two_threads())
    out = _skill(gmail).run(MailReplyInput(client_name="", from_contains="徳野"), _ctx())
    assert out.created is True and out.thread_id == TH_TOKUNO
    assert gmail.queries[0].startswith('from:"徳野" newer_than:30d')

    gmail2 = FakeGmail(_two_threads())
    out2 = _skill(gmail2).run(
        MailReplyInput(client_name="今日のメール", subject_contains="PR関連のご提案について"),
        _ctx(),
    )
    assert out2.created is True and out2.thread_id == TH_TOKUNO
    assert '"今日のメール"' not in gmail2.queries[0]  # 断片は検索句にしない


def test_stage_two_query_drops_operators_when_gmail_returns_nothing() -> None:
    """subject:/from: 演算子で 0 件（CJK 分かち書きの空振り）なら演算子なしで引き直し、
    ローカル照合で正しい 1 件に絞る。"""

    def hook(query: str) -> list[str] | None:
        return [] if "subject:" in query else None

    gmail = FakeGmail(_two_threads(), list_hook=hook)
    out = _skill(gmail).run(
        MailReplyInput(client_name="日本教育財団", subject_contains=SUBJECT_TOKUNO), _ctx()
    )
    assert out.created is True and out.thread_id == TH_TOKUNO
    assert len(gmail.queries) == 2
    assert "subject:" in gmail.queries[0]
    assert "subject:" not in gmail.queries[1]
    assert gmail.queries[1] == '"日本教育財団" newer_than:30d -in:sent in:inbox'


# ── 曖昧: 作らずに候補を返す ────────────────────────────────────────────────


def test_no_hint_with_two_threads_returns_candidates_and_does_not_create() -> None:
    """手がかりが無く 2 件以上 → 下書きを作らず、候補（件名・差出人・日時・冒頭）を返す。"""
    gmail = FakeGmail(_two_threads())
    bedrock = FakeBedrock()
    out = _skill(gmail, bedrock).run(MailReplyInput(client_name="日本教育財団"), _ctx())

    assert out.created is False
    assert out.error == ERROR_AMBIGUOUS
    assert gmail.create_draft_calls == [], "曖昧なら 1 件も作らない"
    assert bedrock.calls == 0, "曖昧なら起草（課金）もしない"
    assert [c.number for c in out.ambiguous_threads] == [1, 2]
    assert [c.thread_id for c in out.ambiguous_threads] == [TH_ISHIKAWA, TH_TOKUNO]  # newest-first
    first, second = out.ambiguous_threads
    assert first.from_display == "石川 花子"
    assert second.from_display == "徳野 太郎"
    assert second.subject == SUBJECT_TOKUNO
    assert second.received_at == "09/05 18:20"
    assert second.preview.startswith("徳野です。")
    assert "下書きはまだ作っていません" in out.note
    assert "1. " in out.note and "2. " in out.note
    assert "番号でお知らせください" in out.note
    assert out.gmail_draft_id == "" and out.open_url == ""


def test_hint_that_matches_nothing_returns_nearby_candidates_instead_of_guessing() -> None:
    """指定の件名に当たるものが無いときも、勝手に別スレッドへ作らない。"""
    gmail = FakeGmail(_two_threads())
    out = _skill(gmail).run(
        MailReplyInput(client_name="日本教育財団", subject_contains="存在しない件名"), _ctx()
    )
    assert out.created is False
    assert out.error == ERROR_AMBIGUOUS
    assert gmail.create_draft_calls == []
    assert len(out.ambiguous_threads) == 2
    assert "一致するスレッドが見つからなかった" in out.note


def test_candidates_are_capped_at_three_with_hidden_count() -> None:
    msgs = [_ishikawa(), _tokuno()]
    for i in range(3):
        extra = _tokuno(msg_id=f"m-x{i}")
        extra.thread_id = f"th-x{i}"
        extra.headers = {**extra.headers, "Subject": f"日本教育財団 別件 {i}"}
        msgs.append(extra)
    gmail = FakeGmail(msgs)
    out = _skill(gmail).run(MailReplyInput(client_name="日本教育財団"), _ctx())
    assert out.created is False
    assert len(out.ambiguous_threads) == 3
    assert "他 2 件は省略" in out.note


def test_single_thread_creates_as_before() -> None:
    gmail = FakeGmail([_tokuno()])
    out = _skill(gmail).run(MailReplyInput(client_name="日本教育財団"), _ctx())
    assert out.created is True
    assert out.thread_id == TH_TOKUNO
    assert out.ambiguous_threads == [] and out.error == ""


def test_same_thread_multiple_messages_is_one_candidate() -> None:
    """同一スレッドの複数通は 1 候補（最新の受信通）に束ねる＝曖昧扱いにしない。"""
    older = _tokuno(msg_id="m-tokuno-old")
    older.internal_date_ms = _ms(2026, 9, 1)
    gmail = FakeGmail([_tokuno(), older])
    out = _skill(gmail).run(MailReplyInput(client_name="日本教育財団"), _ctx())
    assert out.created is True
    assert gmail.create_draft_calls[0]["in_reply_to_message_id"] == "<tokuno-1@vectorinc.co.jp>"


# ── 候補から選ばれた thread_id ───────────────────────────────────────────────


def test_thread_id_from_candidates_creates_without_searching() -> None:
    """選ばれた thread_id は検索せずにそのスレッドへ。相手からの最新通に返信する（自分の通は飛ばす）。"""
    mine = _Msg(
        id="m-mine",
        thread_id=TH_TOKUNO,
        headers={"From": OWNER, "To": "tokuno@vectorinc.co.jp", "Subject": "Re: " + SUBJECT_TOKUNO},
        payload=_payload("承知しました。"),
        internal_date_ms=_ms(2026, 9, 6, 9, 0),
    )
    gmail = FakeGmail([_ishikawa(), mine, _tokuno()])
    out = _skill(gmail).run(MailReplyInput(thread_id=TH_TOKUNO), _ctx())
    assert out.created is True
    assert out.thread_id == TH_TOKUNO
    assert gmail.queries == []  # 検索しない
    assert gmail.create_draft_calls[0]["to"] == "tokuno@vectorinc.co.jp"
    assert gmail.create_draft_calls[0]["in_reply_to_message_id"] == "<tokuno-1@vectorinc.co.jp>"


def test_unknown_thread_id_returns_thread_not_found_without_creating() -> None:
    gmail = FakeGmail(_two_threads())
    out = _skill(gmail).run(MailReplyInput(thread_id="th-none"), _ctx())
    assert out.created is False
    assert out.error == ERROR_THREAD_NOT_FOUND
    assert gmail.create_draft_calls == []
    assert "見つかりませんでした" in out.note


# ── 誤下書きの回収（discard_draft_id）──────────────────────────────────────


def test_discard_previous_draft_then_recreate_on_the_right_thread() -> None:
    """「それじゃない」→ 直前の下書きを削除してから正しいスレッドに作り直す。"""
    gmail = FakeGmail(_two_threads())
    out = _skill(gmail).run(
        MailReplyInput(
            client_name="日本教育財団",
            subject_contains=SUBJECT_TOKUNO,
            discard_draft_id="draft-old",
        ),
        _ctx(),
    )
    assert gmail.deleted == ["draft-old"]
    assert out.discarded_draft_id == "draft-old"
    assert out.created is True and out.thread_id == TH_TOKUNO
    assert "削除しました" in out.note
    # 削除 → 作成の順（作ってから消すと一瞬 2 通並ぶ）
    assert gmail.calls.index("delete_draft") < gmail.calls.index("create_draft")


def test_discard_refuses_drafts_not_made_by_teamagent_but_still_creates() -> None:
    gmail = FakeGmail(_two_threads(), not_owned={"draft-human"})
    out = _skill(gmail).run(
        MailReplyInput(
            client_name="日本教育財団", from_contains="徳野", discard_draft_id="draft-human"
        ),
        _ctx(),
    )
    assert gmail.deleted == []
    assert out.discarded_draft_id == ""
    assert out.created is True and out.thread_id == TH_TOKUNO
    assert "削除していません" in out.note


def test_discard_failure_is_reported_and_does_not_block_recreation() -> None:
    gmail = FakeGmail(_two_threads())
    out = _skill(gmail).run(
        MailReplyInput(
            client_name="日本教育財団", from_contains="徳野", discard_draft_id="draft-missing"
        ),
        _ctx(),
    )
    assert out.discarded_draft_id == ""
    assert out.created is True
    assert "削除に失敗" in out.note


def test_discard_happens_even_when_the_retry_is_still_ambiguous() -> None:
    """「それじゃない」だけで手がかりが無くても、誤下書きは片付けてから候補を出す。"""
    gmail = FakeGmail(_two_threads())
    out = _skill(gmail).run(
        MailReplyInput(client_name="日本教育財団", discard_draft_id="draft-old"), _ctx()
    )
    assert gmail.deleted == ["draft-old"]
    assert out.created is False and out.error == ERROR_AMBIGUOUS
    assert out.discarded_draft_id == "draft-old"
    assert "削除しました" in out.note


# ── 送信は一切呼ばない ───────────────────────────────────────────────────────


def test_send_api_is_never_called_in_any_flow() -> None:
    flows = [
        MailReplyInput(client_name="日本教育財団", subject_contains=SUBJECT_TOKUNO),
        MailReplyInput(client_name="日本教育財団"),
        MailReplyInput(thread_id=TH_TOKUNO),
        MailReplyInput(client_name="日本教育財団", from_contains="徳野", discard_draft_id="d1"),
    ]
    for inp in flows:
        gmail = FakeGmail(_two_threads())
        _skill(gmail).run(inp, _ctx())
        assert not any("send" in c.lower() for c in gmail.calls), gmail.calls
        assert set(gmail.calls) <= {
            "list_messages",
            "get_message",
            "get_thread",
            "create_draft",
            "delete_draft",
        }
    assert not hasattr(FakeGmail, "send_message") and not hasattr(FakeGmail, "send_draft")


def test_candidate_scan_uses_metadata_and_refetches_only_the_chosen_message() -> None:
    gmail = FakeGmail(_two_threads())
    _skill(gmail).run(
        MailReplyInput(client_name="日本教育財団", subject_contains=SUBJECT_TOKUNO), _ctx()
    )
    assert gmail.get_formats.count("metadata") == 2
    assert gmail.get_formats.count("full") == 1


# ── 観測（G7: 件数と手がかりの有無だけ・件名/本文は出さない）──────────────────


def test_resolution_log_has_counts_and_hint_flags_but_no_subject_text() -> None:
    gmail = FakeGmail(_two_threads())
    structlog.configure(processors=[structlog.testing.LogCapture()])
    with capture_logs() as logs:
        _skill(gmail).run(
            MailReplyInput(client_name="日本教育財団", subject_contains=SUBJECT_TOKUNO), _ctx()
        )
    events = [r for r in logs if r.get("event") == "mail_reply_thread_resolution"]
    assert len(events) == 1
    rec = events[0]
    assert rec["mode"] == "search"
    assert rec["hint_subject"] is True and rec["hint_from"] is False
    assert rec["threads_total"] == 2 and rec["threads_matched"] == 1
    assert rec["outcome"] == "target"
    assert len(rec["chosen_thread_hash"]) == 12 and TH_TOKUNO not in rec["chosen_thread_hash"]
    blob = str(logs)
    assert "日本教育財団" not in blob and "徳野" not in blob and "tokuno@" not in blob


def test_ambiguous_outcome_is_logged() -> None:
    gmail = FakeGmail(_two_threads())
    structlog.configure(processors=[structlog.testing.LogCapture()])
    with capture_logs() as logs:
        _skill(gmail).run(MailReplyInput(client_name="日本教育財団"), _ctx())
    rec = next(r for r in logs if r.get("event") == "mail_reply_thread_resolution")
    assert rec["outcome"] == "ambiguous" and rec["threads_matched"] == 2
    assert rec["chosen_thread_hash"] == ""


# ── 純粋関数 ────────────────────────────────────────────────────────────────


def test_subject_matches_ignores_brackets_underscores_and_re_prefix() -> None:
    assert subject_matches("【日本教育財団様_PR関連のご提案について】", "Re: " + SUBJECT_TOKUNO)
    assert subject_matches("PR関連のご提案", SUBJECT_TOKUNO)
    assert subject_matches("日本教育財団様 PR関連のご提案について", SUBJECT_TOKUNO)
    assert not subject_matches("クオラス", SUBJECT_TOKUNO)
    assert not subject_matches("", SUBJECT_TOKUNO)
    assert not subject_matches("【】", SUBJECT_TOKUNO)


def test_sender_matches_whole_kanji_run_and_ascii_run() -> None:
    frm = "徳野 太郎 <tokuno@vectorinc.co.jp>"
    assert sender_matches("徳野", frm)
    assert sender_matches("徳野さん", frm)
    assert sender_matches("ベクトル徳野", frm)  # 会社＋姓（区切り無し）→ 漢字連続で当てる
    assert sender_matches("tokuno@", frm)
    assert sender_matches("TOKUNO", frm)
    assert not sender_matches("ベクトル", frm)  # 会社名だけでは当てない
    assert not sender_matches("石川", frm)
    assert not sender_matches("", frm)


def test_normalize_for_match_folds_width_case_and_separators() -> None:
    assert normalize_for_match("【ＰＲ関連_ご提案】 について") == "pr関連ご提案について"


def test_received_after_parsing_and_clause() -> None:
    assert parse_received_after_ms("2026-09-06") == _ms(2026, 9, 6, 0, 0)
    assert parse_received_after_ms("") is None
    assert parse_received_after_ms("2026-13-40") is None
    assert gmail_after_clause("2026-09-06") == "after:2026/09/06"
    assert gmail_after_clause("2026-13-40") == ""


def test_build_search_query_shapes() -> None:
    assert (
        build_search_query(
            client_phrase="森ビル",
            subject_hint="",
            from_hint="",
            received_after="",
            lookback_days=7,
            with_hint_operators=True,
        )
        == '"森ビル" newer_than:7d -in:sent in:inbox'
    )
    q = build_search_query(
        client_phrase="森ビル",
        subject_hint='【見積"の件】',
        from_hint="田中",
        received_after="2026-09-01",
        lookback_days=30,
        with_hint_operators=True,
    )
    assert q == (
        '"森ビル" from:"田中" subject:"見積の件" after:2026/09/01 newer_than:30d -in:sent in:inbox'
    )
    assert '"' not in q.replace('"森ビル"', "").replace('"田中"', "").replace('"見積の件"', "")


def test_group_newest_per_thread_and_filter_by_hints() -> None:
    a1 = ThreadCandidateMeta("A", "m1", subject="x 件", from_header="a@x.com", received_at_ms=30)
    a2 = ThreadCandidateMeta("A", "m2", subject="x 件", from_header="a@x.com", received_at_ms=10)
    b1 = ThreadCandidateMeta("B", "m3", subject="y 件", from_header="b@y.com", received_at_ms=20)
    grouped = group_newest_per_thread([a1, a2, b1])
    assert [c.message_id for c in grouped] == ["m1", "m3"]
    assert [c.thread_id for c in filter_by_hints(grouped, subject_hint="y")] == ["B"]
    assert [c.thread_id for c in filter_by_hints(grouped, from_hint="a@x")] == ["A"]
    assert filter_by_hints(grouped, received_after="1970-01-01") == grouped


@pytest.mark.parametrize("bad", ["2026/09/06", "9/6", "yesterday"])
def test_received_after_schema_rejects_non_iso_dates(bad: str) -> None:
    with pytest.raises(ValueError):
        MailReplyInput(received_after=bad)
