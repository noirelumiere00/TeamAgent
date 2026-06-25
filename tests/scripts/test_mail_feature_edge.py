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
    CalendarEventItem,
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


def test_E25_draft_none_text_does_not_create_none_body():
    """Bedrock が text=None を返しても本文 'None' の下書きを作らない（空→スキップ）。"""

    class _NoneText:
        def converse(self, **kw):
            if "分類規則" in str(kw.get("system", "")):
                return _Resp('[{"importance":"high","summary":"x"}]')
            return _Resp(None)  # 下書き応答が None

    g = _Gmail([_to_me()])
    skill = MorningDigestSkill(
        token_store=_Tokens({ME: object()}), gmail=g, gcalendar=_GCal([]), bedrock=_NoneText()
    )
    _run(skill, max_drafts=1)
    # "None" 本文の下書きは作られない（空はスキップ）
    assert all(d["thread_id"] for d in g.created) or g.created == []
    assert g.created == []  # 空応答 → 下書き0


def test_E26_html_only_mail_uses_snippet_fallback():
    """text/plain が無い HTML 専用メールは snippet を本文代替に使う（空で триаж しない）。"""
    captured: list[str] = []

    class _CapBedrock:
        def converse(self, **kw):
            if "分類規則" in str(kw.get("system", "")):
                captured.append(str(kw.get("messages")))
                return _Resp('[{"importance":"medium","summary":"x"}]')
            return _Resp("下書き")

    m = _Msg(
        headers={"From": "c@x.com", "To": ME, "Subject": "html"},
        payload={"mimeType": "text/html", "body": {"data": _b64("<b>hi</b>")}},
        internal_date_ms=1,
        thread_id="T",
    )
    m.snippet = "重要なお知らせの本文プレビュー"  # type: ignore[attr-defined]
    skill = MorningDigestSkill(
        token_store=_Tokens({ME: object()}),
        gmail=_Gmail([m]),
        gcalendar=_GCal([]),
        bedrock=_CapBedrock(),
    )
    _run(skill, max_drafts=0)
    # triage プロンプトに snippet 由来の文字が入る（本文空のまま投げない）
    assert any("重要なお知らせ" in c for c in captured)


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


def test_E27_max_drafts_backfills_when_top_candidate_deduped():
    """上位候補が既存下書きで dedupe されても、下位の作成可能スレッドへ繰り上げる。

    max_drafts=1・高重要×本人宛が2スレッド。上位 tA は既存下書きあり→スキップ。
    旧実装は候補数を max_drafts で打ち切り tA だけ拾って created=0 になっていた。
    修正後は作成数基準のキャップで tB に繰り上げ created=1。
    """
    msgs = [_to_me(thread="tA", subj="A"), _to_me(thread="tB", subj="B")]
    t = '[{"importance":"high","summary":"a"},{"importance":"high","summary":"b"}]'
    g = _Gmail(msgs, existing=["tA"])  # tA には既に下書きがある
    out = _run(_skill(g, triage=t), max_drafts=1)
    assert out.drafts_created == 1
    assert [d["thread_id"] for d in g.created] == ["tB"]  # 下位 tB へ繰り上げ


def test_E28_max_drafts_cap_still_enforced_on_created_count():
    """backfill 後も作成数の上限（max_drafts）は厳守する（作りすぎない）。"""
    msgs = [_to_me(thread=f"t{i}", subj=f"s{i}") for i in range(4)]
    t = "[" + ",".join(['{"importance":"high","summary":"x"}'] * 4) + "]"
    g = _Gmail(msgs)
    out = _run(_skill(g, triage=t), max_drafts=2)
    assert out.drafts_created == 2 and len(g.created) == 2  # 4候補でも2件で停止


def test_E29_fmt_event_time_timed_allday_empty():
    """予定時刻整形：通常は HH:MM–HH:MM、日付のみは終日、空は空。"""
    assert (
        runner._fmt_event_time("2026-06-25T10:00:00+09:00", "2026-06-25T11:30:00+09:00")
        == "10:00–11:30"
    )
    assert runner._fmt_event_time("2026-06-25", "2026-06-26") == "終日"  # 終日イベント
    assert runner._fmt_event_time(None, None) == ""
    assert runner._fmt_event_time("2026-06-25T09:00:00+09:00", None) == "09:00"  # 終了不明


def test_E30_calendar_section_rendered_with_real_titles():
    """回帰固定：カレンダー予定が DM に描画され、本人DMでは実名(display)で出る。

    以前は _collect_calendar が収集しても _format_block_kit に描画が無く、予定が
    一切表示されなかった（2026-06-25 ユーザー指摘）。
    """
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        calendar_events=[
            CalendarEventItem(
                summary_scrubbed="営***",
                summary_display="営業定例 with 法人A",
                start_at="2026-06-25T10:00:00+09:00",
                end_at="2026-06-25T11:00:00+09:00",
                location_display="本社3F",
                meeting_url="https://meet.google.com/abc-defg-hij",
            )
        ],
    )
    _t, blocks = runner._format_block_kit(d, ME)
    dump = str(blocks)
    assert "今日の予定" in dump  # 予定セクションが出る
    assert "営業定例 with 法人A" in dump  # 実名(display)で表示
    assert "10:00–11:00" in dump  # 時刻が出る
    assert "本社3F" in dump  # 場所が出る
    assert "https://meet.google.com/abc-defg-hij|🔗参加" in dump  # 会議リンクが出る


def test_E31_calendar_link_injection_escaped():
    """予定名に <url|text> が含まれても Block Kit でエスケープされ偽リンク化しない。"""
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        calendar_events=[
            CalendarEventItem(
                summary_display="緊急 <https://evil.example|今すぐ>",
                start_at="2026-06-25T09:00:00+09:00",
                end_at="2026-06-25T09:30:00+09:00",
            )
        ],
    )
    _t, blocks = runner._format_block_kit(d, ME)
    dump = str(blocks)
    assert "<https://evil.example|" not in dump
    assert "&lt;https://evil.example|" in dump


# ════ v2 UI: 冒頭文 / スコアボード削除 / 要返信ボタン / 未開封 ════


def test_E32_preamble_and_no_scoreboard():
    """冒頭は『メールと本日の予定をお送りします。』、旧スコアボード(要確認/下書き済)は無い。"""
    d = MorningDigestOutput(user_email_masked="m***@x")
    text, blocks = runner._format_block_kit(d, ME)
    assert text == "メールと本日の予定をお送りします。"
    dump = str(blocks)
    assert "メールと本日の予定をお送りします" in dump
    assert "下書き済" not in dump and "要確認" not in dump  # スコアボード削除


def test_E33_reply_buttons_states():
    """要返信メールのボタン: 未作成→[下書きを作成(action)]＋[確認する(url)]、作成済→[開く(url)]。"""
    # 未作成（draft_token あり）
    m1 = MailDigestItem(
        counterpart_masked="a***@x",
        importance="high",
        subject_display="件名A",
        draft_token="TOK123",
        thread_gmail_url="https://mail.google.com/mail/u/0/#all/tA",
    )
    btns = runner._reply_buttons(m1)
    create = [b for b in btns if b.get("action_id") == "mail_draft"]
    assert create and create[0]["value"] == "TOK123"  # 押下で worker が受ける
    assert any(b.get("url", "").endswith("#all/tA") for b in btns)  # 確認する=スレッド直行
    assert all("value" not in b or b.get("action_id") for b in btns)  # url ボタンは action 無し

    # 作成済 → 作成ボタンは出ず「開く」url ボタン
    m2 = MailDigestItem(
        counterpart_masked="a***@x", importance="high", has_draft=True, draft_token="TOK"
    )
    btns2 = runner._reply_buttons(m2)
    assert not [b for b in btns2 if b.get("action_id") == "mail_draft"]
    assert any("drafts" in b.get("url", "") for b in btns2)


def test_E34_unread_section_lists_unread_non_high():
    """未開封セクションには is_unread かつ非 high のメールが出る（high は要返信側）。"""
    d = MorningDigestOutput(
        user_email_masked="m***@x",
        mail_digest=[
            MailDigestItem(
                counterpart_masked="a***@x",
                importance="high",
                is_unread=True,
                subject_display="高重要",
            ),
            MailDigestItem(
                counterpart_masked="b***@x",
                importance="medium",
                is_unread=True,
                subject_display="未読の通知",
                summary="お知らせ要約",
            ),
            MailDigestItem(
                counterpart_masked="c***@x",
                importance="low",
                is_unread=False,
                subject_display="既読low",
            ),
        ],
    )
    _t, blocks = runner._format_block_kit(d, ME)
    dump = str(blocks)
    assert "未開封" in dump
    assert "未読の通知" in dump and "お知らせ要約" in dump  # 未読medium＋要約
    assert "既読low" not in dump  # 既読は未開封に出ない
    # high(高重要) は要返信側に出る（未開封側ではボタン無し一覧なので action は high 由来のみ）
    assert "高重要" in dump


# ════ v2 ロジック: オンデマンド下書き ════


def test_E35_draft_on_demand_only_skips_auto_generation():
    """DRAFT_ON_DEMAND_ONLY 時、run() は朝に下書きを自動生成しない（has_draft 照合のみ）。"""
    g = _Gmail([_to_me(thread="tX")], existing=["tX"])  # 既に下書きあり
    skill = _skill(g)
    skill._draft_on_demand_only = True
    out = _run(skill, max_drafts=3)
    assert len(g.created) == 0  # 自動生成しない
    assert out.mail_digest and out.mail_digest[0].has_draft is True  # 既存は has_draft 反映


def test_E36_generate_draft_for_thread_creates_reply():
    """ボタン経由のオンデマンド生成：本人宛スレッドに Reply-All 下書きを1件作る。"""
    g = _Gmail([_to_me(thread="tZ", subj="見積")])
    skill = _skill(g)
    ctx = SkillContext(request_id="r", metadata={"user_email": ME})
    res = skill.generate_draft_for_thread("tZ", ME, ctx)
    assert res["created"] is True and res["error"] is None
    assert len(g.created) == 1 and g.created[0]["thread_id"] == "tZ"
    assert res["thread_url"].endswith("#all/tZ")


def test_E37_generate_draft_for_thread_idempotent():
    """既に下書きがあるスレッドは二重作成しない（already=True）。"""
    g = _Gmail([_to_me(thread="tD")], existing=["tD"])
    skill = _skill(g)
    ctx = SkillContext(request_id="r", metadata={"user_email": ME})
    res = skill.generate_draft_for_thread("tD", ME, ctx)
    assert res["already"] is True and res["created"] is False
    assert len(g.created) == 0


def test_E38_generate_draft_for_thread_rejects_cc_only():
    """本人が To に居ない（CC のみ）スレッドは下書きを作らない（not_addressed）。"""
    m = _Msg(
        headers={"From": "a@x.com", "To": "boss@x.com", "Cc": ME, "Subject": "共有"},
        payload=_pl("本文"),
        internal_date_ms=1,
        thread_id="tC",
    )
    g = _Gmail([m])
    skill = _skill(g)
    ctx = SkillContext(request_id="r", metadata={"user_email": ME})
    res = skill.generate_draft_for_thread("tC", ME, ctx)
    assert res["created"] is False and res["error"] == "not_addressed"
    assert len(g.created) == 0


def test_E39_is_unread_collected_from_label_ids():
    """スレッドに UNREAD ラベルがあれば item.is_unread=True（未開封セクションの素材）。"""
    m_unread = _to_me(thread="tU", subj="未読")
    m_unread.label_ids = ("INBOX", "UNREAD")  # type: ignore[attr-defined]
    m_read = _to_me(thread="tR", subj="既読")
    m_read.label_ids = ("INBOX",)  # type: ignore[attr-defined]
    t = '[{"importance":"medium","summary":"a"},{"importance":"medium","summary":"b"}]'
    out = _run(_skill(_Gmail([m_unread, m_read]), triage=t), max_drafts=0)
    by_subj = {i.subject_display: i.is_unread for i in out.mail_digest}
    assert by_subj.get("未読") is True
    assert by_subj.get("既読") is False
