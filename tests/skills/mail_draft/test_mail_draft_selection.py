"""mail_draft の selection 経路（一覧からの選択）の単体テスト。

起草エンジン（mail_reply）は fake に差し替え、mail_draft 自身の責務
（入口の切り分け・候補の同定・上限・ピン留め・在庫切れの正直さ）だけを固定する。
ボタン押下経路（draft_token）の挙動が変わっていないことも併せて確認する。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.mail_draft.schema import MailDraftInput
from teamagent.skills.mail_draft.skill import MAX_SELECTED, MailDraftSkill
from teamagent.skills.mail_followup.skill import VANISHED_REF, evidence_ref

OWNER = "s-komata@vectorinc.co.jp"
NOW_MS = 1_700_000_000_000
MS_PER_DAY = 86_400_000


@dataclass
class _Ref:
    id: str
    thread_id: str


@dataclass
class _Msg:
    id: str
    thread_id: str
    headers: dict[str, str]
    internal_date_ms: int
    label_ids: tuple[str, ...] = ()


class FakeGmail:
    """一覧走査（list_messages + threads.get metadata）だけを持つ fake。"""

    def __init__(self, msgs: list[_Msg]) -> None:
        self._msgs = msgs

    def list_messages(
        self, query: str | None, request_id: str, *, max_results: int = 50, **kw: Any
    ) -> tuple[list[_Ref], None]:
        ordered = sorted(self._msgs, key=lambda m: m.internal_date_ms, reverse=True)
        return ([_Ref(id=m.id, thread_id=m.thread_id) for m in ordered][:max_results], None)

    def get_thread(
        self, thread_id: str, request_id: str, *, format: str = "full", user_id: str = "me"
    ) -> list[_Msg]:
        return [m for m in self._msgs if m.thread_id == thread_id]


@dataclass
class _ReplyOut:
    created: bool = True
    to_display: str = "tanaka@example.co.jp"
    draft_subject: str = "Re: 件名"
    draft_body: str = "本文です"
    gmail_draft_id: str = "draft-1"
    open_url: str = "https://mail.google.com/mail/u/0/#all/t1"
    total_cost_usd: float = 0.01


@dataclass
class FakeReply:
    """MailReplySkill の差し替え（run の入力を記録するだけ）。"""

    out: _ReplyOut = field(default_factory=_ReplyOut)
    calls: list[Any] = field(default_factory=list)

    def run(self, input: Any, ctx: Any) -> _ReplyOut:
        self.calls.append(input)
        return self.out


def _ctx(email: str = OWNER) -> SkillContext:
    return SkillContext(request_id="r", user_id="U1", metadata={"user_email": email})


def _msg(mid: str, thread_id: str, sender: str, subject: str, days_ago: int) -> _Msg:
    return _Msg(
        id=mid,
        thread_id=thread_id,
        headers={"From": sender, "Subject": subject, "To": OWNER},
        internal_date_ms=NOW_MS - days_ago * MS_PER_DAY,
        label_ids=("INBOX",),
    )


def _inbox() -> list[_Msg]:
    return [
        _msg("m1", "t1", "田中 <tanaka@example.co.jp>", "ご提案のご確認をお願いします", 6),
        _msg("m2", "t2", "佐藤 <sato@example.co.jp>", "請求書の送付について", 12),
        _msg("m3", "t3", "鈴木 <suzuki@example.co.jp>", "日程調整の件", 2),
    ]


def _skill(msgs: list[_Msg] | None = None, reply: FakeReply | None = None) -> MailDraftSkill:
    return MailDraftSkill(
        gmail=FakeGmail(_inbox() if msgs is None else msgs),
        reply=reply or FakeReply(),
        now_ms=NOW_MS,
    )


def _refs(msgs: list[_Msg]) -> list[str]:
    return [evidence_ref(m.id) for m in msgs]


# ── 入口の切り分け ─────────────────────────────────────────────────────


def test_neither_token_nor_selection_creates_nothing() -> None:
    reply = FakeReply()

    out = _skill(reply=reply).run(MailDraftInput(), _ctx())

    assert out.error == "no_selection"
    assert out.created is False
    assert reply.calls == []
    assert out.message


def test_requires_user_email() -> None:
    with pytest.raises(PermissionError):
        _skill().run(MailDraftInput(selection="1番で"), _ctx(email=""))


# ── 起草エンジンへの受け渡し ─────────────────────────────────────────────


def test_the_selected_thread_anchor_is_handed_to_the_reply_engine() -> None:
    inbox = _inbox()
    reply = FakeReply()

    out = _skill(inbox, reply).run(
        MailDraftInput(selection="1番で", candidate_refs=_refs(inbox), instructions="丁寧めで"),
        _ctx(),
    )

    assert out.created is True
    assert len(reply.calls) == 1
    sent = reply.calls[0]
    assert sent.target_message_id == "m1"  # 選ばれた件の anchor messageId
    assert sent.instructions == "丁寧めで"
    assert sent.client_name == ""  # 受信箱一覧からの選択なので顧客名は名乗らない


def test_multiple_picks_create_one_draft_each_in_list_order() -> None:
    inbox = _inbox()
    reply = FakeReply()

    out = _skill(inbox, reply).run(
        MailDraftInput(selection="3と1", candidate_refs=_refs(inbox)), _ctx()
    )

    assert [c.target_message_id for c in reply.calls] == ["m1", "m3"]  # 一覧の並び順
    assert len(out.drafts) == 2
    assert out.total_cost_usd == pytest.approx(0.02)


def test_at_most_three_drafts_per_call_and_it_says_so() -> None:
    inbox = [*_inbox(), _msg("m4", "t4", "高橋 <takahashi@example.co.jp>", "ご相談の件", 3)]
    reply = FakeReply()

    out = _skill(inbox, reply).run(
        MailDraftInput(selection="1と2と3と4", candidate_refs=_refs(inbox)), _ctx()
    )

    assert len(reply.calls) == MAX_SELECTED
    assert "一度に作るのは 3 件まで" in out.message


# ── ピン留め（一覧に無くなった件） ────────────────────────────────────────


def test_a_vanished_candidate_never_promotes_another_mail_into_its_number() -> None:
    """消えた件の番号を指されても、**別の件を繰り上げない**（宛先取り違えの防止）。"""
    inbox = _inbox()
    reply = FakeReply()
    # 2 番目は受信箱から消えた（自分で返信した・移動した 等）＝ ref が解決できない。
    refs = [evidence_ref("m1"), evidence_ref("gone"), evidence_ref("m3")]

    out = _skill(inbox, reply).run(MailDraftInput(selection="1と2", candidate_refs=refs), _ctx())

    # 「1」は m1 のまま、「2」は何も作らない（3 番目の m3 を 2 番へ繰り上げない）。
    assert [c.target_message_id for c in reply.calls] == ["m1"]
    assert "見つからなくなっていた" in out.message
    # 3 番はちゃんと 3 番のままであることも確認する。
    reply2 = FakeReply()
    _skill(inbox, reply2).run(MailDraftInput(selection="3", candidate_refs=refs), _ctx())
    assert [c.target_message_id for c in reply2.calls] == ["m3"]


def test_picking_only_a_vanished_number_creates_nothing_and_says_so() -> None:
    inbox = _inbox()
    reply = FakeReply()
    refs = [evidence_ref("m1"), evidence_ref("gone"), evidence_ref("m3")]

    out = _skill(inbox, reply).run(MailDraftInput(selection="2番で", candidate_refs=refs), _ctx())

    assert out.error == "vanished_selection"
    assert out.created is False
    assert reply.calls == []
    assert out.message


def test_stale_refs_do_not_make_it_claim_the_inbox_is_empty() -> None:
    """渡された ref が全部古くても、受信箱に候補があるなら『無い』と言わない。"""
    inbox = _inbox()
    reply = FakeReply()
    stale = [evidence_ref("gone-1"), evidence_ref("gone-2")]

    out = _skill(inbox, reply).run(MailDraftInput(selection="1", candidate_refs=stale), _ctx())

    assert out.error == "vanished_selection"  # 「候補が無い」ではない
    assert reply.calls == []


def test_the_ask_back_hands_over_fresh_refs_for_the_list_it_just_printed() -> None:
    inbox = _inbox()

    out = _skill(inbox, FakeReply()).run(
        MailDraftInput(selection="それでお願い", candidate_refs=_refs(inbox)), _ctx()
    )

    assert out.error == "ambiguous_selection"
    # 出し直した一覧（田中→佐藤→鈴木）と同じ並びの照合鍵を返す
    assert out.candidate_refs == [evidence_ref("m1"), evidence_ref("m2"), evidence_ref("m3")]


def test_the_ask_back_never_returns_fewer_refs_than_the_lines_it_printed() -> None:
    """聞き返しの一覧は「表示 3 行・refs 2 件」になってはいけない（番号ずれの温床）。

    anchor messageId が取れない候補を refs から黙って落とすと、次の『2番』が
    3 番目の件を指す＝別のお客様へ下書きを作る失敗クラスに戻る。位置は埋め草で保つ。
    """
    inbox = _inbox()
    inbox[1] = replace(inbox[1], id="")  # 2 番目だけ anchor id が取れない

    out = _skill(inbox, FakeReply()).run(
        MailDraftInput(selection="それでお願い", candidate_refs=_refs(inbox)), _ctx()
    )

    assert out.error == "ambiguous_selection"
    printed = [line for line in out.message.splitlines() if line[:2] in ("1.", "2.", "3.")]
    assert len(printed) == 3
    assert len(out.candidate_refs) == len(printed)
    assert out.candidate_refs[1] == VANISHED_REF  # 位置は残すが解決はしない


def test_a_number_pointing_at_a_placeholder_ref_creates_nothing() -> None:
    """埋め草の位置を番号で指されても、別の件へ繰り上げない。"""
    inbox = _inbox()
    inbox[1] = replace(inbox[1], id="")
    reply = FakeReply()

    out = _skill(inbox, reply).run(
        MailDraftInput(selection="2番で", candidate_refs=_refs(inbox)), _ctx()
    )

    assert out.error == "vanished_selection"
    assert reply.calls == []


def test_a_number_without_the_list_it_refers_to_creates_nothing_and_asks_again() -> None:
    """**refs が最初から来ない**経路（実測の事故）。番号は位置でしかないので推測しない。

    再現していた事故: 一覧の 1番は佐藤だったのに、refs 省略で再計算した並びの 1番（田中）へ
    下書きを作った。ピン留めが唯一の防波堤なのに任意だったのが原因。
    """
    inbox = _inbox()
    reply = FakeReply()

    out = _skill(inbox, reply).run(MailDraftInput(selection="1番で"), _ctx())

    assert out.error == "ambiguous_selection"
    assert out.created is False
    assert reply.calls == []  # 起草エンジンにすら渡さない
    assert "番号だけでは決めません" in out.message
    assert "1. 田中" in out.message  # 候補を出し直して選ばせる
    assert out.candidate_refs == _refs(inbox)  # 次はこの並びで番号が確定する


def test_a_name_still_works_without_refs_because_it_names_the_thread_itself() -> None:
    """名前は位置ではなく相手そのものを指すので、refs が無くても取り違えない。"""
    inbox = _inbox()
    reply = FakeReply()

    out = _skill(inbox, reply).run(MailDraftInput(selection="鈴木さんの件でお願い"), _ctx())

    assert out.created is True
    assert reply.calls[0].target_message_id == "m3"


def test_an_engine_that_could_not_create_a_draft_is_not_reported_as_created() -> None:
    """起草側が『作れなかった』と言ったら、それを成功として本人に見せない。"""
    inbox = _inbox()
    reply = FakeReply(out=_ReplyOut(created=False, draft_body=""))

    out = _skill(inbox, reply).run(
        MailDraftInput(selection="1", candidate_refs=_refs(inbox)), _ctx()
    )

    assert out.created is False
    assert out.drafts == []
    assert out.error == "not_draftable"
    assert out.message


def test_the_real_engine_is_built_to_read_past_mail_with_the_same_person() -> None:
    """本番経路（fake 注入なし）で、裁定 B の『過去メールまで読む』が有効になっている。

    通しテストは組み立て済みの起草エンジンを注入するため、この配線だけは素通りする。
    ここが False に戻ると別スレッドの経緯を見ない下書きに静かに劣化する。
    """
    engine = MailDraftSkill(token_store=None)._reply_skill()

    assert engine._counterpart_history is True


# ── 上限（連打・コスト） ─────────────────────────────────────────────────


def test_the_daily_quota_is_consumed_per_draft_and_then_blocks() -> None:
    inbox = _inbox()
    reply = FakeReply()
    skill = _skill(inbox, reply)

    for _ in range(MailDraftSkill._QUOTA_LIMIT):
        assert skill.run(MailDraftInput(selection="1", candidate_refs=_refs(inbox)), _ctx()).created

    out = skill.run(MailDraftInput(selection="1", candidate_refs=_refs(inbox)), _ctx())

    assert out.error == "quota"
    assert len(reply.calls) == MailDraftSkill._QUOTA_LIMIT  # 上限後は起草すらしない


def test_the_daily_quota_survives_the_skill_being_rebuilt_for_every_call() -> None:
    """本番の形（呼び出しごとに Skill を作り直す）で上限が効く。

    実測していた抜け: 本番は ``ToolSpec.instantiate()`` が毎回新しい Skill を作るため、
    カウンタをインスタンス変数に置くと毎回 0 に戻り、25 回連続で 25 件すべて作成できた
    （起草エンジン＝Bedrock + drafts.create も 25 回起動した）。単一インスタンスを
    使い回す上のテストだけでは、この抜けを 1 度も通らない。
    """
    inbox = _inbox()
    reply = FakeReply()
    refs = _refs(inbox)

    outs = [
        _skill(inbox, reply).run(MailDraftInput(selection="1", candidate_refs=refs), _ctx())
        for _ in range(MailDraftSkill._QUOTA_LIMIT + 5)
    ]

    assert sum(1 for o in outs if o.created) == MailDraftSkill._QUOTA_LIMIT
    assert [o.error for o in outs[-5:]] == ["quota"] * 5
    assert len(reply.calls) == MailDraftSkill._QUOTA_LIMIT  # 上限後は起草すらしない


# ── ボタン押下経路（既存）が壊れていない ──────────────────────────────────


def test_the_button_path_still_works_and_never_touches_the_inbox_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.skills.morning_digest.draft_token import encode_draft_token

    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "mail-draft-test-secret-" + "m" * 32)

    class _FakeMorning:
        def __init__(self, **_kw: Any) -> None: ...

        def generate_draft_for_thread(self, thread_id: str, requester: str, ctx: Any) -> Any:
            return {"created": True, "error": None, "thread_url": f"#all/{thread_id}"}

    import teamagent.skills.morning_digest.skill as ms

    monkeypatch.setattr(ms, "MorningDigestSkill", lambda **kw: _FakeMorning(**kw))
    reply = FakeReply()

    out = _skill(reply=reply).run(
        MailDraftInput(draft_token=str(encode_draft_token("thr_1", OWNER))), _ctx()
    )

    assert out.created is True
    assert out.drafts == []  # ボタン経路は drafts[] を使わない（後方互換）
    assert reply.calls == []  # selection 経路のエンジンは呼ばれない
