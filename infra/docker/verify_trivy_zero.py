#!/usr/bin/env python3
"""Fail unless Trivy reports exactly C/H/S=0 for one image."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("Results"), list):
        raise ValueError(f"{path}: unexpected Trivy JSON")
    return value


def _vulnerability_counts(report: dict[str, Any]) -> dict[str, int]:
    counts = {"CRITICAL": 0, "HIGH": 0}
    for result in report["Results"]:
        for vulnerability in result.get("Vulnerabilities") or []:
            severity = str(vulnerability.get("Severity", "")).upper()
            if severity in counts:
                counts[severity] += 1
    return counts


def _secret_count(report: dict[str, Any]) -> int:
    return sum(len(result.get("Secrets") or []) for result in report["Results"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("vulnerability_report", type=Path)
    parser.add_argument("secret_report", type=Path)
    args = parser.parse_args()

    vulnerabilities = _vulnerability_counts(_load(args.vulnerability_report))
    secrets = _secret_count(_load(args.secret_report))
    summary = {
        "critical": vulnerabilities["CRITICAL"],
        "high": vulnerabilities["HIGH"],
        "secrets": secrets,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary == {"critical": 0, "high": 0, "secrets": 0} else 1


if __name__ == "__main__":
    raise SystemExit(main())
