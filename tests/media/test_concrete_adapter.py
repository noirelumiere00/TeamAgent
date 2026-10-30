"""The sender preserves unsuccessful results and binds every successful artifact."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from teamagent.adapters.media_job import MediaJobClient, MediaJobError
from teamagent.media.contracts import ConcretePptxOperation, S3ObjectRef

BUNDLE = b"synthetic-snapshot"
OWNER = "synthetic-owner"
REQUEST = "synthetic-request"


def evidence(status: str) -> dict[str, bytes]:
    pptx = b"synthetic-pptx"
    digest = hashlib.sha256(pptx).hexdigest()
    payload = {
        "owner_id": OWNER,
        "request_id": REQUEST,
        "input_sha256": hashlib.sha256(BUNDLE).hexdigest(),
        "status": status,
        "pptx_path": "concrete.pptx" if status == "build_verified" else None,
        "pptx_sha256": digest,
        "slide_count": 29,
    }
    files = {"concrete-result.json": json.dumps(payload).encode()}
    if status != "failed":
        files["concrete-preflight.json"] = json.dumps(
            {
                "pptx_sha256": digest,
                "pages": 29,
                "counts": {"致命的": 0, "重大": 1 if status == "incomplete" else 0},
            }
        ).encode()
    if status == "build_verified":
        files["concrete.pptx"] = pptx
    return files


def client(
    monkeypatch: pytest.MonkeyPatch, files: dict[str, bytes], *, conflict: bool = False
) -> tuple[MediaJobClient, list[ConcretePptxOperation]]:
    instance = MediaJobClient(queue_url="queue", table="jobs", bucket="synthetic-media")
    calls: list[ConcretePptxOperation] = []
    source = S3ObjectRef(
        bucket="synthetic-media",
        key="input/concrete-input.zip",
        version_id="v1",
        sha256=hashlib.sha256(BUNDLE).hexdigest(),
        size=len(BUNDLE),
        content_type="application/zip",
    )
    monkeypatch.setattr(instance, "find_staged", lambda **kwargs: source)
    if conflict:
        source = source.model_copy(update={"sha256": "0" * 64})

    def staged(**kwargs: Any) -> tuple[dict[str, bytes], dict[str, Any]]:
        assert kwargs["timeout_s"] == 480
        operation = kwargs["operation_factory"](1000)
        assert isinstance(operation, ConcretePptxOperation)
        calls.append(operation)
        return files, {}

    monkeypatch.setattr(instance, "_run_staged", staged)
    return instance, calls


@pytest.mark.parametrize("status", ["build_verified", "incomplete", "failed"])
def test_sender_returns_result_for_every_build_status(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    files = evidence(status)
    instance, calls = client(monkeypatch, files)
    result = instance.render_concrete_pptx(
        BUNDLE, owner_id=OWNER, request_id=REQUEST, request_fingerprint="synthetic"
    )
    assert result["files"] == files
    assert calls[0].owner_id == OWNER and calls[0].request_id == REQUEST


@pytest.mark.parametrize(
    "change",
    [
        "missing-result",
        "missing-pptx",
        "missing-preflight",
        "owner_id",
        "request_id",
        "input_sha256",
        "pptx_sha256",
        "status",
        "preflight-sha",
        "major",
        "counts-type",
        "unverified-pptx",
    ],
)
def test_sender_rejects_missing_or_contradictory_result(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    files = evidence("build_verified")
    if change.startswith("missing-"):
        name = {
            "result": "concrete-result.json",
            "pptx": "concrete.pptx",
            "preflight": "concrete-preflight.json",
        }[change.removeprefix("missing-")]
        del files[name]
    elif change in {"preflight-sha", "major", "counts-type"}:
        report = json.loads(files["concrete-preflight.json"])
        if change == "counts-type":
            report["counts"] = []
        elif change == "major":
            report["counts"]["重大"] = 1
        else:
            report["pptx_sha256"] = "0" * 64
        files["concrete-preflight.json"] = json.dumps(report).encode()
    else:
        payload = json.loads(files["concrete-result.json"])
        payload["status" if change == "unverified-pptx" else change] = (
            "failed" if change == "unverified-pptx" else "wrong"
        )
        files["concrete-result.json"] = json.dumps(payload).encode()
    instance, _calls = client(monkeypatch, files)
    with pytest.raises(MediaJobError, match="MEDIA_CONCRETE_"):
        instance.render_concrete_pptx(
            BUNDLE, owner_id=OWNER, request_id=REQUEST, request_fingerprint="synthetic"
        )


def test_sender_rejects_staged_input_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _calls = client(monkeypatch, evidence("failed"), conflict=True)
    with pytest.raises(MediaJobError, match="MEDIA_CONCRETE_INPUT_CONFLICT"):
        instance.render_concrete_pptx(
            BUNDLE, owner_id=OWNER, request_id=REQUEST, request_fingerprint="synthetic"
        )
