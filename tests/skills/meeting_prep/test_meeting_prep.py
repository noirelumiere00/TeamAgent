"""meeting_prep（社外商談の準備レポート・v1）。

固定すること:
- DM 以外では作らない（メールと社内資料を読むため）
- 対象の商談: 今日の時刻つき・社外（社内会議は除く）・タスク枠は除く・終わったものは除く・
  target があれば予定名／会社名で絞る・一番早いもの
- 今日に無ければ MEETING_PREP_LOOKAHEAD_DAYS（既定 7）日先まで 1 回の list_events で探す。
  今日にあれば今日を優先・範囲より先は選ばない・0 なら従来どおり今日だけ・
  明日以降を選んだら冒頭に 1 行で知らせ「いつ」に日付を出す（10-05 本番の空振りの再現）
- 会社名が読めなければ聞き返す（推測しない）
- 材料の番号は web → 金庫 → メールの順。出典欄の URL は材料から機械的に付ける
- LLM の出力: 範囲外の番号は消す・根拠番号の無い行は落とす・材料に無い数字の文は落とす・
  見出しが空なら「確認できず」で埋める
- 材料が取れなくても作る（取れなかった材料を明記）
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest

from teamagent.adapters.gcalendar_client import extract_events
from teamagent.skills.base import SkillContext
from teamagent.skills.meeting_prep.compose import SECTIONS, UNKNOWN, postprocess
from teamagent.skills.meeting_prep.schema import MeetingPrepInput
from teamagent.skills.meeting_prep.skill import DM_ONLY_MESSAGE, MeetingPrepSkill
from teamagent.skills.meeting_prep.sources import Material
from teamagent.skills.morning_digest import calendar_window as calwin

JST = calwin.JST
NOW = _dt.datetime(2026, 10, 5, 9, 0, tzinfo=JST)
DM = "D0BA1TWN6AC"


def _ctx(channel: str = DM) -> SkillContext:
    return SkillContext(
        request_id="r",
        metadata={
            "user_email": "s-komata@vectorinc.co.jp",
            "identity_verified": True,
            "channel_id": channel,
        },
    )


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """「今日」は skill の now だけでなく calendar_window.now_jst も固定する（日付テストの地雷）。"""
    monkeypatch.setattr(calwin, "now_jst", lambda: NOW)
    monkeypatch.delenv("MEETING_PREP_LOOKAHEAD_DAYS", raising=False)


def _raw(
    eid: str, title: str, hh: int, *, guests: bool = True, desc: str = "", day: int = 5
) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "id": eid,
        "summary": title,
        "start": {"dateTime": f"2026-10-{day:02d}T{hh:02d}:00:00+09:00"},
        "end": {"dateTime": f"2026-10-{day:02d}T{hh + 1:02d}:00:00+09:00"},
        "description": desc,
    }
    if guests:
        ev["attendees"] = [
            {"email": "s-komata@vectorinc.co.jp", "self": True},
            {"email": "tanaka@jtb.com"},
        ]
    return ev


class _Cal:
    """Google の events.list と同じく、[timeMin, timeMax) に重なる予定だけを開始順に
    max_results 件まで返す（窓の外を返すフェイクだと「範囲より先を選ばない」を検証できない）。"""

    def __init__(self, raw: list[dict[str, Any]]) -> None:
        self.raw = raw
        self.calls: list[dict[str, Any]] = []

    def list_events(self, request_id: str, **kw: Any) -> list[Any]:
        assert kw.get("want_description") is True
        self.calls.append(kw)
        lo = _dt.datetime.fromisoformat(kw["time_min"])
        hi = _dt.datetime.fromisoformat(kw["time_max"])

        def _at(ev: dict[str, Any], key: str) -> _dt.datetime:
            return _dt.datetime.fromisoformat(ev[key]["dateTime"])

        hits = sorted(
            (ev for ev in self.raw if _at(ev, "end") > lo and _at(ev, "start") < hi),
            key=lambda ev: _at(ev, "start"),
        )
        return extract_events(hits[: kw["max_results"]], want_description=True)


class _Hit:
    def __init__(self, title: str, url: str, content: str) -> None:
        self.title, self.url, self.content = title, url, content
        self.file_name = self.channel_name = None
        self.updated_at = "2026-09-20T00:00:00"
        self.chunk_id = 1


class _Search:
    def __init__(self, hits: list[_Hit], *, fail: bool = False) -> None:
        self.hits, self.fail, self.calls = hits, fail, []

    def run(self, q: Any, ctx: Any) -> Any:
        self.calls.append(q)
        if self.fail:
            raise RuntimeError("db down")
        return type("Out", (), {"hits": self.hits})()


class _Web:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail, self.queries = fail, []

    def run(self, q: Any, ctx: Any) -> Any:
        self.queries.append(q.query)
        if self.fail:
            raise RuntimeError("search down")
        src = type(
            "S",
            (),
            {
                "title": "JTB 2026年度 中間決算",
                "url": "https://www.jtbcorp.jp/ir/1",
                "domain": "jtbcorp.jp",
            },
        )
        return type(
            "Out",
            (),
            {
                "error": "",
                "sources": [src],
                "message": "JTB は旅行大手。上期の取扱額は 1.2 兆円 [1]",
            },
        )()


class _Ref:
    def __init__(self, i: str) -> None:
        self.id, self.thread_id = i, f"t{i}"


class _Gmail:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def list_messages(self, query: str, request_id: str, **kw: Any) -> tuple[list[_Ref], None]:
        self.queries.append(query)
        return [_Ref("1")], None

    def get_message(self, mid: str, request_id: str, **kw: Any) -> Any:
        assert kw.get("format") == "metadata"  # 本文は読まない
        return type(
            "M",
            (),
            {
                "headers": {"Subject": "お見積りの件", "From": "田中 <tanaka@jtb.com>"},
                "internal_date_ms": int(_dt.datetime(2026, 9, 30, tzinfo=JST).timestamp() * 1000),
                "snippet": "見積の再提出をお願いします",
            },
        )()


class _Bedrock:
    def __init__(self, text: str) -> None:
        self.text, self.calls = text, []

    def converse(self, **kw: Any) -> Any:
        self.calls.append(kw)
        usage = type("U", (), {"cost_usd": 0.002})()
        return type("R", (), {"text": self.text, "usage": usage})()


LLM_TEXT = "\n".join(
    [
        "承知しました。以下です。",  # 前置きは捨てる
        "*会社概要*",
        "• 旅行大手。上期の取扱額は 1.2 兆円 [1]",
        "• 社員は 9999 人 [1]",  # 材料に無い数字 → 落とす
        "*相手の近況・直近ニュース*",
        "• 上期の決算を公表 [9]",  # 範囲外の番号 → 消えて根拠なし → 落とす
        "*当社との契約・過去のやり取り*",
        "• 9月に TikTok 施策を受注 [2]",
        "• 見積の再提出を依頼されている [3]",
        "*当日の確認事項*",
        "• 再見積の金額感を確認",
    ]
)


def _skill(raw: list[dict[str, Any]], *, llm: str = LLM_TEXT, **kw: Any) -> MeetingPrepSkill:
    cal = kw.get("cal") or _Cal(raw)
    return MeetingPrepSkill(
        calendar_factory=lambda _e: cal,
        search=kw.get(
            "search",
            _Search(
                [
                    _Hit(
                        "案件決定 JTB TikTok",
                        "https://drive.google.com/x",
                        "9月 JTB TikTok 施策 受注",
                    )
                ]
            ),
        ),
        web=kw.get("web", _Web()),
        gmail_factory=kw.get("gmail_factory", lambda _e: _Gmail()),
        bedrock=_Bedrock(llm),
        now=lambda: NOW,
    )


EXTERNAL = _raw("m1", "JTB様 定例_JTB", 14)


def test_rollout_list_limits_who_can_use_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEETING_PREP_ALLOWED_EMAILS", "someone-else@vectorinc.co.jp")
    assert _skill([EXTERNAL]).run(MeetingPrepInput(), _ctx()).error == "rollout_denied"
    monkeypatch.setenv("MEETING_PREP_ALLOWED_EMAILS", "s-komata@vectorinc.co.jp")
    assert _skill([EXTERNAL]).run(MeetingPrepInput(), _ctx()).error == ""


def test_dm_only() -> None:
    out = _skill([EXTERNAL]).run(MeetingPrepInput(), _ctx(channel="C0123"))
    assert out.error == "dm_only" and out.message == DM_ONLY_MESSAGE


def test_report_has_sections_sources_and_drops_ungrounded_lines() -> None:
    out = _skill([EXTERNAL]).run(MeetingPrepInput(), _ctx())
    assert out.error == "" and out.company
    msg = out.message
    for s in SECTIONS:
        assert f"*{s}*" in msg
    assert "1.2 兆円 [1]" in msg
    assert "9999" not in msg  # 材料に無い数字
    assert "[9]" not in msg and "上期の決算を公表" not in msg  # 範囲外の番号＝根拠なし
    assert "承知しました" not in msg
    assert "*相手の近況・直近ニュース*\n• 確認できず" in msg  # 空の見出しは埋める
    # 出典は web → 金庫 → メールの順・URL は材料から
    assert "[1] <https://www.jtbcorp.jp/ir/1|JTB 2026年度 中間決算>" in msg
    assert "[2] <https://drive.google.com/x|" in msg
    assert (
        "[3] <https://mail.google.com/mail/u/0/#all/t1|メール「お見積りの件」（2026-09-30）>" in msg
    )
    assert [s.kind for s in out.sources] == ["web", "vault", "mail"]


def test_picks_earliest_upcoming_external_and_skips_internal_task_and_past() -> None:
    raw = [
        _raw("past", "ABC様 打合せ_ABC", 7),  # 9:00 の 30 分より前に始まった＝終わった扱い
        _raw("task", "資料作成", 10, guests=False),  # タスク枠
        {
            **_raw("int", "【社内】定例", 11),
            "attendees": [
                {"email": "a@vectorinc.co.jp", "self": True},
                {"email": "b@vectorinc.co.jp"},
            ],
        },
        EXTERNAL,
        _raw("later", "HIS様 提案_HIS", 16),
    ]
    import os

    os.environ["DIGEST_INTERNAL_DOMAIN"] = "vectorinc.co.jp"
    try:
        out = _skill(raw).run(MeetingPrepInput(), _ctx())
    finally:
        os.environ.pop("DIGEST_INTERNAL_DOMAIN")
    assert out.meeting_title == "JTB様 定例_JTB"
    out2 = _skill(raw).run(MeetingPrepInput(target="HIS"), _ctx())
    assert out2.meeting_title == "HIS様 提案_HIS"


def test_no_meeting() -> None:
    out = _skill([_raw("task", "資料作成", 10, guests=False)]).run(MeetingPrepInput(), _ctx())
    assert out.error == "no_meeting"


def test_unknown_company_asks_back_without_searching() -> None:
    web = _Web()
    ev = {
        **_raw("m", "【社外】打ち合わせ", 14),
        "attendees": [
            {"email": "s-komata@vectorinc.co.jp", "self": True},
            {"email": "someone@gmail.com"},
        ],
    }
    out = _skill([ev], web=web).run(MeetingPrepInput(), _ctx())
    assert out.error == "no_company" and "会社名" in out.message
    assert web.queries == []  # 推測で外へ検索しない


def test_failed_materials_are_listed_and_report_still_made() -> None:
    out = _skill(
        [EXTERNAL],
        web=_Web(fail=True),
        search=_Search([], fail=True),
        gmail_factory=lambda _e: (_ for _ in ()).throw(PermissionError("not connected")),
        llm="*会社概要*\n• 確認できず（資料に無い）",
    ).run(MeetingPrepInput(), _ctx())
    assert out.error == ""
    assert (
        "取れなかった材料: 公開情報（会社概要・ニュース）・社内資料・案件の記録・相手とのメール"
        in out.message
    )
    assert out.message.count(f"• {UNKNOWN}") >= 4


def test_company_name_goes_through_the_guard_into_queries() -> None:
    gmail, web = _Gmail(), _Web()
    _skill([EXTERNAL], web=web, gmail_factory=lambda _e: gmail).run(MeetingPrepInput(), _ctx())
    assert web.queries and web.queries[0].startswith("JTB")
    assert gmail.queries and "newer_than:180d" in gmail.queries[0]


@pytest.mark.parametrize(
    ("line", "kept"),
    [
        ("• 受注あり [1]", True),
        ("• 受注あり", False),
        ("• 確認できず（資料に無い）", True),
        ("• 受注あり [5]", False),
    ],
)
def test_postprocess_citation_rules(line: str, kept: bool) -> None:
    mats = [Material(kind="vault", label="a", url="", text="受注あり")]
    body = postprocess(f"*会社概要*\n{line}", materials=mats, grounding_texts=["受注あり"])
    assert (line.split("[")[0].strip() in body) is kept or (not kept and UNKNOWN in body)


def test_task_block_about_a_client_is_not_the_meeting() -> None:
    """「JTB様 資料準備」のような自分だけの作業枠（ゲストも会議リンクも無い）は商談ではない。

    変異: _pick_meeting の is_personal_block を外すと 10:00 の作業枠が選ばれて赤。
    """
    raw = [_raw("prep", "JTB様 資料準備_JTB", 10, guests=False), EXTERNAL]
    out = _skill(raw).run(MeetingPrepInput(), _ctx())
    assert out.meeting_title == "JTB様 定例_JTB"


# ---- 今日に無ければ数日先まで（2026-10-05 17:44 本番:「次の商談の準備」→ 今日だけ見て空振り） ----

LATER = _raw("m7", "JTB様 定例_JTB", 14, day=7)  # 10/7(水) 14:00


def test_next_meeting_on_a_later_day_is_prepared_when_today_has_none() -> None:
    """本番の再現: 今日は社外商談 0 件（タスク枠だけ）・明後日に社外商談。

    修正前は今日だけを読んで no_meeting。修正後はその商談のレポートを、日付つきで作る。
    変異: time_max を今日 +1 日に戻す（先読みを外す）と no_meeting で赤。
    """
    cal = _Cal([_raw("task", "資料作成", 10, guests=False), LATER])
    out = _skill([], cal=cal).run(MeetingPrepInput(), _ctx())
    assert out.error == "" and out.meeting_title == "JTB様 定例_JTB"
    lines = out.message.split("\n")
    assert lines[0] == "今日これからの社外商談は無いので、次の10/7(水)の商談を準備しました。"
    assert lines[1] == "📋 *商談の準備* 10/7(水) 14:00–15:00「JTB様 定例_JTB」"
    # 呼び出しは 1 回で今日 0:00〜7 日先の終わりまで（1 日 100 件の目安で上限を広げる）
    assert len(cal.calls) == 1
    call = cal.calls[0]
    assert call["time_min"] == "2026-10-05T00:00:00+09:00"
    assert call["time_max"] == "2026-10-13T00:00:00+09:00"
    assert call["max_results"] == 800


def test_today_meeting_wins_over_later_days() -> None:
    """今日にあれば今日（明日以降が先に並んでいても）。日付も前置きも付けない。

    変異: 候補の並べ替えを逆順にする（今日優先を外す）と 10/7 が選ばれて赤。
    """
    raw = [_raw("later", "HIS様 提案_HIS", 10, day=6), LATER, EXTERNAL]
    out = _skill(raw).run(MeetingPrepInput(), _ctx())
    assert out.meeting_title == "JTB様 定例_JTB"
    assert out.message.startswith("📋 *商談の準備* 14:00–15:00「JTB様 定例_JTB」")
    # target があっても今日の一致を優先
    out2 = _skill([LATER, EXTERNAL]).run(MeetingPrepInput(target="JTB"), _ctx())
    assert out2.message.startswith("📋 *商談の準備* 14:00–15:00「JTB様 定例_JTB」")


def test_target_only_on_a_later_day_is_found_with_lead_line() -> None:
    raw = [EXTERNAL, _raw("his", "HIS様 提案_HIS", 11, day=8)]
    out = _skill(raw).run(MeetingPrepInput(target="HIS"), _ctx())
    assert out.meeting_title == "HIS様 提案_HIS"
    lines = out.message.split("\n")
    assert (
        lines[0] == "今日これからの「HIS」の社外商談は無いので、次の10/8(木)の商談を準備しました。"
    )
    assert lines[1].startswith("📋 *商談の準備* 10/8(木) 11:00–12:00")


@pytest.mark.parametrize(("day", "picked"), [(12, True), (13, False)])
def test_meetings_beyond_lookahead_are_not_picked(day: int, picked: bool) -> None:
    """7 日先（10/12）の終わりまでは選び、8 日先（10/13）は選ばない。

    変異: 日数を無視して上限（30 日）まで読むと 10/13 が選ばれて赤。
    """
    out = _skill([_raw("far", "JTB様 定例_JTB", 9, day=day)]).run(MeetingPrepInput(), _ctx())
    if picked:
        assert out.error == "" and out.message.startswith(
            "今日これからの社外商談は無いので、次の10/12(月)の商談を準備しました。"
        )
    else:
        assert out.error == "no_meeting"
        assert out.message == (
            "今日から7日先までの予定に社外の商談が見つかりませんでした"
            "（社内会議・タスク枠は除いています）。"
        )


def test_no_meeting_message_with_target_names_the_range() -> None:
    out = _skill([EXTERNAL]).run(MeetingPrepInput(target="HIS"), _ctx())
    assert out.error == "no_meeting"
    assert out.message == (
        "今日から7日先までの予定に「HIS」に当てはまる社外の商談が見つかりませんでした"
        "（社内会議・タスク枠は除いています）。"
    )
    assert "検索" not in out.message and "確認して" not in out.message  # 利用者に作業を頼まない


def test_lookahead_zero_keeps_today_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEETING_PREP_LOOKAHEAD_DAYS", "0")
    cal = _Cal([LATER])
    out = _skill([], cal=cal).run(MeetingPrepInput(), _ctx())
    assert out.error == "no_meeting"
    assert out.message == (
        "今日の予定にこれからの社外の商談が見つかりませんでした（社内会議・タスク枠は除いています）。"
    )
    assert cal.calls[0]["time_max"] == "2026-10-06T00:00:00+09:00"
    assert cal.calls[0]["max_results"] == 100
    # 今日の商談は従来どおり（日付も前置きも無し）
    out2 = _skill([EXTERNAL]).run(MeetingPrepInput(), _ctx())
    assert out2.message.startswith("📋 *商談の準備* 14:00–15:00「JTB様 定例_JTB」")


@pytest.mark.parametrize(
    ("raw", "days"), [("", 7), ("3", 3), ("abc", 7), ("-2", 0), ("99", 30), (" 0 ", 0)]
)
def test_lookahead_days_env(monkeypatch: pytest.MonkeyPatch, raw: str, days: int) -> None:
    from teamagent.skills.meeting_prep.skill import lookahead_days

    monkeypatch.setenv("MEETING_PREP_LOOKAHEAD_DAYS", raw)
    assert lookahead_days() == days
