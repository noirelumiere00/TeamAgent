from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "backfill_source_dates.py"
_SPEC = importlib.util.spec_from_file_location("backfill_source_dates", _PATH)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)

_NOW = dt.datetime(2026, 7, 17, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("external_id", "expected"),
    [
        ("C091ZSVTKF1:1783932899.654649", "2026-07-13T08:54:59.654649+00:00"),
        ("C0A1207GYHZ:1779101519", "2026-05-18T10:51:59+00:00"),
    ],
)
def test_slack_external_id_datetime(external_id: str, expected: str) -> None:
    parsed = _mod.slack_external_id_datetime(external_id, now=_NOW)
    assert parsed is not None
    assert parsed.isoformat() == expected


@pytest.mark.parametrize(
    "external_id",
    [
        "",
        "C123",
        "C123:not-a-number",
        "C123:1783932899.1234567",
        "C123:1783932899:extra",
        "C 123:1783932899",
        "C123:1234.1",  # 2010より前を含む短い/古いepochは拒否
        "C123:9999999999",  # 現在より未来
    ],
)
def test_slack_external_id_datetime_rejects_untrusted_shapes(external_id: str) -> None:
    assert _mod.slack_external_id_datetime(external_id, now=_NOW) is None


def test_sql_is_narrow_and_idempotent() -> None:
    for sql in (_mod._SELECT_NULL_SLACK, _mod._UPDATE_ONE, _mod._COUNT_REMAINING):
        assert "source_type = 'slack'" in sql
        assert "modified_at IS NULL" in sql
    assert "WHERE id = %s" in _mod._UPDATE_ONE


def test_main_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TEAMAGENT_DB_DSN", raising=False)
    with pytest.raises(SystemExit, match="DATABASE_URL"):
        _mod.main([])


def _fake_connection() -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    return conn


def test_dry_run_reports_counts_without_update(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _fake_connection()
    monkeypatch.setenv("DATABASE_URL", "postgresql://redacted")
    monkeypatch.setattr(_mod.psycopg, "connect", lambda _dsn: conn)
    monkeypatch.setattr(
        _mod,
        "_read_candidates",
        lambda _conn, *, lock: [
            ("id-1", "C1:1783932899.654649"),
            ("id-2", "C2:1779101519"),
        ],
    )

    assert _mod.main(["--expected-count", "2"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "invalid_candidates": 0,
        "mode": "dry-run",
        "null_slack_rows": 2,
        "remaining_null_slack_rows": 2,
        "updated": 0,
        "valid_candidates": 2,
    }
    conn.cursor.assert_not_called()


def test_commit_fails_closed_before_update_on_invalid_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _fake_connection()
    monkeypatch.setenv("DATABASE_URL", "postgresql://redacted")
    monkeypatch.setattr(_mod.psycopg, "connect", lambda _dsn: conn)
    monkeypatch.setattr(
        _mod,
        "_read_candidates",
        lambda _conn, *, lock: [("id-1", "malformed")],
    )

    with pytest.raises(RuntimeError, match="解釈できない"):
        _mod.main(["--commit", "--expected-count", "1"])
    conn.cursor.assert_not_called()


def test_commit_updates_all_and_requires_zero_remaining(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _fake_connection()
    update_cur = MagicMock(rowcount=1)
    update_cur.__enter__.return_value = update_cur
    update_cur.__exit__.return_value = False
    count_cur = MagicMock()
    count_cur.__enter__.return_value = count_cur
    count_cur.__exit__.return_value = False
    count_cur.fetchone.return_value = (0,)
    conn.cursor.side_effect = [update_cur, count_cur]
    monkeypatch.setenv("DATABASE_URL", "postgresql://redacted")
    monkeypatch.setattr(_mod.psycopg, "connect", lambda _dsn: conn)
    monkeypatch.setattr(
        _mod,
        "_read_candidates",
        lambda _conn, *, lock: [("id-1", "C1:1783932899.654649")],
    )

    assert _mod.main(["--commit", "--expected-count", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["updated"] == 1
    assert report["remaining_null_slack_rows"] == 0
    update_cur.executemany.assert_called_once()
