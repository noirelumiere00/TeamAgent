"""朝ダイジェストの本人ごとの設定（preferences.py）— 定義・判定・説明文・読み取りの倒し方。"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest

from teamagent.skills.morning_digest import preferences as prefs_mod

MON = _dt.date(2026, 10, 5)  # 月曜
TUE = MON + _dt.timedelta(days=1)


def test_default_is_the_current_behaviour() -> None:
    d = prefs_mod.DigestPreferences()
    assert d.is_default()
    assert all(d.shows(k) for k in prefs_mod.SECTION_KEYS)
    assert {k: d.limit(k) for k in prefs_mod.LIMIT_BOUNDS} == {
        "reply": 5,
        "unread": 5,
        "slack": 5,
        "calendar": 10,
    }
    assert d.skip_reason(MON) is None
    assert d.reminder_allowed("定例")
    assert d.to_storage() == {}  # 既定は 1 項目も書かない


def test_skip_reasons() -> None:
    assert prefs_mod.DigestPreferences(delivery=False).skip_reason(MON) == prefs_mod.SKIP_OFF
    paused = prefs_mod.DigestPreferences(paused_until=MON)
    assert paused.skip_reason(MON) == prefs_mod.SKIP_PAUSED  # 終わりの日も休み（含む）
    assert paused.skip_reason(TUE) is None
    only_mon = prefs_mod.DigestPreferences(weekdays=(0,))
    assert only_mon.skip_reason(MON) is None
    assert only_mon.skip_reason(TUE) == prefs_mod.SKIP_WEEKDAY


def test_reminder_keywords_match_width_and_case_insensitively() -> None:
    p = prefs_mod.DigestPreferences(reminder_skip_keywords=("タスク", "todo"))
    assert not p.reminder_allowed("10:30 タスク確認（公式LINE）")
    assert not p.reminder_allowed("ＴＯＤＯ整理")  # 全角・大文字でも一致
    assert p.reminder_allowed("クライアント定例")
    assert not prefs_mod.DigestPreferences(reminders=False).reminder_allowed("クライアント定例")


@pytest.mark.parametrize(
    "bad",
    [
        {"weekdays": []},
        {"weekdays": [5]},  # 土曜は配信日ではない
        {"hidden_sections": ["mail"]},
        {"limits": {"reply": 9}},
        {"limits": {"brief": 3}},
        {"reminder_lead_minutes": 0},
        {"reminder_skip_keywords": ["あ" * 21]},
        {"reminder_skip_keywords": ["a\u0000b"]},
        {"reminder_skip_keywords": [f"w{i}" for i in range(11)]},
    ],
)
def test_invalid_values_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        prefs_mod.from_storage(bad)


def test_storage_round_trip_and_normalisation() -> None:
    p = prefs_mod.from_storage(
        {
            "weekdays": [4, 0, 0],
            "hidden_sections": ["slack", "reply"],
            "limits": {"calendar": 10, "unread": 3},  # 既定値の 10 は捨てる
            "reminder_skip_keywords": ["タスク", "ﾀｽｸ", " 社内 "],
            "unknown_future_key": 1,  # 新しい版が書いた鍵は読み捨てる
        }
    )
    assert p.weekdays == (0, 4)
    assert p.hidden_sections == ("reply", "slack")  # 表示順に並べ直す
    assert p.limits == {"unread": 3}
    assert p.reminder_skip_keywords == ("タスク", "社内")  # 半角カナも同じ語として 1 つ
    assert prefs_mod.from_storage(p.to_storage()) == p


def test_from_storage_rejects_non_objects() -> None:
    with pytest.raises(ValueError):
        prefs_mod.from_storage(["delivery", False])


class _Reader:
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self.result, self.exc = result, exc

    def get(self, user_email: str, *, request_id: str) -> tuple[dict[str, Any] | None, int]:
        if self.exc is not None:
            raise self.exc
        return self.result


def test_load_falls_back_to_default_on_failure_or_broken_row() -> None:
    """読めない・壊れている → 既定（今までどおり配信）。止める側に倒さない。"""
    assert (
        prefs_mod.load_preferences(None, "a@x.jp", request_id="r") is prefs_mod.DEFAULT_PREFERENCES
    )
    boom = _Reader(exc=RuntimeError("relation digest_preferences does not exist"))
    assert prefs_mod.load_preferences(boom, "a@x.jp", request_id="r").is_default()
    assert prefs_mod.load_preferences(
        _Reader(({"weekdays": []}, 3)), "a@x.jp", request_id="r"
    ).is_default()
    assert prefs_mod.load_preferences(_Reader((None, 0)), "a@x.jp", request_id="r").is_default()
    got = prefs_mod.load_preferences(_Reader(({"delivery": False}, 2)), "a@x.jp", request_id="r")
    assert got.delivery is False


def test_describe_lists_every_item() -> None:
    p = prefs_mod.DigestPreferences(
        paused_until=TUE,
        weekdays=(0, 2),
        hidden_sections=("slack",),
        limits={"reply": 3},
        auto_drafts=False,
        reminder_lead_minutes=10,
        reminder_skip_keywords=("タスク",),
    )
    text = prefs_mod.describe(p, MON, default_lead_minutes=5)
    assert "10/6(火) まで休み" in text and "10/7(水) 以降の月・水曜に再開" in text
    assert "要返信メール（最大3件）" in text
    assert "載せない欄: Slack 返信漏れ" in text
    assert "自動では作りません" in text
    assert "開始 10 分前" in text and "「タスク」" in text


def test_describe_default_and_stopped() -> None:
    text = prefs_mod.describe(prefs_mod.DEFAULT_PREFERENCES, MON, default_lead_minutes=5)
    assert "平日（月〜金）の朝に届きます" in text
    assert "載せない欄: なし" in text and "開始 5 分前" in text
    stopped = prefs_mod.describe(
        prefs_mod.DigestPreferences(delivery=False, reminders=False), MON, default_lead_minutes=5
    )
    assert "止めています（「朝のサマリーを再開して」" in stopped
    assert "予定の直前リマインド: 止めています" in stopped
