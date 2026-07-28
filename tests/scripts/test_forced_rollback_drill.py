"""forced rollback drill controller の状態機械契約テスト。

実 AWS/Terraform には到達させず、controller を一時 repo にコピーして
同階層の authorizer/runtime guard と aggregate validator を fake 化する。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONTROLLER = PROJECT_ROOT / "infra" / "deploy" / "forced_rollback_drill.sh"

DRILL_ID = "11111111-1111-4111-8111-111111111111"
OTHER_DRILL_ID = "22222222-2222-4222-8222-222222222222"
ACCOUNT_ID = "718959508629"
REGION = "ap-northeast-1"
ENVIRONMENT = "dev"
INITIAL_APPLIED_AT = 2_000_000_000

CONSUMERS = (
    ("mcp", "teamagent-dev-mcp", "core", "ecs_service"),
    ("connect_web", "teamagent-dev-connect-web", "core", "ecs_service"),
    (
        "canary",
        "teamagent-dev-canary",
        "core",
        "eventbridge_rule_ecs_target",
    ),
    (
        "ingest",
        "teamagent-dev-ingest",
        "core",
        "eventbridge_rule_ecs_target",
    ),
    (
        "morning_digest",
        "teamagent-dev-morning-digest",
        "core",
        "eventbridge_rule_ecs_target",
    ),
    (
        "x_buzz_worker",
        "teamagent-dev-x-buzz-worker",
        "core",
        "lambda_taskdef_arn_environment",
    ),
    (
        "tiktok_acquire",
        "teamagent-dev-tiktok-acquire",
        "media",
        "lambda_taskdef_arn_environment",
    ),
)


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def _write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_bytes(value))
    path.chmod(mode)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _images(*, old: bool) -> dict[str, str]:
    core_digest = "a" * 64 if old else "b" * 64
    media_digest = "c" * 64 if old else "d" * 64
    # OpenClaw is an unchanged consumer in this MCP release.
    openclaw_digest = "e" * 64
    registry = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"
    return {
        "mcp": f"{registry}/teamagent-mcp@sha256:{core_digest}",
        "x_buzz": f"{registry}/teamagent-mcp@sha256:{core_digest}",
        "tiktok": f"{registry}/teamagent-media-worker@sha256:{media_digest}",
        "openclaw": f"{registry}/teamagent-openclaw@sha256:{openclaw_digest}",
    }


def _subjects(*, old: bool) -> list[dict[str, str]]:
    images = _images(old=old)
    return [
        {
            "pipeline": "mcp",
            "name": "core",
            "release_repository": "teamagent-mcp",
            "digest": images["mcp"].split("@", 1)[1],
        },
        {
            "pipeline": "mcp",
            "name": "media",
            "release_repository": "teamagent-media-worker",
            "digest": images["tiktok"].split("@", 1)[1],
        },
    ]


def _resources(*, old: bool) -> list[dict[str, Any]]:
    images = _images(old=old)
    revision = 31 if old else 32
    result: list[dict[str, Any]] = []
    for consumer_id, family, subject, activator_type in CONSUMERS:
        image_key = {"core": "mcp", "media": "tiktok"}[subject]
        task_definition_arn = (
            f"arn:aws:ecs:{REGION}:{ACCOUNT_ID}:task-definition/{family}:{revision}"
        )
        identity = {
            "mcp": "teamagent-dev-mcp",
            "connect_web": "teamagent-dev-connect-web",
            "openclaw": "teamagent-dev-openclaw",
            "canary": "teamagent-dev-canary-hourly",
            "ingest": "teamagent-dev-ingest-weekly",
            "morning_digest": "teamagent-dev-morning-digest-weekday",
            "x_buzz_worker": "teamagent-dev-x-buzz-dispatch",
            "tiktok_acquire": "teamagent-dev-tiktok-acquire-dispatch",
        }[consumer_id]
        terraform_address = {
            "mcp": "aws_ecs_task_definition.mcp",
            "connect_web": "aws_ecs_task_definition.connect_web[0]",
            "canary": "aws_ecs_task_definition.canary[0]",
            "ingest": "aws_ecs_task_definition.ingest[0]",
            "morning_digest": "aws_ecs_task_definition.morning_digest[0]",
            "x_buzz_worker": "aws_ecs_task_definition.x_buzz_worker[0]",
            "tiktok_acquire": "aws_ecs_task_definition.tiktok_acquire[0]",
        }[consumer_id]
        if activator_type == "ecs_service":
            activation_state: Any = 1
        elif activator_type == "eventbridge_rule_ecs_target":
            activation_state = "DISABLED" if consumer_id in {"canary", "ingest"} else "ENABLED"
        else:
            activation_state = True
        activation = {
            "type": activator_type,
            "identity": identity,
            "state": activation_state,
        }
        result.append(
            {
                "consumer_id": consumer_id,
                "image": images[image_key],
                "pipeline": "mcp",
                "subject": subject,
                "terraform_address": terraform_address,
                "task_definition_arn": task_definition_arn,
                "activation": activation,
            }
        )
    return sorted(result, key=lambda item: item["consumer_id"])


def _live_contract(*, old: bool) -> dict[str, Any]:
    return {
        "images": _images(old=old),
        "resources": _resources(old=old),
        "rule_states": {
            "canary": "DISABLED",
            "ingest": "DISABLED",
            "morning": "ENABLED",
        },
    }


@dataclass
class DrillHarness:
    repo: Path
    controller: Path
    contract: Path
    initial_receipt: Path
    var_file: Path
    drill_dir: Path
    calls: Path
    validator_calls: Path
    old_live: Path
    new_live: Path
    env: dict[str, str]

    def run(
        self,
        *args: str,
        input_text: str | None = None,
        now: int | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if now is not None:
            env["FAKE_NOW_EPOCH"] = str(now)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(self.controller), *args],
            cwd=self.repo,
            env=env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def prepare(
        self,
        *,
        contract: Path | None = None,
        out_dir: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            "prepare",
            "--contract",
            str(contract or self.contract),
            "--initial-apply-receipt",
            str(self.initial_receipt),
            "--var-file",
            str(self.var_file),
            "--out-dir",
            str(out_dir or self.drill_dir),
            now=INITIAL_APPLIED_AT + 60,
        )

    def preflight(self) -> subprocess.CompletedProcess[str]:
        return self.run(
            "preflight",
            "--drill-dir",
            str(self.drill_dir),
            "--targets",
            "old,new",
            now=INITIAL_APPLIED_AT + 120,
        )

    def plan(self, leg: str, *, now: int) -> tuple[subprocess.CompletedProcess[str], str]:
        before_count = len(self._guard_calls("plan"))
        result = self.run(
            "plan-leg",
            "--drill-dir",
            str(self.drill_dir),
            "--leg",
            leg,
            now=now,
        )
        if result.returncode != 0:
            return result, ""
        new_calls = self._guard_calls("plan")[before_count:]
        assert len(new_calls) == 1, self.call_text()
        plan_path = Path(_arg_value(new_calls[0], "--out"))
        plan_sha256 = _sha256(plan_path)
        assert plan_sha256 in result.stdout, (
            "plan-leg は生成した saved plan の exact SHA-256 を stdout に表示する"
        )
        return result, plan_sha256

    def apply(
        self,
        leg: str,
        *,
        approval_leg: str,
        plan_sha256: str,
        now: int,
        drill_id: str = DRILL_ID,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        approval = f"APPROVE {drill_id} {approval_leg} {plan_sha256}\n"
        return self.run(
            "apply-leg",
            "--drill-dir",
            str(self.drill_dir),
            "--leg",
            leg,
            input_text=approval,
            now=now,
            extra_env=extra_env,
        )

    def state(self) -> dict[str, Any]:
        return json.loads((self.drill_dir / "state.json").read_text(encoding="utf-8"))

    def call_text(self) -> str:
        if not self.calls.exists():
            return ""
        return self.calls.read_text(encoding="utf-8")

    def call_lines(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [
            line.split("\t") for line in self.calls.read_text(encoding="utf-8").splitlines() if line
        ]

    def _guard_calls(self, command: str) -> list[list[str]]:
        return [
            line
            for line in self.call_lines()
            if len(line) >= 2 and line[0] == "guard" and line[1] == command
        ]


def _arg_value(call: list[str], option: str) -> str:
    index = call.index(option)
    return call[index + 1]


def _make_contract(
    root: Path,
    *,
    drill_id: str = DRILL_ID,
) -> tuple[Path, Path, Path, Path, Path]:
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    consumer_manifest = inputs / "consumer-manifest.json"
    _write_json(
        consumer_manifest,
        {
            "schema_version": 1,
            "mode": "receipt-required",
            "consumers": [{"consumer_id": item[0]} for item in CONSUMERS],
        },
    )

    guard_receipts: dict[str, dict[str, str]] = {}
    for index, name in enumerate(
        (
            "alarm_delivery",
            "versioning",
            "log_readiness",
            "alarm_migration",
        ),
        start=1,
    ):
        receipt = inputs / f"{name}.json"
        _write_json(
            receipt,
            {
                "kind": f"fake-{name}-receipt",
                "schema_version": 1,
                "sequence": index,
            },
        )
        guard_receipts[name] = {"path": str(receipt), "sha256": _sha256(receipt)}

    targets: dict[str, Any] = {}
    for label, old in (("old", True), ("new", False)):
        candidate_sha = "6" * 64 if old else "7" * 64
        approval_sha = "8" * 64 if old else "9" * 64
        targets[label] = {
            "images": _images(old=old),
            "subjects": _subjects(old=old),
            "resources": _resources(old=old),
            "preflight_migration_id": f"drill-{label}-preflight",
            "runtime_migration_id": f"drill-{label}-runtime",
            "candidate": {
                "receipt_key": (
                    "release-receipts/mcp/" + ("1" if old else "2") * 40 + f"/{candidate_sha}.json"
                ),
                "receipt_version_id": f"{label}-candidate-version",
                "receipt_signature_version_id": f"{label}-candidate-signature-version",
            },
            "approval": {
                "payload_bucket": "teamagent-dev-image-release-evidence",
                "payload_key": f"approvals/{label}/{approval_sha}.json",
                "payload_version_id": f"{label}-approval-version",
                "payload_sha256": approval_sha,
                "signature_bucket": "teamagent-dev-image-release-evidence",
                "signature_key": f"approvals/{label}/{approval_sha}.json.sig",
                "signature_version_id": f"{label}-approval-signature-version",
                "signature_sha256": ("0" * 64 if old else "1" * 64),
            },
            "consumer_manifest": {
                "path": str(consumer_manifest),
                "sha256": _sha256(consumer_manifest),
            },
        }

    contract = inputs / "drill.json"
    _write_json(
        contract,
        {
            "kind": "teamagent.forced-rollback-drill-contract",
            "schema_version": 1,
            "ready": True,
            "blocked_reason": "",
            "drill_id": drill_id,
            "pipeline": "mcp",
            "environment": {
                "account_id": ACCOUNT_ID,
                "region": REGION,
                "name": ENVIRONMENT,
            },
            "limits": {
                "max_start_delay_seconds": 1800,
                "max_old_dwell_seconds": 1200,
            },
            "control": {
                "git_commit": "4" * 40,
                "initial_release_apply_locator": {
                    "path": str(inputs / "initial-release.apply.json"),
                },
            },
            "actors": {
                "initiating_principal": {
                    "arn": (f"arn:aws:iam::{ACCOUNT_ID}:user/forced-rollback-drill-operator"),
                    "user_id": "AIDAEXAMPLEDRILLOPERATOR",
                    "source_identity": "",
                },
                "automation_principals": [
                    (f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-terraform-runtime")
                ],
            },
            "targets": targets,
            "guard_receipts": {
                **guard_receipts,
                "media_cutover": None,
            },
            "evidence": {
                "artifact_manifest": [],
                "integrity": {
                    "kms_key_arn": "",
                    "signature": {},
                    "immutable_object": {},
                },
            },
        },
    )

    initial_receipt = inputs / "initial-release.apply.json"
    _write_json(
        initial_receipt,
        {
            "kind": "terraform-runtime-apply-receipt",
            "schema_version": 7,
            "status": "applied",
            "applied_at_epoch": INITIAL_APPLIED_AT,
            "plan_sha256": "5" * 64,
            "pre_live_contract": _live_contract(old=True),
            "pre_state_contract": {
                "state": {
                    "lineage": "33333333-3333-4333-8333-333333333333",
                    "serial": 99,
                    "address_set_sha256": "a" * 64,
                },
                "task_revisions": {consumer_id: 31 for consumer_id, *_ in CONSUMERS},
            },
            "post_live_contract": _live_contract(old=False),
            "post_state_contract": {
                "state": {
                    "lineage": "33333333-3333-4333-8333-333333333333",
                    "serial": 100,
                    "address_set_sha256": "a" * 64,
                },
                "task_revisions": {consumer_id: 32 for consumer_id, *_ in CONSUMERS},
            },
        },
    )
    var_file = inputs / "terraform.tfvars.json"
    _write_json(var_file, {"environment": ENVIRONMENT})
    old_live = inputs / "old-live.json"
    new_live = inputs / "new-live.json"
    _write_json(old_live, _live_contract(old=True))
    _write_json(new_live, _live_contract(old=False))
    return contract, initial_receipt, var_file, old_live, new_live


def _install_fakes(repo: Path) -> tuple[Path, Path]:
    deploy = repo / "infra" / "deploy"
    codebuild = repo / "infra" / "codebuild"
    fake_bin = repo / "fake-bin"
    calls = repo / "fake-calls.log"
    validator_calls = repo / "validator-calls.log"

    _write_executable(
        deploy / "authorize_image_release.sh",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        {
          printf 'authorize'
          printf '\t%s' "$@"
          printf '\n'
        } >>"$FAKE_DRILL_CALLS"

        channel=""
        gate_vars=""
        while [ "$#" -gt 0 ]; do
          case "$1" in
            --channel) channel="${2:?}"; shift 2 ;;
            --terraform-gate-vars-out) gate_vars="${2:?}"; shift 2 ;;
            --pipeline|--receipt-key|--receipt-version-id|--receipt-signature-version-id|\
            --consumer-manifest|--approval-payload-bucket|--approval-payload-key|\
            --approval-payload-version-id|--approval-payload-sha256|\
            --approval-signature-bucket|--approval-signature-key|\
            --approval-signature-version-id|--approval-signature-sha256)
              shift 2
              ;;
            *) exit 91 ;;
          esac
        done
        [ "$channel" = rollback ] || [ "$channel" = active ] || exit 92
        [ -n "$gate_vars" ] || exit 93
        if [ "${FAKE_AUTH_FAIL_CHANNEL:-}" = "$channel" ]; then
          exit 44
        fi
        mkdir -p "$(dirname -- "$gate_vars")"
        python3 - "$gate_vars" "$channel" <<'PY'
        import json
        import os
        import sys

        output, channel = sys.argv[1:]
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        with os.fdopen(os.open(output, flags, 0o600), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "image_deployment_consumer_manifest": {"channel": channel},
                    "image_release_consumer_receipt_bindings": {"mcp": channel},
                    "image_release_receipt_catalog": {channel: {"version_id": channel}},
                },
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
        PY
        if [ "$channel" = rollback ]; then
          receipt_sha="$(printf '%064d' 2)"
          commit="$(printf '%040d' 2)"
        else
          receipt_sha="$(printf '%064d' 3)"
          commit="$(printf '%040d' 3)"
        fi
        echo "Guarded release authorization completed (no deployment performed):"
        echo "  pipeline=mcp"
        echo "  channel=$channel"
        echo "  commit=$commit"
        echo "  receipt_key=release-receipts/mcp/$commit/$receipt_sha.json"
        echo "  receipt_version_id=${channel}-fresh-version"
        echo "  receipt_signature_key=release-receipts/mcp/$commit/$receipt_sha.json.sig"
        echo "  receipt_signature_version_id=${channel}-fresh-signature-version"
        echo "  terraform_gate_vars=$gate_vars"
        """,
    )

    _write_executable(
        deploy / "terraform_runtime_guard.sh",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        command="${1:-}"
        [ -n "$command" ] || exit 90
        shift
        {
          printf 'guard\t%s' "$command"
          printf '\t%s' "$@"
          printf '\n'
        } >>"$FAKE_DRILL_CALLS"

        out=""
        plan=""
        receipt=""
        migration=""
        var_file=""
        args=("$@")
        while [ "$#" -gt 0 ]; do
          case "$1" in
            --out) out="${2:?}"; shift 2 ;;
            --plan) plan="${2:?}"; shift 2 ;;
            --receipt) receipt="${2:?}"; shift 2 ;;
            --migration|--runtime-migration)
              migration="${2:?}"
              shift 2
              ;;
            --var-file) var_file="${2:?}"; shift 2 ;;
            --runtime-sync) shift ;;
            --preflight-receipt|--alarm-delivery-receipt|\
            --versioning-receipt|--log-readiness-receipt|\
            --alarm-migration-receipt|--media-cutover-receipt|\
            --apply-attempt-id|--media-authorization)
              shift 2
              ;;
            *) exit 94 ;;
          esac
        done
        if [ "${FAKE_GUARD_FAIL_COMMAND:-}" = "$command" ]; then
          exit 45
        fi

        case "$command" in
          snapshot)
            live_path="$FAKE_NEW_LIVE"
            if [ "${FAKE_FINAL_LIVE:-new}" = old ]; then
              live_path="$FAKE_OLD_LIVE"
            fi
            python3 - "$live_path" <<'PY'
        import json
        import sys

        with open(sys.argv[1], encoding="utf-8") as handle:
            live = json.load(handle)
        resources = {
            resource["consumer_id"]: resource
            for resource in live["resources"]
        }

        def emit(name, value):
            print(f"{name} = {json.dumps(value, separators=(',', ':'))}")

        emit("openclaw_image", live["images"]["openclaw"])
        emit("mcp_image", live["images"]["mcp"])
        emit("x_buzz_image", live["images"]["x_buzz"])
        emit("media_worker_image", live["images"]["tiktok"])
        emit(
            "enable_connect_web",
            resources["connect_web"]["activation"]["state"] > 0,
        )
        emit(
            "ingest_rule_enabled",
            resources["ingest"]["activation"]["state"] == "ENABLED",
        )
        emit(
            "morning_digest_rule_enabled",
            resources["morning_digest"]["activation"]["state"] == "ENABLED",
        )
        emit(
            "canary_rule_enabled",
            resources["canary"]["activation"]["state"] == "ENABLED",
        )
        emit(
            "enable_x_research",
            resources["x_buzz_worker"]["activation"]["state"],
        )
        emit(
            "enable_tiktok_acquire",
            resources["tiktok_acquire"]["activation"]["state"],
        )
        PY
            ;;
          preflight)
            [ -n "$out" ] && [ -n "$migration" ] || exit 95
            mkdir -p "$(dirname -- "$out")"
            python3 - "$out" "$migration" "$FAKE_OLD_LIVE" "$FAKE_NEW_LIVE" <<'PY'
        import json
        import os
        import sys

        output, migration, old_live_path, new_live_path = sys.argv[1:]
        with open(
            old_live_path if "old" in migration else new_live_path,
            encoding="utf-8",
        ) as handle:
            live = json.load(handle)
        now = int(os.environ.get("FAKE_NOW_EPOCH", "2000000120"))
        bad = os.environ.get("FAKE_BAD_PREFLIGHT", "")
        with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            json.dump(
                {
                    "kind": "runtime-preflight-receipt",
                    "migration_id": migration,
                    "images": live["images"],
                    "created_at_epoch": now,
                    "expires_at_epoch": now + 7200,
                    "supply_chain": {
                        "main": {
                            "rekor_transparency_log_verified": (
                                bad != "signature"
                            ),
                            "signature_count": 1,
                        }
                    },
                    "profiles": {
                        "main": {
                            "exit_code": 1 if bad == "run-task" else 0,
                            "stopped_reason_code": "EssentialContainerExited",
                            "image": live["images"]["mcp"],
                            "image_digest": live["images"]["mcp"].split("@", 1)[1],
                        }
                    },
                },
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
        PY
            ;;
          plan)
            # A controller mutation that drops --var-file must fail the positive path.
            [ -n "$out" ] && [ -n "$receipt" ] && [ -n "$migration" ] && \
              [ -n "$var_file" ] || exit 97
            mkdir -p "$(dirname -- "$out")"
            python3 - "$out" "$receipt" "$migration" "$var_file" \
              "$FAKE_OLD_LIVE" "$FAKE_NEW_LIVE" <<'PY'
        import hashlib
        import json
        import os
        import sys

        output, receipt_path, migration, var_file, old_live_path, new_live_path = (
            sys.argv[1:]
        )
        rollback = "old" in migration
        with open(old_live_path, encoding="utf-8") as handle:
            old_live = json.load(handle)
        with open(new_live_path, encoding="utf-8") as handle:
            new_live = json.load(handle)
        intent_id = (
            "44444444-4444-4444-8444-444444444444"
            if rollback
            else "55555555-5555-4555-8555-555555555555"
        )
        bad_baseline = os.environ.get("FAKE_BAD_LEG2_BASELINE", "")
        plan = {
            "migration_id": migration,
            "var_file": var_file,
            "image_deployment_intent_id": intent_id,
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        with os.fdopen(os.open(output, flags, 0o600), "w") as handle:
            json.dump(plan, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        plan_sha = hashlib.sha256(open(output, "rb").read()).hexdigest()
        with os.fdopen(os.open(receipt_path, flags, 0o600), "w") as handle:
            json.dump(
                {
                    "kind": "terraform-runtime-plan-receipt",
                    "migration_id": migration,
                    "plan_sha256": plan_sha,
                    "var_file_sha256": hashlib.sha256(
                        open(var_file, "rb").read()
                    ).hexdigest(),
                    "created_at_epoch": int(
                        os.environ.get("FAKE_NOW_EPOCH", "2000000180")
                    ),
                    "image_deployment_intent_id": intent_id,
                    "images": {
                        "live": new_live["images"] if rollback else old_live["images"],
                        "desired": old_live["images"] if rollback else new_live["images"],
                    },
                    "state_contract": {
                        "state": {
                            "lineage": "33333333-3333-4333-8333-333333333333",
                            "serial": (
                                100
                                if rollback or bad_baseline == "serial"
                                else 101
                            ),
                            "address_set_sha256": "a" * 64,
                        },
                        "task_revisions": {
                            resource["consumer_id"]: (
                                32
                                if rollback or bad_baseline == "revisions"
                                else 31
                            )
                            for resource in (
                                new_live["resources"] if rollback else old_live["resources"]
                            )
                        },
                    },
                },
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
        PY
            ;;
          verify)
            [ -f "$plan" ] || exit 98
            if [ -n "$receipt" ]; then [ -f "$receipt" ] || exit 99; fi
            ;;
          apply)
            [ -f "$plan" ] && [ -n "$out" ] || exit 100
            mkdir -p "$(dirname -- "$out")"
            python3 - "$plan" "$out" "$FAKE_OLD_LIVE" "$FAKE_NEW_LIVE" <<'PY'
        import hashlib
        import json
        import os
        import sys

        plan_path, output, old_live_path, new_live_path = sys.argv[1:]
        with open(plan_path, encoding="utf-8") as handle:
            plan = json.load(handle)
        old = "old" in plan["migration_id"]
        with open(old_live_path if old else new_live_path, encoding="utf-8") as handle:
            live = json.load(handle)
        serial = 101 if old else 102
        revision = 31 if old else 32
        attempt_id = (
            "66666666-6666-4666-8666-666666666666"
            if old
            else "77777777-7777-4777-8777-777777777777"
        )
        plan_sha = hashlib.sha256(open(plan_path, "rb").read()).hexdigest()
        bad_gate = os.environ.get("FAKE_BAD_APPLY_GATE", "")
        value = {
            "kind": "terraform-runtime-apply-receipt",
            "schema_version": 7,
            "status": "applied",
            "applied_at_epoch": int(os.environ.get("FAKE_NOW_EPOCH", "2000000300")),
            "plan_sha256": plan_sha,
            "image_deployment_intent_id": (
                "88888888-8888-4888-8888-888888888888"
                if bad_gate == "intent"
                else plan["image_deployment_intent_id"]
            ),
            "apply_attempt_id": attempt_id,
            "post_live_contract": live,
            "post_state_contract": {
                "state": {
                    "lineage": "33333333-3333-4333-8333-333333333333",
                    "serial": serial,
                    "address_set_sha256": "a" * 64,
                },
                "task_revisions": {
                    resource["consumer_id"]: revision
                    for resource in live["resources"]
                },
            },
            "ecs_service_saga_receipt": {
                "stage": "APPLIED",
                "steady": True,
            },
            "ecs_service_saga_verification_receipt": {
                "stage": (
                    "APPLIED" if bad_gate == "steady" else "VERIFIED_APPLIED"
                ),
                "steady": True,
                "apply_attempt_id": attempt_id,
                "plan_sha256": plan_sha,
                "resources": live["resources"],
            },
            "post_apply_service_probe": {
                "kind": "teamagent-post-apply-service-probe-receipt",
                "apply_attempt_id": attempt_id,
                "task": {"exit_code": 0},
                "result": {
                    "checks": [False] if bad_gate == "run-task" else [True, True]
                },
            },
            "openclaw_rollout_result": {
                "passed": bad_gate != "dm",
                "applyAttemptId": attempt_id,
            },
            "deployment_finalization_receipt": {
                "state": "PENDING" if bad_gate == "finalizer" else "APPLIED",
                "apply_attempt_id": attempt_id,
                "plan_sha256": plan_sha,
            },
        }
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        with os.fdopen(os.open(output, flags, 0o600), "w") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        PY
            ;;
          *)
            exit 101
            ;;
        esac
        """,
    )

    _write_executable(
        fake_bin / "date",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        if [ "$#" -eq 1 ] && [ "$1" = "+%s" ] && [ -n "${FAKE_NOW_EPOCH:-}" ]; then
          printf '%s\n' "$FAKE_NOW_EPOCH"
        else
          exec /bin/date "$@"
        fi
        """,
    )
    for name in ("aws", "terraform"):
        _write_executable(
            fake_bin / name,
            f"""
            #!/usr/bin/env bash
            set -euo pipefail
            printf 'FORBIDDEN-{name}' >>"$FAKE_DRILL_CALLS"
            exit 103
            """,
        )

    codebuild.mkdir(parents=True, exist_ok=True)
    (codebuild / "teamagent_release_approval.py").write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import json


            def canonical_json_bytes(value: object) -> bytes:
                return (
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    + "\\n"
                ).encode()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (codebuild / "forced_rollback_drill_evidence.py").write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import json
            import os


            def validate_drill_evidence(
                aggregate: object,
                expected: object,
            ) -> dict:
                if not isinstance(aggregate, dict):
                    raise ValueError("aggregate must be an object")
                if not isinstance(expected, dict) or set(expected) != {
                    "git_commit",
                    "drill_contract_sha256",
                    "initial_release_apply",
                    "initial_release_verified_at_utc",
                    "scope",
                }:
                    raise ValueError("trusted expected bindings are required")
                if aggregate.get("kind") != "teamagent.forced-rollback-drill":
                    raise ValueError("wrong aggregate kind")
                if aggregate.get("status") not in {
                    "PASSED",
                    "FAILED",
                    "RECONCILE_REQUIRED",
                }:
                    raise ValueError("wrong aggregate status")
                legs = aggregate.get("legs")
                if not isinstance(legs, list) or [
                    leg.get("name") for leg in legs
                ] != ["rollback_to_previous", "restore_active"]:
                    raise ValueError("both ordered legs are required")
                if aggregate["status"] == "PASSED":
                    if [leg.get("result") for leg in legs] != [
                        "PASSED",
                        "PASSED",
                    ]:
                        raise ValueError("PASSED requires two passed legs")
                    terminal = aggregate.get("safe_terminal_state", {})
                    if terminal.get("classification") != "INITIAL_NEW":
                        raise ValueError("PASSED requires initial-new terminal state")
                    if terminal.get("live_snapshot") != aggregate.get(
                        "baseline", {}
                    ).get("initial_new"):
                        raise ValueError("terminal state is not exact initial new")
                for key in (
                    "git_commit",
                    "drill_contract_sha256",
                    "initial_release_apply",
                    "initial_release_verified_at_utc",
                ):
                    if aggregate.get("control", {}).get(key) != expected[key]:
                        raise ValueError(f"untrusted control binding: {key}")
                if aggregate.get("scope") != expected["scope"]:
                    raise ValueError("aggregate scope differs from trusted scope")
                if aggregate.get("scope") is expected["scope"]:
                    raise ValueError("trusted scope aliases aggregate input")
                marker = os.environ["FAKE_VALIDATOR_CALLS"]
                with open(marker, "a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {"aggregate": aggregate, "expected": expected},
                            sort_keys=True,
                        )
                        + "\\n"
                    )
                return dict(aggregate)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return calls, validator_calls


@pytest.fixture
def drill(tmp_path: Path) -> DrillHarness:
    repo = tmp_path / "repo"
    deploy = repo / "infra" / "deploy"
    deploy.mkdir(parents=True)
    controller = deploy / "forced_rollback_drill.sh"
    shutil.copy2(CONTROLLER, controller)
    controller.chmod(0o755)
    registry = repo / "infra" / "codebuild" / "image_deployment_consumers.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        PROJECT_ROOT / "infra" / "codebuild" / "image_deployment_consumers.json",
        registry,
    )
    contract, initial, var_file, old_live, new_live = _make_contract(repo)
    calls, validator_calls = _install_fakes(repo)
    fake_bin = repo / "fake-bin"
    return DrillHarness(
        repo=repo,
        controller=controller,
        contract=contract,
        initial_receipt=initial,
        var_file=var_file,
        drill_dir=repo / "drill-output",
        calls=calls,
        validator_calls=validator_calls,
        old_live=old_live,
        new_live=new_live,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_DRILL_CALLS": str(calls),
            "FAKE_VALIDATOR_CALLS": str(validator_calls),
            "FAKE_OLD_LIVE": str(old_live),
            "FAKE_NEW_LIVE": str(new_live),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )


@pytest.mark.parametrize(
    ("args", "stderr_anchor"),
    [
        ((), "a subcommand is required"),
        (("not-a-command",), "unknown subcommand"),
        (("prepare", "--not-an-option"), "unknown argument"),
    ],
)
def test_missing_unknown_command_and_unknown_argument_fail(
    drill: DrillHarness,
    args: tuple[str, ...],
    stderr_anchor: str,
) -> None:
    result = drill.run(*args)

    assert result.returncode != 0
    assert stderr_anchor in result.stderr
    assert not drill.drill_dir.exists()
    assert drill.call_text() == ""


def test_full_positive_path_has_all_seven_one_way_states(
    drill: DrillHarness,
) -> None:
    prepared = drill.prepare()
    assert prepared.returncode == 0, prepared.stderr
    assert drill.state()["state"] == "PREPARED"
    assert drill.state()["contract_sha256"] == _sha256(drill.contract)
    assert drill.state()["git_commit"] == "4" * 40
    assert drill.call_text() == "", "prepare は ECS/guard/authorizer を呼ばない"

    preflighted = drill.preflight()
    assert preflighted.returncode == 0, preflighted.stderr
    assert drill.state()["state"] == "PREFLIGHTED"
    preflight_calls = drill._guard_calls("preflight")
    assert [_arg_value(call, "--migration") for call in preflight_calls] == [
        "drill-old-preflight",
        "drill-new-preflight",
    ]

    leg1_plan, leg1_sha = drill.plan(
        "rollback-to-previous",
        now=INITIAL_APPLIED_AT + 180,
    )
    assert leg1_plan.returncode == 0, leg1_plan.stderr
    assert drill.state()["state"] == "LEG1_PLANNED"

    leg1_apply = drill.apply(
        "rollback-to-previous",
        approval_leg="rollback",
        plan_sha256=leg1_sha,
        now=INITIAL_APPLIED_AT + 240,
    )
    assert leg1_apply.returncode == 0, leg1_apply.stderr
    leg1_state = drill.state()
    assert leg1_state["state"] == "LEG1_APPLIED"
    assert leg1_state["legs"]["rollback_to_previous"]["approval"] == {
        "approval_id": "OK-1",
        "drill_id": DRILL_ID,
        "action": "rollback",
        "plan_sha256": leg1_sha,
        "approval_text_sha256": hashlib.sha256(
            f"APPROVE {DRILL_ID} rollback {leg1_sha}\n".encode()
        ).hexdigest(),
        "consumed_at_epoch": INITIAL_APPLIED_AT + 240,
    }

    leg2_plan, leg2_sha = drill.plan(
        "restore-active",
        now=INITIAL_APPLIED_AT + 300,
    )
    assert leg2_plan.returncode == 0, leg2_plan.stderr
    planned_state = drill.state()
    assert planned_state["state"] == "LEG2_PLANNED"

    plan_calls = drill._guard_calls("plan")
    assert len(plan_calls) == 2
    assert "--prior-apply-receipt" not in plan_calls[1]
    leg2_plan_state = planned_state["legs"]["restore_active"]["plan"]
    assert leg2_plan_state["state_serial"] == 101
    assert (
        leg2_plan_state["terraform_lineage"]
        == leg1_state["legs"]["rollback_to_previous"]["apply"]["terraform_lineage"]
    )
    assert (
        leg2_plan_state["baseline_task_revisions_sha256"] == planned_state["target_sha256"]["old"]
    )

    leg2_apply = drill.apply(
        "restore-active",
        approval_leg="restore",
        plan_sha256=leg2_sha,
        now=INITIAL_APPLIED_AT + 360,
    )
    assert leg2_apply.returncode == 0, leg2_apply.stderr
    applied_state = drill.state()
    assert applied_state["state"] == "LEG2_APPLIED"
    assert applied_state["legs"]["restore_active"]["approval"]["approval_id"] == "OK-2"
    assert applied_state["legs"]["restore_active"]["approval"]["drill_id"] == DRILL_ID
    assert applied_state["legs"]["restore_active"]["approval"]["plan_sha256"] == leg2_sha
    assert (
        applied_state["legs"]["rollback_to_previous"]["plan"]["intent_id"]
        != applied_state["legs"]["restore_active"]["plan"]["intent_id"]
    )
    assert (
        applied_state["legs"]["rollback_to_previous"]["apply"]["apply_attempt_id"]
        != applied_state["legs"]["restore_active"]["apply"]["apply_attempt_id"]
    )
    assert (
        applied_state["legs"]["rollback_to_previous"]["authorization"]["sha256"]
        != applied_state["legs"]["restore_active"]["authorization"]["sha256"]
    )

    finalized = drill.run(
        "finalize",
        "--drill-dir",
        str(drill.drill_dir),
        now=INITIAL_APPLIED_AT + 420,
    )
    assert finalized.returncode == 0, finalized.stderr
    assert drill.state()["state"] == "FINALIZED"
    assert "PASSED" in finalized.stdout
    assert drill.validator_calls.exists()
    validator_lines = drill.validator_calls.read_text(encoding="utf-8").splitlines()
    assert len(validator_lines) == 1
    validator_call = json.loads(validator_lines[0])
    trusted = validator_call["expected"]
    assert trusted["git_commit"] == "4" * 40
    assert trusted["drill_contract_sha256"] == _sha256(drill.contract)
    assert (
        trusted["initial_release_apply"]
        == json.loads(drill.contract.read_text(encoding="utf-8"))["control"][
            "initial_release_apply_locator"
        ]
    )
    assert trusted["scope"]["pipelines"] == ["mcp"]
    assert [subject["name"] for subject in trusted["scope"]["subjects"]] == [
        "core",
        "media",
    ]
    assert all(
        set(subject)
        == {
            "pipeline",
            "name",
            "release_repository",
            "previous_digest",
            "initial_new_digest",
        }
        for subject in trusted["scope"]["subjects"]
    )
    assert [resource["consumer_id"] for resource in trusted["scope"]["resources"]] == sorted(
        consumer[0] for consumer in CONSUMERS
    )
    assert all(
        set(resource)
        == {
            "consumer_id",
            "terraform_address",
            "pipeline",
            "subject",
            "previous_task_definition_arn",
            "previous_task_revision",
            "initial_new_task_definition_arn",
            "initial_new_task_revision",
        }
        for resource in trusted["scope"]["resources"]
    )

    authorize_calls = [line for line in drill.call_lines() if line[0] == "authorize"]
    assert [_arg_value(call, "--channel") for call in authorize_calls] == [
        "rollback",
        "active",
    ]
    assert [call[1] for call in drill.call_lines() if call[0] == "guard"] == [
        "preflight",
        "preflight",
        "plan",
        "verify",
        "apply",
        "plan",
        "verify",
        "apply",
        "snapshot",
    ]


def test_prepare_rejects_old_not_bound_as_initial_release_pre_live(
    drill: DrillHarness,
) -> None:
    receipt = json.loads(drill.initial_receipt.read_text(encoding="utf-8"))
    receipt["pre_live_contract"] = _live_contract(old=False)
    receipt["pre_state_contract"]["task_revisions"] = {
        consumer_id: 32 for consumer_id, *_ in CONSUMERS
    }
    _write_json(drill.initial_receipt, receipt)

    result = drill.prepare()

    assert result.returncode != 0
    assert "previous-old and initial-new targets" in result.stderr
    assert not drill.drill_dir.exists()
    assert drill.call_text() == ""


def test_plan_leg_rejects_skipped_preflight_without_calling_collaborators(
    drill: DrillHarness,
) -> None:
    assert drill.prepare().returncode == 0
    calls_before = drill.call_text()

    result = drill.run(
        "plan-leg",
        "--drill-dir",
        str(drill.drill_dir),
        "--leg",
        "rollback-to-previous",
    )

    assert result.returncode != 0
    assert "expected PREFLIGHTED, found PREPARED" in result.stderr
    assert drill.state()["state"] == "PREPARED"
    assert drill.call_text() == calls_before


@pytest.mark.parametrize("bad_gate", ["signature", "run-task"])
def test_preflight_rejects_failed_signature_or_run_task_evidence(
    drill: DrillHarness,
    bad_gate: str,
) -> None:
    assert drill.prepare().returncode == 0

    result = drill.run(
        "preflight",
        "--drill-dir",
        str(drill.drill_dir),
        "--targets",
        "old,new",
        now=INITIAL_APPLIED_AT + 120,
        extra_env={"FAKE_BAD_PREFLIGHT": bad_gate},
    )

    assert result.returncode != 0
    assert "preflight receipt does not bind the target" in result.stderr
    assert drill.state()["state"] == "RECOVERY_REQUIRED"
    assert drill.state()["failures"][-1]["phase"] == "preflight"
    assert drill._guard_calls("plan") == []
    assert drill._guard_calls("apply") == []


def test_restore_plan_rejected_until_rollback_apply_completed(
    drill: DrillHarness,
) -> None:
    assert drill.prepare().returncode == 0
    assert drill.preflight().returncode == 0
    leg1_plan, _ = drill.plan(
        "rollback-to-previous",
        now=INITIAL_APPLIED_AT + 180,
    )
    assert leg1_plan.returncode == 0
    calls_before = drill.call_text()

    result = drill.run(
        "plan-leg",
        "--drill-dir",
        str(drill.drill_dir),
        "--leg",
        "restore-active",
    )

    assert result.returncode != 0
    assert "expected LEG1_APPLIED, found LEG1_PLANNED" in result.stderr
    assert drill.state()["state"] == "LEG1_PLANNED"
    assert drill.call_text() == calls_before


@pytest.mark.parametrize("bad_baseline", ["serial", "revisions"])
def test_restore_plan_must_bind_rollback_post_apply_state_and_revisions(
    drill: DrillHarness,
    bad_baseline: str,
) -> None:
    assert drill.prepare().returncode == 0
    assert drill.preflight().returncode == 0
    planned1, plan1_sha = drill.plan(
        "rollback-to-previous",
        now=INITIAL_APPLIED_AT + 180,
    )
    assert planned1.returncode == 0
    assert (
        drill.apply(
            "rollback-to-previous",
            approval_leg="rollback",
            plan_sha256=plan1_sha,
            now=INITIAL_APPLIED_AT + 240,
        ).returncode
        == 0
    )

    result = drill.run(
        "plan-leg",
        "--drill-dir",
        str(drill.drill_dir),
        "--leg",
        "restore-active",
        now=INITIAL_APPLIED_AT + 300,
        extra_env={"FAKE_BAD_LEG2_BASELINE": bad_baseline},
    )

    assert result.returncode != 0
    assert "plan receipt does not bind the exact baseline and target" in result.stderr
    assert drill.state()["state"] == "RECOVERY_REQUIRED"
    assert drill.state()["legs"]["restore_active"]["status"] == "FAILED"
    assert len(drill._guard_calls("apply")) == 1


@pytest.mark.parametrize("artifact", ["preflight", "consumer-manifest"])
def test_plan_rejects_tampered_copied_input_artifacts(
    drill: DrillHarness,
    artifact: str,
) -> None:
    assert drill.prepare().returncode == 0
    assert drill.preflight().returncode == 0
    if artifact == "preflight":
        path = drill.drill_dir / "preflight" / "old.json"
    else:
        path = drill.drill_dir / "inputs" / "old.consumer-manifest.json"
    path.write_text("{}\n", encoding="utf-8")
    calls_before = drill.call_text()

    result = drill.run(
        "plan-leg",
        "--drill-dir",
        str(drill.drill_dir),
        "--leg",
        "rollback-to-previous",
        now=INITIAL_APPLIED_AT + 180,
    )

    assert result.returncode != 0
    assert "SHA-256 does not match" in result.stderr
    assert drill.state()["state"] == "PREFLIGHTED"
    assert drill.call_text() == calls_before


def test_same_leg_apply_cannot_be_replayed(
    drill: DrillHarness,
) -> None:
    assert drill.prepare().returncode == 0
    assert drill.preflight().returncode == 0
    planned, plan_sha = drill.plan(
        "rollback-to-previous",
        now=INITIAL_APPLIED_AT + 180,
    )
    assert planned.returncode == 0
    first = drill.apply(
        "rollback-to-previous",
        approval_leg="rollback",
        plan_sha256=plan_sha,
        now=INITIAL_APPLIED_AT + 240,
    )
    assert first.returncode == 0, first.stderr
    calls_before = drill.call_text()

    replay = drill.apply(
        "rollback-to-previous",
        approval_leg="rollback",
        plan_sha256=plan_sha,
        now=INITIAL_APPLIED_AT + 250,
    )

    assert replay.returncode != 0
    assert "expected LEG1_PLANNED, found LEG1_APPLIED" in replay.stderr
    assert drill.state()["state"] == "LEG1_APPLIED"
    assert drill.call_text() == calls_before
    assert len(drill._guard_calls("apply")) == 1


@pytest.mark.parametrize(
    "bad_gate",
    ["intent", "steady", "run-task", "dm", "finalizer"],
)
def test_apply_never_marks_leg_applied_without_all_pre_finalization_gates(
    drill: DrillHarness,
    bad_gate: str,
) -> None:
    assert drill.prepare().returncode == 0
    assert drill.preflight().returncode == 0
    planned, plan_sha = drill.plan(
        "rollback-to-previous",
        now=INITIAL_APPLIED_AT + 180,
    )
    assert planned.returncode == 0

    result = drill.apply(
        "rollback-to-previous",
        approval_leg="rollback",
        plan_sha256=plan_sha,
        now=INITIAL_APPLIED_AT + 240,
        extra_env={"FAKE_BAD_APPLY_GATE": bad_gate},
    )

    assert result.returncode != 0
    assert "does not prove all pre-finalization leg gates" in result.stderr
    state = drill.state()
    assert state["state"] == "RECOVERY_REQUIRED"
    assert state["legs"]["rollback_to_previous"]["status"] == "FAILED"
    assert state["failures"][-1]["phase"] == "apply"
    assert len(drill._guard_calls("apply")) == 1


@pytest.mark.parametrize(
    "damage",
    ["missing", "broken-json", "unknown-state", "unexpected-key"],
)
def test_missing_corrupt_or_unknown_state_fails_closed(
    drill: DrillHarness,
    damage: str,
) -> None:
    assert drill.prepare().returncode == 0
    state_path = drill.drill_dir / "state.json"
    if damage == "missing":
        state_path.unlink()
    elif damage == "broken-json":
        state_path.write_text("{", encoding="utf-8")
    else:
        state = drill.state()
        if damage == "unknown-state":
            state["state"] = "UNREVIEWED_STATE"
        else:
            state["unexpected"] = True
        _write_json(state_path, state)
    calls_before = drill.call_text()

    result = drill.preflight()

    assert result.returncode != 0
    if damage == "missing":
        assert "state.json is missing" in result.stderr
    else:
        assert "state.json is corrupt or has an unexpected state" in result.stderr
    assert drill.call_text() == calls_before


def test_prepare_cannot_overwrite_interrupted_drill_with_another_id(
    drill: DrillHarness,
) -> None:
    assert drill.prepare().returncode == 0
    state_path = drill.drill_dir / "state.json"
    original_state = state_path.read_bytes()
    other_contract, *_ = _make_contract(
        drill.repo / "other-contract-root",
        drill_id=OTHER_DRILL_ID,
    )

    result = drill.prepare(contract=other_contract)

    assert result.returncode != 0
    assert "output drill directory already exists" in result.stderr
    assert state_path.read_bytes() == original_state
    assert drill.state()["drill_id"] == DRILL_ID


def test_approval_is_bound_to_exact_plan_sha(
    drill: DrillHarness,
) -> None:
    assert drill.prepare().returncode == 0
    assert drill.preflight().returncode == 0
    planned, plan_sha = drill.plan(
        "rollback-to-previous",
        now=INITIAL_APPLIED_AT + 180,
    )
    assert planned.returncode == 0
    wrong_sha = ("f" if not plan_sha.startswith("f") else "e") + plan_sha[1:]
    calls_before = drill.call_text()

    rejected = drill.apply(
        "rollback-to-previous",
        approval_leg="rollback",
        plan_sha256=wrong_sha,
        now=INITIAL_APPLIED_AT + 240,
    )

    assert rejected.returncode != 0
    assert "OK-1 approval does not bind" in rejected.stderr
    assert drill.state()["state"] == "LEG1_PLANNED"
    assert drill.call_text() == calls_before
    assert len(drill._guard_calls("apply")) == 0


def test_rollback_approval_cannot_be_reused_for_restore(
    drill: DrillHarness,
) -> None:
    assert drill.prepare().returncode == 0
    assert drill.preflight().returncode == 0
    planned1, plan1_sha = drill.plan(
        "rollback-to-previous",
        now=INITIAL_APPLIED_AT + 180,
    )
    assert planned1.returncode == 0
    assert (
        drill.apply(
            "rollback-to-previous",
            approval_leg="rollback",
            plan_sha256=plan1_sha,
            now=INITIAL_APPLIED_AT + 240,
        ).returncode
        == 0
    )
    planned2, plan2_sha = drill.plan(
        "restore-active",
        now=INITIAL_APPLIED_AT + 300,
    )
    assert planned2.returncode == 0
    calls_before = drill.call_text()

    # drill ID と leg2 plan SHA は正しい。approval leg/ID だけを OK-1 相当に戻す。
    rejected = drill.apply(
        "restore-active",
        approval_leg="rollback",
        plan_sha256=plan2_sha,
        now=INITIAL_APPLIED_AT + 360,
    )

    assert rejected.returncode != 0
    assert "OK-2 approval does not bind" in rejected.stderr
    assert drill.state()["state"] == "LEG2_PLANNED"
    assert drill.call_text() == calls_before
    assert len(drill._guard_calls("apply")) == 1


def test_old_residence_over_twenty_minutes_enters_recovery_required(
    drill: DrillHarness,
) -> None:
    assert drill.prepare().returncode == 0
    assert drill.preflight().returncode == 0
    planned1, plan1_sha = drill.plan(
        "rollback-to-previous",
        now=INITIAL_APPLIED_AT + 180,
    )
    assert planned1.returncode == 0
    leg1_applied_at = INITIAL_APPLIED_AT + 240
    assert (
        drill.apply(
            "rollback-to-previous",
            approval_leg="rollback",
            plan_sha256=plan1_sha,
            now=leg1_applied_at,
        ).returncode
        == 0
    )
    planned2, plan2_sha = drill.plan(
        "restore-active",
        now=leg1_applied_at + 60,
    )
    assert planned2.returncode == 0
    apply_calls_before = len(drill._guard_calls("apply"))

    timed_out = drill.apply(
        "restore-active",
        approval_leg="restore",
        plan_sha256=plan2_sha,
        now=leg1_applied_at + 1201,
    )

    assert timed_out.returncode != 0
    assert "old dwell exceeded 20 minutes" in timed_out.stderr
    state = drill.state()
    assert state["state"] == "RECOVERY_REQUIRED"
    assert len(drill._guard_calls("apply")) == apply_calls_before
    assert state["old_dwell"]["deadline_epoch"] - state["old_dwell"]["started_at_epoch"] == 1200
    assert state["old_dwell"]["exceeded_at_epoch"] == leg1_applied_at + 1201
    assert state["legs"]["restore_active"]["status"] == "FAILED"
    assert state["failures"][-1]["phase"] == "old-dwell"


def test_rollback_apply_after_thirty_minute_start_limit_requires_recovery(
    drill: DrillHarness,
) -> None:
    assert drill.prepare().returncode == 0
    assert drill.preflight().returncode == 0
    planned, plan_sha = drill.plan(
        "rollback-to-previous",
        now=INITIAL_APPLIED_AT + 180,
    )
    assert planned.returncode == 0
    apply_calls_before = len(drill._guard_calls("apply"))

    late = drill.apply(
        "rollback-to-previous",
        approval_leg="rollback",
        plan_sha256=plan_sha,
        now=INITIAL_APPLIED_AT + 1801,
    )

    assert late.returncode != 0
    assert "30 minutes" in late.stderr
    assert drill.state()["state"] == "RECOVERY_REQUIRED"
    assert drill.state()["failures"][-1]["phase"] == "approval"
    assert len(drill._guard_calls("apply")) == apply_calls_before


def test_failed_leg_history_prevents_later_passed_finalize(
    drill: DrillHarness,
) -> None:
    assert drill.prepare().returncode == 0
    assert drill.preflight().returncode == 0
    planned1, plan1_sha = drill.plan(
        "rollback-to-previous",
        now=INITIAL_APPLIED_AT + 180,
    )
    assert planned1.returncode == 0
    assert (
        drill.apply(
            "rollback-to-previous",
            approval_leg="rollback",
            plan_sha256=plan1_sha,
            now=INITIAL_APPLIED_AT + 240,
        ).returncode
        == 0
    )
    planned2, plan2_sha = drill.plan(
        "restore-active",
        now=INITIAL_APPLIED_AT + 300,
    )
    assert planned2.returncode == 0
    assert (
        drill.apply(
            "restore-active",
            approval_leg="restore",
            plan_sha256=plan2_sha,
            now=INITIAL_APPLIED_AT + 360,
        ).returncode
        == 0
    )

    # A recovered exact-new live state must not erase an earlier failed leg.
    state_path = drill.drill_dir / "state.json"
    historical_failure = drill.state()
    historical_failure["failures"].append(
        {
            "at_epoch": INITIAL_APPLIED_AT + 250,
            "leg": "rollback-to-previous",
            "phase": "steady",
            "reason": "recorded historical gate failure",
        }
    )
    _write_json(state_path, historical_failure)

    finalized = drill.run(
        "finalize",
        "--drill-dir",
        str(drill.drill_dir),
        now=INITIAL_APPLIED_AT + 420,
    )

    assert finalized.returncode != 0
    assert "PASSED" not in finalized.stdout
    assert drill.state()["state"] == "FINALIZED"
    assert drill.state()["final_status"] == "FAILED"
    aggregate = json.loads((drill.drill_dir / "aggregate.json").read_text(encoding="utf-8"))
    assert aggregate["status"] == "FAILED"


def test_finalize_rejects_final_receipt_that_is_not_exact_initial_new(
    drill: DrillHarness,
) -> None:
    assert drill.prepare().returncode == 0
    assert drill.preflight().returncode == 0
    planned1, plan1_sha = drill.plan(
        "rollback-to-previous",
        now=INITIAL_APPLIED_AT + 180,
    )
    assert planned1.returncode == 0
    assert (
        drill.apply(
            "rollback-to-previous",
            approval_leg="rollback",
            plan_sha256=plan1_sha,
            now=INITIAL_APPLIED_AT + 240,
        ).returncode
        == 0
    )
    planned2, plan2_sha = drill.plan(
        "restore-active",
        now=INITIAL_APPLIED_AT + 300,
    )
    assert planned2.returncode == 0
    assert (
        drill.apply(
            "restore-active",
            approval_leg="restore",
            plan_sha256=plan2_sha,
            now=INITIAL_APPLIED_AT + 360,
        ).returncode
        == 0
    )

    state_path = drill.drill_dir / "state.json"
    state = drill.state()
    receipt_path = Path(state["legs"]["restore_active"]["apply"]["path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    old_live = json.loads(drill.old_live.read_text(encoding="utf-8"))
    receipt["post_live_contract"] = old_live
    receipt["post_state_contract"]["task_revisions"] = {
        resource["consumer_id"]: 31 for resource in old_live["resources"]
    }
    receipt["ecs_service_saga_verification_receipt"]["resources"] = old_live["resources"]
    _write_json(receipt_path, receipt)
    state["legs"]["restore_active"]["apply"]["sha256"] = _sha256(receipt_path)
    _write_json(state_path, state)

    finalized = drill.run(
        "finalize",
        "--drill-dir",
        str(drill.drill_dir),
        now=INITIAL_APPLIED_AT + 420,
    )

    assert finalized.returncode != 0
    assert "PASSED" not in finalized.stdout
    assert drill.state()["state"] == "FINALIZED"
    assert drill.state()["final_status"] == "RECONCILE_REQUIRED"


def test_finalize_rejects_fresh_live_drift_after_restore_receipt(
    drill: DrillHarness,
) -> None:
    assert drill.prepare().returncode == 0
    assert drill.preflight().returncode == 0
    planned1, plan1_sha = drill.plan(
        "rollback-to-previous",
        now=INITIAL_APPLIED_AT + 180,
    )
    assert planned1.returncode == 0
    assert (
        drill.apply(
            "rollback-to-previous",
            approval_leg="rollback",
            plan_sha256=plan1_sha,
            now=INITIAL_APPLIED_AT + 240,
        ).returncode
        == 0
    )
    planned2, plan2_sha = drill.plan(
        "restore-active",
        now=INITIAL_APPLIED_AT + 300,
    )
    assert planned2.returncode == 0
    assert (
        drill.apply(
            "restore-active",
            approval_leg="restore",
            plan_sha256=plan2_sha,
            now=INITIAL_APPLIED_AT + 360,
        ).returncode
        == 0
    )

    finalized = drill.run(
        "finalize",
        "--drill-dir",
        str(drill.drill_dir),
        now=INITIAL_APPLIED_AT + 420,
        extra_env={"FAKE_FINAL_LIVE": "old"},
    )

    assert finalized.returncode != 0
    assert "PASSED" not in finalized.stdout
    assert drill.state()["state"] == "FINALIZED"
    assert drill.state()["final_status"] == "RECONCILE_REQUIRED"
    assert not (drill.drill_dir / "final-live.snapshot.json").exists()
    assert drill._guard_calls("snapshot")


def test_controller_has_no_unguarded_mutation_path_and_plan_keeps_var_file() -> None:
    body = CONTROLLER.read_text(encoding="utf-8")
    executable = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))

    assert "set -euo pipefail" in body
    assert os.access(CONTROLLER, os.X_OK)
    assert "validate_drill_evidence(aggregate, expected)" in body
    assert "validate_drill_aggregate" not in body
    guard_plan_marker = 'bash "$TERRAFORM_RUNTIME_GUARD" plan'
    assert executable.count(guard_plan_marker) == 1
    plan_start = executable.index(guard_plan_marker)
    plan_end = executable.index("; then", plan_start)
    plan_block = executable[plan_start:plan_end]
    assert '--var-file "$merged_var"' in plan_block
    assert plan_block.index("--var-file") < plan_block.index("--out")

    assert re.search(r"\bterraform\s+(?:-[^\s]+\s+)*apply\b", executable) is None
    assert re.search(r"\baws\s+ecs\s+update-service\b", executable) is None
    assert "--restore-and-verify" not in executable
