#!/usr/bin/env python3
"""Fail closed unless reviewed release evidence accepts an exact source/image."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

_OID = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_URI = re.compile(
    r"^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/"
    r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$"
)
_BASELINE_GATES = {
    "local_runtime_evidence",
    "ecr_basic_scan",
    "fargate_runtime_smoke",
}


class ReleaseGateError(ValueError):
    """Release evidence is missing, unreviewed, stale, or subject-mismatched."""


def verify_release_gate(
    path: Path,
    *,
    expected_sha256: str,
    expected_commit: str,
    expected_tree: str,
    expected_branch: str,
    expected_image_uri: str,
) -> dict[str, Any]:
    if _DIGEST.fullmatch(expected_sha256) is None:
        raise ReleaseGateError("expected release-gate SHA256 must be 64 lowercase hex")
    if _OID.fullmatch(expected_commit) is None or _OID.fullmatch(expected_tree) is None:
        raise ReleaseGateError("expected commit/tree must be full SHA-1 object IDs")
    if not expected_branch or any(character.isspace() for character in expected_branch):
        raise ReleaseGateError("expected branch is invalid")
    if _IMAGE_URI.fullmatch(expected_image_uri) is None:
        raise ReleaseGateError("expected image must be an immutable ECR digest URI")
    raw = path.read_bytes()
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ReleaseGateError("release-gate file SHA256 mismatch")
    try:
        gate = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseGateError("release-gate evidence is invalid JSON") from exc
    if not isinstance(gate, dict) or gate.get("schema_version") != "1":
        raise ReleaseGateError("release-gate schema is invalid")
    if gate.get("decision") != "ACCEPTED":
        raise ReleaseGateError("release decision is not ACCEPTED")
    reviewed = gate.get("reviewed")
    expected_reviewed = {
        "git_commit": expected_commit,
        "git_tree": expected_tree,
        "git_branch": expected_branch,
        "image_uri": expected_image_uri,
    }
    if reviewed != expected_reviewed:
        raise ReleaseGateError("reviewed release subject differs from expected source/image")
    required = gate.get("required_gates")
    if (
        not isinstance(required, list)
        or not required
        or any(not isinstance(name, str) or not name for name in required)
        or len(set(required)) != len(required)
        or not _BASELINE_GATES.issubset(required)
    ):
        raise ReleaseGateError("required release gates are incomplete or duplicated")
    gates = gate.get("gates")
    if not isinstance(gates, dict) or set(gates) != set(required):
        raise ReleaseGateError("release gate result set differs from required_gates")
    for name in required:
        result = gates[name]
        if not isinstance(result, dict) or result.get("status") != "ACCEPTED":
            raise ReleaseGateError(f"{name}: gate status is not ACCEPTED")
        if _DIGEST.fullmatch(str(result.get("evidence_sha256") or "")) is None:
            raise ReleaseGateError(f"{name}: evidence SHA256 is missing")
        subject = result.get("subject")
        if not isinstance(subject, dict):
            raise ReleaseGateError(f"{name}: evidence subject is missing")
        expected_source = {
            "git_commit": expected_commit,
            "git_tree": expected_tree,
            "git_branch": expected_branch,
        }
        if any(subject.get(field) != value for field, value in expected_source.items()):
            raise ReleaseGateError(f"{name}: source subject mismatch")
        image_uri = subject.get("image_uri")
        if name != "local_runtime_evidence":
            if image_uri != expected_image_uri:
                raise ReleaseGateError(f"{name}: image subject mismatch")
        elif image_uri is not None and image_uri != expected_image_uri:
            raise ReleaseGateError(f"{name}: image subject mismatch")
    return {
        "release_gate_sha256": actual_sha256,
        "required_gates": required,
        "git_commit": expected_commit,
        "git_tree": expected_tree,
        "git_branch": expected_branch,
        "image_uri": expected_image_uri,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_gate", type=Path)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--expected-branch", required=True)
    parser.add_argument("--expected-image-uri", required=True)
    args = parser.parse_args()
    try:
        summary = verify_release_gate(
            args.release_gate.resolve(),
            expected_sha256=args.expected_sha256,
            expected_commit=args.expected_commit,
            expected_tree=args.expected_tree,
            expected_branch=args.expected_branch,
            expected_image_uri=args.expected_image_uri,
        )
    except (OSError, ReleaseGateError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, **summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
