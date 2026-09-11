"""日次上限・費用 cap と、上限に当たったときの「断らない」文言。

本番の失敗モードを再現する:
- 同じ人が 1 日に 3 本投げる（1 人 2 本/日）
- 全体が埋まる
- ジョブが失敗する（＝カウンタは戻らない）
- JST の日付が変わる（UTC 日付で切ると朝 9 時にリセットされる事故）
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from teamagent.skills.clip_proposal.limits import (
    DEFAULT_GLOBAL_PER_DAY,
    DEFAULT_PER_USER_PER_DAY,
    CostLedger,
    DailyQuota,
    build_busy_message,
    build_deferred_message,
    build_queued_message,
    configured_cost_cap_usd,
    configured_daily_cost_cap_usd,
    jst_date_key,
    next_reset_label,
)

_REFUSAL_WORDS = ("できません", "対応できません", "お受けできません", "ご自身で")


def test_initial_values_match_the_agreed_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIP_PROPOSAL_MAX_JOBS_PER_DAY", raising=False)
    monkeypatch.delenv("CLIP_COST_CAP_USD", raising=False)
    monkeypatch.delenv("CLIP_DAILY_COST_CAP_USD", raising=False)
    assert DEFAULT_PER_USER_PER_DAY == 2
    assert DEFAULT_GLOBAL_PER_DAY == 20
    assert configured_cost_cap_usd() == 1.0
    # 全体の日次 cap は未設定なら 1 依頼 cap × 全体本数で導出する。
    assert configured_daily_cost_cap_usd() == 20.0


def test_per_user_daily_limit_defers_the_third_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIP_PROPOSAL_MAX_JOBS_PER_DAY", raising=False)
    quota = DailyQuota()
    assert quota.try_reserve("user-a").accepted
    assert quota.try_reserve("user-a").accepted
    third = quota.try_reserve("user-a")
    assert not third.accepted
    assert third.reason == "per_user"
    # 別の人はまだ受けられる（全体枠を食い潰していない）。
    assert quota.try_reserve("user-b").accepted


def test_global_daily_limit_defers_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIP_PROPOSAL_MAX_JOBS_PER_DAY", "50")
    monkeypatch.setenv("CLIP_PROPOSAL_MAX_JOBS_PER_DAY_TOTAL", "2")
    quota = DailyQuota()
    assert quota.try_reserve("user-a").accepted
    assert quota.try_reserve("user-a").accepted
    decision = quota.try_reserve("user-b")
    assert not decision.accepted
    assert decision.reason == "global"


def test_counter_is_not_refunded_when_the_job_fails() -> None:
    """失敗させ続ければ無限に課金できる経路を作らない（戻す API を持たない）。"""

    quota = DailyQuota()
    quota.try_reserve("user-a")
    assert not hasattr(quota, "release")
    assert not hasattr(quota, "refund")
    used_user, used_total, _cost = quota.snapshot("user-a")
    assert (used_user, used_total) == (1, 1)


def test_counters_reset_on_the_jst_calendar_day() -> None:
    quota = DailyQuota()
    # 2026-09-11 23:00 UTC = 2026-09-12 08:00 JST
    before = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)  # JST 2026-09-11 23:00
    after = datetime(2026, 9, 11, 16, 0, tzinfo=UTC)  # JST 2026-09-12 01:00
    quota.try_reserve("user-a", now=before)
    quota.try_reserve("user-a", now=before)
    assert not quota.try_reserve("user-a", now=before).accepted
    assert quota.try_reserve("user-a", now=after).accepted


def test_jst_date_key_does_not_roll_at_utc_midnight() -> None:
    assert jst_date_key(datetime(2026, 9, 11, 23, 0, tzinfo=UTC)) == "20260912"
    assert jst_date_key(datetime(2026, 9, 11, 14, 0, tzinfo=UTC)) == "20260911"


def test_daily_cost_cap_defers_further_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIP_DAILY_COST_CAP_USD", "0.5")
    quota = DailyQuota()
    assert quota.try_reserve("user-a").accepted
    quota.add_cost(0.9)
    decision = quota.try_reserve("user-b")
    assert not decision.accepted
    assert decision.reason == "daily_cost"


# ---------------------------------------------------------------------------
# 文言（「できません」で終わらせない）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["per_user", "global", "daily_cost"])
def test_deferred_message_offers_a_next_slot_and_a_contact(reason: str) -> None:
    now = datetime(2026, 9, 11, 3, 0, tzinfo=UTC)  # JST 12:00
    message = build_deferred_message(reason, now=now)
    assert "9/12 0:00（JST）" in message
    assert "小俣さん" in message
    assert not any(word in message for word in _REFUSAL_WORDS)


def test_busy_message_does_not_ask_for_a_resubmission() -> None:
    message = build_busy_message(position=2, wait_minutes=30)
    assert "2 番目" in message
    assert "自動で始めます" in message
    assert "もう一度送っていただく必要はありません" in message
    assert not any(word in message for word in _REFUSAL_WORDS)


def test_queued_message_echoes_the_client_name_back() -> None:
    message = build_queued_message(client_name="初田製作所", eta_minutes=15)
    assert "「初田製作所」" in message
    assert "違っていればこのスレッドで教えてください" in message


def test_queued_message_admits_an_unknown_client_without_refusing() -> None:
    message = build_queued_message(client_name="", eta_minutes=15)
    assert "分かり次第反映します" in message
    assert not any(word in message for word in _REFUSAL_WORDS)


def test_next_reset_label_is_tomorrow_in_jst() -> None:
    assert next_reset_label(datetime(2026, 12, 31, 3, 0, tzinfo=UTC)) == "1/1 0:00（JST）"


# ---------------------------------------------------------------------------
# CostLedger（cap の分母は実測累計・リトライ分も足す）
# ---------------------------------------------------------------------------


def test_cost_ledger_accumulates_retries() -> None:
    ledger = CostLedger(cap_usd=1.0)
    for _ in range(3):  # 1 コールにつき最大 3 回のリトライが走りうる
        ledger = ledger.add(0.4)
    assert ledger.calls == 3
    assert ledger.spent_usd == pytest.approx(1.2)
    assert ledger.exhausted
