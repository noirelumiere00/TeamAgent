#!/usr/bin/env python3
"""Terraform local-exec bridge for the live HMAC pre-registration gate."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from scripts.hmac_rollout_gate import (
    LiveRolloutGate,
    RolloutGateError,
    _Boto3Factory,
    load_control,
)


def _load_mapping(path: str) -> dict[str, object]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise RolloutGateError("gate_unreadable")
    return value


def main() -> int:
    try:
        if os.environ.get("HMAC_GATE_ENABLED") != "true":
            return 0
        manifest_path = os.environ["HMAC_PREFLIGHT_MANIFEST"]
        control_path = os.environ["HMAC_ROLLOUT_CONTROL"]
        task = os.environ["HMAC_GATE_TASK"]
        mode = os.environ.get("HMAC_GATE_MODE", "candidate")
        candidate = json.loads(os.environ["HMAC_GATE_CANDIDATE_JSON"])
        if type(candidate) is not dict:
            raise RolloutGateError("gate_unreadable")
        manifest = _load_mapping(manifest_path)
        manifest["now"] = int(time.time())
        gate = LiveRolloutGate(
            control=load_control(_load_mapping(control_path)),
            manifest=manifest,
            clients=_Boto3Factory(),
        )
        gate.terraform_pre_register(task=task, definition=candidate, mode=mode)
    except RolloutGateError as exc:
        result: dict[str, object] = {"code": exc.code, "ok": False}
        if exc.scope is not None:
            result["scope"] = exc.scope
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 2
    except Exception:
        print('{"code":"gate_client_error","ok":false}')
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
