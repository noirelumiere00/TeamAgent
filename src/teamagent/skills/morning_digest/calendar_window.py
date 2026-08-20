"""朝ダイジェストのカレンダー日付ロジック（JST 固定・skill と runner の唯一の真実源）。

2026-08-20 の本番事象（翌日 8/21 の終日予定が「今日の予定」に出た）の根治用モジュール。

真因は 2 つで、どちらも「日付」を誰も持っていなかったことに帰着する:
  1. 取得窓が `datetime.now(UTC)` 起点の移動 24 時間だった。9:30 JST に走ると窓は
     [8/20 09:30 JST, 8/21 09:30 JST) となり、8/21 の終日予定（JST 8/21 00:00 開始）が
     正当に窓へ入る。同時に「今日の 00:00〜09:30 に始まる予定」が窓の手前に落ちていた。
  2. 下流に日付フィルタも日付表示も無く、見出しが「今日の予定」決め打ちだった。

本モジュールは以下を 1 箇所に集約する:
  - 窓は **JST の暦日 00:00 起点**（`jst_day_window`）
  - 終日予定は **date 値をそのまま JST の暦日**として扱う（datetime へ変換して astimezone
    しない＝前日/翌日に寄せない）。Google の `end.date` は **排他的**（8/21 のみの終日は
    end=8/22）なので、最終日は `end - 1 日`
  - 表示ラベルは「8/21(金) 終日」「10:30–11:00」のように **日付を明示**できる形

⚠️ naive datetime に依存しないこと。offset の無い入力は JST とみなして明示的に付与する
（コンテナのローカル TZ は UTC なので、naive のまま astimezone すると 9 時間ずれる）。
"""

from __future__ import annotations

import datetime as _dt

JST = _dt.timezone(_dt.timedelta(hours=9), name="JST")

_WEEKDAY_JA = ("月", "火", "水", "木", "金", "土", "日")


def now_jst() -> _dt.datetime:
    """現在時刻（JST・aware）。テストはこの関数を差し替えて「今日」を固定する。"""
    return _dt.datetime.now(JST)


def jst_day_window(now: _dt.datetime, horizon_hours: int) -> tuple[_dt.datetime, _dt.datetime]:
    """「今日」の取得窓 [JST 当日 00:00, +horizon_hours) を返す（既定 24h＝当日ぴったり）。

    ⚠️ 実行時刻起点ではなく **当日 00:00 起点**。これが今回の日付ずれの根治点。
    """
    base = now.astimezone(JST) if now.tzinfo is not None else now.replace(tzinfo=JST)
    start = base.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + _dt.timedelta(hours=max(1, int(horizon_hours)))


def is_all_day(start_at: str | None, all_day: bool | None = None) -> bool:
    """終日予定か。API 由来の明示フラグを最優先し、無ければ「T が無い＝date のみ」で判定。

    Google Calendar API v3 は終日を `start.date="2026-08-21"`（時刻なし）で返すが、
    経路によっては `"2026-08-21T00:00:00Z"` の形に整形されることがある。前者は文字列
    ヒューリスティクスで拾えるが後者は拾えないため、アダプタ側の `all_day` を優先する。
    """
    if all_day is not None:
        return bool(all_day)
    s = (start_at or "").strip()
    return bool(s) and "T" not in s


def parse_jst_date(value: str | None) -> _dt.date | None:
    """ "2026-08-21" / "2026-08-21T00:00:00Z" の先頭 10 字を暦日として読む（TZ 変換しない）。

    ⚠️ str 以外（LLM 由来の型崩れ等）は None。呼び出し元は描画/整形なので落とさない。
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if len(s) < 10:
        return None
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def parse_jst_datetime(value: str | None) -> _dt.datetime | None:
    """ISO 文字列を JST の aware datetime へ。offset 無しは JST とみなす（naive 禁止）。

    ⚠️ str 以外・パース不能は None（旧 `_fmt_meeting_button_time` の TypeError 握り潰しと同義）。
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if "T" not in s:
        d = parse_jst_date(s)
        return _dt.datetime.combine(d, _dt.time.min, tzinfo=JST) if d else None
    try:
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def event_bounds(
    start_at: str | None, end_at: str | None, all_day: bool | None = None
) -> tuple[_dt.datetime, _dt.datetime] | None:
    """予定の占有区間 [start, end) を JST aware で返す。解釈不能なら None（＝落とさない）。

    終日は `end.date` が排他的である Google 仕様をそのまま区間の上端に使う
    （8/21 のみの終日 → [8/21 00:00 JST, 8/22 00:00 JST)）。end 欠損/逆転は 1 日とみなす。
    """
    if is_all_day(start_at, all_day):
        sd = parse_jst_date(start_at)
        if sd is None:
            return None
        ed = parse_jst_date(end_at)
        if ed is None or ed <= sd:
            ed = sd + _dt.timedelta(days=1)
        return (
            _dt.datetime.combine(sd, _dt.time.min, tzinfo=JST),
            _dt.datetime.combine(ed, _dt.time.min, tzinfo=JST),
        )
    start = parse_jst_datetime(start_at)
    if start is None:
        return None
    end = parse_jst_datetime(end_at) or start
    return (start, max(start, end))


def overlaps_window(
    bounds: tuple[_dt.datetime, _dt.datetime],
    window_start: _dt.datetime,
    window_end: _dt.datetime,
) -> bool:
    """区間 [s, e) が窓 [window_start, window_end) と重なるか（境界は半開区間で判定）。"""
    start, end = bounds
    if end <= start:  # 長さ 0 の予定（開始＝終了）
        return window_start <= start < window_end
    return start < window_end and end > window_start


def weekday_ja(day: _dt.date) -> str:
    """曜日の 1 文字（"月"〜"日"）。日付表示も曜日照合もここを唯一の真実源にする。"""
    return _WEEKDAY_JA[day.weekday()]


def fmt_jst_date(day: _dt.date) -> str:
    """ "8/20(木)" 形式（見出し・行頭の日付明示用）。"""
    return f"{day.month}/{day.day}({weekday_ja(day)})"


def event_when_label(
    start_at: str | None,
    end_at: str | None,
    *,
    all_day: bool | None = None,
    target_date: _dt.date | None = None,
) -> str:
    """予定 1 件の時刻ラベル。`target_date` と違う日なら日付を明示する。

    例:
      当日の時刻付き           → "10:30–11:00"
      翌日の時刻付き           → "8/21(金) 10:30–11:00"
      当日の終日               → "終日"
      翌日の終日               → "8/21(金) 終日"
      複数日にまたがる終日     → "終日(8/19–8/21)"（end.date 排他を -1 日した最終日）
    """
    if is_all_day(start_at, all_day):
        bounds = event_bounds(start_at, end_at, all_day=True)
        if bounds is None:
            return "終日"
        first = bounds[0].date()
        last = bounds[1].date() - _dt.timedelta(days=1)  # end.date は排他的
        if last > first:
            return f"終日({first.month}/{first.day}–{last.month}/{last.day})"
        if target_date is not None and first != target_date:
            return f"{fmt_jst_date(first)} 終日"
        return "終日"

    start = parse_jst_datetime(start_at)
    if start is None:
        return ""
    prefix = ""
    if target_date is not None and start.date() != target_date:
        prefix = f"{fmt_jst_date(start.date())} "
    end = parse_jst_datetime(end_at)
    if end is None or end <= start:
        return f"{prefix}{start:%H:%M}"
    return f"{prefix}{start:%H:%M}–{end:%H:%M}"
