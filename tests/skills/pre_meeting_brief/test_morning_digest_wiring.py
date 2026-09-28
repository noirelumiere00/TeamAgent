"""morning_digest 側の配線（既定 OFF・封じ込め・写し替え・max_results ゲート）。

⚠️ フェイクは本番の失敗モードを再現する:
  - ``_FakeGcal`` は ``want_description`` が偽なら **description を落とした** イベントを返す
    （本物の events.list も fields 指定で description を取らない）
  - メール経路は 0 件・Slack 経路は未設定（連携前の利用者と同じ状態）
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest.schema import MorningDigestInput
from teamagent.skills.morning_digest.skill import MorningDigestSkill, _brief_enabled

ME = "komata@vectorinc.co.jp"


@pytest.fixture(autouse=True)
def _freeze_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """「今日」を予定と同じ 2026-09-11 に固定する。

    本番は ``calendar_window.now_jst()`` の壁時計で今日を決めるため、固定しないと
    09-11 以外の日は予定が対象外になり全件空で落ちる（#401 CI・09-14 に顕在化）。
    """
    from teamagent.skills.morning_digest import calendar_window as calwin

    monkeypatch.setattr(
        calwin, "now_jst", lambda: _dt.datetime(2026, 9, 11, 7, 0, tzinfo=calwin.JST)
    )


RAW_EVENTS: list[dict[str, Any]] = [
    {
        "id": "e1",
        "summary": "【社外】青葉広告山田様",
        "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
        "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
        "description": "クライアント：北都リゾート／代理店：青葉広告（山田様）",
        "organizer": {"email": "boss@vectorinc.co.jp"},
        "attendees": [
            {"email": "me@vectorinc.co.jp", "self": True},
            {"email": "yamada@aoba-ad.co.jp"},
        ],
    }
]


class _FakeGcal:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.items = RAW_EVENTS if items is None else items
        self.kwargs: list[dict[str, Any]] = []

    def list_events(self, request_id: str, **kw: Any) -> list[Any]:
        from teamagent.adapters.gcalendar_client import extract_events

        self.kwargs.append(kw)
        if kw.get("want_description"):
            return list(extract_events(self.items, want_description=True))
        return list(extract_events(self.items))


class _NoMail:
    """メール経路を無害化（本テストの関心はブリーフ配線のみ・LLM 課金ゼロ）。"""

    def list_messages(
        self, query: str, request_id: str, max_results: int = 30
    ) -> tuple[list[Any], None]:
        return ([], None)

    def list_drafts(self, request_id: str, **_: Any) -> list[Any]:
        return []


class _FakeTokenStore:
    def get(self, user_email: str) -> Any:
        return object()


def _skill(gcal: _FakeGcal) -> MorningDigestSkill:
    skill = MorningDigestSkill()
    skill._gcal_for = lambda token: gcal  # type: ignore[method-assign]
    return skill


def _runnable_skill(gcal: _FakeGcal) -> MorningDigestSkill:
    """``run()`` を最後まで通せる skill（メール 0 件・Slack 未設定・Bedrock なし）。"""
    return MorningDigestSkill(
        token_store=_FakeTokenStore(),
        gmail=_NoMail(),
        gcalendar=gcal,
        bedrock=None,
    )


def _ctx() -> SkillContext:
    return SkillContext(request_id="r", metadata={"user_email": ME})


def _input() -> MorningDigestInput:
    return MorningDigestInput(max_drafts=0)


# ── 取得上限 / description もゲートの内側 ────────────────────────────────
def test_calendar_uses_max_results_100_when_brief_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ゲート ON のときだけ 100 件取る。

    変異: ``_CALENDAR_MAX_RESULTS`` を 20 に戻すと赤。
    """
    monkeypatch.setenv("MORNING_DIGEST_BRIEF", "true")
    gcal = _FakeGcal()
    _skill(gcal)._collect_calendar(None, _input(), _ctx())
    assert gcal.kwargs[0]["max_results"] == 100
    assert gcal.kwargs[0]["want_description"] is True


def test_calendar_keeps_legacy_limits_when_brief_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env 未設定なら **従来どおり 20 件・description なし**。

    100 は見出しの「📅{件数}」「残り N 件」を変え、MORNING_DIGEST_REMINDERS=1 の環境では
    21 件目以降にも予定リマインド DM の予約を作る＝OFF でも利用者の画面が変わってしまう。

    変異: ``_collect_calendar`` の ``max_results`` を無条件 100 に戻すと赤。
    """
    monkeypatch.delenv("MORNING_DIGEST_BRIEF", raising=False)
    gcal = _FakeGcal()
    _skill(gcal)._collect_calendar(None, _input(), _ctx())
    assert gcal.kwargs[0]["max_results"] == 20
    assert gcal.kwargs[0]["want_description"] is False


def test_saturation_flag_is_raised_when_limit_is_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MORNING_DIGEST_BRIEF", "true")
    many = [
        {
            "id": f"e{i}",
            "summary": f"予定{i}",
            "start": {"dateTime": "2026-09-11T10:00:00+09:00"},
            "end": {"dateTime": "2026-09-11T11:00:00+09:00"},
        }
        for i in range(100)
    ]
    gcal = _FakeGcal(many)
    skill = _skill(gcal)
    skill._collect_calendar(None, _input(), _ctx())
    assert skill._calendar_saturated is True


def test_derived_signal_fields_are_copied_into_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """派生値は build_signal_input 経由で写る。生 description は入らない。"""
    monkeypatch.setenv("MORNING_DIGEST_BRIEF", "true")
    items = _skill(_FakeGcal())._collect_calendar(None, _input(), _ctx())
    item = items[0]
    assert item.has_client_line is True
    assert item.client_hint_display == "北都リゾート"
    assert item.agency_display == "青葉広告(山田様)"
    assert item.attendee_domains == ["vectorinc.co.jp", "aoba-ad.co.jp"]
    assert item.attendee_list_available is True
    # 生 description はどのフィールドにも載らない
    dumped = item.model_dump_json()
    assert "クライアント：北都リゾート／代理店" not in dumped


# ── 2 経路の判定一致（切り位置・正規化の順序を揃える）────────────────
#: 120 字を超え、**121 字目以降に除外語** を置いた予定名。
#: 生のまま 120 字で切ると「ヨミ会」が消えて別判定になる。
LONG_TITLE_WITH_TRAILING_EXCLUSION = "案件Ａ" * 45 + "ヨミ会"


def test_long_title_is_judged_the_same_on_both_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """150 字超の予定名でも 定期便経路 と tool 経路 の判定が一致する。

    事故の形: ``build_signal_input`` は **NFKC してから 200 字**、
    ``summary_display`` は **生のまま 120 字**。切り位置も順序も違うので、
    121 字目以降に除外語や「様」がある予定は経路で判定が割れる
    （実測: tool=internal / 定期便=uncertain → 朝の DM にだけ社内定例が並ぶ）。

    変異（どちらでも赤になることを実測済み）:
      - ``morning_digest/skill.py`` の ``title_signal=sig.title`` を落とす
        → ``signals_from_item`` が display（120 字）へフォールバックして判定が割れる
      - ``signals_from_item`` を ``normalize_text(summary_display)`` へ戻す → 同上
    """
    monkeypatch.setenv("MORNING_DIGEST_BRIEF", "true")
    from teamagent.adapters.gcalendar_client import extract_events
    from teamagent.skills.pre_meeting_brief.classify import classify_external
    from teamagent.skills.pre_meeting_brief.signals import build_signal_input, signals_from_item

    assert len(LONG_TITLE_WITH_TRAILING_EXCLUSION) > 120
    raw = [
        {
            "id": "long-1",
            "summary": LONG_TITLE_WITH_TRAILING_EXCLUSION,
            "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
            "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
            "organizer": {"email": "boss@vectorinc.co.jp"},
            "attendees": [{"email": "me@vectorinc.co.jp", "self": True}],
        }
    ]
    internal = frozenset({"vectorinc.co.jp"})

    # tool 経路: 生 item から直接
    (detail,) = extract_events(raw, want_description=True)
    tool_sig = build_signal_input(detail)

    # 定期便経路: morning_digest の **実物の写し替え** を通す（手組みしない）
    (item,) = _skill(_FakeGcal(raw))._collect_calendar(None, _input(), _ctx())
    runner_sig = signals_from_item(item)

    assert runner_sig.title == tool_sig.title
    assert "ヨミ会" in runner_sig.title  # 120 字で切られていない
    assert classify_external(runner_sig, internal_domains=internal) == classify_external(
        tool_sig, internal_domains=internal
    )
    assert classify_external(tool_sig, internal_domains=internal) == "internal"


def test_display_title_stays_raw_and_short(monkeypatch: pytest.MonkeyPatch) -> None:
    """判定用の写し（title_signal）と **表示用**（summary_display）は別物のまま。

    判定を揃えるために display の上限を 200 字へ広げると、朝の DM の予定行が
    長くなる／NFKC で字面が変わる。分けていることをここで固定する。
    """
    monkeypatch.setenv("MORNING_DIGEST_BRIEF", "true")
    raw = [
        {
            "id": "long-2",
            "summary": LONG_TITLE_WITH_TRAILING_EXCLUSION,
            "start": {"dateTime": "2026-09-11T14:00:00+09:00"},
            "end": {"dateTime": "2026-09-11T15:00:00+09:00"},
        }
    ]
    (item,) = _skill(_FakeGcal(raw))._collect_calendar(None, _input(), _ctx())
    assert len(item.summary_display) == 120
    assert item.summary_display == LONG_TITLE_WITH_TRAILING_EXCLUSION[:120]
    assert len(item.title_signal) == len(LONG_TITLE_WITH_TRAILING_EXCLUSION)


# ── 既定 OFF（呼び出し側のゲートを固定する） ──────────────────────────
def test_brief_enabled_helper_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MORNING_DIGEST_BRIEF", raising=False)
    assert _brief_enabled() is False
    monkeypatch.setenv("MORNING_DIGEST_BRIEF", "true")
    assert _brief_enabled() is True


def test_brief_skill_is_never_constructed_when_env_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env 未設定で ``run()`` を最後まで通しても skill を **1 度も new しない**。

    ヘルパ ``_brief_enabled()`` 単体の assert では「呼び出し側にゲートが在る」ことを
    証明できない（ゲートを ``if True:`` にしても緑のままになる）。ここではスパイを
    差し込み、呼び出し回数 0 と出力フィールド未設定の両方を固定する。

    変異: ``skill.py`` の ``if _brief_enabled():`` を ``if True:`` に置き換えると赤。
    """
    import teamagent.skills.pre_meeting_brief.skill as brief_mod

    calls: list[str] = []
    real = brief_mod.PreMeetingBriefSkill

    class _Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, **kw: Any) -> None:
            calls.append("init")
            super().__init__(**kw)

        def run(self, input: Any, ctx: Any) -> Any:
            calls.append("run")
            return super().run(input, ctx)

    monkeypatch.setattr(brief_mod, "PreMeetingBriefSkill", _Spy)
    monkeypatch.delenv("MORNING_DIGEST_BRIEF", raising=False)

    out = _runnable_skill(_FakeGcal()).run(_input(), _ctx())

    assert calls == []
    assert out.pre_meeting_brief is None
    assert out.brief_scanned is False
    assert out.brief_skip_reason == ""
    # 📅 は通常どおり出る（OFF でも予定セクションは動く）。
    assert len(out.calendar_events) == 1


def test_brief_skill_is_constructed_when_env_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """ON の側も固定する（OFF テストが『そもそも到達しない』で緑にならないように）。"""
    import teamagent.skills.pre_meeting_brief.skill as brief_mod

    calls: list[str] = []
    real = brief_mod.PreMeetingBriefSkill

    class _Spy(real):  # type: ignore[misc, valid-type]
        def __init__(self, **kw: Any) -> None:
            calls.append("init")
            super().__init__(**kw)

    monkeypatch.setattr(brief_mod, "PreMeetingBriefSkill", _Spy)
    monkeypatch.setenv("MORNING_DIGEST_BRIEF", "true")

    _runnable_skill(_FakeGcal()).run(_input(), _ctx())
    assert calls == ["init"]


# ── 封じ込め（節が落ちても 📅/📧 は通常配信） ────────────────────────
def test_brief_failure_is_contained_inside_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_collect_brief`` が必ず raise しても ``run()`` は成立する。

    ⚠️ テスト本体で try/except を書いて errors へ積むのは **封じ込めの検査ではない**
    （skill 側の try/except を丸ごと消しても緑になる）。ここでは ``run()`` を呼び、
    戻り値だけを見る。

    変異: ``skill.py`` の ``_collect_brief`` を包む try/except を外すと
    ``RuntimeError`` が ``run()`` の外まで抜けて赤。
    """
    import teamagent.skills.morning_digest.skill as mod

    def _boom(self: Any, o: Any, c: Any) -> None:
        raise RuntimeError("金庫が落ちた")

    monkeypatch.setenv("MORNING_DIGEST_BRIEF", "true")
    monkeypatch.setattr(mod.MorningDigestSkill, "_collect_brief", _boom)

    out = _runnable_skill(_FakeGcal()).run(_input(), _ctx())

    assert out.errors == ["brief: RuntimeError"]
    assert out.pre_meeting_brief is None
    assert out.brief_scanned is False
    # 📅 は通常どおり埋まっている（節のためだけに digest 全体を落とさない）。
    assert [e.summary_display for e in out.calendar_events] == ["【社外】青葉広告山田様"]
