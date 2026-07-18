#!/usr/bin/env python3
"""Generate the subject receipt consumed by verify_runtime_evidence.py."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from verify_runtime_evidence import (
    EvidenceError,
    _archive_git_tree_oid,
    _archive_inventory,
    _load_json,
    _sha256,
    _timestamp,
    _tree_digest,
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
    parser.add_argument("--review-base-ref", required=True)
    parser.add_argument("--review-base-oid", required=True)
    parser.add_argument("--merge-base-oid", required=True)
    parser.add_argument("--build-context", type=Path, required=True)
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
    source_tree_sha256, source_inventory, tracked_members = _archive_inventory(
        evidence_dir / "source-tracked.tar"
    )
    context_tree_sha256, context_inventory, context_members = _archive_inventory(
        evidence_dir / "build-context.tar"
    )
    actual_context_sha256, actual_context_files = _tree_digest(args.build_context.resolve())
    if actual_context_sha256 != context_tree_sha256 or actual_context_files != len(
        context_inventory
    ):
        raise EvidenceError("retained build context does not match the context used for build")
    fixture_path = "infra/docker/app-html-runtime-fixture.html"
    baked_path = "src/teamagent/connect_web/static/app.html"
    fixture = source_inventory.get(fixture_path)
    if fixture is None:
        raise EvidenceError("tracked app HTML fixture is missing")
    expected_context = dict(source_inventory)
    expected_context[baked_path] = fixture
    if context_inventory != expected_context:
        raise EvidenceError("build context materialization is not exact")
    git_tree = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", f"{args.head}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if _archive_git_tree_oid(evidence_dir / "source-tracked.tar") != git_tree:
        raise EvidenceError("retained source archive does not match the expected Git tree")
    receipt = {
        "schema_version": "2",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "git": {
            "head": args.head,
            "branch": args.branch,
            "review_base_ref": args.review_base_ref,
            "review_base_oid": args.review_base_oid,
            "merge_base_oid": args.merge_base_oid,
        },
        "scan_window": {
            "started_at": args.started_at,
            "finished_at": args.finished_at,
        },
        "scanner": scanner,
        "source": {
            "git_tree": git_tree,
            "archive_sha256": _sha256(evidence_dir / "source-tracked.tar"),
            "source_tree_sha256": source_tree_sha256,
            "tracked_members": tracked_members,
            "scan_artifact_name": args.source_scan_artifact_name,
            "build_context": {
                "archive_sha256": _sha256(evidence_dir / "build-context.tar"),
                "tree_sha256": context_tree_sha256,
                "members": context_members,
                "materialized_baked_app_html": {
                    "source": fixture_path,
                    "destination": baked_path,
                    "sha256": fixture[2],
                },
            },
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
