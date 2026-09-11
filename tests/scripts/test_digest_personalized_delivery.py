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


def test_planner_does_not_reserve_when_the_time_is_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既定時刻のままの人は予約を作らない＝一括実行に残す（DELTA §1-3）。

    予約を作ると (a) 一括配信が走らない土曜にも DM が出る (b) 平日は bulk と同時刻に
    1 人 1 タスクの Fargate が余分に立つ、が同時に起きる。

    変異: ``run_planner`` の ``no_timed_event or clamped_to_default`` 分岐を外すと
    予約が 2 件作られて赤。
    """
    created: list[dict[str, Any]] = []

    class _Sched:
        def schedule_digest(self, **kw: Any) -> bool:
            created.append(kw)
            return True

    monkeypatch.setenv("MORNING_DIGEST_PERSONALIZED", "true")
    monkeypatch.setenv("MORNING_DIGEST_DEFAULT_TIME", "09:30")
    monkeypatch.setenv("DIGEST_USER_REF_PEPPER", "p")
    monkeypatch.setattr(mod, "_build_token_store", lambda: object())
    import teamagent.adapters.scheduler_client as sched_mod

    monkeypatch.setattr(sched_mod.SchedulerClient, "from_env", classmethod(lambda cls: _Sched()))

    cals = {
        # 時刻つき予定なし（終日のみ）→ 既定時刻
        "a@vectorinc.co.jp": _Cal([_Ev("2026-09-11", all_day=True)]),
        # 最初の予定が 15:00 → 14:00 は上限を超える（＝既定時刻のまま）
        "b@vectorinc.co.jp": _Cal([_Ev("2026-09-11T15:00:00+09:00")]),
        # 最初の予定が 10:00 → 09:00（＝個人別配信の対象）
        USER: _Cal([_Ev("2026-09-11T10:00:00+09:00")]),
    }
    monkeypatch.setattr(mod, "_read_only_calendar", lambda store, email: cals[email])

    assert mod.run_planner(["a@vectorinc.co.jp", "b@vectorinc.co.jp", USER]) == 0
    assert [c["name"] for c in created] == [
        digest_schedule_name(user_ref(USER, pepper="p"), "20260911")
    ]


def test_planner_cron_matches_the_bulk_schedule_weekdays() -> None:
    """planner の既定 cron は一括配信と同じ稼働日（＝土日に個人別配信だけが出ない）。

    変異: tf の既定を ``cron(0 19 * * ? *)``（毎日）へ戻すと赤。
    """
    tf = (PROJECT_ROOT / "infra" / "terraform" / "morning_digest_schedule.tf").read_text(
        encoding="utf-8"
    )
    assert 'default     = "cron(0 19 ? * SUN-THU *)"' in tf
    assert 'default     = "cron(30 0 ? * MON-FRI *)"' in tf


# ── カレンダー未連携者への週 1 回のお知らせ（PLAN §2-1）──────────────
def test_unlinked_user_is_notified_on_monday_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """未連携の人が「自分は対象外」だと永久に気づけない状態を作らない。

    変異: ``run_planner`` の ``_notify_calendar_unlinked`` 呼び出しを外すと赤。
    """
    monkeypatch.setenv("MORNING_DIGEST_BRIEF", "true")
    sent = _patch_delivery(monkeypatch, ok=True)
    monday = _dt.date(2026, 9, 14)
    assert monday.weekday() == 0
    assert mod._notify_calendar_unlinked(USER, monday) is True
    assert sent == [USER]


def test_unlinked_user_is_not_notified_on_other_days(monkeypatch: pytest.MonkeyPatch) -> None:
    """毎日は送らない（週 1 回）。

    変異: ``_is_weekly_notice_day`` を ``True`` 固定にすると赤。
    """
    monkeypatch.setenv("MORNING_DIGEST_BRIEF", "true")
    sent = _patch_delivery(monkeypatch, ok=True)
    friday = _dt.date(2026, 9, 11)
    assert friday.weekday() == 4
    assert mod._notify_calendar_unlinked(USER, friday) is False
    assert sent == []


def test_unlinked_notice_is_silent_when_brief_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定 OFF: ブリーフが点いていない環境では 1 通も出さない。"""
    monkeypatch.delenv("MORNING_DIGEST_BRIEF", raising=False)
    sent = _patch_delivery(monkeypatch, ok=True)
    assert mod._notify_calendar_unlinked(USER, _dt.date(2026, 9, 14)) is False
    assert sent == []


def test_unlinked_notice_never_contains_a_raw_auth_url() -> None:
    """認可 URL は文面に貼らない（長い URL の再タイプ事故の実績がある）。

    代わりに「この DM で『連携』」＝ Aico が続きを引き受ける導線にする。
    """
    assert "http" not in mod.CALENDAR_UNLINKED_LINE
    assert "連携" in mod.CALENDAR_UNLINKED_LINE


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
                title_display="【社外】青葉広告山田様",
                clients_display=["北都リゾート"],
                cases=[CaseRef(company_display="南島リゾートパーク", owner_display="田中")],
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
    assert "南島リゾートパーク" in dumped


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


# ── 下限に張り付いた回の 1 行（ペイロードにフラグを載せない証明）────────
def _digest_with_first_event(start_iso: str) -> MorningDigestOutput:
    from teamagent.skills.morning_digest.schema import CalendarEventItem

    digest = _digest_with_brief()
    digest.calendar_events = [
        CalendarEventItem(summary_display="朝会", start_at=start_iso, end_at=start_iso)
    ]
    return digest


def test_early_notice_appears_only_on_a_floor_clamped_scheduled_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """06:30 の予定 → 06:00 送信（下限に張り付き）→ 冒頭に 1 行。

    ⚠️ 予約ペイロードにフラグは載っていない。発火側が planner と同じ純関数で
    計算し直している。変異: 計算を落として常に False にすると赤。
    """
    monkeypatch.setattr(sys, "argv", ["x"])
    monkeypatch.setenv("MORNING_DIGEST_MODE", "single")
    monkeypatch.setenv("MORNING_DIGEST_DEFAULT_TIME", "09:30")
    monkeypatch.setenv("MORNING_DIGEST_COMPACT", "true")
    digest = _digest_with_first_event("2026-09-11T06:30:00+09:00")
    _text, blocks = mod._format_block_kit_compact(digest, USER)
    assert "最初の予定が近いため" in str(blocks)


def test_early_notice_absent_on_a_normal_scheduled_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["x"])
    monkeypatch.setenv("MORNING_DIGEST_MODE", "single")
    monkeypatch.setenv("MORNING_DIGEST_DEFAULT_TIME", "09:30")
    monkeypatch.setenv("MORNING_DIGEST_COMPACT", "true")
    digest = _digest_with_first_event("2026-09-11T10:00:00+09:00")
    _text, blocks = mod._format_block_kit_compact(digest, USER)
    assert "最初の予定が近いため" not in str(blocks)


def test_early_notice_never_on_the_bulk_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定時刻の一括実行は通常どおりの時刻で届いている＝1 行を添えない。"""
    monkeypatch.setattr(sys, "argv", ["x"])
    monkeypatch.setenv("MORNING_DIGEST_MODE", "bulk")
    monkeypatch.setenv("MORNING_DIGEST_COMPACT", "true")
    digest = _digest_with_first_event("2026-09-11T06:30:00+09:00")
    _text, blocks = mod._format_block_kit_compact(digest, USER)
    assert "最初の予定が近いため" not in str(blocks)


# ── pepper は平文 env ではなく Secrets Manager 経由 ──────────────────
def _schedule_tf() -> str:
    return (PROJECT_ROOT / "infra" / "terraform" / "morning_digest_schedule.tf").read_text(
        encoding="utf-8"
    )


def test_pepper_is_injected_as_a_secret_not_a_plain_env() -> None:
    """pepper が taskdef の environment に平文で入っていたら脅威モデルが成立しない。

    pepper が守る相手は「Scheduler/SQS のペイロードを読める者」だが、その主体は
    同一 AWS アカウントで ecs:DescribeTaskDefinition も持つのが普通で、environment に
    置くと pepper ごと読めて総当たりが依然成立する。terraform state / tfvars にも
    平文が残る（CLAUDE.md「シークレットは設定ファイルに平文で書かない」）。

    変異: environment へ ``{ name = "DIGEST_USER_REF_PEPPER", value = ... }`` を戻すと赤。
    """
    tf = _schedule_tf()
    assert '{ name = "DIGEST_USER_REF_PEPPER", value =' not in tf
    assert 'name = "DIGEST_USER_REF_PEPPER", valueFrom = s.arn' in tf
    assert "local.digest_user_ref_pepper_secrets" in tf
    # 値そのものを受け取る変数を残さない（tfvars に平文で書けてしまうため）。
    assert 'variable "digest_user_ref_pepper" {' not in tf
    assert 'variable "digest_user_ref_pepper_secret_name" {' in tf


def test_pepper_secret_is_readable_by_the_execution_role() -> None:
    """secrets(valueFrom) は実行ロールに GetSecretValue が無いと起動時に落ちる。"""
    tf = _schedule_tf()
    assert "local.digest_user_ref_pepper_iam_arns" in tf


def test_runtime_guard_lists_the_pepper_as_a_secret() -> None:
    """guard の allowlist も env ではなく secrets 側へ移す。

    変異: allowed_env 側へ戻すと（secrets の差分が許可されず）guard が落ちる構図を
    ここで固定する。
    """
    guard = (PROJECT_ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh").read_text(
        encoding="utf-8"
    )
    line = next(
        ln
        for ln in guard.splitlines()
        if ln.strip().startswith("'aws_ecs_task_definition.morning_digest[0]|morning|")
    )
    fields = line.split("|")
    allowed_env, allowed_secrets = fields[3], fields[4]
    assert "DIGEST_USER_REF_PEPPER" not in allowed_env
    assert "DIGEST_USER_REF_PEPPER" in allowed_secrets
