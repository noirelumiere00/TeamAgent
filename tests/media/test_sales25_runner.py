"""Real pinned rendering and adversarial transport snapshots, with anonymous input only."""

from __future__ import annotations

import hashlib
import io
import stat
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.media_job import MediaJobClient, MediaJobError
from teamagent.media.contracts import (
    MediaArtifact,
    MediaJobRequest,
    MediaJobResult,
    S3ObjectRef,
    Sales25PptxOperation,
)
from teamagent.media.deadline import DeadlineBudget
from teamagent.media.operations import execute_operation
from teamagent.media.sales25_runner import render_sales25, unpack_snapshot
from teamagent.skills.first_visit import sales25_contract as contract
from teamagent.skills.omiyage_report.sales25 import MachineChecks
from tests.skills.first_visit.sales25_fixture import OWNER, prepare


def _snapshot(root: Path, *, owner: str = OWNER) -> bytes:
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    manifest = {
        "schema_version": 1,
        "owner_id": owner,
        "files": {
            name: {"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
            for name, body in files.items()
        },
    }
    files["wire-manifest.json"] = contract.json_bytes(manifest)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return data.getvalue()


def _ref(
    body: bytes, *, name: str = "snapshot.zip", content_type: str = "application/zip"
) -> S3ObjectRef:
    return S3ObjectRef(
        bucket="synthetic-media",
        key=f"media-jobs/mj_{'1' * 24}/input/{name}",
        version_id="synthetic-version",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        content_type=content_type,
    )


def test_real_child_runs_pinned_clis_and_keeps_complete_qa(tmp_path: Path) -> None:
    authored = tmp_path / "authored"
    prepare(authored)
    reviews = contract.read_json(authored / "top5-analysis.json")
    reviews["posts"]["9000000010"]["audio"] = {
        "status": "unmeasured",
        "summary": "原音未確認",
        "reason": "合成fixtureで音声非対応",
    }
    (authored / "top5-analysis.json").write_bytes(contract.json_bytes(reviews))
    contract.seal_inputs(authored, owner_id=OWNER)
    body = _snapshot(authored)
    workdir = tmp_path / "worker"
    workdir.mkdir()

    def load(ref: S3ObjectRef, destination: Path) -> Path:
        assert ref.sha256 == hashlib.sha256(body).hexdigest()
        destination.write_bytes(body)
        return destination

    output = execute_operation(
        Sales25PptxOperation(kind="sales25_pptx", source=_ref(body), owner_id=OWNER),
        workdir=workdir,
        load_object=load,
        budget=DeadlineBudget(time.time() + 60),
    )
    assert {a.name for a in output.artifacts} == {"deck.pptx", "sales25-qa.json", "sales25-qa.zip"}
    qa = contract.read_json(workdir / "sales25-qa.json")
    digest = hashlib.sha256((workdir / "deck.pptx").read_bytes()).hexdigest()
    checks = MachineChecks.model_validate(qa["checks"])
    assert checks.accepts(digest)
    assert qa["slide_count"] == 25
    assert qa["owner_id"] == OWNER
    assert qa["input_sha256"] == hashlib.sha256(body).hexdigest()
    assert qa["status"] == "incomplete"
    assert checks.check_top5_analysis.reason == "audio_unverified"
    assert qa["page_qa"] == "not_available"
    assert qa["visual_review"] == "not_verified"
    with zipfile.ZipFile(workdir / "sales25-qa.zip") as archive:
        assert {
            "filler-qa/fill_template.json",
            "qa/check_fmt.json",
            "qa/check_top5_analysis.json",
            "qa/deck-qa.json",
            "qa/text-fit.json",
            "inputs/sales25-manifest.json",
            "inputs/wire-manifest.json",
            "inputs/deck-plan.json",
            "inputs/deck-input.json",
        } <= set(archive.namelist())


@pytest.mark.parametrize(
    "mutation",
    ["owner", "sha", "membership", "traversal", "symlink", "duplicate", "expanded_bound"],
)
def test_snapshot_mutations_fail_before_authoring(
    tmp_path: Path, mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = tmp_path / "input"
    inputs.mkdir()
    (inputs / "case.json").write_bytes(contract.json_bytes({"owner_id": OWNER}))
    snapshot = _snapshot(inputs, owner="other-owner" if mutation == "owner" else OWNER)
    with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    if mutation == "sha":
        entries["case.json"] += b" "
    if mutation == "membership":
        entries["extra.json"] = b"{}"
    if mutation == "traversal":
        entries["../escaped.json"] = b"{}"
    source = tmp_path / "snapshot.zip"
    with zipfile.ZipFile(source, "w") as archive:
        for name, data in entries.items():
            entry = zipfile.ZipInfo(name)
            if mutation == "symlink" and name == "case.json":
                entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, data)
        if mutation == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("case.json", b"{}")
    if mutation == "expanded_bound":
        with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("large", b"x" * 20_000)
        monkeypatch.setattr(
            "teamagent.media.sales25_runner.MAX_INPUT_BYTES", source.stat().st_size + 1
        )
    with pytest.raises(ValueError):
        unpack_snapshot(source, tmp_path / "unpacked", owner_id=OWNER)
    assert not (tmp_path / "escaped.json").exists()


def test_real_text_overflow_blocks_delivery(tmp_path: Path) -> None:
    authored = tmp_path / "authored"
    prepare(authored)
    data = contract.read_json(authored / "deck-input.json")
    data["texts"]["date"] = "検査" * 120
    (authored / "deck-input.json").write_bytes(contract.json_bytes(data))
    contract.seal_inputs(authored, owner_id=OWNER)
    source = tmp_path / "input.zip"
    source.write_bytes(_snapshot(authored))
    workdir = tmp_path / "worker"
    workdir.mkdir()
    render_sales25(workdir, source, OWNER)
    qa = contract.read_json(workdir / "sales25-qa.json")
    assert qa["status"] == "invalid"
    assert qa["checks"]["text_fit"]["status"] == "failed"
    assert not MachineChecks.model_validate(qa["checks"]).accepts(qa["output_sha256"])


@pytest.mark.parametrize("tamper", [None, "owner", "input", "output", "missing"])
def test_client_keeps_immutable_refs_and_rejects_mixed_qa(
    monkeypatch: pytest.MonkeyPatch, tamper: str | None
) -> None:
    owner = "T_TEST\x1fU_SELF"
    client = MediaJobClient(
        queue_url="queue", table="jobs", bucket="synthetic-media", clock=lambda: 100
    )
    snapshot = b"synthetic-snapshot"
    pptx = b"synthetic-pptx"
    qa = {
        "owner_id": owner,
        "input_sha256": hashlib.sha256(snapshot).hexdigest(),
        "output_sha256": hashlib.sha256(pptx).hexdigest(),
        "status": "incomplete",
    }
    if tamper in {"owner", "input", "output"}:
        qa[{"owner": "owner_id", "input": "input_sha256", "output": "output_sha256"}[tamper]] = (
            "mismatch"
        )
    files = {"deck.pptx": pptx, "sales25-qa.json": contract.json_bytes(qa), "sales25-qa.zip": b"qa"}
    if tamper == "missing":
        del files["sales25-qa.zip"]
    monkeypatch.setattr(client, "find_staged", lambda **kwargs: None)
    monkeypatch.setattr(client, "stage_bytes", lambda **kwargs: _ref(kwargs["body"]))

    def run_sync(
        request: MediaJobRequest, **kwargs: Any
    ) -> tuple[dict[str, bytes], dict[str, Any]]:
        assert isinstance(request.operation, Sales25PptxOperation)
        assert request.operation.owner_id == owner
        result = MediaJobResult(
            job_id=request.job_id,
            status="done",
            artifacts=tuple(
                MediaArtifact(name=name, object=_ref(body, name=name))
                for name, body in files.items()
            ),
        )
        kwargs["capture_result"](result)
        return files, {}

    monkeypatch.setattr(client, "run_sync", run_sync)
    if tamper:
        with pytest.raises(MediaJobError, match="MEDIA_SALES25_"):
            client.render_sales25_pptx(snapshot, owner_id=owner, request_fingerprint="fixture")
    else:
        result = client.render_sales25_pptx(snapshot, owner_id=owner, request_fingerprint="fixture")
        assert result["files"] == files
        assert len(result["artifacts"]) == 3
        assert all(a["object"]["version_id"] == "synthetic-version" for a in result["artifacts"])


def test_client_retry_reuses_exact_input_version_and_rejects_changed_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MediaJobClient(
        queue_url="queue", table="jobs", bucket="synthetic-media", clock=lambda: 100
    )
    bundle = b"synthetic-snapshot"
    source: S3ObjectRef | None = None
    requests: list[MediaJobRequest] = []
    staged: list[dict[str, Any]] = []

    def stage_bytes(**kwargs: Any) -> S3ObjectRef:
        nonlocal source
        staged.append(kwargs)
        source = _ref(kwargs["body"]).model_copy(update={"version_id": str(len(staged))})
        return source

    def run_sync(
        request: MediaJobRequest, **kwargs: Any
    ) -> tuple[dict[str, bytes], dict[str, Any]]:
        requests.append(request)
        if len(requests) == 1:
            raise MediaJobError("MEDIA_JOB_TIMEOUT")  # Submit succeeded; parent checkpoint did not.
        pptx = b"synthetic-pptx"
        return {
            "deck.pptx": pptx,
            "sales25-qa.zip": b"qa",
            "sales25-qa.json": contract.json_bytes(
                {
                    "owner_id": OWNER,
                    "input_sha256": hashlib.sha256(bundle).hexdigest(),
                    "output_sha256": hashlib.sha256(pptx).hexdigest(),
                    "status": "incomplete",
                }
            ),
        }, {}

    monkeypatch.setattr(client, "find_staged", lambda **kwargs: source)
    monkeypatch.setattr(client, "stage_bytes", stage_bytes)
    monkeypatch.setattr(client, "run_sync", run_sync)
    with pytest.raises(MediaJobError, match="MEDIA_JOB_TIMEOUT"):
        client.render_sales25_pptx(bundle, owner_id=OWNER, request_fingerprint="resume-fixture")
    client.render_sales25_pptx(bundle, owner_id=OWNER, request_fingerprint="resume-fixture")
    assert len(staged) == 1
    assert len(requests) == 2
    assert requests[0].job_id == requests[1].job_id
    assert requests[0].operation == requests[1].operation
    with pytest.raises(MediaJobError, match="MEDIA_SALES25_INPUT_CONFLICT"):
        client.render_sales25_pptx(
            bundle + b"changed", owner_id=OWNER, request_fingerprint="resume-fixture"
        )
    assert len(staged) == 1 and len(requests) == 2
