#!/usr/bin/env python3
"""Fail unless two meaningful Trivy reports bind one image at C/H/S=0."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from verify_runtime_evidence import _load_json, verify_trivy_pair


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("vulnerability_report", type=Path)
    parser.add_argument("secret_report", type=Path)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--artifact-name", required=True)
    args = parser.parse_args()

    now = datetime.now(UTC)
    summary = verify_trivy_pair(
        _load_json(args.vulnerability_report),
        _load_json(args.secret_report),
        expected_image_id=args.image_id,
        expected_artifact_name=args.artifact_name,
        scan_started_at=now - timedelta(hours=24),
        scan_finished_at=now + timedelta(minutes=1),
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
