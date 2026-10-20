from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from tests.adapters.test_dm_quality_reader import TEAM, ReadOnlySlack, extract


def _module():
    path = Path(__file__).resolve().parents[2] / "scripts/run_dm_quality.py"
    spec = importlib.util.spec_from_file_location("run_dm_quality_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manual_october_21_command_uses_jst_and_only_writes_private_aggregates(
    monkeypatch, tmp_path
):
    module = _module()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 21, 1, tzinfo=UTC)

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "fake-read-token")
    monkeypatch.setenv("SLACK_TEAM_ID", TEAM)
    monkeypatch.delenv("DM_QUALITY_ID_KEY", raising=False)
    monkeypatch.setattr("slack_sdk.WebClient", lambda **_: object())
    data = extract(ReadOnlySlack())
    monkeypatch.setattr(
        module, "DmQualityReader", lambda _: SimpleNamespace(extract=lambda **_: data)
    )
    monkeypatch.setattr(
        module,
        "BedrockClient",
        lambda **kwargs: SimpleNamespace(
            model_id=kwargs["model_id"],
            converse=lambda **_: SimpleNamespace(
                text='{"verdict":"good"}',
                stop_reason="end_turn",
                usage=SimpleNamespace(cost_usd=0.001),
            ),
        ),
    )
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_dm_quality.py",
            "--start",
            "2026-10-14",
            "--end",
            "2026-10-21",
            "--output",
            str(output),
        ],
    )
    assert module.main() == 0
    report = json.loads(output.read_text())
    assert report["start"] == "2026-10-14T00:00:00+09:00"
    assert report["end"] == "2026-10-21T00:00:00+09:00"
    assert report["complete"] and report["good_rate"] == 1
    assert "feedback" not in report
    assert "合成" not in output.read_text()


def test_fatal_read_failure_replaces_stale_success_with_missing(monkeypatch, tmp_path, capsys):
    module = _module()
    for name, value in {
        "SLACK_BOT_TOKEN": "secret",
        "SLACK_TEAM_ID": TEAM,
    }.items():
        monkeypatch.setenv(name, value)

    def fail(**kwargs):
        raise ValueError("sensitive upstream text")

    monkeypatch.setattr("slack_sdk.WebClient", fail)
    output = tmp_path / "report.json"
    output.write_text('{"stale":"old success"}')
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_dm_quality.py",
            "--start",
            "2026-01-01",
            "--end",
            "2026-01-08",
            "--output",
            str(output),
        ],
    )
    assert module.main() == 2
    report = json.loads(output.read_text())
    assert not report["complete"] and report["good_rate"] is None
    assert "sensitive" not in capsys.readouterr().err
