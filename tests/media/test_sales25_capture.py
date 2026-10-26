from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from teamagent.media import tool_worker
from teamagent.media.contracts import S3ObjectRef, TikTokAcquireOperation, make_job_request
from teamagent.media.deadline import DeadlineBudget
from teamagent.media.operations import OperationOutput, ProducedArtifact, _tiktok_acquire
from teamagent.media.tiktok_capture import _sales25_source_ok
from teamagent.media.tool_contracts import OutputCapability, _output_slots
from teamagent.media.worker import run_job
from tests.media.test_tool_worker import (
    _ATTEMPT_ID,
    _control,
    _identity_environment,
    _no_signal,
    _output_capability,
)
from tests.media.test_worker import _Backend


def _operation() -> TikTokAcquireOperation:
    return TikTokAcquireOperation(
        kind="tiktok_acquire",
        keywords=("brand-a",),
        n_per_kw=100,
        videos_per_kw=0,
        artifact_mode="metadata_only",
        edition="sales25",
    )


def _payload() -> dict[str, Any]:
    return {
        "ok": True,
        "type": "keyword",
        "query": "brand-a",
        "fetched_at": "2026-10-23T01:00:00.000Z",
        "fetched_on": "2026-10-23",
        "sessions": 1,
        "order_basis": "search_display_order",
        "unfiltered": True,
        "missingness_preserved": True,
        "stop_reason": "exhausted",
        "status": "ok",
        "error_code": None,
        "diagnostics": {"captchaDetected": False, "pagesFetched": 1},
        "videos": [
            {
                "id": "12345678",
                "url": "https://www.tiktok.com/@account-a/photo/12345678",
                "rank_display": 1,
                "mediaType": "photo",
                "imageCount": 2,
                "isAd": True,
                "stats": {
                    "playCount": None,
                    "diggCount": 0,
                    "commentCount": 2,
                    "shareCount": 3,
                    "collectCount": 4,
                },
                "missingFields": ["stats.playCount"],
            }
        ],
    }


def _browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: Any) -> list[list[str]]:
    node, scraper = tmp_path / "node", tmp_path / "search.mjs"
    node.touch()
    scraper.touch()
    monkeypatch.setenv("TIKTOK_NODE_BIN", str(node))
    monkeypatch.setenv("TIKTOK_SCRAPER_PATH", str(scraper))
    commands: list[list[str]] = []

    def process(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        assert kwargs["timeout_s"] == 240
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return subprocess.CompletedProcess(command, 0, body, b"")

    monkeypatch.setattr("teamagent.media.operations._run_process", process)
    monkeypatch.setattr(
        "teamagent.media.operations._fetch_public_image",
        lambda *_args, **_kwargs: pytest.fail("sales25 must not save thumbnails"),
    )
    return commands


def _contents(result: OperationOutput) -> dict[str, Any]:
    return {artifact.name: json.loads(artifact.path.read_bytes()) for artifact in result.artifacts}


def test_partial_capture_preserves_null_zero_photo_ad_and_one_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = _payload()
    commands = _browser(monkeypatch, tmp_path, payload)
    output = _tiktok_acquire(_operation(), tmp_path, DeadlineBudget(400, clock=lambda: 100))
    artifacts = _contents(output)
    assert set(artifacts) == {"posts.json", "sales25-raw.json", "acquisition-status.json"}
    assert artifacts["sales25-raw.json"] == payload
    status = artifacts["acquisition-status.json"]
    assert status == {
        "query": "brand-a",
        "accepted": True,
        "status": "partial",
        "target": 100,
        "actual": 1,
        "shortfall": 99,
        "stop_reason": "exhausted",
        "diagnostics": payload["diagnostics"],
        "raw_ref": "sales25-raw.json",
    }
    assert len(commands) == 1
    assert commands[0][-4:] == ["--edition", "sales25", "--sessions", "1"]
    assert output.metadata["counts"]["videos"] == 0
    assert artifacts["posts.json"]["posts"][0]["plays"] == 0
    # The raw object is the sales25 input; the legacy posts artifact stays compatible.
    assert artifacts["sales25-raw.json"]["videos"][0]["stats"]["playCount"] is None


def test_complete_100_and_true_empty_keep_exact_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = _payload()
    first = payload["videos"][0]
    payload["videos"] = [
        {
            **copy.deepcopy(first),
            "id": str(12345678 + rank),
            "rank_display": rank + 1,
            "url": f"https://www.tiktok.com/@account-a/photo/{12345678 + rank}",
        }
        for rank in range(100)
    ]
    _browser(monkeypatch, tmp_path, payload)
    result = _tiktok_acquire(_operation(), tmp_path, DeadlineBudget(400, clock=lambda: 100))
    status = _contents(result)["acquisition-status.json"]
    assert (status["status"], status["actual"], status["shortfall"]) == ("complete", 100, 0)
    payload.update(videos=[], status="observed_zero", error_code="TIKTOK_TRULY_EMPTY")
    _browser(monkeypatch, tmp_path, payload)
    result = _tiktok_acquire(_operation(), tmp_path, DeadlineBudget(400, clock=lambda: 100))
    status = _contents(result)["acquisition-status.json"]
    assert status["accepted"] is True
    assert (status["status"], status["actual"], status["shortfall"]) == ("partial", 0, 100)


@pytest.mark.parametrize(
    "code,reason,diagnostic",
    [
        ("TIKTOK_BOT_WALL", "captcha", "captchaDetected"),
        ("TIKTOK_CDN_DENIED", "cdn_denied", "cdnDenied"),
    ],
)
def test_refusal_retains_partial_raw_but_accepts_no_posts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: str, reason: str, diagnostic: str
) -> None:
    payload = _payload()
    payload.update(ok=False, status="failed", error_code=code, errorCode=code, stop_reason=reason)
    payload["diagnostics"][diagnostic] = True
    commands = _browser(monkeypatch, tmp_path, payload)
    output = _tiktok_acquire(_operation(), tmp_path, DeadlineBudget(400, clock=lambda: 100))
    artifacts = _contents(output)
    assert artifacts["sales25-raw.json"] == payload
    assert artifacts["acquisition-status.json"]["accepted"] is False
    assert artifacts["acquisition-status.json"]["status"] == "failed"
    assert artifacts["posts.json"]["posts"] == []
    assert len(commands) == 1


def test_invalid_browser_output_is_failed_evidence_not_observed_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _browser(monkeypatch, tmp_path, b"not JSON")
    output = _tiktok_acquire(_operation(), tmp_path, DeadlineBudget(400, clock=lambda: 100))
    artifacts = _contents(output)
    assert artifacts["sales25-raw.json"]["status"] == "failed"
    assert artifacts["sales25-raw.json"]["error_code"] == "MEDIA_TIKTOK_OUTPUT_INVALID"
    assert artifacts["sales25-raw.json"]["fetched_at"] is None
    assert artifacts["sales25-raw.json"]["fetched_on"] is None
    assert artifacts["acquisition-status.json"]["accepted"] is False
    assert len(commands) == 1


@pytest.mark.parametrize(
    "invalid",
    [
        "null_without_evidence",
        "false_count",
        "bad_rank",
        "bad_date",
        "bad_stop_reason",
        "cdn",
        "api_status",
        "api_http",
        "api_shape",
        "exception",
    ],
)
def test_sales25_source_rejects_incoherent_or_refused_success(invalid: str) -> None:
    payload = _payload()
    assert _sales25_source_ok(payload, query="brand-a", target=100)
    if invalid == "null_without_evidence":
        payload["videos"][0]["missingFields"] = []
    elif invalid == "false_count":
        payload["videos"][0]["stats"]["diggCount"] = False
    elif invalid == "bad_rank":
        payload["videos"][0]["rank_display"] = 2
    elif invalid == "bad_date":
        payload["fetched_at"] = "unknown"
    elif invalid == "bad_stop_reason":
        payload["stop_reason"] = []
    else:
        key = {
            "cdn": "cdnDenied",
            "api_status": "apiNonZeroStatus",
            "api_http": "apiHttpErrors",
            "api_shape": "apiInvalidResponses",
            "exception": "exception",
        }[invalid]
        payload["diagnostics"][key] = True
    assert not _sales25_source_ok(payload, query="brand-a", target=100)


def test_capture_write_failure_is_fatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _browser(monkeypatch, tmp_path, _payload())
    write_text = Path.write_text

    def fail(path: Path, *args: Any, **kwargs: Any) -> int:
        if path.name == "sales25-raw.json":
            raise OSError("synthetic full disk")
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail)
    with pytest.raises(OSError, match="synthetic full disk"):
        _tiktok_acquire(_operation(), tmp_path, DeadlineBudget(400, clock=lambda: 100))


def test_output_slots_include_only_required_metadata_artifacts() -> None:
    request = make_job_request(
        operation=_operation(),
        output_bucket="teamagent-media-test",
        request_fingerprint="sales25-slots",
        now_epoch_s=100,
        timeout_s=300,
    )
    slots = _output_slots(request, _ATTEMPT_ID, 1)
    assert [(name, size) for name, _key, size in slots] == [
        ("posts.json", 16 * 1024 * 1024),
        ("sales25-raw.json", 16 * 1024 * 1024),
        ("acquisition-status.json", 1024 * 1024),
    ]


@pytest.mark.parametrize("failure", ["missing", "upload", "retry"])
def test_worker_requires_capture_and_never_retries_sales25(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    backend = _Backend()
    request = make_job_request(
        operation=_operation(),
        output_bucket="teamagent-media-test",
        request_fingerprint="sales25-mandatory",
        now_epoch_s=100,
        timeout_s=300,
    )
    calls = []

    def execute(*_args: Any, **kwargs: Any) -> OperationOutput:
        calls.append(True)
        if failure == "retry":
            raise TimeoutError("synthetic browser timeout")
        names = ["posts.json", "sales25-raw.json", "acquisition-status.json"]
        if failure == "missing":
            names.remove("sales25-raw.json")
        artifacts = []
        for name in names:
            path = kwargs["workdir"] / name
            path.write_bytes(b"{}")
            artifacts.append(ProducedArtifact(name, path, "application/json"))
        return OperationOutput(tuple(artifacts), {})

    upload = backend.upload_artifact

    def fail_upload(request: Any, lease: Any, artifact: ProducedArtifact) -> S3ObjectRef:
        if failure == "upload" and artifact.name == "sales25-raw.json":
            raise OSError("synthetic upload outage")
        return upload(request, lease, artifact)

    monkeypatch.setattr("teamagent.media.worker.execute_operation", execute)
    monkeypatch.setattr(backend, "upload_artifact", fail_upload)
    result = run_job(request, backend=backend, temp_root=tmp_path, now_epoch_s=100, owner="worker")
    assert result.status == "failed"
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["missing", "upload"])
def test_roleless_worker_requires_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    base = _control()
    request = make_job_request(
        operation=_operation(),
        output_bucket=base.request.output_bucket,
        request_fingerprint="sales25-roleless",
        now_epoch_s=1000,
        timeout_s=300,
        job_id=base.request.job_id,
    )
    names = ("posts.json", "sales25-raw.json", "acquisition-status.json")
    control = replace(
        base,
        request=request,
        outputs=tuple(
            _output_capability(name=name, key=f"{request.output_prefix}output/{name}")
            for name in names
        ),
    )
    monkeypatch.setattr(tool_worker, "_signal_scope", _no_signal)

    def execute(*_args: Any, **kwargs: Any) -> OperationOutput:
        artifacts = []
        for name in names:
            if failure == "missing" and name == "sales25-raw.json":
                continue
            path = kwargs["workdir"] / name
            path.write_bytes(b"{}")
            artifacts.append(ProducedArtifact(name, path, "application/json"))
        return OperationOutput(tuple(artifacts), {})

    def upload(
        capability: OutputCapability,
        *,
        path: Path,
        content_type: str,
        bucket: str,
        budget: DeadlineBudget,
    ) -> S3ObjectRef:
        if failure == "upload" and capability.name == "sales25-raw.json":
            raise OSError("synthetic upload outage")
        body = path.read_bytes()
        return S3ObjectRef(
            bucket=bucket,
            key=capability.key,
            version_id="v1",
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
            content_type=content_type,
        )

    monkeypatch.setattr(tool_worker, "execute_operation", execute)
    monkeypatch.setattr(tool_worker, "_upload_path", upload)
    result = tool_worker.run_tool(
        control, environ=_identity_environment(control), temp_root=tmp_path, clock=lambda: 1001
    )
    assert result.status == "failed"
    assert result.error_code == (
        "MEDIA_TIKTOK_OUTPUT_INVALID" if failure == "missing" else "MEDIA_WORKER_FAILED"
    )
