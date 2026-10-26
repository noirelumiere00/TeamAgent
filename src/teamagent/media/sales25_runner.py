"""Bounded, offline sales25 rendering behind the existing isolated media child."""

from __future__ import annotations

import hashlib
import shutil
import stat
import zipfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

from teamagent.media.contracts import MAX_INPUT_BYTES
from teamagent.media.deck_qa import inspect_pptx
from teamagent.skills.first_visit import sales25
from teamagent.skills.first_visit import sales25_contract as contract


def unpack_snapshot(source: Path, root: Path, *, owner_id: str) -> None:
    """Verify the exact owner/file inventory before allowing any fixed-kit authoring."""
    if not 0 < source.stat().st_size <= MAX_INPUT_BYTES:
        raise ValueError("sales25 ZIP exceeds input bound")
    root.mkdir(exist_ok=False)
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        names = [entry.filename for entry in members]
        if (
            not 1 <= len(members) <= 5000
            or len(set(names)) != len(names)
            or sum(entry.file_size for entry in members) > MAX_INPUT_BYTES
        ):
            raise ValueError("sales25 ZIP membership or expanded size is invalid")
        for entry in members:
            name = entry.filename
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if (
                entry.is_dir()
                or "\\" in name
                or PurePosixPath(name).as_posix() != name
                or mode not in {0, stat.S_IFREG}
                or entry.flag_bits & 1
            ):
                raise ValueError("sales25 ZIP member is not a plain file")
            destination = contract.local_path(root, name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
    manifest = contract.read_json(root / "wire-manifest.json")
    files = {
        name: {
            "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
            "size": (root / name).stat().st_size,
        }
        for name in names
        if name != "wire-manifest.json"
    }
    if manifest != {"schema_version": 1, "owner_id": owner_id, "files": files}:
        raise ValueError("sales25 snapshot owner/file membership/SHA-256 mismatch")
    if contract.read_json(root / "case.json").get("owner_id") != owner_id:
        raise ValueError("sales25 case owner mismatch")


def render_sales25(workdir: Path, source: Path, owner_id: str) -> dict[str, Any]:
    """Run the pinned three CLIs and retain machine checks without implying visual QA."""
    inputs = workdir / "sales25-input"
    unpack_snapshot(source, inputs, owner_id=owner_id)
    wire_manifest = workdir / "sales25-wire-manifest.json"
    (inputs / "wire-manifest.json").rename(wire_manifest)
    if not (inputs / "sales25-manifest.json").exists():
        from teamagent.skills.first_visit.sales25_authoring import build_manuscript

        build_manuscript(inputs, owner_id=owner_id)
        contract.seal_inputs(inputs, owner_id=owner_id)
    output = workdir / "sales25-output"
    result = sales25.generate(inputs, output, owner_id=owner_id)
    deck = output / "deck.pptx"
    l1 = inspect_pptx(deck, expected_slides=25)
    text_findings = [
        asdict(finding)
        for finding in l1.findings
        if finding.kind in {"text_overflow", "text_autofit_collision"}
    ]
    text_fit = {"status": "failed" if text_findings else "passed", "findings": text_findings}
    if l1.error_count or text_findings:
        result["status"] = "invalid"
    checked_sha256 = hashlib.sha256(deck.read_bytes()).hexdigest()
    fmt = contract.read_json(output / "qa/check_fmt.json")
    analysis = contract.read_json(output / "qa/check_top5_analysis.json")
    analysis_status = analysis["result"].get("status")
    audio_unverified = any(
        review.get("audio", {}).get("status") == "unmeasured"
        for review in contract.read_json(inputs / "top5-analysis.json")["posts"].values()
    )
    selection = analysis["result"].get("selection_queries", [])
    shortfall = any(item["shortfall"] for item in result["acquisition"]) or any(
        len(ranking) < 10 for item in selection for ranking in item.get("rankings", {}).values()
    )
    reason = "audio_unverified" if audio_unverified else "shortfall" if shortfall else None
    if analysis_status == "incomplete" and reason is None:
        analysis_status = "failed"
        result["status"] = "invalid"
    checks = {
        "check_fmt": {
            "status": "passed"
            if fmt["returncode"] == 0 and fmt["result"].get("status") == "structure_passed"
            else "failed",
            "checked_sha256": checked_sha256,
        },
        "check_top5_analysis": {
            "status": analysis_status
            if analysis["returncode"] in {0, 2} and analysis_status in {"passed", "incomplete"}
            else "failed",
            "checked_sha256": checked_sha256,
            "reason": reason if analysis_status in {"passed", "incomplete"} else None,
        },
        "deck_qa_l1": {
            "status": "failed" if l1.error_count else "passed",
            "checked_sha256": checked_sha256,
            "error_count": l1.error_count,
            "review_pages": sorted({f.slide for f in l1.findings if f.severity == "warn"}),
        },
        "text_fit": {
            "status": text_fit["status"],
            "checked_sha256": checked_sha256,
        },
    }
    if checked_sha256 != result["output_sha256"]:
        raise ValueError("sales25 PPTX changed during machine checks")
    result.update(
        checks=checks,
        owner_id=owner_id,
        input_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        slide_count=l1.slide_count,
        machine_check="failed" if result["status"] == "invalid" else "passed",
        page_qa="not_available",
        deck_qa=l1.summary(),
        text_fit=text_fit,
    )
    (output / "qa/deck-qa.json").write_bytes(contract.json_bytes(l1.to_dict()))
    (output / "qa/text-fit.json").write_bytes(contract.json_bytes(text_fit))
    qa = workdir / "sales25-qa.json"
    qa.write_bytes(contract.json_bytes(result))
    with zipfile.ZipFile(workdir / "sales25-qa.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file() and path != deck:
                archive.write(path, path.relative_to(output).as_posix())
        for name in ("sales25-manifest.json", "deck-plan.json", "deck-input.json"):
            archive.write(inputs / name, "inputs/" + name)
        archive.write(wire_manifest, "inputs/wire-manifest.json")
        archive.write(qa, qa.name)
    deck.rename(workdir / "deck.pptx")
    return {"status": result["status"], "slide_count": l1.slide_count, "page_qa": "not_available"}
