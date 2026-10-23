"""Offline-only sales25 generation via three pinned CLIs; no production registration."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from teamagent.skills.first_visit import sales25_contract as contract


def _cli(
    script: Literal["fill_template", "check_fmt", "check_top5_analysis"],
    args: list[str],
    *,
    qa: Path,
    bundle: Path,
    python: Path,
) -> dict[str, Any]:
    contract.verify_bundle(bundle)
    command = [str(python), "-B", str(bundle / "scripts" / f"{script}.py"), *args]
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired as exc:
        (qa / f"{script}.stdout").write_bytes(exc.stdout or b"")
        (qa / f"{script}.stderr").write_bytes(exc.stderr or b"")
        raise ValueError(f"{script} timed out; see QA logs") from exc
    (qa / f"{script}.stdout").write_text(process.stdout, encoding="utf-8")
    (qa / f"{script}.stderr").write_text(process.stderr, encoding="utf-8")
    try:
        result = contract.read_json(qa / f"{script}.stdout")
    except ValueError as exc:
        raise ValueError(f"{script} returned no valid JSON; see QA logs") from exc
    record = {"returncode": process.returncode, "result": result}
    (qa / f"{script}.json").write_bytes(contract.json_bytes(record))
    return record


def check_deck(
    root: Path,
    pptx: Path,
    qa: Path,
    *,
    owner_id: str,
    bundle: Path = contract.BUNDLE,
    python: Path | None = None,
) -> dict[str, Any]:
    """Persist both checkers, including incomplete (exit 2), against these exact bytes."""
    root = root.absolute()
    if qa.resolve().is_relative_to(root.resolve()):
        raise ValueError("QA directory must be outside sealed inputs")
    contract.verify_manifest(root, owner_id=owner_id, bundle=bundle)
    qa.mkdir(parents=True, exist_ok=False)
    before = hashlib.sha256(pptx.read_bytes()).hexdigest()
    interpreter = python or Path(sys.executable)
    fmt = _cli(
        "check_fmt",
        ["--pptx", str(pptx.resolve()), "--plan", str(root / "deck-plan.json")],
        qa=qa,
        bundle=bundle,
        python=interpreter,
    )
    analysis = _cli(
        "check_top5_analysis",
        [
            "--plan",
            str(root / "deck-plan.json"),
            "--reviews",
            str(root / "top5-analysis.json"),
            "--run-dir",
            str(root),
            "--pptx",
            str(pptx.resolve()),
        ],
        qa=qa,
        bundle=bundle,
        python=interpreter,
    )
    after = hashlib.sha256(pptx.read_bytes()).hexdigest()
    valid = (
        fmt["returncode"] == 0
        and fmt["result"].get("status") == "structure_passed"
        and analysis["returncode"] in {0, 2}
        and analysis["result"].get("selection_mode") == "query_top10"
        and analysis["result"].get("status") in {"passed", "incomplete"}
        and before == after
    )
    contract.verify_manifest(root, owner_id=owner_id, bundle=bundle)
    acquisition = []
    for query in contract.read_json(root / "case.json")["queries"]:
        qid = query["query_id"]
        actual = len(contract.read_json(root / f"selection/source_{qid}.json")["posts"])
        acquisition.append(
            {"query_id": qid, "target": 100, "actual": actual, "shortfall": max(0, 100 - actual)}
        )
    status = "invalid" if not valid else analysis["result"]["status"]
    if status == "passed" and any(q["shortfall"] for q in acquisition):
        status = "incomplete"
    result = {
        "status": status,
        "acquisition": acquisition,
        "delivery_status": "incomplete",
        "visual_review": "not_verified",
        "output_sha256": after,
        "input_manifest_sha256": hashlib.sha256(
            (root / "sales25-manifest.json").read_bytes()
        ).hexdigest(),
        "kit_lock_sha256": contract.LOCK_SHA256,
    }
    (qa / "result.json").write_bytes(contract.json_bytes(result))
    return result


def generate(
    root: Path,
    output_dir: Path,
    *,
    owner_id: str,
    bundle: Path = contract.BUNDLE,
    python: Path | None = None,
) -> dict[str, Any]:
    """Generate into a new directory outside sealed inputs; never imply delivery readiness."""
    root = root.absolute()
    if output_dir.resolve().is_relative_to(root):
        raise ValueError("output directory must be outside sealed inputs")
    contract.verify_manifest(root, owner_id=owner_id, bundle=bundle)
    output_dir.mkdir(parents=True, exist_ok=False)
    output_dir = output_dir.resolve()
    logs = output_dir / "filler-qa"
    logs.mkdir()
    pptx = output_dir / "deck.pptx"
    filled = _cli(
        "fill_template",
        ["--input", str(root / "deck-input.json"), "--run-dir", str(root), "--output", str(pptx)],
        qa=logs,
        bundle=bundle,
        python=python or Path(sys.executable),
    )
    if (filled["returncode"], filled["result"].get("status")) not in {
        (0, "filled"),
        (1, "partial"),
    } or filled["result"].get("unresolved_tokens"):
        raise ValueError("sales25 filler failed; see filler-qa")
    result = check_deck(
        root, pptx, output_dir / "qa", owner_id=owner_id, bundle=bundle, python=python
    )
    if filled["result"].get("status") == "partial":
        result["fill_limitations"] = filled["result"].get("limitations", [])
        if result["status"] == "passed":
            result["status"] = "incomplete"
        (output_dir / "qa/result.json").write_bytes(contract.json_bytes(result))
    return result
