"""朝ダイジェストの表示用の文字列を、絵文字（書記素クラスタ）の途中で切らないことのテスト。

本人 DM にそのまま出るフィールドをコードポイント単位で切ると、切り口に片割れ（孤立した
地域指示子は□で囲んだ英字に見える・宙ぶらりんの ZWJ・肌色の抜けた 👍 など）が残る。
ここでは各フィールドの上限ちょうどにクラスタがかかる入力を流し、クラスタが丸ごと落ちる
（＝割れない）こと、境界ちょうどで終わるクラスタは残ることを確かめる。

- skill 側: fake の Gmail / Bedrock / Calendar / Slack で ``MorningDigestSkill.run()`` を通す
  （件名・LLM の要約/期限/依頼/次アクション・予定名・場所・チャンネル名・本文・差出人名）。
  クラスタを落として Slack 本文が上限に届かなくても「本文が途中で切れています」が出ること。
- 描画側: DM を組み立てるときの 2 つ目の切り詰め（``_truncate`` と Slack 見出しの冒頭一文）。

⚠️ 不可視文字（ZWJ・VS16・結合文字）はソースに直接書かず ``\\u`` エスケープで書く。
"""

from __future__ import annotations

import base64
import datetime as _dt
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from teamagent.skills._shared.slack_handoff import (
    NOTE_BODY_TRUNCATED,
    QUOTE_CLOSE,
    QUOTE_OPEN,
    headline_from_body,
    triage_slack_handoff,
)
from teamagent.skills._shared.slack_unreplied import UnrepliedCollection, UnrepliedMention
from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest import calendar_window as calwin
from teamagent.skills.morning_digest.schema import MorningDigestInput
from teamagent.skills.morning_digest.skill import MorningDigestSkill

ME = "me@vectorinc.co.jp"

# 表示に来る書記素クラスタ（見た目の 1 文字）。途中で切ると片割れが DM に残るもの。
_CLUSTERS = {
    "family": "\U0001f468‍\U0001f469‍\U0001f467",  # 👨 ZWJ 👩 ZWJ 👧
    "flag": "\U0001f1ef\U0001f1f5",  # 地域指示子 2 個（🇯🇵）
    "skin_tone": "\U0001f44d\U0001f3fd",  # 👍 肌色
    "keycap": "1️⃣",  # 1 VS16 囲み
    "tag_flag": "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
    "ivs_kanji": "葛\U000e0100",  # 異体字セレクタ付きの漢字
    "decomposed_kana": "が",  # か＋結合濁点
    "cjk": "定",
}

# skill がそのフィールドを切る長さ（schema の max_length と同じ値。LLM の deadline だけは
# schema に上限が無く skill 側の 80 字だけ）。
_MAIL_LIMITS = {
    "subject_display": 160,
    "summary": 200,
    "deadline": 80,
    "ask": 120,
    "next_step": 120,
}
_CALENDAR_LIMITS = {"summary_display": 120, "location_display": 120}
_SLACK_LIMITS = {"channel_name_display": 80, "excerpt_display": 1500, "from_display_name": 80}


def _cases(cluster: str, limits: dict[str, int]) -> list[tuple[dict[str, str], dict[str, str]]]:
    """(フィールド→入力, フィールド→期待する表示値) の組を返す。

    上限がクラスタの内側に来る並び（1 個目の直後・2 個目の直後…）をすべて作る。この場合は
    クラスタを丸ごと落とした先頭部分が正解。最後に「上限ちょうどでクラスタが終わる」並びを
    足す（この場合はクラスタを残すのが正解＝落としすぎていない）。
    """
    out: list[tuple[dict[str, str], dict[str, str]]] = []
    for inside in range(1, len(cluster)):
        raw: dict[str, str] = {}
        want: dict[str, str] = {}
        for field, limit in limits.items():
            head = "定" * (limit - inside)
            raw[field] = head + cluster + "例"
            want[field] = head
        out.append((raw, want))
    raw, want = {}, {}
    for field, limit in limits.items():
        head = "例" * (limit - len(cluster))
        raw[field] = head + cluster + "例"
        want[field] = head + cluster
    out.append((raw, want))
    return out


# ── fakes（本番のアダプタと同じ形の値を返すだけ。切り詰めは一切しない） ──────────────


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text
        self.usage = type("U", (), {"cost_usd": 0.001})()


class _Bedrock:
    def __init__(self, triage: dict[str, Any] | None = None) -> None:
        self._triage = triage

    def converse(self, **kw: Any) -> _Resp:
        assert self._triage is not None, "triage が呼ばれない前提のテストで呼ばれた"
        return _Resp(json.dumps([self._triage]))


class _Msg:
    def __init__(self, subject: str) -> None:
        self.headers = {"From": "c@x.com", "To": ME, "Subject": subject}
        self.payload = {
            "mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode("ご確認ください".encode()).decode()},
        }
        self.internal_date_ms = 1000
        self.thread_id = "T1"
        self.id = "m1"
        self.label_ids = ()


class _Gmail:
    def __init__(self, msgs: list[_Msg]) -> None:
        self._msgs = msgs

    def list_messages(self, q: str, rid: str, max_results: int = 30) -> Any:
        refs = [type("R", (), {"id": m.id, "thread_id": m.thread_id})() for m in self._msgs]
        return (refs, None)

    def get_thread(self, tid: str, rid: str, **_: Any) -> list[Any]:
        return [m for m in self._msgs if m.thread_id == tid]

    def list_drafts(self, rid: str, **_: Any) -> list[Any]:
        return []


class _GCal:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def list_events(self, request_id: str, **kwargs: Any) -> list[Any]:
        return self._events


@dataclass
class _CalEvent:
    summary: str = ""
    start: str = "2026-06-18T10:00:00+09:00"
    end: str = "2026-06-18T11:00:00+09:00"
    location: str = ""
    all_day: bool = False


class _SlackProvider:
    def __init__(self, mention: UnrepliedMention) -> None:
        self._collection = UnrepliedCollection(items=(mention,), total_unreplied=1, scanned=True)

    def collect_detailed(self, email: str, horizon: int, rid: str) -> UnrepliedCollection:
        return self._collection


class _Tokens:
    def get(self, e: str) -> Any:
        return object()


def _run(
    *,
    msgs: list[_Msg] | None = None,
    triage: dict[str, Any] | None = None,
    events: list[Any] | None = None,
    slack: Any = None,
) -> Any:
    skill = MorningDigestSkill(
        token_store=_Tokens(),
        gmail=_Gmail(msgs or []),
        gcalendar=_GCal(events or []),
        bedrock=_Bedrock(triage),
        slack=slack,
    )
    skill._draft_on_demand_only = True  # 下書き生成はこのテストの対象外
    out = skill.run(
        MorningDigestInput(max_drafts=0), SkillContext(request_id="r", metadata={"user_email": ME})
    )
    assert not out.errors, out.errors
    return out


# ── skill 側 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(_CLUSTERS))
def test_mail_display_fields_do_not_split_a_cluster(name: str) -> None:
    """メールの件名と、LLM が返した要約・期限・依頼・次アクション（どれも DM にそのまま出る）。"""
    for raw, want in _cases(_CLUSTERS[name], _MAIL_LIMITS):
        triage = {
            "id": "5feceb66",  # _short_hash(0)（id 結合の本番契約に合わせる）
            "importance": "high",
            "summary": raw["summary"],
            "deadline": raw["deadline"],
            "ask": raw["ask"],
            "next_step": raw["next_step"],
            "meeting_start": None,
            "meeting_end": None,
            "meeting_title": "",
            "scheduling_request": False,
        }
        out = _run(msgs=[_Msg(raw["subject_display"])], triage=triage)
        item = out.mail_digest[0]
        assert {field: getattr(item, field) for field in _MAIL_LIMITS} == want


@pytest.mark.parametrize("name", sorted(_CLUSTERS))
def test_calendar_display_fields_do_not_split_a_cluster(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """予定名と場所（本人 DM 表示用の未マスク値）。"""
    monkeypatch.setattr(
        calwin, "now_jst", lambda: _dt.datetime(2026, 6, 18, 9, 30, tzinfo=calwin.JST)
    )
    for raw, want in _cases(_CLUSTERS[name], _CALENDAR_LIMITS):
        event = _CalEvent(summary=raw["summary_display"], location=raw["location_display"])
        item = _run(events=[event]).calendar_events[0]
        assert {field: getattr(item, field) for field in _CALENDAR_LIMITS} == want


@pytest.mark.parametrize("name", sorted(_CLUSTERS))
def test_slack_display_fields_do_not_split_a_cluster(name: str) -> None:
    """Slack 返信漏れのチャンネル名・本文・差出人の表示名。"""
    for raw, want in _cases(_CLUSTERS[name], _SLACK_LIMITS):
        mention = UnrepliedMention(
            channel_id="C1",
            channel_name=raw["channel_name_display"],
            ts="1000.1",
            text=raw["excerpt_display"],
            permalink="https://x/p1",
            occurred_at="2026-07-10T09:00:00+09:00",
            user="U1",
            user_display=raw["from_display_name"],
        )
        item = _run(slack=_SlackProvider(mention)).slack_unread[0]
        assert {field: getattr(item, field) for field in _SLACK_LIMITS} == want


def _slack_note(text: str) -> tuple[int, str]:
    """本文 ``text`` の返信漏れを digest に通し、(表示本文の長さ, カードの補足行) を返す。"""
    mention = UnrepliedMention(
        channel_id="C1",
        channel_name="sales",
        ts="1000.1",
        text=text,
        permalink="https://x/p1",
        occurred_at="2026-07-10T09:00:00+09:00",
    )
    item = _run(slack=_SlackProvider(mention)).slack_unread[0]
    now = _dt.datetime(2026, 7, 10, 12, 0, tzinfo=calwin.JST)
    card = triage_slack_handoff([item], now=now, me_user_id=None).cards[0]
    return len(item.excerpt_display), card.note


@pytest.mark.parametrize("name", sorted(set(_CLUSTERS) - {"cjk"}))
def test_slack_body_cut_before_a_cluster_is_still_reported_as_truncated(name: str) -> None:
    """絵文字を丸ごと落とすと表示本文は 1500 字に届かない。それでも「本文が途中で切れて
    います」は出る（描画側の「1500 字以上なら切れている」推定だけに頼ると消える）。"""
    cluster = _CLUSTERS[name]
    shown, note = _slack_note("定" * 1499 + cluster + "例")
    assert shown == 1499  # クラスタが丸ごと落ちて上限に届いていない
    assert note == NOTE_BODY_TRUNCATED
    # 切っていない本文には出さない（上限ちょうど・上限ちょうどでクラスタが終わる・上限未満）。
    assert _slack_note("定" * 1500) == (1500, "")
    assert _slack_note("定" * (1500 - len(cluster)) + cluster) == (1500, "")
    assert _slack_note("定" * 10 + cluster) == (10 + len(cluster), "")


# ── 描画側（DM を組み立てるときの 2 つ目の切り詰め） ────────────────────────────────

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "run_morning_digest_fargate.py"
)


def _load_runner() -> Any:
    mod_name = "run_morning_digest_grapheme_cut_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_runner = _load_runner()


@pytest.mark.parametrize("name", sorted(_CLUSTERS))
def test_renderer_truncate_does_not_split_a_cluster(name: str) -> None:
    """密度優先描画の件名・要約（60 字）と Slack chip の名前の切り詰め。「…」の手前も割らない。"""
    cluster = _CLUSTERS[name]
    limit = 60
    for inside in range(1, len(cluster)):
        head = "定" * (limit - 1 - inside)
        assert _runner._truncate(head + cluster + "例" * 5, limit) == head + "…"
    head = "例" * (limit - 1 - len(cluster))
    got = _runner._truncate(head + cluster + "例" * 5, limit)
    assert got == head + cluster + "…"
    assert len(got) == limit


# NFKC を通るので、合成済みの 1 字に畳まれる「か＋結合濁点」は除く（割れようがない）。
_HEADLINE_CLUSTERS = sorted(set(_CLUSTERS) - {"decomposed_kana"})


@pytest.mark.parametrize("name", _HEADLINE_CLUSTERS)
def test_slack_headline_from_body_does_not_split_a_cluster(name: str) -> None:
    """依頼文が取れないときの見出し（相手の冒頭一文・括弧の内側 40 字）。"""
    cluster = _CLUSTERS[name]
    limit = 40
    for inside in range(1, len(cluster)):
        head = "定" * (limit - 1 - inside)
        got = headline_from_body(head + cluster + "例" * 5 + "。")
        assert got == QUOTE_OPEN + head + "…" + QUOTE_CLOSE
    head = "例" * (limit - 1 - len(cluster))
    got = headline_from_body(head + cluster + "例" * 5 + "。")
    assert got == QUOTE_OPEN + head + cluster + "…" + QUOTE_CLOSE
