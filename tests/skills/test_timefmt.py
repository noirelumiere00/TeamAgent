"""_shared/timefmt: メール時刻の JST 決定論フォーマットの検証。

2026-08-14 UTC表示バグ根治（本番実測: 8/13(木)19:00 JST 受信 → 「8/13（火）10:00」誤表示）。
実測値 2026-08-13T10:00:27Z = epoch ms 1786615227000 を正とする。
"""

from __future__ import annotations

from teamagent.skills._shared.timefmt import jst_display_or_none, jst_iso_or_none

# e-TimeCard 実メールの internalDate（2026-08-13T10:00:27Z）
_REAL_MS = 1786615227000


def test_iso_is_jst_with_offset() -> None:
    assert jst_iso_or_none(_REAL_MS) == "2026-08-13T19:00:27+09:00"


def test_display_has_jst_time_and_japanese_weekday() -> None:
    # 2026-08-13 は木曜。UTC のままだと 10:00・曜日誤りになる（バグの再発検知）。
    assert jst_display_or_none(_REAL_MS) == "08/13(木) 19:00"


def test_none_only_is_unknown_zero_epoch_is_valid() -> None:
    assert jst_iso_or_none(None) is None
    assert jst_display_or_none(None) is None
    # 0 = 1970-01-01T00:00Z は有効な epoch（JST では 09:00）
    assert jst_iso_or_none(0) == "1970-01-01T09:00:00+09:00"
    assert jst_display_or_none(0) == "01/01(木) 09:00"


def test_midnight_boundary_date_shifts_to_jst_day() -> None:
    # UTC 15:30 = JST 翌日 00:30（[:10] 日付切出しのズレ根治の確認）
    ms = 1786635000000  # 2026-08-13T15:30:00Z = 2026-08-14T00:30 JST（金）
    assert jst_iso_or_none(ms) == "2026-08-14T00:30:00+09:00"
    assert jst_display_or_none(ms) == "08/14(金) 00:30"
