#!/usr/bin/env python3
"""Terraform local-exec bridge for the live HMAC pre-registration gate."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT))
sys.path.insert(0, str(_REPOSITORY_ROOT / "src"))

from scripts.hmac_rollout_gate import (  # noqa: E402
    DeploymentIntent,
    LiveRolloutGate,
    RolloutGateError,
    _Boto3Factory,
    load_control,
)
from scripts.terraform_hmac_payload import (  # noqa: E402
    candidate_change_from_plan,
    saved_plan_sha256,
    show_saved_plan,
    validate_saved_plan_hmac_files,
    validate_saved_plan_runtime_mutations,
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
        cleanup_domain = os.environ.get("HMAC_CLEANUP_DOMAIN", "")
        if (
            (mode in {"candidate", "rollback"} and cleanup_domain)
            or (mode == "cleanup" and cleanup_domain not in {"mail_action", "report_link"})
            or mode not in {"candidate", "cleanup", "rollback"}
        ):
            raise RolloutGateError("cleanup_domain_invalid")
        if not os.environ.get("TEAMAGENT_APPLY_ATTEMPT_ID"):
            raise RolloutGateError("deployment_intent_missing")
        apply_attempt_id = os.environ["TEAMAGENT_APPLY_ATTEMPT_ID"]
        plan_path = Path(os.environ["TEAMAGENT_SAVED_PLAN_PATH"])
        plan_sha256 = saved_plan_sha256(plan_path)
        plan = show_saved_plan(plan_path)
        validate_saved_plan_runtime_mutations(plan)
        validate_saved_plan_hmac_files(
            plan,
            manifest_path=Path(manifest_path),
            control_path=Path(control_path),
            mode=mode,
            cleanup_domain=cleanup_domain,
        )
        candidate, actions = candidate_change_from_plan(plan, task=task)
        if actions == ("no-op",):
            return 0
        manifest = _load_mapping(manifest_path)
        manifest["now"] = int(time.time())
        gate = LiveRolloutGate(
            control=load_control(_load_mapping(control_path)),
            manifest=manifest,
            clients=_Boto3Factory(),
            deployment_intent=DeploymentIntent(
                plan_sha256=plan_sha256,
                apply_attempt_id=apply_attempt_id,
            ),
        )
        if mode == "cleanup":
            cleanup = gate._active_cleanup()
            if cleanup is None or cleanup.domain != cleanup_domain:
                raise RolloutGateError("cleanup_domain_drift")
            if cleanup.prepared_plan_sha256 != plan_sha256:
                raise RolloutGateError("terraform_plan_drift")
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
