"""AiLa メール機能 — 「100点」深掘りエッジ/敵対ケース。

ハッピーパスでなく、境界値・異常入力・取りこぼし・偽装を攻めてバグを炙り出す。
失敗したテスト = 実バグ。runner も使うため importlib でロード。
"""

from __future__ import annotations

# シナリオ ID（test_E01 等）は大文字を意図的に使うため N802 を無効化。
# ruff: noqa: N802
import base64
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest.schema import (
    MailDigestItem,
    MorningDigestInput,
    MorningDigestOutput,
)
from teamagent.skills.morning_digest.skill import MorningDigestSkill, _strip_sentinels

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("run_md_edge_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_md_edge_under_test"] = module
    spec.loader.exec_module(module)
    return module


runner = _load()
ME = "me@vectorinc.co.jp"


def _b64(t: str) -> str:
    return base64.urlsafe_b64encode(t.encode()).decode()


def _pl(text: str) -> dict[str, Any]:
    return {"mimeType": "text/plain", "body": {"data": _b64(text)}}


@dataclass
class _Ref:
    id: str
    thread_id: str = ""


@dataclass
class _Msg:
    headers: dict[str, str]
    payload: dict[str, Any]
    internal_date_ms: int | None = 0
    thread_id: str = ""
    id: str = ""


@dataclass
class _Ev:
    summary: str = ""
    start: str = ""
    end: str = ""
    location: str = ""


class _Gmail:
    def __init__(self, msgs, *, existing=None, raise_on_create=()):
        for i, m in enumerate(msgs):
            if not m.id:
                m.id = f"m{i}"
            if not m.thread_id:
                m.thread_id = f"t{i}"
        self._msgs = msgs
        self.created: list[dict[str, Any]] = []
        self._existing = list(existing or [])
        self._raise_on = set(raise_on_create)

    def list_messages(self, q, rid, max_results=30):
        return ([_Ref(id=m.id, thread_id=m.thread_id) for m in self._msgs], None)

    def get_thread(self, tid, rid, **_):
        return [m for m in self._msgs if m.thread_id == tid]

    def get_message(self, mid, rid, **_):
        return next(m for m in self._msgs if m.id == mid)

    def list_drafts(self, rid, **_):
        return [
            type("D", (), {"id": f"d{i}", "thread_id": t})() for i, t in enumerate(self._existing)
        ]

    def create_draft(
        self,
        *,
        to,
        subject,
        body_text,
        request_id,
        thread_id=None,
        cc=None,
        in_reply_to_message_id=None,
        user_id="me",
    ):
        if thread_id in self._raise_on:
            raise RuntimeError("create failed")
        self.created.append({"to": to, "cc": cc, "thread_id": thread_id})
        return type("D", (), {"id": f"dr{len(self.created)}", "thread_id": thread_id})()


class _GCal:
    def __init__(self, events):
        self._e = events

    def list_events(self, rid, **_):
        return self._e


class _Tokens:
    def __init__(self, d):
        self._d = d

    def get(self, e):
        return self._d.get(e.lower())


class _Resp:
    def __init__(self, text, cost=0.001):
        self.text = text
        self.usage = type("U", (), {"cost_usd": cost})()


class _Bedrock:
    def __init__(self, triage, draft="下書き"):
        self._t = triage
        self._d = draft
        self.captured: list[dict[str, Any]] = []

    def converse(self, **kw):
        self.captured.append(kw)
        return _Resp(self._t if "分類規則" in str(kw.get("system", "")) else self._d)


def _skill(gmail, triage='[{"importance":"high","summary":"x"}]', gcal=None, **kw):
    return MorningDigestSkill(
        token_store=_Tokens({ME: object()}),
        gmail=gmail,
        gcalendar=gcal or _GCal([]),
        bedrock=_Bedrock(triage),
        **kw,
    )


def _run(skill, **inp):
    return skill.run(
        MorningDigestInput(**inp), SkillContext(request_id="r", metadata={"user_email": ME})
    )


def _to_me(frm="c@acme.co.jp", subj="件名", thread="T", body="本文です。", date=1000):
    return _Msg(
        headers={"From": frm, "To": ME, "Subject": subj},
        payload=_pl(body),
        internal_date_ms=date,
        thread_id=thread,
    )


# ════ 回帰固定：今回直した2バグ ════


def test_E01_calendar_times_populated_not_blank():
    """バグ①回帰：CalendarEvent.start/end を読み、時刻が空にならない。"""
    ev = _Ev(summary="営業MTG", start="2026-06-25T10:00:00+09:00", end="2026-06-25T11:00:00+09:00")
    out = _run(_skill(_Gmail([_to_me()]), gcal=_GCal([ev])), max_drafts=0)
    assert out.calendar_events[0].start_at == "2026-06-25T10:00:00+09:00"
    assert out.calendar_events[0].end_at == "2026-06-25T11:00:00+09:00"


def test_E02_slack_escape_neutralizes_link_injection():
    """バグ②回帰：実件名の <url|text> が Block Kit でエスケープされ偽リンク化しない。"""
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        mail_digest=[
            MailDigestItem(
                counterpart_masked="a***@x.com",
                importance="high",
                subject_display="緊急 <https://evil.example|今すぐクリック>",
                counterpart_display="攻撃者 <hack@evil>",
            )
        ],
    )
    _t, blocks = runner._format_block_kit(d, ME)
    dump = str(blocks)
    assert "<https://evil.example|" not in dump  # 生のリンク構文は残らない
    assert "&lt;https://evil.example|" in dump  # エスケープ済み


def test_E18_slack_escape_unit():
    assert runner._slack_escape("a & b < c > d") == "a &amp; b &lt; c &gt; d"


# ════ 収集・本文のエッジ ════


def test_E03_empty_inbox_no_crash():
    out = _run(_skill(_Gmail([]), triage="[]"), max_drafts=3)
    assert out.mail_digest == [] and out.drafts_created == 0


def test_E04_html_only_mail_no_plaintext_no_crash():
    m = _Msg(
        headers={"From": "c@x.com", "To": ME, "Subject": "html"},
        payload={"mimeType": "text/html", "body": {"data": _b64("<b>hi</b>")}},
        internal_date_ms=1,
        thread_id="T",
    )
    out = _run(_skill(_Gmail([m])), max_drafts=3)
    assert len(out.mail_digest) == 1  # 本文無くても項目化・落ちない


def test_E05_self_sent_mail_no_draft():
    """自分が自分宛/自分発のみ → 返信先が立たず下書きしない。"""
    m = _Msg(
        headers={"From": ME, "To": ME, "Subject": "メモ"},
        payload=_pl("自分用メモ"),
        internal_date_ms=1,
        thread_id="T",
    )
    g = _Gmail([m])
    _run(_skill(g), max_drafts=3)
    assert len(g.created) == 0


def test_E06_malformed_headers_no_crash():
    m = _Msg(headers={}, payload=_pl("ヘッダ無し"), internal_date_ms=1, thread_id="T")
    out = _run(_skill(_Gmail([m])), max_drafts=3)
    assert len(out.mail_digest) == 1


def test_E19_thread_with_only_self_messages():
    msgs = [
        _Msg(
            headers={"From": ME, "To": "c@x.com", "Subject": "S"},
            payload=_pl("自分1"),
            internal_date_ms=1,
            thread_id="T",
        ),
        _Msg(
            headers={"From": ME, "To": "c@x.com", "Subject": "Re: S"},
            payload=_pl("自分2"),
            internal_date_ms=2,
            thread_id="T",
        ),
    ]
    g = _Gmail(msgs)
    out = _run(_skill(g), max_drafts=3)
    assert len(out.mail_digest) == 1 and len(g.created) == 0  # 最新も自分発→返信先なし


def test_E20_huge_body_truncated_no_crash():
    m = _to_me(body="あ" * 50000)
    out = _run(_skill(_Gmail([m])), max_drafts=0)
    assert len(out.mail_digest) == 1


# ════ triage の異常応答 ════


def test_E07_triage_more_items_than_input_no_crash():
    t = "[" + ",".join(['{"importance":"high","summary":"x"}'] * 5) + "]"
    out = _run(_skill(_Gmail([_to_me()]), triage=t), max_drafts=0)  # 1通だが5件返る
    assert len(out.mail_digest) == 1 and out.mail_digest[0].importance == "high"


def test_E08_triage_importance_uppercase_whitespace_normalized():
    out = _run(
        _skill(_Gmail([_to_me()]), triage='[{"importance":" HIGH ","summary":"x"}]'), max_drafts=0
    )
    assert out.mail_digest[0].importance == "high"


def test_E08b_triage_unknown_importance_defaults_medium():
    out = _run(
        _skill(_Gmail([_to_me()]), triage='[{"importance":"urgent!!","summary":"x"}]'), max_drafts=0
    )
    assert out.mail_digest[0].importance == "medium"


def test_E09_deadline_as_number_coerced():
    out = _run(
        _skill(
            _Gmail([_to_me()]), triage='[{"importance":"high","summary":"x","deadline":20260630}]'
        ),
        max_drafts=0,
    )
    assert out.mail_digest[0].deadline == "20260630"


# ════ 下書きのエッジ ════


def test_E10_reply_all_over_max_cc_returns_none():
    cc_list = ", ".join(f"p{i}@x.com" for i in range(30))
    m = _Msg(
        headers={"From": "a@x.com", "To": f"{ME}, {cc_list}", "Subject": "大量"},
        payload=_pl("本文"),
        internal_date_ms=1,
        thread_id="T",
    )
    g = _Gmail([m])
    skill = MorningDigestSkill(
        token_store=_Tokens({ME: object()}),
        gmail=g,
        gcalendar=_GCal([]),
        bedrock=_Bedrock('[{"importance":"high","summary":"x"}]'),
        reply_all=True,
    )
    _run(skill, max_drafts=1)
    assert g.created and g.created[0]["cc"] is None  # 暴発防止で cc 無し


def test_E11_create_draft_failure_others_continue():
    msgs = [_to_me(thread="t1", subj="A"), _to_me(thread="t2", subj="B")]
    t = '[{"importance":"high","summary":"a"},{"importance":"high","summary":"b"}]'
    g = _Gmail(msgs, raise_on_create={"t1"})  # t1 の作成だけ失敗
    out = _run(_skill(g, triage=t), max_drafts=3)
    assert out.drafts_created == 1 and g.created[0]["thread_id"] == "t2"


def test_E12_none_internal_date_sort_no_crash():
    msgs = [_to_me(thread="t1", date=None), _to_me(thread="t2", date=5)]
    out = _run(
        _skill(
            _Gmail(msgs),
            triage='[{"importance":"high","summary":"a"},{"importance":"low","summary":"b"}]',
        ),
        max_drafts=0,
    )
    assert len(out.mail_digest) == 2


def test_E13_subject_display_capped_160():
    m = _to_me(subj="件" * 300)
    out = _run(_skill(_Gmail([m])), max_drafts=0)
    assert len(out.mail_digest[0].subject_display) <= 160


def test_E14_existing_draft_with_none_thread_does_not_block():
    g = _Gmail([_to_me(thread="T")], existing=[None])  # 既存下書きの thread が None
    out = _run(_skill(g), max_drafts=3)
    assert out.drafts_created == 1  # None は集合に入らず、正常な下書きは作る


# ════ 宛先/境界の細部 ════


def test_E15_strip_sentinels_partial_tokens_unchanged():
    assert _strip_sentinels("a << b >> c") == "a << b >> c"  # 2連は対象外
    assert _strip_sentinels("<<<x>>>") == "‹‹‹x›››"


def test_E16_requester_in_both_to_and_cc_is_addressed():
    m = _Msg(
        headers={"From": "a@x.com", "To": f"boss@x.com, {ME}", "Cc": ME, "Subject": "S"},
        payload=_pl("本文"),
        internal_date_ms=1,
        thread_id="T",
    )
    g = _Gmail([m])
    _run(_skill(g), max_drafts=1)
    assert len(g.created) == 1  # To に本人がいれば対象


def test_E17_max_threads_caps_collection():
    msgs = [_to_me(thread=f"t{i}", subj=f"s{i}") for i in range(30)]
    t = "[" + ",".join(['{"importance":"low","summary":"x"}'] * 30) + "]"
    out = _run(_skill(_Gmail(msgs), triage=t), max_threads=5, max_drafts=0)
    assert len(out.mail_digest) == 5  # max_threads で打ち切り


def test_E21_calendar_failure_does_not_break_mail():
    class _BoomCal:
        def list_events(self, rid, **_):
            raise RuntimeError("cal down")

    skill = MorningDigestSkill(
        token_store=_Tokens({ME: object()}),
        gmail=_Gmail([_to_me()]),
        gcalendar=_BoomCal(),
        bedrock=_Bedrock('[{"importance":"high","summary":"x"}]'),
    )
    out = _run(skill, max_drafts=0)
    assert len(out.mail_digest) == 1 and any("calendar" in e for e in out.errors)


def test_E22_cost_aggregated_across_triage_and_drafts():
    out = _run(_skill(_Gmail([_to_me()])), max_drafts=1)
    assert out.total_cost_usd > 0  # triage + draft のコストが積算される


def test_E24_iso_or_none_treats_epoch_zero_as_valid():
    from teamagent.skills.morning_digest.skill import _iso_or_none

    assert _iso_or_none(None) is None
    assert _iso_or_none(0) == "1970-01-01T00:00:00+00:00"  # 0 は有効な時刻
    assert _iso_or_none(1718681400000) is not None


def test_E23_mass_mail_salutation_after_long_preamble_no_draft():
    """各位 が本文先頭120字以降にあっても一斉送信と判定し下書きしない（窓拡大の回帰）。"""
    from teamagent.skills._shared.mail_compose import is_mass_or_impersonal

    body = "\n\n[ロゴ画像]\n\n" + ("ご案内 " * 30) + "\n各位\n本年もよろしくお願いします。"
    assert is_mass_or_impersonal({"From": "info@x.com"}, body) is True
    # 個人宛の通常メールは False のまま
    assert is_mass_or_impersonal({"From": "a@x.com"}, "山田様\nお世話になります。") is False
