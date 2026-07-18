#!/usr/bin/env python3
"""Generate the subject receipt consumed by verify_runtime_evidence.py."""

from __future__ import annotations

import argparse
import json
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from verify_runtime_evidence import (
    _load_json,
    _sha256,
    _timestamp,
    verify_scanner_snapshot,
    verify_trivy_pair,
)


def _image_receipt(
    evidence_dir: Path,
    *,
    name: str,
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    inspect_record = json.loads(
        (evidence_dir / f"{name}-image-inspect.json").read_text(encoding="utf-8")
    )[0]
    image_id = str(inspect_record["Id"])
    labels = inspect_record["Config"]["Labels"]
    parent_digests = {
        key: value
        for key, value in labels.items()
        if key.startswith("io.teamagent.contract.") and key.endswith("-arm64-digest")
    }
    scan = verify_trivy_pair(
        _load_json(evidence_dir / f"{name}-trivy-vulnerability.json"),
        _load_json(evidence_dir / f"{name}-trivy-secret.json"),
        expected_image_id=image_id,
        expected_artifact_name=image_id,
        scan_started_at=started_at,
        scan_finished_at=finished_at,
    )
    return {
        "subject_kind": "local-oci-digest-not-registry-pushed",
        "image_id": image_id,
        "local_repo_digests": sorted(inspect_record["RepoDigests"]),
        "artifact_id": scan["artifact_id"],
        "parent_registry_child_digests": parent_digests,
        "trivy_zero": {
            "critical": scan["critical"],
            "high": scan["high"],
            "secrets": scan["secrets"],
        },
        "explicitly_absent_live_cves": scan["explicitly_absent_live_cves"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence_dir", type=Path)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--source-scan-artifact-name", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--finished-at", required=True)
    args = parser.parse_args()
    evidence_dir = args.evidence_dir.resolve()
    repo_root = args.repo_root.resolve()
    started = _timestamp(args.started_at, field="started_at")
    finished = _timestamp(args.finished_at, field="finished_at")
    scanner = verify_scanner_snapshot(
        _load_json(evidence_dir / "trivy-version.json"),
        scan_started_at=started,
        scan_finished_at=finished,
    )
    with tarfile.open(evidence_dir / "source-tracked.tar", "r:") as archive:
        tracked_members = len(archive.getmembers())
    git_tree = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", f"{args.head}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    receipt = {
        "schema_version": "1",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "git": {
            "head": args.head,
            "branch": args.branch,
        },
        "scan_window": {
            "started_at": args.started_at,
            "finished_at": args.finished_at,
        },
        "scanner": scanner,
        "source": {
            "git_tree": git_tree,
            "archive_sha256": _sha256(evidence_dir / "source-tracked.tar"),
            "tracked_members": tracked_members,
            "scan_artifact_name": args.source_scan_artifact_name,
        },
        "images": {
            name: _image_receipt(
                evidence_dir,
                name=name,
                started_at=started,
                finished_at=finished,
            )
            for name in ("core", "media")
        },
        "external_gates": {
            "ecr_basic_scan": "NOT_RUN_LOCAL_PUSH_PROHIBITED",
            "fargate_runtime_smoke": "NOT_RUN_LOCAL_AWS_ACCESS_PROHIBITED",
        },
    }
    (evidence_dir / "receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
