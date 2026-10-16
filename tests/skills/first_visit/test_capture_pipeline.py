"""Actual adapter evidence reaches the input gate through persisted snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import structlog

from teamagent.adapters import tiktok_scraper
from teamagent.skills.first_visit.capture import materialize_capture
from teamagent.skills.first_visit.recording import CaptureSession

from .fixture import VID
from .test_conversion import URL, synthetic_sources


@pytest.mark.parametrize(
    "source_reason,stop_reason",
    [("target_reached", "capped"), ("has_more_false", "exhausted"), ("no_new_data", "no_new")],
)
def test_adapter_saved_capture_has_no_acquisition_gate_shortfalls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_reason: str,
    stop_reason: str,
) -> None:
    kit_path = os.environ.get("FIRST_VISIT_KIT_PATH")
    if not kit_path:
        pytest.skip("FIRST_VISIT_KIT_PATH is required for the pinned input gate")
    request, axes, normalized, _, details = synthetic_sources()
    for name in ("MEDIA_TASK_QUEUE", "MEDIA_JOBS_TABLE", "MEDIA_JOB_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")
    monkeypatch.setattr(tiktok_scraper, "_node_bin", lambda: "/usr/bin/node")
    objects: dict[str, bytes] = {}

    def upload(key: str, body: bytes, content_type: str) -> dict[str, Any]:
        objects[key] = body
        return {
            "bucket": "private-synthetic",
            "key": key,
            "version_id": "v1",
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
            "content_type": content_type,
        }

    session = CaptureSession(
        request.model_dump(mode="json"),
        "pipeline-request",
        "synthetic-owner",
        [(axis.role, axis.label, axis.query) for axis in axes],
        2,
        uploader=upload,
        log=structlog.get_logger(__name__),
    )
    for index, axis in enumerate(axes):
        # This is the JS wire shape, including a normal result shortfall.
        payload = {
            "ok": True,
            "type": "keyword",
            "count": 1,
            "videos": [{"id": VID, "url": URL}],
            "diag": {"sessionsRun": 1, "captchaDetected": False},
            "firstVisitCapture": {
                "acquired_on": "2026-10-14T03:00:00.000Z",
                "order_basis": "search_display_order",
                "sessions": 1,
                "stop_reason": source_reason,
                "missingness_preserved": True,
                "posts": [normalized["posts"][index]],
                "shortfalls": [
                    {
                        "kw": axis.query,
                        "requested": 2,
                        "actual": 1,
                        "reason": "MEDIA_TIKTOK_RESULT_SHORTFALL",
                    }
                ],
                "sessions_data": [{"responses": [{"posts": [{"id": VID}]}]}],
            },
        }

        def fake_process(
            *args: Any, payload: dict[str, Any] = payload, **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess([], 0, json.dumps(payload), "")

        def capture(evidence: dict[str, Any], index: int = index) -> None:
            session.record(index, evidence)

        with monkeypatch.context() as process_patch:
            process_patch.setattr(subprocess, "run", fake_process)
            search_result = tiktok_scraper.search_tiktok(
                axis.query,
                max_videos=2,
                capture=capture,
            )
        assert search_result.count == 1
    manifest_key = max(key for key in objects if key.endswith("capture-manifest.json"))
    capture_ref = json.loads(objects[manifest_key])["capture"]
    root = tmp_path / "case"
    result = materialize_capture(
        capture_ref,
        root,
        reader=lambda ref: objects[ref["key"]],
        kit_path=Path(kit_path),
        case_details=details,
        selection=[{"id": VID, "url": URL}],
    )
    problems = result["missing_inputs"]
    assert problems
    assert not any(problem.startswith(("raw/", "kit:", "case.json:")) for problem in problems)
    assert any("labels.json" in problem for problem in problems)
    assert result["manifest"] is None
    for index in range(1, 4):
        raw = json.loads((root / f"raw/axis_{index:02d}.json").read_bytes())
        assert raw["aico"]["status"] == "ok"
        assert raw["stop_reason"] == stop_reason
        assert raw["aico"]["source"]["source_stop_reason"] == source_reason
