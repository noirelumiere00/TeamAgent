from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

import scripts.terraform_hmac_payload as payload_module
from scripts.hmac_rollout_gate import (
    RolloutGateError,
    _canonical_registerable_task_definition,
    _task_artifact_digest,
)
from scripts.terraform_hmac_payload import (
    LIVE_TASK_GATE_ADDRESSES,
    MORNING_PROMOTION_ADDRESSES,
    PRODUCTION_GATE_ADDRESS,
    TASK_ADDRESSES,
    WORKER_ARTIFACT_BINDING_KEYS,
    WORKER_DEPLOY_ADDRESS,
    candidates_from_plan,
    cleanup_worker_bindings_from_plan,
    main,
    task_from_change,
    validate_saved_plan_event_target,
    validate_saved_plan_hmac_files,
    validate_saved_plan_runtime_mutations,
    worker_bindings_from_plan,
)


def _provider_after(*, family: str = "teamagent-dev-mcp") -> dict[str, object]:
    task_arn = f"arn:aws:ecs:ap-northeast-1:123456789012:task-definition/{family}:91"
    return {
        "arn": task_arn,
        "arn_without_revision": task_arn.rsplit(":", maxsplit=1)[0],
        "container_definitions": json.dumps(
            [
                {
                    "name": "teamagent-mcp",
                    "image": (
                        "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/"
                        f"teamagent@sha256:{'1' * 64}"
                    ),
                    "essential": True,
                    "environment": [{"name": "MODE", "value": "candidate"}],
                    "secrets": [],
                }
            ],
            separators=(",", ":"),
            sort_keys=True,
        ),
        "cpu": "1024",
        "enable_fault_injection": False,
        "ephemeral_storage": [{"size_in_gib": 40}],
        "execution_role_arn": "arn:aws:iam::123456789012:role/mcp-execution",
        "family": family,
        "id": family,
        "inference_accelerator": [{"device_name": "device-1", "device_type": "eia2.medium"}],
        "ipc_mode": None,
        "memory": "2048",
        "network_mode": "awsvpc",
        "pid_mode": None,
        "placement_constraints": [
            {"expression": "attribute:ecs.cpu-architecture == arm64", "type": "memberOf"}
        ],
        "proxy_configuration": [
            {
                "container_name": "teamagent-mcp",
                "properties": {"Ignored_UID": "1337", "AppPorts": "8787"},
                "type": "APPMESH",
            }
        ],
        "requires_compatibilities": ["FARGATE"],
        "revision": 91,
        "runtime_platform": [
            {
                "cpu_architecture": "ARM64",
                "operating_system_family": "LINUX",
            }
        ],
        "skip_destroy": False,
        "tags": {"Workload": "mcp"},
        "tags_all": {"Environment": "dev", "Workload": "mcp"},
        "task_role_arn": "arn:aws:iam::123456789012:role/mcp-task",
        "track_latest": False,
        "volume": [
            {
                "configure_at_launch": False,
                "docker_volume_configuration": [],
                "efs_volume_configuration": [
                    {
                        "authorization_config": [
                            {"access_point_id": "fsap-0123456789abcdef0", "iam": "ENABLED"}
                        ],
                        "file_system_id": "fs-0123456789abcdef0",
                        "root_directory": "/",
                        "transit_encryption": "ENABLED",
                        "transit_encryption_port": 2999,
                    }
                ],
                "fsx_windows_file_server_volume_configuration": [],
                "host_path": None,
                "name": "shared-data",
            }
        ],
    }


def test_real_provider_after_value_maps_to_complete_registerable_payload() -> None:
    payload = task_from_change(_provider_after(), task="mcp")

    assert payload == {
        "containerDefinitions": [
            {
                "environment": [{"name": "MODE", "value": "candidate"}],
                "essential": True,
                "image": (
                    f"123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent@sha256:{'1' * 64}"
                ),
                "name": "teamagent-mcp",
                "secrets": [],
            }
        ],
        "cpu": "1024",
        "enableFaultInjection": False,
        "ephemeralStorage": {"sizeInGib": 40},
        "executionRoleArn": "arn:aws:iam::123456789012:role/mcp-execution",
        "family": "teamagent-dev-mcp",
        "inferenceAccelerators": [{"deviceName": "device-1", "deviceType": "eia2.medium"}],
        "memory": "2048",
        "networkMode": "awsvpc",
        "placementConstraints": [
            {
                "expression": "attribute:ecs.cpu-architecture == arm64",
                "type": "memberOf",
            }
        ],
        "proxyConfiguration": {
            "containerName": "teamagent-mcp",
            "properties": {"Ignored_UID": "1337", "AppPorts": "8787"},
            "type": "APPMESH",
        },
        "requiresCompatibilities": ["FARGATE"],
        "runtimePlatform": {
            "cpuArchitecture": "ARM64",
            "operatingSystemFamily": "LINUX",
        },
        "tags": [
            {"key": "Environment", "value": "dev"},
            {"key": "Workload", "value": "mcp"},
        ],
        "taskRoleArn": "arn:aws:iam::123456789012:role/mcp-task",
        "volumes": [
            {
                "configuredAtLaunch": False,
                "efsVolumeConfiguration": {
                    "authorizationConfig": {
                        "accessPointId": "fsap-0123456789abcdef0",
                        "iam": "ENABLED",
                    },
                    "fileSystemId": "fs-0123456789abcdef0",
                    "rootDirectory": "/",
                    "transitEncryption": "ENABLED",
                    "transitEncryptionPort": 2999,
                },
                "name": "shared-data",
            }
        ],
    }
    assert _canonical_registerable_task_definition(payload) == payload


def test_provider_payload_and_live_digest_bind_every_registerable_task_field() -> None:
    payload = task_from_change(_provider_after(), task="mcp")
    base_digest = _task_artifact_digest(payload)
    mutations = (
        ("family", "teamagent-dev-mcp-other"),
        ("taskRoleArn", "arn:aws:iam::123456789012:role/other-task"),
        ("executionRoleArn", "arn:aws:iam::123456789012:role/other-execution"),
        ("cpu", "2048"),
        ("memory", "4096"),
        (
            "runtimePlatform",
            {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
        ),
        ("volumes", []),
        ("tags", [{"key": "Environment", "value": "prod"}]),
    )
    for field, value in mutations:
        changed = copy.deepcopy(payload)
        changed[field] = value
        assert _task_artifact_digest(changed) != base_digest

    response = copy.deepcopy(payload)
    response.update(
        {
            "compatibilities": ["EC2", "FARGATE"],
            "registeredAt": "2026-07-19T00:00:00Z",
            "registeredBy": "arn:aws:iam::123456789012:role/registrar",
            "requiresAttributes": [],
            "revision": 91,
            "status": "ACTIVE",
            "taskDefinitionArn": (
                "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-mcp:91"
            ),
        }
    )
    assert _task_artifact_digest(response) == base_digest
    response["unreviewedRegisterableField"] = True
    with pytest.raises(RolloutGateError, match="task_artifact_incomplete"):
        _task_artifact_digest(response)


def test_cleanup_plan_requires_replacement_for_every_exact_task_candidate() -> None:
    changes = []
    for task, address in TASK_ADDRESSES.items():
        family = {
            "mcp": "teamagent-dev-mcp",
            "connect_web": "teamagent-dev-connect-web",
            "morning_digest": "teamagent-dev-morning-digest",
        }[task]
        changes.append(
            {
                "address": address,
                "change": {
                    "actions": ["no-op"],
                    "after": _provider_after(family=family),
                },
            }
        )
    with pytest.raises(RolloutGateError, match="terraform_plan_task_invalid"):
        candidates_from_plan({"resource_changes": changes})
    assert set(
        candidates_from_plan(
            {"resource_changes": changes},
            allow_noop=True,
        )
    ) == set(TASK_ADDRESSES)


def _worker_saved_plan(
    artifacts: dict[str, str],
    *,
    mode: str = "cleanup",
    cleanup_domain: str = "mail_action",
    advance_stage: bool = False,
) -> dict[str, object]:
    worker_input = {
        "rotation_epoch": "hmac-2026-07",
        "mode": mode,
        "cleanup_domain": cleanup_domain,
        "advance_stage": advance_stage,
        "provenance_key_arn": (
            "arn:aws:kms:ap-northeast-1:123456789012:key/12345678-1234-4123-8123-123456789abc"
        ),
        "complete_artifacts": dict(artifacts),
    }
    release_bindings = {
        "rotation_epoch": worker_input["rotation_epoch"],
        "gate_mode": mode,
        "cleanup_domain": cleanup_domain,
        "manifest_sha256": artifacts["reviewed_manifest"],
        "rollout_control_sha256": artifacts["rollout_control"],
        "worker_enabled": True,
        "worker_mode": mode,
        "worker_artifacts": dict(artifacts),
        "worker_provenance_key_arn": worker_input["provenance_key_arn"],
    }
    return {
        "resource_changes": [
            {
                "address": WORKER_DEPLOY_ADDRESS,
                "change": {
                    "actions": ["delete", "create"],
                    "after": {"input": worker_input},
                },
            },
            {
                "address": PRODUCTION_GATE_ADDRESS,
                "change": {
                    "actions": ["delete", "create"],
                    "after": {
                        "input": {
                            "hmac_release_bindings": release_bindings,
                        }
                    },
                },
            },
        ]
    }


def test_cleanup_worker_files_and_release_gate_share_one_exact_saved_plan() -> None:
    artifacts = {
        name: hashlib.sha256(name.encode()).hexdigest() for name in WORKER_ARTIFACT_BINDING_KEYS
    }
    plan = _worker_saved_plan(artifacts)

    assert cleanup_worker_bindings_from_plan(plan, domain="mail_action") == artifacts

    drifted = copy.deepcopy(plan)
    drifted["resource_changes"][1]["change"]["after"]["input"]["hmac_release_bindings"][  # type: ignore[index]
        "worker_artifacts"
    ]["candidate_env"] = "f" * 64
    with pytest.raises(RolloutGateError, match="terraform_plan_worker_invalid"):
        cleanup_worker_bindings_from_plan(drifted, domain="mail_action")

    reconciled = copy.deepcopy(plan)
    reconciled["resource_changes"][0]["change"]["actions"] = ["no-op"]  # type: ignore[index]
    with pytest.raises(RolloutGateError, match="terraform_plan_worker_invalid"):
        cleanup_worker_bindings_from_plan(reconciled, domain="mail_action")
    assert (
        cleanup_worker_bindings_from_plan(
            reconciled,
            domain="mail_action",
            allow_noop=True,
        )
        == artifacts
    )


def test_saved_plan_snapshot_hashes_and_worker_cli_fail_closed_on_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = tmp_path / "manifest.json"
    control = tmp_path / "control.json"
    manifest.write_text('{"reviewed":true}\n', encoding="utf-8")
    control.write_text('{"control":true}\n', encoding="utf-8")
    artifacts = {
        name: hashlib.sha256(name.encode()).hexdigest() for name in WORKER_ARTIFACT_BINDING_KEYS
    }
    artifacts["reviewed_manifest"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    artifacts["rollout_control"] = hashlib.sha256(control.read_bytes()).hexdigest()
    plan = _worker_saved_plan(artifacts)

    assert (
        validate_saved_plan_hmac_files(
            plan,
            manifest_path=manifest,
            control_path=control,
            mode="cleanup",
            cleanup_domain="mail_action",
        )["manifest_sha256"]
        == artifacts["reviewed_manifest"]
    )
    assert (
        worker_bindings_from_plan(
            plan,
            mode="cleanup",
            cleanup_domain="mail_action",
            advance_stage=False,
        )
        == artifacts
    )

    plan_path = tmp_path / "saved.tfplan"
    plan_path.write_bytes(b"opaque")
    monkeypatch.setenv("TEAMAGENT_HMAC_DEPLOY_FROM_TERRAFORM", "1")
    monkeypatch.setenv("TEAMAGENT_SAVED_PLAN_PATH", str(plan_path))
    monkeypatch.setenv("HMAC_WORKER_MODE", "cleanup")
    monkeypatch.setenv("HMAC_CLEANUP_DOMAIN", "mail_action")
    monkeypatch.setenv("HMAC_WORKER_ADVANCE_STAGE", "0")
    monkeypatch.setenv("HMAC_WORKER_EXPECTED_HASHES", json.dumps(artifacts))
    monkeypatch.setattr(payload_module, "show_saved_plan", lambda _path: plan)
    assert main(["verify-worker-bindings"]) == 0
    assert capsys.readouterr().out == '{"code":"ok","ok":true}\n'

    manifest.write_text('{"reviewed":false}\n', encoding="utf-8")
    with pytest.raises(RolloutGateError, match="terraform_plan_hmac_invalid"):
        validate_saved_plan_hmac_files(
            plan,
            manifest_path=manifest,
            control_path=control,
            mode="cleanup",
            cleanup_domain="mail_action",
        )
    monkeypatch.setenv(
        "HMAC_WORKER_EXPECTED_HASHES",
        json.dumps({**artifacts, "candidate_env": "f" * 64}),
    )
    assert main(["verify-worker-bindings"]) == 2
    assert capsys.readouterr().out == ('{"code":"terraform_plan_worker_invalid","ok":false}\n')


def test_eventbridge_full_target_is_bound_to_same_saved_plan(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    control = tmp_path / "control.json"
    manifest.write_text("{}\n", encoding="utf-8")
    expected_rule: dict[str, object] = {
        "Arn": ("arn:aws:events:ap-northeast-1:123456789012:rule/teamagent-dev-morning-digest"),
        "CreatedBy": "123456789012",
        "Description": "Reviewed morning digest schedule",
        "EventBusName": "default",
        "EventPattern": None,
        "ManagedBy": None,
        "Name": "teamagent-dev-morning-digest",
        "RoleArn": None,
        "ScheduleExpression": "cron(0 22 * * ? *)",
        "State": "DISABLED",
    }
    control.write_text(
        json.dumps({"morning_digest": {"expected_rule": expected_rule}}) + "\n",
        encoding="utf-8",
    )
    manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    control_digest = hashlib.sha256(control.read_bytes()).hexdigest()
    task_definition = (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-morning-digest:92"
    )
    target: dict[str, object] = {
        "Id": "morning",
        "Arn": "arn:aws:ecs:ap-northeast-1:123456789012:cluster/teamagent-dev",
        "RoleArn": "arn:aws:iam::123456789012:role/events-morning",
        "Input": "{}",
        "EcsParameters": {
            "TaskDefinitionArn": task_definition,
            "TaskCount": 1,
            "LaunchType": "FARGATE",
            "PlatformVersion": "LATEST",
            "NetworkConfiguration": {
                "awsvpcConfiguration": {
                    "Subnets": ["subnet-1"],
                    "SecurityGroups": ["sg-1"],
                    "AssignPublicIp": "ENABLED",
                }
            },
        },
        "RetryPolicy": {
            "MaximumEventAgeInSeconds": 3600,
            "MaximumRetryAttempts": 1,
        },
    }
    planned_target = copy.deepcopy(target)
    planned_target["EcsParameters"]["TaskDefinitionArn"] = None  # type: ignore[index]
    release = {
        "rotation_epoch": "hmac-2026-07",
        "gate_mode": "candidate",
        "cleanup_domain": "",
        "manifest_sha256": manifest_digest,
        "rollout_control_sha256": control_digest,
        "worker_enabled": False,
        "worker_mode": "candidate",
        "worker_artifacts": {},
        "worker_provenance_key_arn": "",
    }
    plan = {
        "resource_changes": [
            {
                "address": PRODUCTION_GATE_ADDRESS,
                "change": {
                    "actions": ["delete", "create"],
                    "after": {"input": {"hmac_release_bindings": release}},
                },
            },
            *[
                {
                    "address": address,
                    "change": {
                        "actions": ["create", "delete"],
                        "after": {
                            "input": {
                                "action": action,
                                "workload": "morning_digest",
                                "mode": "candidate",
                                "rotation_epoch": "hmac-2026-07",
                                "cleanup_domain": "",
                                "expected_rule": expected_rule,
                                "target": planned_target,
                                "task_definition_arn": None,
                                "manifest_sha256": manifest_digest,
                                "rollout_control_sha256": control_digest,
                            }
                        },
                    },
                }
                for action, address in zip(
                    ("pre-update", "post-update"),
                    MORNING_PROMOTION_ADDRESSES,
                    strict=True,
                )
            ],
        ],
    }

    validate_saved_plan_event_target(
        plan,
        target=target,
        task_definition=task_definition,
        mode="candidate",
        cleanup_domain="",
        manifest_path=manifest,
        control_path=control,
    )
    changed = copy.deepcopy(target)
    changed["RetryPolicy"]["MaximumRetryAttempts"] = 2  # type: ignore[index]
    with pytest.raises(RolloutGateError, match="terraform_plan_event_invalid"):
        validate_saved_plan_event_target(
            plan,
            target=changed,
            task_definition=task_definition,
            mode="candidate",
            cleanup_domain="",
            manifest_path=manifest,
            control_path=control,
        )


def test_ecs_service_task_definition_change_requires_pre_and_post_plan_gates() -> None:
    old_arn = "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-mcp:91"
    new_arn = old_arn.rsplit(":", maxsplit=1)[0] + ":92"
    service = {
        "address": "aws_ecs_service.mcp[0]",
        "change": {
            "actions": ["update"],
            "before": {"task_definition": old_arn},
            "after": {"task_definition": new_arn},
            "after_unknown": {},
        },
    }
    release = {
        "rotation_epoch": "hmac-2026-07",
        "gate_mode": "candidate",
        "cleanup_domain": "",
        "manifest_sha256": "a" * 64,
        "rollout_control_sha256": "b" * 64,
        "worker_enabled": False,
        "worker_mode": "candidate",
        "worker_artifacts": {},
        "worker_provenance_key_arn": "",
    }

    def gate_input(action: str) -> dict[str, object]:
        return {
            "action": action,
            "workload": "mcp",
            "mode": "candidate",
            "rotation_epoch": "hmac-2026-07",
            "cleanup_domain": "",
            "task_definition_arn": new_arn,
            "manifest_sha256": "a" * 64,
            "rollout_control_sha256": "b" * 64,
        }

    production = {
        "address": PRODUCTION_GATE_ADDRESS,
        "change": {
            "actions": ["create", "delete"],
            "after": {"input": {"hmac_release_bindings": release}},
        },
    }
    live = {
        "address": LIVE_TASK_GATE_ADDRESSES["mcp"],
        "change": {
            "actions": ["create", "delete"],
            "after": {
                "input": {
                    **gate_input("pre-register"),
                    "task_address": TASK_ADDRESSES["mcp"],
                }
            },
        },
    }
    live["change"]["after"]["input"].pop("task_definition_arn")  # type: ignore[index]
    pre = {
        "address": "terraform_data.hmac_mcp_pre_update[0]",
        "change": {
            "actions": ["create", "delete"],
            "after": {"input": gate_input("pre-update")},
        },
    }
    post = {
        "address": "terraform_data.hmac_mcp_post_update[0]",
        "change": {
            "actions": ["create", "delete"],
            "after": {"input": gate_input("post-update")},
        },
    }
    plan = {
        "variables": {"hmac_runtime_promotion_tasks": {"value": ["mcp"]}},
        "resource_changes": [production, service, live, pre, post],
    }

    validate_saved_plan_runtime_mutations(plan)
    with pytest.raises(RolloutGateError, match="terraform_plan_runtime_invalid"):
        missing_post = copy.deepcopy(plan)
        missing_post["resource_changes"].remove(  # type: ignore[union-attr]
            next(
                change
                for change in missing_post["resource_changes"]  # type: ignore[index]
                if change["address"] == "terraform_data.hmac_mcp_post_update[0]"
            )
        )
        validate_saved_plan_runtime_mutations(missing_post)

    unrelated = copy.deepcopy(service)
    unrelated["change"]["before"]["task_definition"] = new_arn  # type: ignore[index]
    no_mutation = {
        "variables": {"hmac_runtime_promotion_tasks": {"value": []}},
        "resource_changes": [production, unrelated],
    }
    validate_saved_plan_runtime_mutations(no_mutation)

    for mutation in ("wrong-action", "wrong-workload", "extra-input"):
        adversarial = copy.deepcopy(plan)
        gate = next(
            change
            for change in adversarial["resource_changes"]
            if change["address"] == "terraform_data.hmac_mcp_pre_update[0]"
        )
        if mutation == "wrong-action":
            gate["change"]["after"]["input"]["action"] = "post-update"
        elif mutation == "wrong-workload":
            gate["change"]["after"]["input"]["workload"] = "connect_web"
        else:
            gate["change"]["after"]["input"]["unreviewed"] = True
        with pytest.raises(RolloutGateError, match="terraform_plan_runtime_invalid"):
            validate_saved_plan_runtime_mutations(adversarial)
