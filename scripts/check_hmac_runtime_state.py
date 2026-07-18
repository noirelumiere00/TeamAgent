#!/usr/bin/env python3
"""Secret-free worker/ECS HMAC readiness check.

The command emits only a boolean result. Key material, generations, task metadata, and exception
details are deliberately excluded from stdout/stderr.
"""

from __future__ import annotations

import argparse
import json

from teamagent.hmac_durable_state import HmacDurableStateError, require_runtime_startup
from teamagent.hmac_keyring import (
    MAIL_ACTION_MAX_TOKEN_TTL_S,
    REPORT_LINK_MAX_TOKEN_TTL_S,
)

_DOMAINS = {
    "MAIL_ACTION": ("mail_action", MAIL_ACTION_MAX_TOKEN_TTL_S),
    "REPORT_LINK": ("report_link", REPORT_LINK_MAX_TOKEN_TTL_S),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check HMAC durable runtime readiness.")
    parser.add_argument(
        "--domains",
        required=True,
        choices=("MAIL_ACTION", "REPORT_LINK", "MAIL_ACTION,REPORT_LINK"),
    )
    parser.add_argument("--worker-attestation", action="store_true")
    args = parser.parse_args(argv)
    try:
        required = tuple(_DOMAINS[name] for name in args.domains.split(","))
        if args.worker_attestation and args.domains != "MAIL_ACTION,REPORT_LINK":
            raise HmacDurableStateError("worker readiness requires both HMAC domains")
        require_runtime_startup(required, worker_attestation=args.worker_attestation)
        result = {"ok": True}
    except Exception:
        result = {"ok": False}
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
