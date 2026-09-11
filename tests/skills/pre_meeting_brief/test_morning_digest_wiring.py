"""morning_digest 側の配線（既定 OFF・封じ込め・写し替え・max_results ゲート）。

⚠️ フェイクは本番の失敗モードを再現する:
  - ``_FakeGcal`` は ``want_description`` が偽なら **description を落とした** イベントを返す
    （本物の events.list も fields 指定で description を取らない）
  - メール経路は 0 件・Slack 経路は未設定（連携前の利用者と同じ状態）
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.morning_digest.schema import MorningDigestInput
from teamagent.skills.morning_digest.skill import MorningDigestSkill, _brief_enabled

ME = "komata@vectorinc.co.jp"

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
