"""一覧（mail_followup）→ 選択（mail_draft）→ Gmail 下書き保存 の通しテスト。

2026-08-21 裁定の B/C を固定する:
  B: 選ばれた **1 件だけ** 本文・スレッド全文・**同じ相手との過去メール**・Slack まで読む
  C: **Gmail の下書きに保存**し、Slack には本文とリンクを返す。**送信は絶対にしない**

fake なのは Gmail（アダプタ）と Bedrock だけで、skill 層は本物を通す
（mail_followup の走査 → 判定層 → mail_draft の同定 → mail_reply の起草 → drafts.create）。
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.mail_draft.schema import MailDraftInput
from teamagent.skills.mail_draft.skill import MailDraftSkill
from teamagent.skills.mail_followup.schema import MailFollowupInput
from teamagent.skills.mail_followup.skill import MailFollowupSkill, clear_scan_cache
from teamagent.skills.mail_reply.skill import MailReplySkill

OWNER = "s-komata@vectorinc.co.jp"
NOW_MS = 1_700_000_000_000
MS_PER_DAY = 86_400_000


# ── fakes ─────────────────────────────────────────────────────────────────


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
    body: str = ""
    days_ago: int = 0
    label_ids: tuple[str, ...] = ()

    @property
    def payload(self) -> dict[str, Any]:
        return _payload(self.body)

    @property
    def internal_date_ms(self) -> int:
        return NOW_MS - self.days_ago * MS_PER_DAY


@dataclass
class _Draft:
    id: str
    message_id: str = "msg-x"
    thread_id: str | None = None


class FakeGmail:
    """受信箱まるごとの fake。**送信系メソッドを 1 つでも呼んだら即座に失敗する**。"""

    def __init__(self, msgs: list[_Msg]) -> None:
        self._msgs = msgs
        self.queries: list[str] = []
        self.thread_formats: list[str] = []
        self.body_fetches: list[str] = []
        self.create_draft_calls: list[dict[str, Any]] = []
        # 「送信 API が呼ばれない」を数で固定するためのカウンタ（実装には呼ぶ経路が無い）。
        self.send_calls = 0

    # ── 読み取り ──────────────────────────────────────────────────────
    def list_messages(
        self, query: str | None, request_id: str, *, max_results: int = 50, **kw: Any
    ) -> tuple[list[_Ref], None]:
        q = query or ""
        self.queries.append(q)
        if "in:inbox" in q:
            hits = [m for m in self._msgs if "INBOX" in m.label_ids]
        elif "from:" in q:
            address = q.split("from:", 1)[1].split(" ", 1)[0].rstrip(")")
            hits = [m for m in self._msgs if address in m.headers.get("From", "")]
        else:  # pragma: no cover - 想定外のクエリ形
            hits = []
        # ⚠️ newer_than を無視する fake は本番の失敗モード（窓の外の件は「無い」ことに
        # なる＝一覧に出ていた件が選択時に消える）を再現できないので、必ず効かせる。
        window = re.search(r"newer_than:(\d+)d", q)
        if window:
            cutoff = NOW_MS - int(window.group(1)) * MS_PER_DAY
            hits = [m for m in hits if m.internal_date_ms >= cutoff]
        hits = sorted(hits, key=lambda m: m.internal_date_ms, reverse=True)
        return ([_Ref(id=m.id, thread_id=m.thread_id) for m in hits][:max_results], None)

    def get_thread(
        self, thread_id: str, request_id: str, *, format: str = "full", user_id: str = "me"
    ) -> list[_Msg]:
        self.thread_formats.append(format)
        return [m for m in self._msgs if m.thread_id == thread_id]

    def get_message(self, msg_id: str, request_id: str, **kw: Any) -> _Msg:
        self.body_fetches.append(msg_id)
        return next(m for m in self._msgs if m.id == msg_id)

    # ── 書き込み（下書きだけ）──────────────────────────────────────────
    def create_draft(
        self, *, to: str, subject: str, body_text: str, request_id: str, **kw: Any
    ) -> _Draft:
        self.create_draft_calls.append({"to": to, "subject": subject, "body": body_text, **kw})
        return _Draft(id=f"draft-{len(self.create_draft_calls)}", thread_id=kw.get("thread_id"))

    # ── 送信系（アダプタ層 denylist で物理封鎖されている面。呼べば即失敗）──────
    def send_message(self, *a: Any, **kw: Any) -> None:
        self.send_calls += 1
        raise AssertionError("送信 API が呼ばれた（下書き保存のみのはず）")

    def send_draft(self, *a: Any, **kw: Any) -> None:
        self.send_calls += 1
        raise AssertionError("下書き送信 API が呼ばれた（下書き保存のみのはず）")


@dataclass
class _Usage:
    cost_usd: float = 0.002


@dataclass
class _Resp:
    text: str
    usage: _Usage = field(default_factory=_Usage)


class FakeBedrock:
    def __init__(self, text: str = "田中様\n\nご提案の件、承知しました。") -> None:
        self._text = text
        self.calls: list[dict[str, Any]] = []

    def converse(self, *, messages: Any, request_id: str, **kw: Any) -> _Resp:
        self.calls.append({"messages": messages, **kw})
        return _Resp(text=self._text)

    @property
    def last_prompt(self) -> str:
        return str(self.calls[-1]["messages"][0]["content"][0]["text"])


class FakeSlack:
    """deal_provider 契約（bullets / cost_usd）だけを満たす fake。

    ⚠️ 本番の ``SlackContextProvider`` は **クエリが空なら横断検索そのものを行わない**
    （``_sanitize_query`` が空文字を返し search を飛ばす）。手掛かりに関係なく常に
    bullets を返す fake にすると、「件名を検索クエリへ流用しない」修正が効いているかを
    1 つも検知できない（緑が実質を失う）ので、その分岐をここで再現する。
    """

    def __init__(self, bullets: list[str]) -> None:
        self._bullets = bullets
        self.hints: list[str] = []

    def fetch(self, client_hint: str, requester: str, ctx: Any) -> Any:
        self.hints.append(client_hint)
        found = list(self._bullets) if str(client_hint).strip() else []
        return type("R", (), {"bullets": found, "cost_usd": 0.0})()


def _ctx() -> SkillContext:
    return SkillContext(request_id="r-test", user_id="U1", metadata={"user_email": OWNER})


# ── 受信箱フィクスチャ ─────────────────────────────────────────────────


def _inbox() -> list[_Msg]:
    """一覧の並びが (1)田中 (2)佐藤 (3)鈴木 になる受信箱（既存トリアージ試験と同型）。"""
    return [
        _Msg(
            id="m-tanaka",
            thread_id="t-tanaka",
            headers={
                "From": "田中 <tanaka@example.co.jp>",
                "Subject": "ご提案の件、ご確認をお願いします",
                "To": OWNER,
                "Message-ID": "<tanaka-1@example.co.jp>",
            },
            body="お世話になります。ご提案の件、ご確認をお願いします。予算感を教えてください。",
            days_ago=6,
            label_ids=("INBOX",),
        ),
        _Msg(
            id="m-sato",
            thread_id="t-sato",
            headers={
                "From": "佐藤 <sato@example.co.jp>",
                "Subject": "請求書の送付について",
                "To": f"{OWNER}, other@example.co.jp",
            },
            body="請求書をお送りします。",
            days_ago=12,
            label_ids=("INBOX",),
        ),
        _Msg(
            id="m-suzuki",
            thread_id="t-suzuki",
            headers={
                "From": "鈴木 <suzuki@example.co.jp>",
                "Subject": "日程調整の件",
                "To": OWNER,
                "Cc": "boss@vectorinc.co.jp",
            },
            body="日程を調整させてください。",
            days_ago=2,
            label_ids=("INBOX",),
        ),
        # 田中との **別スレッド**（既に自分が返している＝候補には出ないが、過去の経緯として効く）。
        _Msg(
            id="m-tanaka-old",
            thread_id="t-tanaka-old",
            headers={
                "From": "田中 <tanaka@example.co.jp>",
                "Subject": "前回のお見積り",
                "To": OWNER,
            },
            body="前回のお見積りは単価50万円でご提示いただいた件です。",
            days_ago=40,
            label_ids=("INBOX",),
        ),
        _Msg(
            id="m-tanaka-old-reply",
            thread_id="t-tanaka-old",
            headers={"From": OWNER, "Subject": "Re: 前回のお見積り", "To": "tanaka@example.co.jp"},
            body="承知しました。",
            days_ago=39,
            label_ids=("INBOX", "SENT"),
        ),
    ]


def _list_candidates(gmail: FakeGmail) -> Any:
    """裁定 A の一覧を実際に出して、その items（＝利用者が見たもの）を返す。"""
    out = MailFollowupSkill(gmail=gmail, now_ms=NOW_MS).run(MailFollowupInput(), _ctx())
    assert out.error == "inbox_triage"
    return out


def _draft_skill(
    gmail: FakeGmail, bedrock: FakeBedrock, *, deal_provider: Any = None
) -> MailDraftSkill:
    reply = MailReplySkill(
        gmail=gmail, bedrock=bedrock, deal_provider=deal_provider, counterpart_history=True
    )
    return MailDraftSkill(gmail=gmail, reply=reply, now_ms=NOW_MS)


# ── 裁定 C: 下書きが保存され、送信は 1 度も呼ばれない ──────────────────────


def test_number_selection_saves_a_gmail_draft_and_returns_body_and_link() -> None:
    gmail = FakeGmail(_inbox())
    listed = _list_candidates(gmail)
    bedrock = FakeBedrock()

    out = _draft_skill(gmail, bedrock).run(
        MailDraftInput(
            selection="1番で",
            candidate_refs=[item.evidence_ref for item in listed.items],
        ),
        _ctx(),
    )

    assert out.created is True
    assert out.error == ""
    assert len(out.drafts) == 1
    draft = out.drafts[0]
    # Gmail の下書きとして保存されている（宛先・件名・本文つき）
    assert len(gmail.create_draft_calls) == 1
    call = gmail.create_draft_calls[0]
    assert call["to"] == "tanaka@example.co.jp"
    assert call["subject"] == "Re: ご提案の件、ご確認をお願いします"
    assert call["thread_id"] == "t-tanaka"
    # Slack には本文とリンクを返す（裁定 C）
    assert draft.body == "田中様\n\nご提案の件、承知しました。"
    assert draft.open_url == "https://mail.google.com/mail/u/0/#all/t-tanaka"
    assert draft.body in out.message
    assert draft.open_url in out.message
    assert "送信はしていません" in out.message


def test_the_send_api_is_never_called_on_the_selection_path() -> None:
    """送信 API 呼び出し回数を **0 で固定**する（fake は呼ばれたら即失敗する）。"""
    gmail = FakeGmail(_inbox())
    listed = _list_candidates(gmail)

    out = _draft_skill(gmail, FakeBedrock()).run(
        MailDraftInput(selection="1と3", candidate_refs=[i.evidence_ref for i in listed.items]),
        _ctx(),
    )

    assert out.created is True
    assert gmail.send_calls == 0
    assert len(gmail.create_draft_calls) == 2  # 下書きは作る（送信はしない）


# ── 裁定 B: 選ばれた件だけ、本文・スレッド全文・過去メールまで読む ────────────


def test_the_selected_mail_is_read_in_full_including_past_mail_with_the_same_person() -> None:
    gmail = FakeGmail(_inbox())
    listed = _list_candidates(gmail)
    bedrock = FakeBedrock()

    _draft_skill(gmail, bedrock).run(
        MailDraftInput(selection="1", candidate_refs=[i.evidence_ref for i in listed.items]),
        _ctx(),
    )

    prompt = bedrock.last_prompt
    assert "予算感を教えてください" in prompt  # 選んだ件の本文
    assert "前回のお見積りは単価50万円" in prompt  # 同じ相手との**別スレッド**の過去メール
    assert "# 同じ相手との過去のやり取り" in prompt
    # 選ばなかった件の本文は 1 文字も渡らない
    assert "請求書をお送りします" not in prompt
    assert "日程を調整させてください" not in prompt


def test_only_the_selected_thread_has_its_body_fetched() -> None:
    """一覧側は本文経路を 1 度も通らず、選択後に初めて本文を取る。"""
    gmail = FakeGmail(_inbox())
    listed = _list_candidates(gmail)
    assert gmail.body_fetches == []  # 裁定 A: 一覧では本文を読まない
    assert set(gmail.thread_formats) == {"metadata"}

    _draft_skill(gmail, FakeBedrock()).run(
        MailDraftInput(selection="1", candidate_refs=[i.evidence_ref for i in listed.items]),
        _ctx(),
    )

    assert "m-sato" not in gmail.body_fetches
    assert "m-suzuki" not in gmail.body_fetches
    assert "m-tanaka" in gmail.body_fetches


def test_the_customers_subject_is_never_used_as_a_slack_search_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部顧客の件名を社内 Slack の検索クエリに流用しない（実測した情報混入）。

    再現していた事故: client_name が空だと件名がそのまま横断検索のクエリになり、
    「値引き不可と決定」「A社の見積は300万」といった**無関係な社内発言**が
    『# 社内Slackの関連文脈』として起草プロンプトに入った。件名は相手が自由に書ける
    文字列なので、汎用件名ほど無関係な社内メッセージを引き当てる。
    """
    monkeypatch.setenv("USE_SLACK_CONTEXT", "1")
    gmail = FakeGmail(_inbox())
    listed = _list_candidates(gmail)
    bedrock = FakeBedrock()
    slack = FakeSlack(["［案件横断 #経営］値引き不可と決定（A社の見積は300万）"])

    _draft_skill(gmail, bedrock, deal_provider=slack).run(
        MailDraftInput(selection="1", candidate_refs=[i.evidence_ref for i in listed.items]),
        _ctx(),
    )

    # 手掛かりは空＝横断検索は行われない（現スレッドの文脈だけが残る）。
    assert slack.hints == [""]
    assert "ご提案の件" not in "".join(slack.hints)
    assert "値引き不可と決定" not in bedrock.last_prompt


def test_a_named_client_still_gets_slack_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """本人が案件名を名指しした経路の Slack 横断検索は従来どおり効く（退行の検知）。"""
    monkeypatch.setenv("USE_SLACK_CONTEXT", "1")
    gmail = FakeGmail(_inbox())
    bedrock = FakeBedrock()
    slack = FakeSlack(["［案件横断 #営業］先方の予算は300万円で確定"])
    reply = MailReplySkill(gmail=gmail, bedrock=bedrock, deal_provider=slack)

    from teamagent.skills.mail_reply.schema import MailReplyInput

    reply.run(MailReplyInput(client_name="田中", target_message_id="m-tanaka"), _ctx())

    assert slack.hints == ["田中"]
    assert "先方の予算は300万円で確定" in bedrock.last_prompt


# ── 費用: 一覧 → 選択で受信箱を 2 度走査しない ──────────────────────────────


def test_the_selection_reuses_the_list_it_just_showed_instead_of_rescanning() -> None:
    """下書き 1 通のために受信箱をフル走査し直さない（実測 threads.get 82 回の是正）。

    一覧と選択は別々のツール呼び出しで Skill が作り直されるため、素直に書くと同じ受信箱を
    2 回走査する（逐次 HTTP・バッチ不可で 8〜16 秒）。直前に本人へ見せた一覧を使い回す。
    """
    gmail = FakeGmail(_inbox())
    listed = _list_candidates(gmail)
    inbox_scans = sum(1 for q in gmail.queries if "in:inbox" in q)
    metadata_reads = gmail.thread_formats.count("metadata")
    assert inbox_scans == 1 and metadata_reads > 0  # 一覧側は実際に走査している

    out = _draft_skill(gmail, FakeBedrock()).run(
        MailDraftInput(selection="1番で", candidate_refs=[i.evidence_ref for i in listed.items]),
        _ctx(),
    )

    assert out.created is True
    assert sum(1 for q in gmail.queries if "in:inbox" in q) == inbox_scans  # 再走査ゼロ
    assert gmail.thread_formats.count("metadata") == metadata_reads


def test_passing_the_same_window_back_does_not_force_a_rescan() -> None:
    """一覧が返した lookback_days をそのまま渡す（＝推奨の呼び方）でも往復を増やさない。

    「条件を明示されたら必ず走査し直す」にすると、SOUL の指示どおりに呼ぶほど遅く高くなる。
    キャッシュがその条件を満たしているなら走査し直す理由が無い。
    """
    gmail = FakeGmail(_inbox())
    listed = _list_candidates(gmail)
    inbox_scans = sum(1 for q in gmail.queries if "in:inbox" in q)

    out = _draft_skill(gmail, FakeBedrock()).run(
        MailDraftInput(
            selection="1番で",
            candidate_refs=[i.evidence_ref for i in listed.items],
            lookback_days=listed.lookback_days,
        ),
        _ctx(),
    )

    assert out.created is True
    assert sum(1 for q in gmail.queries if "in:inbox" in q) == inbox_scans


def test_a_wider_window_than_the_cached_one_is_honored_with_a_fresh_scan() -> None:
    """利用者が**より広い窓**を指定したら、キャッシュの都合で握り潰さず走査し直す。"""
    gmail = FakeGmail([*_inbox(), _old_mail()])
    listed = _list_candidates(gmail)  # 既定 14 日＝21 日前の件は一覧に出ない
    assert all("高橋" not in (i.subject_scrubbed or "") for i in listed.items)
    inbox_scans = sum(1 for q in gmail.queries if "in:inbox" in q)

    out = _draft_skill(gmail, FakeBedrock()).run(
        MailDraftInput(selection="高橋さんの件", lookback_days=30),
        _ctx(),
    )

    assert sum(1 for q in gmail.queries if "in:inbox" in q) == inbox_scans + 1
    assert out.created is True
    assert gmail.create_draft_calls[0]["to"] == "takahashi@old.co.jp"


def test_an_ask_back_does_not_rescan_the_inbox_either() -> None:
    """言い直し（聞き返し）のたびにフル走査しない。"""
    gmail = FakeGmail(_inbox())
    listed = _list_candidates(gmail)
    inbox_scans = sum(1 for q in gmail.queries if "in:inbox" in q)

    out = _draft_skill(gmail, FakeBedrock()).run(
        MailDraftInput(
            selection="それでお願い", candidate_refs=[i.evidence_ref for i in listed.items]
        ),
        _ctx(),
    )

    assert out.error == "ambiguous_selection"
    assert sum(1 for q in gmail.queries if "in:inbox" in q) == inbox_scans


# ── 一覧と選択で「見ている窓」を一致させる ────────────────────────────────


def _old_mail() -> _Msg:
    """21 日前に来たまま止まっている 1 通（既定 14 日窓の**外側**）。"""
    return _Msg(
        id="m-old",
        thread_id="t-old",
        headers={
            "From": "高橋 <takahashi@old.co.jp>",
            "Subject": "お見積りのご確認をお願いします",
            "To": OWNER,
        },
        body="お見積りをご確認ください。",
        days_ago=21,
        label_ids=("INBOX",),
    )


def test_the_window_of_the_list_is_carried_into_the_selection() -> None:
    """『20日以上放置』で出した一覧の件が、選択時に窓の外へ落ちて『消えた』ことにならない。

    再現していた誤答: 一覧は newer_than:23d（idle_days+3）で作られるのに、選択側は既定
    14 日で走査し直すため、受信箱にあるのに vanished_selection（ご自身で返信済み・移動された
    等）と**事実と異なる**説明を返していた。
    """
    gmail = FakeGmail([*_inbox(), _old_mail()])
    listed = MailFollowupSkill(gmail=gmail, now_ms=NOW_MS).run(
        MailFollowupInput(idle_days=20), _ctx()
    )

    assert listed.lookback_days == 23  # 実効窓を戻り値で開示している
    assert listed.items and listed.items[0].idle_days == 21
    # 別タスクへ着地した想定（プロセス内キャッシュに頼らず窓を引き継げるか）。
    clear_scan_cache()

    # 呼び出し側は「一覧と同じ条件」＝ idle_days だけを引き継ぐ（lookback_days は既定 14 のまま）。
    # 窓を idle_days から導けないと、21 日前の件は 14 日窓の外＝候補に出てこない。
    out = _draft_skill(gmail, FakeBedrock()).run(
        MailDraftInput(
            selection="1",
            candidate_refs=[i.evidence_ref for i in listed.items],
            idle_days=20,
        ),
        _ctx(),
    )

    assert out.error == ""  # vanished_selection（＝事実と異なる説明）にしない
    assert out.created is True
    assert gmail.create_draft_calls[0]["to"] == "takahashi@old.co.jp"


def test_a_selection_without_conditions_falls_back_to_the_list_that_was_shown() -> None:
    """条件を書き忘れて呼ばれても、**さっき見せた一覧**の件はちゃんと作れる。

    実運用で最も起きる形（LLM が idle_days を引き継ぎ忘れる）。既定 14 日で走査し直すと
    21 日放置の件は窓の外＝『見つからなくなっていました』と事実と異なる説明になる。
    """
    gmail = FakeGmail([*_inbox(), _old_mail()])
    listed = MailFollowupSkill(gmail=gmail, now_ms=NOW_MS).run(
        MailFollowupInput(idle_days=20), _ctx()
    )
    inbox_scans = sum(1 for q in gmail.queries if "in:inbox" in q)

    out = _draft_skill(gmail, FakeBedrock()).run(
        MailDraftInput(selection="1番で", candidate_refs=[i.evidence_ref for i in listed.items]),
        _ctx(),
    )

    assert out.error == ""
    assert out.created is True
    assert gmail.create_draft_calls[0]["to"] == "takahashi@old.co.jp"
    assert sum(1 for q in gmail.queries if "in:inbox" in q) == inbox_scans


def test_the_ask_back_keeps_the_idle_filter_the_user_asked_for() -> None:
    """『20日以上放置』で頼まれた一覧の聞き返しに、2日前の件を混ぜて出し直さない。

    選択側が idle_days を引き継がないと、出し直した候補が依頼の条件と食い違う
    （「20日以上」と言ったのに 2 日前の件が 1番として並ぶ）。
    """
    gmail = FakeGmail([*_inbox(), _old_mail()])
    listed = MailFollowupSkill(gmail=gmail, now_ms=NOW_MS).run(
        MailFollowupInput(idle_days=20), _ctx()
    )
    assert len(listed.items) == 1  # 一覧に出たのは 21 日放置の 1 件だけ
    clear_scan_cache()

    out = _draft_skill(gmail, FakeBedrock()).run(
        MailDraftInput(
            selection="それでお願い",
            candidate_refs=[i.evidence_ref for i in listed.items],
            idle_days=20,
        ),
        _ctx(),
    )

    assert out.error == "ambiguous_selection"
    assert gmail.create_draft_calls == []
    assert "1. 高橋" in out.message
    for fresher in ("田中", "佐藤", "鈴木"):
        assert fresher not in out.message
    assert len(out.candidate_refs) == 1


# ── 指示（トーン・盛り込みたい内容）の反映 ────────────────────────────────


def test_the_users_instructions_reach_the_draft_prompt() -> None:
    gmail = FakeGmail(_inbox())
    listed = _list_candidates(gmail)
    bedrock = FakeBedrock()

    _draft_skill(gmail, bedrock).run(
        MailDraftInput(
            selection="1と3、丁寧めで",
            candidate_refs=[i.evidence_ref for i in listed.items],
            instructions="丁寧めで。来週の訪問を提案する",
        ),
        _ctx(),
    )

    prompt = bedrock.last_prompt
    assert "# 担当者の指示" in prompt
    assert "丁寧めで。来週の訪問を提案する" in prompt


# ── 番号の指し先が一覧と一致する ──────────────────────────────────────────


def test_the_number_points_at_what_the_user_actually_saw_even_if_new_mail_arrived() -> None:
    """一覧を出したあとに新着が来ても『1番』の指し先は変わらない（取り違え防止）。"""
    inbox = _inbox()
    gmail = FakeGmail(inbox)
    listed = _list_candidates(gmail)
    assert listed.message.splitlines()[1].startswith("1. 田中")

    # 返事が来るまでの間に、点数がもっと高いメールが視野に入る（自分ひとり宛＋催促語＋古い）。
    inbox.append(
        _Msg(
            id="m-new",
            thread_id="t-new",
            headers={
                "From": "高橋 <takahashi@example.co.jp>",
                "Subject": "至急ご確認ください",
                "To": OWNER,
            },
            body="至急ご確認ください。",
            days_ago=13,
            label_ids=("INBOX",),
        )
    )
    # ⚠️ プロセス内キャッシュが効いたままだと新着が視野に入らず、この試験が
    # 「ピン留めが効いている」ことを 1 度も通らない（緑が実質を失う）。別タスクへ
    # 着地した想定でキャッシュを捨て、**再走査させたうえで**番号の指し先を確かめる。
    clear_scan_cache()

    out = _draft_skill(gmail, FakeBedrock()).run(
        MailDraftInput(selection="1", candidate_refs=[i.evidence_ref for i in listed.items]),
        _ctx(),
    )

    assert out.drafts[0].label.startswith("田中")
    assert gmail.create_draft_calls[-1]["to"] == "tanaka@example.co.jp"


# ── 曖昧なら作らない ─────────────────────────────────────────────────────


@pytest.mark.parametrize("selection", ["それでお願い", "8番で", "0番", "1と9", "例の件"])
def test_an_ambiguous_selection_creates_nothing_and_asks_again(selection: str) -> None:
    gmail = FakeGmail(_inbox())
    listed = _list_candidates(gmail)

    out = _draft_skill(gmail, FakeBedrock()).run(
        MailDraftInput(selection=selection, candidate_refs=[i.evidence_ref for i in listed.items]),
        _ctx(),
    )

    assert out.error == "ambiguous_selection"
    assert out.created is False
    assert out.drafts == []
    assert gmail.create_draft_calls == []  # 1 件も作らない
    assert "下書きはまだ作っていません" in out.message
    assert "1. 田中" in out.message  # 候補を出し直して選ばせる


def test_a_name_that_matches_exactly_one_candidate_is_accepted() -> None:
    gmail = FakeGmail(_inbox())
    listed = _list_candidates(gmail)

    out = _draft_skill(gmail, FakeBedrock()).run(
        MailDraftInput(
            selection="鈴木さんの件でお願いします",
            candidate_refs=[i.evidence_ref for i in listed.items],
        ),
        _ctx(),
    )

    assert out.created is True
    assert gmail.create_draft_calls[0]["to"] == "suzuki@example.co.jp"


# ── 失敗時も本人向けの文面は必ず返る ──────────────────────────────────────


def test_a_gmail_failure_returns_a_message_and_never_claims_zero() -> None:
    class Broken(FakeGmail):
        def list_messages(self, query: str | None, request_id: str, **kw: Any) -> Any:
            raise RuntimeError("503 backend error")

    out = _draft_skill(Broken(_inbox()), FakeBedrock()).run(
        MailDraftInput(selection="1番で"), _ctx()
    )

    assert out.error == "gmail_api_failed"
    assert out.created is False
    assert out.message  # 空返しにしない
    assert "0 件という意味ではありません" in out.message


def test_an_empty_inbox_says_so_instead_of_drafting() -> None:
    out = _draft_skill(FakeGmail([]), FakeBedrock()).run(MailDraftInput(selection="1番で"), _ctx())

    assert out.error == "no_candidates"
    assert out.created is False
    assert out.message


def test_a_draft_that_cannot_be_created_still_returns_a_message() -> None:
    class NoDraft(FakeGmail):
        def create_draft(self, **kw: Any) -> Any:
            raise RuntimeError("403 insufficient scope")

    gmail = NoDraft(_inbox())
    listed = _list_candidates(gmail)

    out = _draft_skill(gmail, FakeBedrock()).run(
        MailDraftInput(selection="1", candidate_refs=[i.evidence_ref for i in listed.items]),
        _ctx(),
    )

    assert out.error == "reauth_needed"
    assert out.created is False
    assert out.message
