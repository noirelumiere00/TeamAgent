"""Saved anonymous response through JS, worker, core adapter and the #682 contract."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.media_job import MediaJobClient
from teamagent.media.contracts import (
    MediaArtifact,
    MediaJobRequest,
    MediaJobResult,
    TikTokAcquireOperation,
)
from teamagent.media.deadline import DeadlineBudget
from teamagent.media.operations import _tiktok_acquire
from teamagent.skills.first_visit import sales25_contract
from tests.media.test_adapter import _ref
from tests.skills.first_visit.sales25_fixture import OWNER, inputs

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("outcome", ["partial", "empty", "captcha", "cdn"])
def test_saved_response_reaches_sales25_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    script = """
import fs from 'node:fs';
import {parseSearchItem, sales25Result} from './tools/tiktok_scraper/search.mjs';
const saved = JSON.parse(fs.readFileSync('tools/tiktok_scraper/fixtures/anonymous-search-response.json'));
const outcome = process.argv[1];
const videos = outcome === 'empty' ? [] : saved.data.map(row => parseSearchItem(row, 'sales25'));
const result = sales25Result({ok:true, query:'ブランドA 合成検索', type:'keyword', videos,
  diag:{pagesFetched:1, stop_reason:'exhausted', captchaDetected:outcome === 'captcha',
        cdnDenied:outcome === 'cdn'}}, 100, new Date('2026-10-23T03:00:00Z'));
process.stdout.write(JSON.stringify(result));
"""
    generated = subprocess.run(
        ["node", "--input-type=module", "--eval", script, outcome],
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=20,
    )
    payload = json.loads(generated.stdout)
    command_count = 0

    def process(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal command_count
        command_count += 1
        assert command[-4:] == ["--edition", "sales25", "--sessions", "1"]
        return subprocess.CompletedProcess(
            command, 0 if payload["ok"] else 2, generated.stdout, b""
        )

    node = tmp_path / "node"
    node.touch()
    monkeypatch.setenv("TIKTOK_NODE_BIN", str(node))
    monkeypatch.setenv("TIKTOK_SCRAPER_PATH", str(ROOT / "tools/tiktok_scraper/search.mjs"))
    monkeypatch.setattr("teamagent.media.operations._run_process", process)
    client = MediaJobClient(
        queue_url="queue", table="jobs", bucket="test-bucket", clock=lambda: 100
    )

    def run_sync(
        request: MediaJobRequest, **kwargs: Any
    ) -> tuple[dict[str, bytes], dict[str, Any]]:
        assert isinstance(request.operation, TikTokAcquireOperation)
        output = _tiktok_acquire(
            request.operation, tmp_path, DeadlineBudget(400, clock=lambda: 100)
        )
        artifacts = {artifact.name: artifact.path.read_bytes() for artifact in output.artifacts}
        kwargs["capture_result"](
            MediaJobResult(
                job_id=request.job_id,
                status="done",
                artifacts=tuple(
                    MediaArtifact(
                        name=name,
                        object=_ref(request.job_id, body).model_copy(
                            update={"key": f"media-jobs/{request.job_id}/output/{name}"}
                        ),
                    )
                    for name, body in artifacts.items()
                ),
                metadata=output.metadata,
            )
        )
        return artifacts, output.metadata

    monkeypatch.setattr(client, "run_sync", run_sync)
    evidence = client.search_tiktok_sales25(
        "ブランドA 合成検索", owner_id=OWNER, request_fingerprint=f"anonymous-{outcome}"
    )
    assert command_count == 1
    raw = evidence["raw"]
    assert raw["owner_id"] == OWNER
    assert raw["fetched_at"] == "2026-10-23T03:00:00.000Z"
    assert {artifact["name"] for artifact in evidence["artifacts"]} == {
        "posts.json",
        "sales25-raw.json",
        "acquisition-status.json",
    }
    case, raw_by_query, decisions = inputs()
    raw_by_query["q0"] = raw
    decisions["q0"] = [
        {"id": video["id"], "eligible": True, "reason": "匿名fixture"} for video in raw["videos"]
    ]
    status = evidence["acquisition_status"]
    assert status["target"] == 100
    assert status["actual"] == (0 if outcome == "empty" else 3)
    assert status["shortfall"] == 100 - status["actual"]
    if outcome in {"captcha", "cdn"}:
        assert status["accepted"] is False and status["status"] == "failed"
        with pytest.raises(ValueError):
            sales25_contract.convert_inputs(case, raw_by_query, decisions, owner_id=OWNER)
        return
    assert status["accepted"] is True and status["status"] == "partial"
    converted = sales25_contract.convert_inputs(case, raw_by_query, decisions, owner_id=OWNER)
    assert converted["raw/q0.json"] == raw
    posts = converted["selection/source_q0.json"]["posts"]
    if outcome == "empty":
        assert posts == [] and raw["error_code"] == "TIKTOK_TRULY_EMPTY"
    else:
        assert posts[1]["stats"]["playCount"] is None
        assert posts[2]["stats"]["playCount"] == 0
        assert posts[1]["isAd"] is True and posts[1]["imageCount"] == 2
        assert "/photo/" in posts[1]["url"]
        assert [post["rank_display"] for post in posts] == [1, 2, 3]
