#!/usr/bin/env python3
"""Disabled legacy self-attestable release gate.

Production authorization is accepted only through the immutable KMS-signed
S3 VersionId receipt and one-use saved-plan/intent gate.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "FATAL: legacy operator JSON/hash release authorization is disabled; "
        "use plan_image_release.sh with immutable KMS-signed receipts",
        file=sys.stderr,
    )
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
