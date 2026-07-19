#!/usr/bin/env python3
"""Seal or verify an immutable EC2 worker release tree."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_APPLICATION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_APPLICATION_ROOT / "src"))

from teamagent.worker_release import (  # noqa: E402
    WorkerReleaseError,
    seal_release,
    verify_release,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    seal = commands.add_parser("seal")
    seal.add_argument("--root", type=Path, required=True)
    location = seal.add_mutually_exclusive_group(required=True)
    location.add_argument("--final-path", type=Path)
    location.add_argument("--final-root", type=Path)
    seal.add_argument("--executable", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--expected-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "seal":
            tree_digest, executable_digest = seal_release(
                args.root,
                final_path=args.final_path,
                final_root=args.final_root,
                executable=args.executable,
            )
            result = {
                "executable_sha256": executable_digest,
                "ok": True,
                "final_release": str(
                    args.final_path
                    if args.final_path is not None
                    else args.final_root / tree_digest
                ),
                "release_tree_sha256": tree_digest,
            }
        else:
            result = {
                "ok": verify_release(args.root, expected_sha256=args.expected_sha256),
            }
    except (OSError, WorkerReleaseError):
        result = {"ok": False}
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
