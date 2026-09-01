"""render_child を別プロセスで起動するときの sys.path 受け渡しの回帰テスト。

media image は teamagent を venv へ install せず PYTHONPATH=/app/src だけで
到達させる。_child_environment は allowlist 方式で PYTHONPATH を落とすため、
_renderer_json が明示的に渡さないと子は ModuleNotFoundError で即死し、
stdout が空 → MEDIA_RENDER_OUTPUT_INVALID になる（2026-09-01 本番障害）。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from teamagent.media.deadline import DeadlineBudget
from teamagent.media.operations import (
    MediaOperationError,
    _render_child_search_path,
    _renderer_json,
)


def _budget() -> DeadlineBudget:
    return DeadlineBudget(time.time() + 60)


def test_render_child_search_path_resolves_media_package() -> None:
    roots = _render_child_search_path().split(":")

    assert roots
    assert any((Path(root) / "teamagent" / "media" / "render_child.py").is_file() for root in roots)


def test_renderer_json_passes_search_path_to_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_process(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"ok": True, "metadata": {"slides": 1}}).encode("utf-8"),
            b"",
        )

    monkeypatch.setattr("teamagent.media.operations._run_process", fake_run_process)

    metadata = _renderer_json({"kind": "slides"}, workdir=tmp_path, budget=_budget())

    assert metadata == {"slides": 1}
    # allowlist は PYTHONPATH を落とすので、明示指定が無いと子は teamagent を import できない。
    assert "PYTHONPATH" in captured["env"]
    probe = subprocess.run(
        [captured["command"][0], "-c", "import teamagent.media.render_child"],
        env={"PATH": captured["env"]["PATH"], "PYTHONPATH": captured["env"]["PYTHONPATH"]},
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr.decode("utf-8", "replace")


def test_renderer_json_reports_child_error_code(tmp_path: Path) -> None:
    """子が実際に起動できていれば、無診断の OUTPUT_INVALID ではなく契約コードが返る。"""

    with pytest.raises(MediaOperationError) as excinfo:
        _renderer_json({"kind": "unsupported"}, workdir=tmp_path, budget=_budget())

    assert excinfo.value.code == "MEDIA_RENDER_MANIFEST_INVALID"
