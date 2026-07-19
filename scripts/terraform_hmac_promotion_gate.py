#!/usr/bin/env python3
"""Apply-time HMAC promotion bridge bound to the production saved-plan intent."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from scripts.hmac_rollout_gate import (
    DeploymentIntent,
    LiveRolloutGate,
    RolloutGateError,
    _Boto3Factory,
    load_control,
)
from scripts.terraform_hmac_payload import (
    saved_plan_sha256,
    show_saved_plan,
    validate_saved_plan_event_target,
    validate_saved_plan_hmac_files,
    validate_saved_plan_runtime_mutations,
)


def _mapping_file(path: str) -> dict[str, object]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise RolloutGateError("gate_unreadable")
    return value


def main() -> int:
    try:
        if os.environ.get("HMAC_GATE_ENABLED") != "true":
            return 0
        if os.environ.get("TEAMAGENT_HMAC_PROMOTION_FROM_TERRAFORM") != "1":
            raise RolloutGateError("deployment_intent_missing")
        plan_path = Path(os.environ["TEAMAGENT_SAVED_PLAN_PATH"])
        if not os.environ.get("TEAMAGENT_APPLY_ATTEMPT_ID") or not plan_path.is_file():
            raise RolloutGateError("deployment_intent_missing")
        task = os.environ["HMAC_GATE_TASK"]
        action = os.environ["HMAC_GATE_ACTION"]
        mode = os.environ.get("HMAC_GATE_MODE", "candidate")
        cleanup_domain = os.environ.get("HMAC_CLEANUP_DOMAIN", "")
        if (
            (mode in {"candidate", "rollback"} and cleanup_domain)
            or (mode == "cleanup" and cleanup_domain not in {"mail_action", "report_link"})
            or mode not in {"candidate", "cleanup", "rollback"}
        ):
            raise RolloutGateError("cleanup_domain_invalid")
        task_definition = os.environ["HMAC_REGISTERED_TASK_ARN"]
        plan_sha256 = saved_plan_sha256(plan_path)
        manifest_path = Path(os.environ["HMAC_PREFLIGHT_MANIFEST"])
        control_path = Path(os.environ["HMAC_ROLLOUT_CONTROL"])
        plan = show_saved_plan(plan_path)
        validate_saved_plan_runtime_mutations(plan)
        validate_saved_plan_hmac_files(
            plan,
            manifest_path=manifest_path,
            control_path=control_path,
            mode=mode,
            cleanup_domain=cleanup_domain,
        )
        target: dict[str, object] | None = None
        if action == "event-transaction" and task == "morning_digest":
            raw_target = json.loads(os.environ["HMAC_EVENT_TARGET_JSON"])
            if type(raw_target) is not dict:
                raise RolloutGateError("scheduled_target_invalid", scope=task)
            target = raw_target
            validate_saved_plan_event_target(
                plan,
                target=target,
                task_definition=task_definition,
                mode=mode,
                cleanup_domain=cleanup_domain,
                manifest_path=manifest_path,
                control_path=control_path,
            )
        manifest = _mapping_file(str(manifest_path))
        manifest["now"] = int(time.time())
        gate = LiveRolloutGate(
            control=load_control(_mapping_file(str(control_path))),
            manifest=manifest,
            clients=_Boto3Factory(),
            deployment_intent=DeploymentIntent(
                plan_sha256=plan_sha256,
                apply_attempt_id=os.environ["TEAMAGENT_APPLY_ATTEMPT_ID"],
            ),
        )
        if mode == "cleanup":
            cleanup = gate._active_cleanup()
            if cleanup is None or cleanup.domain != cleanup_domain:
                raise RolloutGateError("cleanup_domain_drift")
            if cleanup.prepared_plan_sha256 != plan_sha256:
                raise RolloutGateError("terraform_plan_drift")
        if action == "event-transaction" and task == "morning_digest":
            if target is None:
                raise RolloutGateError("scheduled_target_invalid", scope=task)
            gate.event_target_transaction(
                task_definition=task_definition,
                target=target,
                mode=mode,
            )
        elif action == "pre-update":
            gate.pre_update(
                task=task,
                task_definition=task_definition,
                mode=mode,
            )
        elif action == "post-update":
            gate.post_update(
                task=task,
                task_definition=task_definition,
                mode=mode,
            )
        else:
            raise RolloutGateError("unknown_action")
    except RolloutGateError as exc:
        result: dict[str, object] = {"code": exc.code, "ok": False}
        if exc.scope is not None:
            result["scope"] = exc.scope
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 2
    except Exception:
        print('{"code":"gate_client_error","ok":false}')
        return 2
    print('{"code":"ok","ok":true}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
