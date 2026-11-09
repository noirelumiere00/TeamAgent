"""Real offline concrete assembly, plus hostile owner-bound ZIP snapshots."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from teamagent.media import operations
from teamagent.media.concrete_runner import unpack_snapshot
from teamagent.media.contracts import MAX_INPUT_BYTES, ConcretePptxOperation, S3ObjectRef
from teamagent.media.deadline import DeadlineBudget
from teamagent.media.operations import MediaOperationError, OperationOutput, execute_operation
from teamagent.skills.first_visit import contract
from teamagent.skills.first_visit.concrete_wire import pack_run
from teamagent.skills.first_visit.kit import ARCHIVE
from tests.skills.first_visit.fixture import write_json
from tests.skills.first_visit.test_concrete import concrete_case as concrete_case
from tests.skills.first_visit.test_concrete import concrete_kit as concrete_kit

OWNER = "synthetic-owner"
REQUEST = "synthetic-request"


def ref(body: bytes) -> S3ObjectRef:
    return S3ObjectRef(
        bucket="synthetic-media",
        key="input/concrete.zip",
        version_id="v1",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        content_type="application/zip",
    )


def run(
    body: bytes, work: Path, *, source: S3ObjectRef | None = None, seconds: float = 120
) -> OperationOutput:
    work.mkdir()

    def load(_ref: S3ObjectRef, destination: Path) -> Path:
        destination.write_bytes(body)
        return destination

    return execute_operation(
        ConcretePptxOperation(
            kind="concrete_pptx", source=source or ref(body), owner_id=OWNER, request_id=REQUEST
        ),
        workdir=work,
        load_object=load,
        budget=DeadlineBudget(time.time() + seconds),
    )


def seal(root: Path, kit: Path) -> bytes:
    contract.seal_inputs(root, kit_path=kit, kit_archive=ARCHIVE, part="concrete")
    return pack_run(root, OWNER)


@pytest.mark.parametrize("outcome", ["verified", "major", "kit", "research", "timeout"])
def test_actual_operation_child_and_build(
    concrete_case: Path,
    concrete_kit: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    if outcome == "major":
        data = contract._read(concrete_case / "authored/synthesis.json")
        data["summary"]["headline"] = "前回の振り返り"
        write_json(concrete_case / "authored/synthesis.json", data)
    elif outcome == "research":
        data = contract._read(concrete_case / "case.json")
        data["brands"][0].pop("match")
        write_json(concrete_case / "case.json", data)
    body = seal(concrete_case, concrete_kit)
    kit = concrete_kit
    if outcome == "kit":
        kit = tmp_path / "tampered-kit"
        with tarfile.open(ARCHIVE) as archive:
            archive.extractall(kit, filter="data")
        (kit / "tiktok-deck/src/build_research.js").write_text("process.exit(0)")
    monkeypatch.setattr(operations, "_CONCRETE_KIT_PATH", kit)
    if outcome == "timeout":
        monkeypatch.setattr(operations, "_CONCRETE_CLI_TIMEOUT_S", 0.000001)
    work = tmp_path / "worker"
    output = run(body, work)
    artifacts = {a.name: a.path for a in output.artifacts}
    result = json.loads(artifacts["concrete-result.json"].read_bytes())
    assert result["owner_id"] == OWNER and result["request_id"] == REQUEST
    assert result["input_sha256"] == hashlib.sha256(body).hexdigest()
    expected = (
        "build_verified"
        if outcome == "verified"
        else ("incomplete" if outcome == "major" else "failed")
    )
    assert result["status"] == output.metadata["status"] == expected, result
    if outcome in {"verified", "major"}:
        report = json.loads(artifacts["concrete-preflight.json"].read_bytes())
        assert report["pages"] == result["slide_count"] == 29
        assert report["pptx_sha256"] == result["pptx_sha256"]
        assert (report["counts"].get("重大", 0) > 0) == (outcome == "major")
    if outcome == "verified":
        assert (
            hashlib.sha256(artifacts["concrete.pptx"].read_bytes()).hexdigest()
            == (result["pptx_sha256"])
        )
        assert result["pptx_path"] == "concrete.pptx"
        assert [cli["returncode"] for cli in result["cli"]] == [0, 0, 0]
    else:
        assert "concrete.pptx" not in artifacts
        assert result["pptx_path"] is None and result["problems"]
    if outcome == "timeout":
        assert result["cli"][0]["returncode"] is None
        assert "timed out" in result["cli"][0]["stderr_summary"]


def rewrite(body: bytes, changes: dict[str, bytes | zipfile.ZipInfo]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(body)) as source, zipfile.ZipFile(output, "w") as target:
        for entry in source.infolist():
            if entry.filename not in changes:
                target.writestr(entry, source.read(entry))
        for name, value in changes.items():
            if isinstance(value, zipfile.ZipInfo):
                target.writestr(value, b"case.json")
            else:
                target.writestr(name, value)
    return output.getvalue()


@pytest.mark.parametrize(
    "attack",
    [
        "owner",
        "sha",
        "../escaped",
        "/absolute",
        "link",
        "extra",
        "wire-extra",
        "changed",
        "size",
        "expanded",
        "duplicate",
        "noncanonical",
        "backslash",
        "badzip",
    ],
)
def test_hostile_snapshot_is_rejected(
    concrete_case: Path,
    concrete_kit: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    body = seal(concrete_case, concrete_kit)
    if attack == "sha":
        with pytest.raises(MediaOperationError, match="integrity") as error:
            run(body, tmp_path / "worker", source=ref(body).model_copy(update={"sha256": "0" * 64}))
        assert error.value.code == "MEDIA_CONCRETE_INPUT_INVALID"
        return
    if attack == "owner":
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            wire = json.loads(archive.read("wire-manifest.json"))
        wire["owner_id"] = "another-owner"
        body = rewrite(body, {"wire-manifest.json": json.dumps(wire).encode()})
    elif attack in {"../escaped", "/absolute", "extra"}:
        body = rewrite(body, {attack: b"unexpected"})
    elif attack in {"noncanonical", "backslash"}:
        name = "a/./b" if attack == "noncanonical" else "a\\b"
        body = rewrite(body, {name: b"unexpected"})
    elif attack == "link":
        link = zipfile.ZipInfo("symlink")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        body = rewrite(body, {"symlink": link})
    elif attack == "changed":
        body = rewrite(body, {"authored/synthesis.json": b"{}"})
    elif attack == "wire-extra":
        # Even a matching transfer manifest cannot add files to the sealed case.
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            wire = json.loads(archive.read("wire-manifest.json"))
        wire["files"]["extra"] = {"sha256": hashlib.sha256(b"x").hexdigest(), "size": 1}
        body = rewrite(body, {"extra": b"x", "wire-manifest.json": json.dumps(wire).encode()})
    elif attack in {"size", "expanded"}:
        if attack == "expanded":
            compressed = io.BytesIO()
            with (
                zipfile.ZipFile(io.BytesIO(body)) as src,
                zipfile.ZipFile(compressed, "w", zipfile.ZIP_DEFLATED) as dst,
            ):
                for name in src.namelist():
                    dst.writestr(name, src.read(name))
            body = compressed.getvalue()
        limit = len(body) - 1 if attack == "size" else len(body) + 1
        monkeypatch.setattr("teamagent.media.concrete_runner.MAX_INPUT_BYTES", limit)
    elif attack == "duplicate":
        out = io.BytesIO(body)
        with pytest.warns(UserWarning), zipfile.ZipFile(out, "a") as archive:
            archive.writestr("case.json", b"{}")
        body = out.getvalue()
    elif attack == "badzip":
        body = b"not a ZIP"
    source = tmp_path / "input.zip"
    source.write_bytes(body)
    with pytest.raises(MediaOperationError) as error:
        unpack_snapshot(source, tmp_path / "unpacked", owner_id=OWNER)
    assert error.value.code == "MEDIA_CONCRETE_INPUT_INVALID"
    assert not (tmp_path / "escaped").exists()
    if attack in {"noncanonical", "backslash"}:
        # Unsafe names are refused by name, before anything is extracted.
        assert not (tmp_path / "unpacked" / "a").exists()
        assert not (tmp_path / "unpacked" / "a\\b").exists()


def test_real_render_child_timeout(
    concrete_case: Path,
    concrete_kit: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = seal(concrete_case, concrete_kit)
    monkeypatch.setattr(operations, "_CONCRETE_KIT_PATH", concrete_kit)
    with pytest.raises(MediaOperationError) as error:
        run(body, tmp_path / "worker", seconds=0.01)
    assert error.value.code == "MEDIA_JOB_DEADLINE_EXCEEDED"


def test_pack_rejects_changed_seal(concrete_case: Path, concrete_kit: Path) -> None:
    seal(concrete_case, concrete_kit)
    (concrete_case / "unexpected.json").write_text("{}")
    with pytest.raises(ValueError, match="manifest"):
        pack_run(concrete_case, OWNER)


def test_contract_bounds() -> None:
    source = ref(b"zip")
    for field in ("owner_id", "request_id"):
        for value in ("", " ", "a" * 201):
            raw: dict[str, Any] = {
                "kind": "concrete_pptx",
                "source": source,
                "owner_id": OWNER,
                "request_id": REQUEST,
                field: value,
            }
            with pytest.raises(ValueError):
                ConcretePptxOperation.model_validate(raw)
    for changes in ({"size": 0}, {"size": MAX_INPUT_BYTES + 1}, {"content_type": "text/plain"}):
        with pytest.raises(ValueError):
            ConcretePptxOperation(
                kind="concrete_pptx",
                source=source.model_copy(update=changes),
                owner_id=OWNER,
                request_id=REQUEST,
            )
