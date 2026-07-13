"""取り込み鮮度監視（freshness）の単体テスト。

2026-07-13 の「Slack が 6 週間サイレント停止」の再発防止機能。
find_stale_sources（純関数）と IngestOpsAlerter.send_freshness_warning を固定する。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from teamagent.ingest.freshness import (
    find_stale_sources,
    max_age_days_from_env,
)
from teamagent.ingest.ops_alert import IngestOpsAlerter


class _FakeCursor:
    """execute/fetchall だけを持つ DBAPI カーソルのダブル。"""

    def __init__(self, rows: list[tuple[str, dt.datetime | None]]) -> None:
        self._rows = rows

    def execute(self, sql: str) -> None:
        assert "max(ingested_at)" in sql and "documents" in sql

    def fetchall(self) -> list[tuple[str, dt.datetime | None]]:
        return self._rows


_NOW = dt.datetime(2026, 7, 13, tzinfo=dt.UTC)


def test_stale_source_detected() -> None:
    # slack は 46 日前（stale）、gdrive/gsheets は当日（fresh）
    rows = [
        ("slack", dt.datetime(2026, 5, 28, tzinfo=dt.UTC)),
        ("gdrive", dt.datetime(2026, 7, 12, tzinfo=dt.UTC)),
        ("gsheets", dt.datetime(2026, 7, 13, tzinfo=dt.UTC)),
    ]
    stale = find_stale_sources(_FakeCursor(rows), now=_NOW, max_age_days=8)
    assert [s.source_type for s in stale] == ["slack"]
    assert stale[0].age_days is not None and stale[0].age_days > 40
    assert "経過" in stale[0].reason


def test_missing_source_is_stale() -> None:
    # slack が documents に1件も無い → 未取り込みとして stale
    rows = [
        ("gdrive", dt.datetime(2026, 7, 12, tzinfo=dt.UTC)),
        ("gsheets", dt.datetime(2026, 7, 12, tzinfo=dt.UTC)),
    ]
    stale = find_stale_sources(_FakeCursor(rows), now=_NOW, max_age_days=8)
    assert [s.source_type for s in stale] == ["slack"]
    assert stale[0].newest is None and stale[0].age_days is None
    assert "1件も" in stale[0].reason


def test_all_fresh_returns_empty() -> None:
    rows = [
        ("slack", dt.datetime(2026, 7, 10, tzinfo=dt.UTC)),
        ("gdrive", dt.datetime(2026, 7, 12, tzinfo=dt.UTC)),
        ("gsheets", dt.datetime(2026, 7, 13, tzinfo=dt.UTC)),
    ]
    assert find_stale_sources(_FakeCursor(rows), now=_NOW, max_age_days=8) == []


def test_naive_timestamp_treated_as_utc() -> None:
    rows = [
        ("slack", dt.datetime(2026, 5, 28)),  # tz-naive
        ("gdrive", dt.datetime(2026, 7, 12, tzinfo=dt.UTC)),
        ("gsheets", dt.datetime(2026, 7, 12, tzinfo=dt.UTC)),
    ]
    stale = find_stale_sources(_FakeCursor(rows), now=_NOW, max_age_days=8)
    assert [s.source_type for s in stale] == ["slack"]


def test_max_age_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INGEST_FRESHNESS_MAX_AGE_DAYS", raising=False)
    assert max_age_days_from_env() == 8
    monkeypatch.setenv("INGEST_FRESHNESS_MAX_AGE_DAYS", "3")
    assert max_age_days_from_env() == 3
    monkeypatch.setenv("INGEST_FRESHNESS_MAX_AGE_DAYS", "bogus")
    assert max_age_days_from_env() == 8  # 不正は既定へ


# ---- alerter.send_freshness_warning ----


class _StaleStub:
    def __init__(self, src: str, reason: str) -> None:
        self.source_type = src
        self.reason = reason


def test_alert_noop_without_webhook() -> None:
    alerter = IngestOpsAlerter(webhook_url=None)
    assert alerter.send_freshness_warning(stale=[_StaleStub("slack", "x")], request_id="r") is False


def test_alert_noop_when_empty() -> None:
    alerter = IngestOpsAlerter(webhook_url="https://hooks.example/x")
    assert alerter.send_freshness_warning(stale=[], request_id="r") is False


def test_alert_noop_on_dry_run() -> None:
    alerter = IngestOpsAlerter(webhook_url="https://hooks.example/x")
    got = alerter.send_freshness_warning(
        stale=[_StaleStub("slack", "x")], request_id="r", dry_run=True
    )
    assert got is False


def test_alert_posts_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: dict[str, Any] = {}

    class _Resp:
        status_code = 200

    def _fake_post(url: str, json: dict[str, Any], timeout: float) -> _Resp:
        posted["url"] = url
        posted["json"] = json
        return _Resp()

    import teamagent.ingest.ops_alert as oa

    monkeypatch.setattr(oa.httpx, "post", _fake_post)
    alerter = IngestOpsAlerter(webhook_url="https://hooks.example/x")
    ok = alerter.send_freshness_warning(
        stale=[_StaleStub("slack", "最終取り込みから 46 日経過")], request_id="r"
    )
    assert ok is True
    assert "slack" in posted["json"]["text"]
    assert "検索に載っていない" in str(posted["json"]["blocks"])
