"""meeting_prep（社外商談の準備レポート・v1）。

固定すること:
- DM 以外では作らない（メールと社内資料を読むため）
- 対象の商談: 今日の時刻つき・社外（社内会議は除く）・タスク枠は除く・終わったものは除く・
  target があれば予定名／会社名で絞る・一番早いもの
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


def _raw(eid: str, title: str, hh: int, *, guests: bool = True, desc: str = "") -> dict[str, Any]:
    ev: dict[str, Any] = {
        "id": eid,
        "summary": title,
        "start": {"dateTime": f"2026-10-05T{hh:02d}:00:00+09:00"},
        "end": {"dateTime": f"2026-10-05T{hh + 1:02d}:00:00+09:00"},
        "description": desc,
    }
    if guests:
        ev["attendees"] = [
            {"email": "s-komata@vectorinc.co.jp", "self": True},
            {"email": "tanaka@jtb.com"},
        ]
    return ev


class _Cal:
    def __init__(self, raw: list[dict[str, Any]]) -> None:
        self.raw = raw

    def list_events(self, request_id: str, **kw: Any) -> list[Any]:
        assert kw.get("want_description") is True
        return extract_events(self.raw, want_description=True)


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
    return MeetingPrepSkill(
        calendar_factory=lambda _e: _Cal(raw),
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
