"""Scheduled ingest keeps its exit status when the video library sync fails."""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from scripts import run_ingest_fargate as fargate
from teamagent.ingest import video_library_sync as sync


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    for name in (
        "GOOGLE_OAUTH_JSON",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_OAUTH_REFRESH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INGEST_DRY_RUN", "0")
    monkeypatch.setenv("INGEST_OWNER_EMAIL", "owner@example.test")
    monkeypatch.setattr(sys, "argv", ["test-ingest"])
    monkeypatch.setattr(fargate, "_materialize_vertex_sa", lambda: None)
    monkeypatch.setattr(fargate, "_resolve_sources_yaml", lambda: None)
    monkeypatch.setattr(fargate, "logger", MagicMock())
    # Even when a test accidentally reaches the sync entry point, it must not reconfigure logging.
    monkeypatch.setattr(sync, "configure_logging", lambda **_: False)
    events = MagicMock()
    events.ingest.return_value = {}
    events.sync.return_value = 0
    monkeypatch.setattr(runpy, "run_path", events.ingest)
    monkeypatch.setattr(sync, "main", events.sync)
    return events


@pytest.mark.parametrize("ingest_code", [0, None])
@pytest.mark.parametrize("yaml_override", [None, "/tmp/ingest_sources_s3.yaml"])
def test_ingest_success_runs_sync_after_ingest_with_resolved_yaml(
    runner: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    ingest_code: int | None,
    yaml_override: str | None,
) -> None:
    monkeypatch.setattr(fargate, "_resolve_sources_yaml", lambda: yaml_override)
    runner.ingest.side_effect = SystemExit(ingest_code)
    assert fargate.main() == 0
    expected = Path(yaml_override) if yaml_override else fargate._BAKED_YAML
    assert runner.mock_calls == [
        call.ingest("scripts/ingest_sources.py", run_name="__main__"),
        call.sync([], sources_path=expected),
    ]
    if yaml_override:
        assert sys.argv[-2:] == ["--yaml", yaml_override]
    else:
        assert "--yaml" not in sys.argv


@pytest.mark.parametrize("outcome", [1, RuntimeError("do-not-log"), SystemExit(7)])
def test_sync_failure_keeps_successful_ingest_exit_code(
    runner: MagicMock, outcome: int | BaseException
) -> None:
    if isinstance(outcome, int):
        runner.sync.return_value = outcome
    else:
        runner.sync.side_effect = outcome
    assert fargate.main() == 0
    runner.sync.assert_called_once()
    assert fargate.logger.error.call_args.args == ("video_library_failed",)
    assert "do-not-log" not in str(fargate.logger.mock_calls)


@pytest.mark.parametrize("code", [1, 2])
def test_ingest_nonzero_exit_skips_sync_and_preserves_code(runner: MagicMock, code: int) -> None:
    runner.ingest.side_effect = SystemExit(code)
    with pytest.raises(SystemExit) as exc:
        fargate.main()
    assert exc.value.code == code
    runner.sync.assert_not_called()
    fargate.logger.error.assert_not_called()


def test_ingest_exception_skips_sync(runner: MagicMock) -> None:
    runner.ingest.side_effect = RuntimeError("ingest failed")
    with pytest.raises(RuntimeError, match="ingest failed"):
        fargate.main()
    runner.sync.assert_not_called()


def test_ingest_dry_run_is_passed_to_video_sync(
    runner: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INGEST_DRY_RUN", "1")
    assert fargate.main() == 0
    assert "--commit" not in sys.argv
    runner.sync.assert_called_once_with(["--dry-run"], sources_path=fargate._BAKED_YAML)


def test_fargate_expands_oauth_before_ingest(
    runner: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "GOOGLE_OAUTH_JSON",
        json.dumps({"client_id": "id", "client_secret": "secret", "refresh_token": "token"}),
    )

    def ingest(*args: object, **kwargs: object) -> dict[str, object]:
        assert os.environ["GOOGLE_CLIENT_ID"] == "id"
        assert os.environ["GOOGLE_CLIENT_SECRET"] == "secret"
        assert os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"] == "token"
        return {}

    runner.ingest.side_effect = ingest
    assert fargate.main() == 0
    runner.sync.assert_called_once()
