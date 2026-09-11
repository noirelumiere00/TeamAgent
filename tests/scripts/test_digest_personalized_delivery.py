"""個人別配信（planner / 単独利用者 / 二重配信の防止）と事例ブリーフ節の runner テスト。

scripts/ は package でないため importlib でロードする（既存テストと同流儀）。
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from teamagent.digest_user_ref import digest_schedule_name, resolve_user_ref, user_ref
from teamagent.skills.morning_digest.schema import MorningDigestOutput
from teamagent.skills.pre_meeting_brief.schema import (
    CaseRef,
    PreMeetingBriefItem,
    PreMeetingBriefOutput,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_morning_digest_fargate.py"


def _load() -> Any:
    name = "run_morning_digest_personalized_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load()

USER = "komata@vectorinc.co.jp"
DAY = _dt.date(2026, 9, 11)


# ── user_ref（不可逆・メールを復元できない）──────────────────────────
def test_user_ref_is_stable_and_does_not_contain_the_email() -> None:
    ref = user_ref(USER, pepper="p")
    assert len(ref) == 32
    assert ref == user_ref(USER, pepper="p")
    assert USER not in ref
    assert "komata" not in ref
    assert "vectorinc" not in ref


def test_user_ref_changes_with_pepper() -> None:
    assert user_ref(USER, pepper="a") != user_ref(USER, pepper="b")


def test_resolve_user_ref_matches_only_a_known_user() -> None:
    ref = user_ref(USER, pepper="p")
    assert resolve_user_ref(ref, [USER, "x@vectorinc.co.jp"], pepper="p") == USER
    # 連携済みでない人の ref は解決しない（fail-closed）
    assert resolve_user_ref(ref, ["x@vectorinc.co.jp"], pepper="p") is None
    assert resolve_user_ref("こわれた", [USER], pepper="p") is None


def test_schedule_name_fits_eventbridge_limits() -> None:
    name = digest_schedule_name(user_ref(USER, pepper="p"), "20260911")
    assert name.startswith("digest-")
    assert len(name) <= 64
    assert all(c.isalnum() or c in "-_." for c in name)


# ── _process_user の claim（二重配信の防止）───────────────────────────
class _StubSkill:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, _input: Any, ctx: Any) -> MorningDigestOutput:
        self.calls.append(ctx.metadata["user_email"])
        return MorningDigestOutput(user_email_masked="k***@vectorinc.co.jp")


class _Store:
    def __init__(self, *, claimable: bool = True) -> None:
        self.claimable = claimable
        self.claims: list[tuple[str, _dt.date, str]] = []
        self.releases: list[str] = []

    def claim(self, email: str, day: _dt.date, *, origin: str, request_id: str) -> bool:
        self.claims.append((email, day, origin))
        return self.claimable

    def release(self, email: str, day: _dt.date, *, request_id: str) -> bool:
        self.releases.append(email)
        return True


def _patch_delivery(monkeypatch: pytest.MonkeyPatch, *, ok: bool) -> list[str]:
    sent: list[str] = []

    async def _fake_deliver(email: str, text: str, blocks: Any) -> tuple[bool, str | None]:
        if ok:
            sent.append(email)
            return (True, "D001")
        return (False, None)

    monkeypatch.setattr(mod, "_deliver_to_slack", _fake_deliver)
    return sent


def test_already_sent_user_is_skipped_without_running_the_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claim が取れなければ skill も配信も走らない（＝2 通目が出ない）。

    変異: ``_process_user`` の claim 分岐を外すと 2 通目が配信されて赤。
    """
    sent = _patch_delivery(monkeypatch, ok=True)
    skill = _StubSkill()
    store = _Store(claimable=False)
    result = mod._process_user(skill, object(), USER, store=store, day=DAY, origin="bulk")
    assert result == "skipped"
    assert skill.calls == []
    assert sent == []


def test_claimed_user_is_delivered_once(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _patch_delivery(monkeypatch, ok=True)
    store = _Store(claimable=True)
    result = mod._process_user(
        _StubSkill(), object(), USER, store=store, day=DAY, origin="scheduled"
    )
    assert result == "delivered"
    assert sent == [USER]
    assert store.claims == [(USER, DAY, "scheduled")]
    assert store.releases == []


def test_failed_delivery_releases_the_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slack が受け付けなければ印を戻す（一括実行が拾い直せる）。"""
    _patch_delivery(monkeypatch, ok=False)
    store = _Store(claimable=True)
    result = mod._process_user(
        _StubSkill(), object(), USER, store=store, day=DAY, origin="scheduled"
    )
    assert result == "error"
    assert store.releases == [USER]


def test_without_store_behaviour_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定 OFF（store=None）なら claim を 1 度も呼ばず現行動作のまま。"""
    sent = _patch_delivery(monkeypatch, ok=True)
    result = mod._process_user(_StubSkill(), object(), USER, day=DAY)
    assert result == "delivered"
    assert sent == [USER]


def test_delivery_store_is_none_when_feature_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MORNING_DIGEST_PERSONALIZED", raising=False)
    assert mod._delivery_store() is None


# ── planner ───────────────────────────────────────────────────────────
class _Ev:
    def __init__(self, start: str, all_day: bool = False) -> None:
        self.start = start
        self.end = start
        self.all_day = all_day


class _Cal:
    def __init__(self, events: list[_Ev]) -> None:
        self.events = events
        self.kwargs: list[dict[str, Any]] = []

    def list_events(self, request_id: str, **kw: Any) -> list[_Ev]:
        self.kwargs.append(kw)
        return self.events


def test_planner_ignores_all_day_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """終日予定だけの日は「予定なし」扱い＝既定時刻。

    変異: all_day のフィルタを外すと 00:00 起点で 06:00 に張り付き赤。
    """
    monkeypatch.setenv("MORNING_DIGEST_DEFAULT_TIME", "09:30")
    cal = _Cal([_Ev("2026-09-11", all_day=True)])
    plan = mod._plan_send_time(cal, DAY, "r")
    assert plan.no_timed_event is True
    assert plan.fire_at.hour == 9 and plan.fire_at.minute == 30


def test_planner_uses_first_timed_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MORNING_DIGEST_DEFAULT_TIME", "09:30")
    cal = _Cal(
        [
            _Ev("2026-09-11", all_day=True),
            _Ev("2026-09-11T08:00:00+09:00"),
            _Ev("2026-09-11T14:00:00+09:00"),
        ]
    )
    plan = mod._plan_send_time(cal, DAY, "r")
    assert (plan.fire_at.hour, plan.fire_at.minute) == (7, 0)


def test_planner_reads_all_events_not_just_twenty(monkeypatch: pytest.MonkeyPatch) -> None:
    """予定 25 件の日でも最終予定まで窓判定される（max_results 引き上げの証明）。

    変異: ``max_results=100`` を 20 に戻すと、Google 側が 20 件で打ち切るため
    21 件目以降が見えず、最初の予定の判定材料が欠ける。ここでは呼び出し引数で固定する。
    """
    cal = _Cal([_Ev(f"2026-09-11T{h:02d}:00:00+09:00") for h in range(9, 20)])
    mod._plan_send_time(cal, DAY, "r")
    assert cal.kwargs[0]["max_results"] == 100


def test_planner_creates_one_idempotent_reservation_per_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, Any]] = []

    class _Sched:
        def schedule_digest(self, **kw: Any) -> bool:
            created.append(kw)
            return True

    monkeypatch.setenv("MORNING_DIGEST_PERSONALIZED", "true")
    monkeypatch.setenv("MORNING_DIGEST_DEFAULT_TIME", "09:30")
    monkeypatch.setenv("DIGEST_USER_REF_PEPPER", "p")
    monkeypatch.setattr(mod, "_build_token_store", lambda: object())
    monkeypatch.setattr(
        mod, "_read_only_calendar", lambda store, email: _Cal([_Ev("2026-09-11T10:00:00+09:00")])
    )
    import teamagent.adapters.scheduler_client as sched_mod

    monkeypatch.setattr(sched_mod.SchedulerClient, "from_env", classmethod(lambda cls: _Sched()))

    assert mod.run_planner([USER]) == 0
    assert len(created) == 1
    payload = created[0]
    # ⚠️ ペイロードにメールアドレス・社名・channel・本文が載っていないこと。
    assert payload["user_ref"] == user_ref(USER, pepper="p")
    assert "@" not in payload["user_ref"]
    assert set(payload) == {"name", "user_ref", "date_iso", "fire_at", "request_id"}
    assert payload["name"] == digest_schedule_name(user_ref(USER, pepper="p"), "20260911")


def test_planner_does_nothing_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定 OFF: 予約を 1 件も作らない（env 未設定なら 1 バイトも変わらない）。"""
    monkeypatch.delenv("MORNING_DIGEST_PERSONALIZED", raising=False)
    called: list[str] = []
    monkeypatch.setattr(mod, "_build_token_store", lambda: called.append("token"))
    assert mod.run_planner([USER]) == 0
    assert called == []


def test_planner_skips_users_without_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    """カレンダー未連携の人は予約を作らず、既定時刻の一括実行に残る（現行動作の維持）。"""
    created: list[dict[str, Any]] = []

    class _Sched:
        def schedule_digest(self, **kw: Any) -> bool:
            created.append(kw)
            return True

    monkeypatch.setenv("MORNING_DIGEST_PERSONALIZED", "true")
    monkeypatch.setattr(mod, "_build_token_store", lambda: object())
    monkeypatch.setattr(mod, "_read_only_calendar", lambda store, email: None)
    import teamagent.adapters.scheduler_client as sched_mod

    monkeypatch.setattr(sched_mod.SchedulerClient, "from_env", classmethod(lambda cls: _Sched()))
    assert mod.run_planner([USER]) == 0
    assert created == []


# ── モード判定 ────────────────────────────────────────────────────────
def test_mode_defaults_to_bulk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_morning_digest_fargate.py"])
    monkeypatch.delenv("MORNING_DIGEST_MODE", raising=False)
    monkeypatch.delenv("MORNING_DIGEST_USER_REF", raising=False)
    assert mod._mode() == "bulk"


def test_mode_planner_from_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["x", "--mode=planner"])
    assert mod._mode() == "planner"


def test_mode_single_from_user_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["x"])
    monkeypatch.delenv("MORNING_DIGEST_MODE", raising=False)
    monkeypatch.setenv("MORNING_DIGEST_USER_REF", "a" * 32)
    assert mod._mode() == "single"


def test_unresolvable_user_ref_delivers_to_nobody(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scheduler 由来の値を無検証で信用しない（解決できなければ配信しない）。"""
    monkeypatch.setenv("DIGEST_USER_REF_PEPPER", "p")
    monkeypatch.setenv("MORNING_DIGEST_USER_REF", "f" * 32)
    assert mod._resolve_single_user([USER]) is None


def test_resolved_user_ref_returns_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGEST_USER_REF_PEPPER", "p")
    monkeypatch.setenv("MORNING_DIGEST_USER_REF", user_ref(USER, pepper="p"))
    assert mod._resolve_single_user([USER, "x@vectorinc.co.jp"]) == USER


# ── 事例ブリーフ節の描画（compact / legacy）──────────────────────────
def _digest_with_brief(**kw: Any) -> MorningDigestOutput:
    brief = PreMeetingBriefOutput(
        scanned=True,
        corpus_available=True,
        date=DAY.isoformat(),
        external_count=1,
        items=[
            PreMeetingBriefItem(
                start_at="2026-09-11T14:00:00+09:00",
                end_at="2026-09-11T15:00:00+09:00",
                title_display="【社外】電通吉田様",
                clients_display=["富士急"],
                cases=[CaseRef(company_display="ジャングリア沖縄", owner_display="清水")],
            )
        ],
        **kw,
    )
    return MorningDigestOutput(
        user_email_masked="k***@vectorinc.co.jp",
        calendar_date=DAY.isoformat(),
        pre_meeting_brief=brief,
        brief_scanned=True,
    )


@pytest.mark.parametrize("compact", [True, False])
def test_brief_section_is_rendered_in_both_layouts(
    compact: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compact と legacy の両方に節が出る（compact=false を置き去りにしない）。"""
    monkeypatch.setenv("MORNING_DIGEST_COMPACT", "true" if compact else "false")
    digest = _digest_with_brief()
    fmt = mod._format_block_kit_compact if compact else mod._format_block_kit
    _text, blocks = fmt(digest, USER)
    dumped = str(blocks)
    assert "アポ前 事例ブリーフィング" in dumped
    assert "ジャングリア沖縄" in dumped


@pytest.mark.parametrize("compact", [True, False])
def test_corpus_missing_removes_the_whole_section(
    compact: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事例集が未取込なら節そのものが出ず、📅/📧 は通常配信される。"""
    monkeypatch.setenv("MORNING_DIGEST_COMPACT", "true" if compact else "false")
    digest = _digest_with_brief()
    digest.pre_meeting_brief.corpus_available = False
    fmt = mod._format_block_kit_compact if compact else mod._format_block_kit
    _text, blocks = fmt(digest, USER)
    dumped = str(blocks)
    assert "アポ前 事例ブリーフィング" not in dumped
    assert "スキップ" not in dumped
    assert "の予定" in dumped  # 📅 節は出ている


def test_no_brief_object_renders_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定 OFF（skill を呼んでいない）なら pre_meeting_brief は None＝節が無い。"""
    monkeypatch.setenv("MORNING_DIGEST_COMPACT", "true")
    digest = MorningDigestOutput(user_email_masked="k***@x.co.jp", calendar_date=DAY.isoformat())
    _text, blocks = mod._format_block_kit_compact(digest, USER)
    assert "アポ前 事例ブリーフィング" not in str(blocks)
