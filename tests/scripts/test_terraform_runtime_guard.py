"""Terraform runtime validatorをAWS書込みなしの敵対fixtureで検証する。"""

from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
GUARD = PROJECT_ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh"
ACCOUNT = "718959508629"
REGION = "ap-northeast-1"
REPOSITORY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/teamagent-mcp"
LIVE_IMAGE = f"{REPOSITORY}@sha256:{'f' * 64}"
ROLLOUT_IMAGE = f"{REPOSITORY}@sha256:{'a' * 64}"
TIKTOK_REPOSITORY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/teamagent-dev-tiktok-acquire"
TIKTOK_IMAGE = f"{TIKTOK_REPOSITORY}@sha256:{'e' * 64}"

COMPONENTS = {
    "mcp": ("teamagent-mcp", "teamagent-dev-mcp", 55),
    "connect_web": ("connect-web", "teamagent-dev-connect-web", 48),
    "ingest": ("ingest", "teamagent-dev-ingest", 41),
    "morning": ("morning-digest", "teamagent-dev-morning-digest", 44),
    "canary": ("canary", "teamagent-dev-canary", 13),
    "tiktok": ("acquire", "teamagent-dev-tiktok-acquire", 25),
    "x_buzz": ("worker", "teamagent-dev-x-buzz-worker", 19),
}
TASK_ADDRESSES = {
    "mcp": "aws_ecs_task_definition.mcp",
    "connect_web": "aws_ecs_task_definition.connect_web[0]",
    "ingest": "aws_ecs_task_definition.ingest[0]",
    "morning": "aws_ecs_task_definition.morning_digest[0]",
    "canary": "aws_ecs_task_definition.canary[0]",
    "tiktok": "aws_ecs_task_definition.tiktok_acquire[0]",
    "x_buzz": "aws_ecs_task_definition.x_buzz_worker[0]",
}
DISPATCHERS = {
    "tiktok": {
        "component": "tiktok",
        "function_name": "teamagent-dev-tiktok-acquire-dispatch",
        "container": "acquire",
        "cluster": f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/teamagent-dev-tiktok",
        "security_group": "sg-tiktok",
        "queue_arn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:teamagent-dev-tiktok-acquire-jobs",
        "code_sha256": "dGlrdG9rLWRpc3BhdGNoLWNvZGUtaGFzaA==",
    },
    "x_buzz": {
        "component": "x_buzz",
        "function_name": "teamagent-dev-x-buzz-dispatch",
        "container": "worker",
        "cluster": f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/teamagent-dev",
        "security_group": "sg-x-buzz",
        "queue_arn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:teamagent-dev-x-buzz-jobs",
        "code_sha256": "eC1idXp6LWRpc3BhdGNoLWNvZGUtaGFzaA==",
    },
}
RULES = {
    "ingest": (
        "aws_cloudwatch_event_rule.ingest_weekly[0]",
        "teamagent-dev-ingest-weekly",
        "DISABLED",
        "cron(0 18 ? * MON *)",
    ),
    "morning": (
        "aws_cloudwatch_event_rule.morning_digest_weekday[0]",
        "teamagent-dev-morning-digest-weekday",
        "ENABLED",
        "cron(30 0 ? * MON-FRI *)",
    ),
    "canary": (
        "aws_cloudwatch_event_rule.canary_hourly[0]",
        "teamagent-dev-canary-hourly",
        "DISABLED",
        "rate(1 hour)",
    ),
}


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)


def _task_arn(component: str) -> str:
    _, family, revision = COMPONENTS[component]
    return f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{family}:{revision}"


def _environment() -> list[dict[str, str]]:
    return [
        {"name": "BASE_FLAG", "value": "1"},
        {"name": "USE_TIKTOK_TOOLS", "value": "0"},
        {"name": "USE_VIDEO_TOOLS", "value": "0"},
    ]


def _container(component: str, image: str = LIVE_IMAGE) -> dict[str, Any]:
    name, _, _ = COMPONENTS[component]
    if component == "tiktok":
        image = TIKTOK_IMAGE
    container: dict[str, Any] = {
        "name": name,
        "image": image,
        "cpu": 0,
        "memory": 0,
        "essential": True,
        "command": ["python", "-m", f"teamagent.runtime.{name}"],
        "environment": _environment(),
        "secrets": [
            {
                "name": "APP_TOKEN",
                "valueFrom": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:test",
            }
        ],
        "portMappings": [{"containerPort": 8787, "hostPort": 8787, "protocol": "tcp"}],
        "healthCheck": {
            "command": ["CMD-SHELL", "true"],
            "interval": 30,
            "timeout": 5,
            "retries": 3,
            "startPeriod": 0,
        },
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": f"/teamagent/dev/{name}",
                "awslogs-region": REGION,
                "awslogs-stream-prefix": "runtime",
            },
        },
    }
    if component in {"tiktok", "x_buzz"}:
        container.update(
            {
                "command": ["python", "-m", f"teamagent.workers.{name}"],
                "environment": [{"name": "WORKER_KIND", "value": component}],
                "secrets": [],
                "portMappings": [],
                "healthCheck": None,
                "user": "10001",
                "readonlyRootFilesystem": True,
                "linuxParameters": {
                    "initProcessEnabled": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "mountPoints": [
                    {
                        "sourceVolume": "tmp",
                        "containerPath": "/tmp",
                        "readOnly": False,
                    }
                ],
            }
        )
    return container


def _task_after(component: str) -> dict[str, Any]:
    _, family, _ = COMPONENTS[component]
    is_worker = component in {"tiktok", "x_buzz"}
    return {
        "family": family,
        "task_role_arn": f"arn:aws:iam::{ACCOUNT}:role/{family}-task",
        "execution_role_arn": f"arn:aws:iam::{ACCOUNT}:role/{family}-exec",
        "cpu": "512",
        "memory": "1024",
        "network_mode": "awsvpc",
        "requires_compatibilities": ["FARGATE"],
        "runtime_platform": [
            {
                "cpu_architecture": "ARM64" if is_worker else "X86_64",
                "operating_system_family": "LINUX",
            }
        ],
        "ephemeral_storage": [{"size_in_gib": 40}] if is_worker else [],
        "ipc_mode": "",
        "pid_mode": "",
        "placement_constraints": [],
        "proxy_configuration": [],
        "inference_accelerator": [],
        "volume": [{"name": "tmp"}] if is_worker else [],
        "container_definitions": json.dumps([_container(component)]),
    }


def _task_aws(component: str) -> dict[str, Any]:
    task = _task_after(component)
    _, family, _ = COMPONENTS[component]
    result = {
        "taskDefinitionArn": _task_arn(component),
        "family": family,
        "taskRoleArn": task["task_role_arn"],
        "executionRoleArn": task["execution_role_arn"],
        "cpu": task["cpu"],
        "memory": task["memory"],
        "networkMode": task["network_mode"],
        "requiresCompatibilities": task["requires_compatibilities"],
        "runtimePlatform": {
            "cpuArchitecture": task["runtime_platform"][0]["cpu_architecture"],
            "operatingSystemFamily": task["runtime_platform"][0]["operating_system_family"],
        },
        "containerDefinitions": json.loads(task["container_definitions"]),
        "volumes": [{"name": volume["name"]} for volume in task["volume"]],
    }
    if task["ephemeral_storage"]:
        result["ephemeralStorage"] = {"sizeInGiB": task["ephemeral_storage"][0]["size_in_gib"]}
    return result


def _dispatcher_environment(component: str) -> dict[str, str]:
    dispatch = DISPATCHERS[component]
    return {
        "CLUSTER_ARN": dispatch["cluster"],
        "SUBNETS": "subnet-a,subnet-b",
        "SG_ID": dispatch["security_group"],
        "CONTAINER": dispatch["container"],
        "TASKDEF_ARN": _task_arn(dispatch["component"]),
    }


def _lambda_arn(component: str) -> str:
    return f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{DISPATCHERS[component]['function_name']}"


def _lambda_aws(component: str) -> dict[str, Any]:
    dispatch = DISPATCHERS[component]
    function_name = dispatch["function_name"]
    return {
        "FunctionName": function_name,
        "FunctionArn": _lambda_arn(component),
        "State": "Active",
        "LastUpdateStatus": "Successful",
        "Role": f"arn:aws:iam::{ACCOUNT}:role/{function_name}",
        "Runtime": "python3.12",
        "Handler": "handler.handler",
        "Architectures": ["arm64"],
        "CodeSha256": dispatch["code_sha256"],
        "Description": "",
        "Timeout": 30,
        "MemorySize": 128,
        "PackageType": "Zip",
        "Environment": {"Variables": _dispatcher_environment(component)},
        "TracingConfig": {"Mode": "PassThrough"},
        "EphemeralStorage": {"Size": 512},
        "SnapStart": {"ApplyOn": "None"},
    }


def _lambda_tf(component: str) -> dict[str, Any]:
    aws_value = _lambda_aws(component)
    return {
        "function_name": aws_value["FunctionName"],
        "arn": aws_value["FunctionArn"],
        "role": aws_value["Role"],
        "runtime": aws_value["Runtime"],
        "handler": aws_value["Handler"],
        "architectures": aws_value["Architectures"],
        "source_code_hash": aws_value["CodeSha256"],
        "description": "",
        "timeout": 30,
        "memory_size": 128,
        "package_type": "Zip",
        "environment": [{"variables": _dispatcher_environment(component)}],
        "reserved_concurrent_executions": -1,
        "tags": {},
        "tags_all": {},
    }


def _mapping_arn(component: str) -> str:
    return f"arn:aws:lambda:{REGION}:{ACCOUNT}:event-source-mapping:{component}-uuid"


def _mapping_aws(component: str) -> dict[str, Any]:
    return {
        "UUID": f"{component}-uuid",
        "EventSourceMappingArn": _mapping_arn(component),
        "State": "Enabled",
        "EventSourceArn": DISPATCHERS[component]["queue_arn"],
        "FunctionArn": _lambda_arn(component),
        "BatchSize": 1,
    }


def _mapping_tf(component: str) -> dict[str, Any]:
    aws_value = _mapping_aws(component)
    return {
        "arn": aws_value["EventSourceMappingArn"],
        "uuid": aws_value["UUID"],
        "enabled": True,
        "event_source_arn": aws_value["EventSourceArn"],
        "function_name": aws_value["FunctionArn"],
        "batch_size": 1,
        "tags": {},
        "tags_all": {},
    }


def _service_tf(component: str) -> dict[str, Any]:
    name = f"teamagent-dev-{'connect-web' if component == 'connect_web' else 'mcp'}"
    security_group = "sg-connect" if component == "connect_web" else "sg-mcp"
    load_balancer = []
    if component == "connect_web":
        load_balancer = [
            {
                "target_group_arn": "arn:aws:elasticloadbalancing:test:targetgroup/web",
                "elb_name": "",
                "container_name": "connect-web",
                "container_port": 8788,
            }
        ]
    return {
        "name": name,
        "cluster": f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/teamagent-dev",
        "desired_count": 1,
        "launch_type": "FARGATE",
        "capacity_provider_strategy": [],
        "platform_version": "LATEST",
        "availability_zone_rebalancing": "DISABLED",
        "deployment_maximum_percent": 200,
        "deployment_minimum_healthy_percent": 100,
        "deployment_circuit_breaker": [{"enable": False, "rollback": False}],
        "deployment_controller": [{"type": "ECS"}],
        "alarms": [],
        "network_configuration": [
            {
                "assign_public_ip": True,
                "security_groups": [security_group],
                "subnets": ["subnet-a", "subnet-b"],
            }
        ],
        "load_balancer": load_balancer,
        "service_registries": [],
        "health_check_grace_period_seconds": 60 if component == "connect_web" else 0,
        "iam_role": "/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS",
        "scheduling_strategy": "REPLICA",
        "placement_constraints": [],
        "ordered_placement_strategy": [],
        "enable_execute_command": False,
        "enable_ecs_managed_tags": False,
        "propagate_tags": "NONE",
        "tags": {},
        "tags_all": {},
        "service_connect_configuration": [],
        "volume_configuration": [],
        "vpc_lattice_configurations": [],
        "task_definition": _task_arn(component),
    }


def _target_tf(component: str) -> dict[str, Any]:
    _, rule_name, _, _ = RULES[component]
    return {
        "target_id": f"target-{component}",
        "arn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/teamagent-dev",
        "role_arn": f"arn:aws:iam::{ACCOUNT}:role/events-{component}",
        "input": "",
        "input_path": "",
        "input_transformer": [],
        "retry_policy": [{"maximum_event_age_in_seconds": 3600, "maximum_retry_attempts": 1}],
        "dead_letter_config": [],
        "event_bus_name": "default",
        "rule": rule_name,
        "ecs_target": [
            {
                "task_definition_arn": _task_arn(component),
                "task_count": 1,
                "launch_type": "FARGATE",
                "platform_version": "LATEST",
                "group": "",
                "enable_ecs_managed_tags": False,
                "enable_execute_command": False,
                "propagate_tags": "",
                "tags": {},
                "capacity_provider_strategy": [],
                "placement_constraint": [],
                "ordered_placement_strategy": [],
                "network_configuration": [
                    {
                        "assign_public_ip": True,
                        "security_groups": [f"sg-{component}"],
                        "subnets": ["subnet-a", "subnet-b"],
                    }
                ],
            }
        ],
    }


def _rule_tf(component: str) -> dict[str, Any]:
    _, name, state, schedule = RULES[component]
    return {
        "name": name,
        "arn": f"arn:aws:events:{REGION}:{ACCOUNT}:rule/{name}",
        "state": state,
        "is_enabled": state == "ENABLED",
        "schedule_expression": schedule,
        "event_pattern": "",
        "description": f"{component} schedule",
        "role_arn": "",
        "event_bus_name": "default",
        "tags": {},
        "tags_all": {},
    }


def _change(
    address: str, resource_type: str, actions: list[str], before: Any, after: Any
) -> dict[str, Any]:
    return {
        "address": address,
        "mode": "managed",
        "type": resource_type,
        "name": address.split(".", 1)[1].split("[")[0],
        "change": {
            "actions": actions,
            "before": before,
            "after": after,
            "after_unknown": {},
        },
    }


def _safe_plan() -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    for component, address in TASK_ADDRESSES.items():
        after = _task_after(component)
        change = _change(
            address,
            "aws_ecs_task_definition",
            ["create", "delete"],
            copy.deepcopy(after),
            after,
        )
        change["change"]["after_unknown"] = {
            "arn": True,
            "arn_without_revision": True,
            "enable_fault_injection": True,
            "id": True,
            "revision": True,
        }
        changes.append(change)

    configurations: list[dict[str, Any]] = []
    for component, address in (
        ("mcp", "aws_ecs_service.mcp[0]"),
        ("connect_web", "aws_ecs_service.connect_web[0]"),
    ):
        before = _service_tf(component)
        after = copy.deepcopy(before)
        after["task_definition"] = None
        change = _change(address, "aws_ecs_service", ["update"], before, after)
        change["change"]["after_unknown"] = {"task_definition": True}
        changes.append(change)
        config_address = address.removesuffix("[0]")
        task_address = TASK_ADDRESSES[component]
        configurations.append(
            {
                "address": config_address,
                "expressions": {"task_definition": {"references": [f"{task_address}.arn"]}},
            }
        )

    for component in ("ingest", "morning", "canary"):
        address = (
            "aws_cloudwatch_event_target."
            f"{'morning_digest' if component == 'morning' else component}_run_task[0]"
        )
        before = _target_tf(component)
        after = copy.deepcopy(before)
        after["ecs_target"][0]["task_definition_arn"] = None
        change = _change(address, "aws_cloudwatch_event_target", ["update"], before, after)
        change["change"]["after_unknown"] = {"ecs_target": [{"task_definition_arn": True}]}
        changes.append(change)
        config_address = address.removesuffix("[0]")
        task_address = TASK_ADDRESSES[component]
        configurations.append(
            {
                "address": config_address,
                "expressions": {
                    "ecs_target": [{"task_definition_arn": {"references": [f"{task_address}.arn"]}}]
                },
            }
        )

    for component, (address, _, _, _) in RULES.items():
        value = _rule_tf(component)
        changes.append(
            _change(
                address,
                "aws_cloudwatch_event_rule",
                ["no-op"],
                value,
                copy.deepcopy(value),
            )
        )

    for component, task_address in (
        ("tiktok", TASK_ADDRESSES["tiktok"]),
        ("x_buzz", TASK_ADDRESSES["x_buzz"]),
    ):
        function_name = "tiktok_dispatch" if component == "tiktok" else "x_dispatch"
        address = f"aws_lambda_function.{function_name}[0]"
        before = _lambda_tf(component)
        after = copy.deepcopy(before)
        after["environment"][0]["variables"]["TASKDEF_ARN"] = None
        change = _change(address, "aws_lambda_function", ["update"], before, after)
        change["change"]["after_unknown"] = {"environment": [{"variables": {"TASKDEF_ARN": True}}]}
        changes.append(change)
        configurations.append(
            {
                "address": f"aws_lambda_function.{function_name}",
                "expressions": {
                    "environment": [{"variables": {"references": [f"{task_address}.arn"]}}]
                },
            }
        )

        mapping_address = f"aws_lambda_event_source_mapping.{function_name}[0]"
        mapping = _mapping_tf(component)
        changes.append(
            _change(
                mapping_address,
                "aws_lambda_event_source_mapping",
                ["no-op"],
                mapping,
                copy.deepcopy(mapping),
            )
        )

    policy_read = _change(
        "data.aws_iam_policy_document.tiktok_mcp_policy[0]",
        "aws_iam_policy_document",
        ["read"],
        None,
        {"json": "{}"},
    )
    policy_read["mode"] = "data"
    changes.append(policy_read)

    return {
        "format_version": "1.2",
        "terraform_version": "1.12.2",
        "timestamp": "2026-07-16T00:00:00Z",
        "applyable": True,
        "complete": False,
        "errored": False,
        "variables": {},
        "planned_values": {},
        "prior_state": {},
        "configuration": {"root_module": {"resources": configurations}},
        "resource_changes": changes,
        "resource_drift": [],
        "checks": [],
        "deferred_changes": [],
        "action_invocations": [],
    }


def _find(plan: dict[str, Any], address: str) -> dict[str, Any]:
    return next(item for item in plan["resource_changes"] if item["address"] == address)


def _mutate_plan(plan: dict[str, Any], scenario: str) -> None:
    task = _find(plan, TASK_ADDRESSES["mcp"])
    container = json.loads(task["change"]["after"]["container_definitions"])[0]
    service = _find(plan, "aws_ecs_service.mcp[0]")
    target = _find(plan, "aws_cloudwatch_event_target.ingest_run_task[0]")
    rule = _find(plan, RULES["ingest"][0])
    dispatcher = _find(plan, "aws_lambda_function.tiktok_dispatch[0]")
    mapping = _find(plan, "aws_lambda_event_source_mapping.tiktok_dispatch[0]")

    if scenario == "env_add":
        container["environment"].append({"name": "NEW", "value": "1"})
    elif scenario == "env_change":
        container["environment"][0]["value"] = "changed"
    elif scenario == "env_delete":
        container["environment"].pop()
    elif scenario == "secret_add":
        container["secrets"].append({"name": "NEW", "valueFrom": "arn:new"})
    elif scenario == "secret_change":
        container["secrets"][0]["valueFrom"] = "arn:changed"
    elif scenario == "secret_delete":
        container["secrets"].clear()
    elif scenario == "wrong_container":
        container["name"] = "wrong"
    elif scenario == "duplicate_container":
        containers = [container, copy.deepcopy(container)]
        task["change"]["after"]["container_definitions"] = json.dumps(containers)
        return
    elif scenario == "task_role":
        task["change"]["after"]["task_role_arn"] = "arn:changed"
    elif scenario == "task_cpu":
        task["change"]["after"]["cpu"] = "1024"
    elif scenario == "task_memory":
        task["change"]["after"]["memory"] = "2048"
    elif scenario == "task_runtime":
        task["change"]["after"]["runtime_platform"][0]["cpu_architecture"] = "ARM64"
    elif scenario == "task_command":
        container["command"] = ["false"]
    elif scenario == "task_essential":
        container["essential"] = False
    elif scenario == "task_health":
        container["healthCheck"]["retries"] = 9
    elif scenario == "task_log":
        container["logConfiguration"]["options"]["awslogs-group"] = "/changed"
    elif scenario == "task_ports":
        container["portMappings"][0]["containerPort"] = 9999
    elif scenario == "task_volumes":
        task["change"]["after"]["volume"] = [{"name": "new", "host_path": ""}]
    elif scenario == "task_tags":
        task["change"]["after"]["tags_all"] = {"Unexpected": "tag"}
    elif scenario == "task_track_latest":
        task["change"]["after"]["track_latest"] = True
    elif scenario == "task_skip_destroy":
        task["change"]["after"]["skip_destroy"] = True
    elif scenario == "task_fault_injection":
        task["change"]["after"]["enable_fault_injection"] = True
    elif scenario == "task_restart_policy":
        container["restartPolicy"] = {
            "enabled": True,
            "ignoredExitCodes": [0],
            "restartAttemptPeriod": 60,
        }
    elif scenario == "task_version_consistency":
        container["versionConsistency"] = "disabled"
    elif scenario == "task_credential_specs":
        container["credentialSpecs"] = ["credentialspecdomainless:arn:unexpected"]
    elif scenario == "service_desired":
        service["change"]["after"]["desired_count"] = 2
    elif scenario == "service_network":
        service["change"]["after"]["network_configuration"][0]["subnets"] = ["other"]
    elif scenario == "service_lb":
        service["change"]["after"]["load_balancer"] = [{"container_name": "bad"}]
    elif scenario == "service_deployment":
        service["change"]["after"]["deployment_maximum_percent"] = 300
    elif scenario == "service_force_deployment":
        service["change"]["after"]["force_new_deployment"] = True
    elif scenario == "service_wait":
        service["change"]["after"]["wait_for_steady_state"] = True
    elif scenario == "service_triggers":
        service["change"]["after"]["triggers"] = {"unsafe": "change"}
    elif scenario == "service_connect":
        service["change"]["after"]["service_connect_configuration"] = [{"enabled": False}]
    elif scenario == "target_role":
        target["change"]["after"]["role_arn"] = "arn:changed"
    elif scenario == "target_cluster":
        target["change"]["after"]["arn"] = "arn:other-cluster"
    elif scenario == "target_network":
        target["change"]["after"]["ecs_target"][0]["network_configuration"][0]["subnets"] = [
            "other"
        ]
    elif scenario == "target_retry":
        target["change"]["after"]["retry_policy"][0]["maximum_retry_attempts"] = 9
    elif scenario == "target_input":
        target["change"]["after"]["input"] = "changed"
    elif scenario == "rule_schedule":
        rule["change"]["actions"] = ["update"]
        rule["change"]["after"]["schedule_expression"] = "rate(5 minutes)"
    elif scenario == "lambda_static_env":
        dispatcher["change"]["after"]["environment"][0]["variables"]["SG_ID"] = "sg-wrong"
    elif scenario == "lambda_taskdef":
        dispatcher["change"]["after_unknown"] = {}
        dispatcher["change"]["after"]["environment"][0]["variables"]["TASKDEF_ARN"] = (
            f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/wrong:1"
        )
    elif scenario == "lambda_role":
        dispatcher["change"]["after"]["role"] = "arn:aws:iam::000:role/wrong"
    elif scenario == "lambda_code":
        dispatcher["change"]["after"]["source_code_hash"] = "changed"
    elif scenario == "lambda_publish":
        dispatcher["change"]["before"]["publish"] = True
        dispatcher["change"]["after"]["publish"] = True
    elif scenario == "lambda_skip_destroy":
        dispatcher["change"]["before"]["skip_destroy"] = True
        dispatcher["change"]["after"]["skip_destroy"] = True
    elif scenario == "mapping_disabled":
        mapping["change"]["before"]["enabled"] = False
        mapping["change"]["after"]["enabled"] = False
    elif scenario == "mapping_queue":
        mapping["change"]["before"]["event_source_arn"] = "arn:aws:sqs:wrong"
        mapping["change"]["after"]["event_source_arn"] = "arn:aws:sqs:wrong"
    elif scenario == "unknown":
        task["change"]["after_unknown"]["container_definitions"] = True
    elif scenario == "arbitrary_update":
        plan["resource_changes"].append(
            _change("aws_iam_policy.bad", "aws_iam_policy", ["update"], {}, {})
        )
    elif scenario == "arbitrary_create":
        plan["resource_changes"].append(
            _change("aws_iam_policy.bad", "aws_iam_policy", ["create", "delete"], {}, {})
        )
    elif scenario == "missing":
        plan["resource_changes"].remove(rule)
    elif scenario == "schema":
        plan.pop("format_version")
    elif scenario == "deferred":
        plan["deferred_changes"] = [{"reason": "unknown"}]
    elif scenario == "invocation":
        plan["action_invocations"] = [{"address": "action.bad"}]
    elif scenario == "checks":
        plan["checks"] = [{"status": "unknown", "instances": []}]
    elif scenario == "bad_action":
        task["change"]["actions"] = ["delete", "create"]
    elif scenario == "arbitrary_drift":
        plan["resource_drift"] = [
            _change("aws_iam_policy.bad", "aws_iam_policy", ["update"], {}, {})
        ]
    elif scenario == "data_write":
        data_change = _find(plan, "data.aws_iam_policy_document.tiktok_mcp_policy[0]")
        data_change["change"]["actions"] = ["update"]
    elif scenario != "safe":
        raise AssertionError(f"unknown scenario: {scenario}")

    task["change"]["after"]["container_definitions"] = json.dumps([container])


def _fake_aws(path: Path) -> None:
    _write_executable(
        path,
        f"""\
        #!/usr/bin/env python3
        import copy
        import json
        import os
        import sys

        ACCOUNT = {ACCOUNT!r}
        REGION = {REGION!r}
        LIVE_IMAGE = {LIVE_IMAGE!r}
        components = {json.dumps(COMPONENTS)!r}
        components = json.loads(components)
        rules = {json.dumps(RULES)!r}
        rules = json.loads(rules)
        tasks = {json.dumps({key: _task_aws(key) for key in COMPONENTS})!r}
        tasks = json.loads(tasks)
        dispatchers = {json.dumps(DISPATCHERS)!r}
        dispatchers = json.loads(dispatchers)
        lambda_configs = {json.dumps({key: _lambda_aws(key) for key in DISPATCHERS})!r}
        lambda_configs = json.loads(lambda_configs)
        mappings = {json.dumps({key: _mapping_aws(key) for key in DISPATCHERS})!r}
        mappings = json.loads(mappings)
        args = sys.argv[1:]
        if args[:2] == ["--region", REGION]:
            args = args[2:]

        def task_arn(component):
            _, family, revision = components[component]
            return f"arn:aws:ecs:{{REGION}}:{{ACCOUNT}}:task-definition/{{family}}:{{revision}}"

        def environment():
            values = [
                {{"name": "BASE_FLAG", "value": "1"}},
                {{"name": "USE_TIKTOK_TOOLS", "value": "0"}},
                {{"name": "USE_VIDEO_TOOLS", "value": "0"}},
            ]
            if os.environ.get("AWS_FAKE_INVALID_BOOL"):
                values[-1]["value"] = os.environ["AWS_FAKE_INVALID_BOOL"]
            if os.environ.get("AWS_FAKE_DRIFT"):
                values.append({{"name": "LIVE_DRIFT", "value": "1"}})
            return values

        if args[:2] == ["sts", "get-caller-identity"]:
            account = os.environ.get("AWS_FAKE_ACCOUNT", ACCOUNT)
            print(json.dumps({{"Account": account}}))
        elif args[:2] == ["ecs", "describe-services"]:
            state = os.environ.get("AWS_FAKE_SERVICE_STATE", "stable")
            services = []
            for component in ("mcp", "connect_web"):
                name = "teamagent-dev-mcp" if component == "mcp" else "teamagent-dev-connect-web"
                td = task_arn(component)
                deployments = [{{
                    "status": "PRIMARY",
                    "rolloutState": "IN_PROGRESS" if state == "in_progress" else "COMPLETED",
                    "taskDefinition": td,
                }}]
                if state == "two_primary":
                    deployments.append(dict(deployments[0]))
                sg = "sg-mcp" if component == "mcp" else "sg-connect"
                lbs = [] if component == "mcp" else [{{
                    "targetGroupArn": "arn:aws:elasticloadbalancing:test:targetgroup/web",
                    "containerName": "connect-web",
                    "containerPort": 8788,
                }}]
                services.append({{
                    "serviceName": name,
                    "status": "ACTIVE",
                    "desiredCount": 1,
                    "runningCount": 1,
                    "pendingCount": 0,
                    "taskDefinition": td,
                    "clusterArn": f"arn:aws:ecs:{{REGION}}:{{ACCOUNT}}:cluster/teamagent-dev",
                    "deployments": deployments,
                    "launchType": "FARGATE",
                    "platformVersion": "LATEST",
                    "availabilityZoneRebalancing": "DISABLED",
                    "deploymentConfiguration": {{
                        "maximumPercent": 200,
                        "minimumHealthyPercent": 100,
                        "deploymentCircuitBreaker": {{"enable": False, "rollback": False}},
                        "strategy": "ROLLING",
                        "bakeTimeInMinutes": 0,
                    }},
                    "deploymentController": {{"type": "ECS"}},
                    "networkConfiguration": {{"awsvpcConfiguration": {{
                        "assignPublicIp": "ENABLED",
                        "securityGroups": [sg],
                        "subnets": ["subnet-a", "subnet-b"],
                    }}}},
                    "loadBalancers": lbs,
                    "serviceRegistries": [],
                    "healthCheckGracePeriodSeconds": 60 if component == "connect_web" else 0,
                    "roleArn": f"arn:aws:iam::{{ACCOUNT}}:role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS",
                    "schedulingStrategy": "REPLICA",
                    "placementConstraints": [],
                    "placementStrategy": [],
                    "enableExecuteCommand": False,
                    "enableECSManagedTags": False,
                    "propagateTags": "NONE",
                    "tags": [],
                }})
            print(json.dumps({{"failures": [], "services": services}}))
        elif args[:2] == ["events", "describe-rule"]:
            name = args[args.index("--name") + 1]
            component = next(key for key, value in rules.items() if value[1] == name)
            _, _, state, schedule = rules[component]
            print(json.dumps({{
                "Name": name,
                "Arn": f"arn:aws:events:{{REGION}}:{{ACCOUNT}}:rule/{{name}}",
                "State": state,
                "ScheduleExpression": schedule,
                "Description": f"{{component}} schedule",
                "EventBusName": "default",
            }}))
        elif args[:2] == ["events", "list-targets-by-rule"]:
            name = args[args.index("--rule") + 1]
            component = next(key for key, value in rules.items() if value[1] == name)
            print(json.dumps({{"Targets": [{{
                "Id": f"target-{{component}}",
                "Arn": f"arn:aws:ecs:{{REGION}}:{{ACCOUNT}}:cluster/teamagent-dev",
                "RoleArn": f"arn:aws:iam::{{ACCOUNT}}:role/events-{{component}}",
                "EcsParameters": {{
                    "TaskDefinitionArn": task_arn(component),
                    "TaskCount": 1,
                    "LaunchType": "FARGATE",
                    "PlatformVersion": "LATEST",
                    "EnableECSManagedTags": False,
                    "EnableExecuteCommand": False,
                    "NetworkConfiguration": {{"awsvpcConfiguration": {{
                        "AssignPublicIp": "ENABLED",
                        "SecurityGroups": [f"sg-{{component}}"],
                        "Subnets": ["subnet-a", "subnet-b"],
                    }}}},
                }},
                "RetryPolicy": {{
                    "MaximumEventAgeInSeconds": 3600,
                    "MaximumRetryAttempts": 1,
                }},
            }}]}}))
        elif args[:2] == ["ecs", "describe-task-definition"]:
            arn = args[args.index("--task-definition") + 1]
            component = next(key for key in components if components[key][1] in arn)
            task = copy.deepcopy(tasks[component])
            task["taskDefinitionArn"] = arn
            if component not in ("tiktok", "x_buzz"):
                task["containerDefinitions"][0]["environment"] = environment()
            print(json.dumps({{"taskDefinition": task}}))
        elif args[:2] == ["lambda", "get-function-configuration"]:
            name = args[args.index("--function-name") + 1]
            component = next(
                key for key, value in dispatchers.items()
                if value["function_name"] == name
            )
            print(json.dumps(lambda_configs[component]))
        elif args[:2] == ["lambda", "list-tags"]:
            print(json.dumps({{"Tags": {{}}}}))
        elif args[:2] == ["lambda", "get-function-concurrency"]:
            print(json.dumps({{}}))
        elif args[:2] == ["lambda", "list-event-source-mappings"]:
            name = args[args.index("--function-name") + 1]
            component = next(
                key for key, value in dispatchers.items()
                if value["function_name"] == name
            )
            print(json.dumps({{"EventSourceMappings": [mappings[component]]}}))
        elif args[:2] == ["ecr", "describe-images"]:
            if os.environ.get("AWS_FAKE_ECR_MISSING"):
                raise SystemExit(44)
            digest_arg = args[args.index("--image-ids") + 1]
            digest = digest_arg.split("=", 1)[1]
            print(json.dumps({{"imageDetails": [{{"imageDigest": digest}}]}}))
        else:
            print("unexpected fake aws args: " + repr(args), file=sys.stderr)
            raise SystemExit(99)
        """,
    )


def _fake_terraform(path: Path) -> None:
    _write_executable(
        path,
        """\
        #!/usr/bin/env python3
        import json
        import os
        import pathlib
        import sys

        args = [arg for arg in sys.argv[1:] if not arg.startswith("-chdir=")]
        with open(os.environ["TF_FAKE_LOG"], "a", encoding="utf-8") as fh:
            fh.write(" ".join(args) + "\\n")

        if args[0] == "plan":
            out = next(arg.split("=", 1)[1] for arg in args if arg.startswith("-out="))
            core_arg = next(arg for arg in args if arg.startswith("-var=runtime_guard_live="))
            core = json.loads(core_arg.split("=", 2)[2])
            desired = core["desired_mcp_image"]
            plan = json.loads(pathlib.Path(os.environ["TF_FAKE_TEMPLATE"]).read_text())
            plan["variables"] = {
                "mcp_image": {"value": desired},
                "tiktok_acquire_image": {"value": core["desired_tiktok_image"]},
                "runtime_guard_live": {"value": core},
            }
            for change in plan["resource_changes"]:
                if change["type"] == "aws_ecs_task_definition":
                    containers = json.loads(change["change"]["after"]["container_definitions"])
                    containers[0]["image"] = (
                        core["desired_tiktok_image"]
                        if change["address"] == "aws_ecs_task_definition.tiktok_acquire[0]"
                        else desired
                    )
                    change["change"]["after"]["container_definitions"] = json.dumps(containers)
            pathlib.Path(out).write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
        elif args[0] == "show":
            plan_path = pathlib.Path(args[-1])
            data = plan_path.read_bytes()
            publish_race = os.environ.get("TF_FAKE_PUBLISH_RACE_PATH")
            if publish_race:
                race_path = pathlib.Path(publish_race)
                if not race_path.exists():
                    race_path.write_text("racer-owned", encoding="utf-8")
            if os.environ.get("TF_FAKE_SHOW_MODE") == "malformed":
                print("{")
            else:
                sys.stdout.buffer.write(data)
            if os.environ.get("TF_FAKE_SHOW_MODE") == "race":
                plan_path.write_bytes(data + b" ")
        elif args[0] == "apply":
            raise SystemExit("apply must never be reached")
        else:
            raise SystemExit("unexpected fake terraform args: " + repr(args))
        """,
    )


def _harness(tmp_path: Path, scenario: str = "safe") -> tuple[dict[str, str], Path, Path]:
    tmp_path.chmod(0o700)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(mode=0o700)
    _fake_aws(fake_bin / "aws")
    _fake_terraform(fake_bin / "terraform")

    plan_data = _safe_plan()
    _mutate_plan(plan_data, scenario)
    template = tmp_path / "template.json"
    template.write_text(json.dumps(plan_data), encoding="utf-8")
    template.chmod(0o600)
    var_file = tmp_path / "terraform.tfvars"
    var_file.write_text("# fake private tfvars\n", encoding="utf-8")
    var_file.chmod(0o600)
    tf_log = tmp_path / "terraform.log"
    tf_log.write_text("", encoding="utf-8")
    tf_log.chmod(0o600)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "TF_FAKE_LOG": str(tf_log),
            "TF_FAKE_TEMPLATE": str(template),
        }
    )
    return env, var_file, tf_log


def _plan_command(var_file: Path, output: Path, rollout: bool = False) -> list[str]:
    command = [
        "bash",
        str(GUARD),
        "plan",
        "--var-file",
        str(var_file),
        "--out",
        str(output),
    ]
    if rollout:
        command.extend(["--runtime-rollout-image", ROLLOUT_IMAGE])
    else:
        command.append("--runtime-sync")
    return command


def _run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


@pytest.mark.parametrize("rollout", [False, True], ids=["sync", "rollout"])
def test_safe_sync_and_rollout_publish_private_bound_artifacts(
    tmp_path: Path, rollout: bool
) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    plan = tmp_path / "runtime.tfplan"
    result = _run(_plan_command(var_file, plan, rollout), env)

    assert result.returncode == 0, result.stdout + result.stderr
    receipt = Path(f"{plan}.runtime-guard.json")
    assert plan.is_file() and receipt.is_file()
    assert stat.S_IMODE(plan.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    data = json.loads(receipt.read_text(encoding="utf-8"))
    assert data["plan_path"] == str(plan)
    assert data["receipt_path"] == str(receipt)
    assert data["var_file"] == str(var_file)
    assert data["desired_image"] == (ROLLOUT_IMAGE if rollout else LIVE_IMAGE)
    assert data["mode"] == ("rollout" if rollout else "sync")
    assert "apply" not in tf_log.read_text(encoding="utf-8")

    verify = _run(["bash", str(GUARD), "verify", "--plan", str(plan)], env)
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert "read-only検証完了" in verify.stdout
    assert "apply" not in tf_log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "scenario",
    [
        "env_add",
        "env_change",
        "env_delete",
        "secret_add",
        "secret_change",
        "secret_delete",
        "wrong_container",
        "duplicate_container",
        "task_role",
        "task_cpu",
        "task_memory",
        "task_runtime",
        "task_command",
        "task_essential",
        "task_health",
        "task_log",
        "task_ports",
        "task_volumes",
        "task_tags",
        "task_track_latest",
        "task_skip_destroy",
        "task_fault_injection",
        "task_restart_policy",
        "task_version_consistency",
        "task_credential_specs",
        "service_desired",
        "service_network",
        "service_lb",
        "service_deployment",
        "service_force_deployment",
        "service_wait",
        "service_triggers",
        "service_connect",
        "target_role",
        "target_cluster",
        "target_network",
        "target_retry",
        "target_input",
        "rule_schedule",
        "lambda_static_env",
        "lambda_taskdef",
        "lambda_role",
        "lambda_code",
        "lambda_publish",
        "lambda_skip_destroy",
        "mapping_disabled",
        "mapping_queue",
    ],
)
def test_runtime_attribute_regressions_fail_closed(tmp_path: Path, scenario: str) -> None:
    env, var_file, _ = _harness(tmp_path, scenario)
    plan = tmp_path / "unsafe.tfplan"
    result = _run(_plan_command(var_file, plan), env)
    assert result.returncode == 1
    assert not plan.exists()
    assert not Path(f"{plan}.runtime-guard.json").exists()


@pytest.mark.parametrize(
    "scenario",
    [
        "unknown",
        "arbitrary_update",
        "arbitrary_create",
        "missing",
        "schema",
        "deferred",
        "invocation",
        "checks",
        "bad_action",
        "arbitrary_drift",
        "data_write",
    ],
)
def test_plan_schema_action_and_allowlist_regressions_fail_closed(
    tmp_path: Path, scenario: str
) -> None:
    env, var_file, _ = _harness(tmp_path, scenario)
    plan = tmp_path / "unsafe.tfplan"
    result = _run(_plan_command(var_file, plan), env)
    assert result.returncode == 1
    assert not plan.exists()


@pytest.mark.parametrize("show_mode", ["malformed", "race"])
def test_malformed_or_replaced_plan_is_never_published(tmp_path: Path, show_mode: str) -> None:
    env, var_file, _ = _harness(tmp_path)
    env["TF_FAKE_SHOW_MODE"] = show_mode
    plan = tmp_path / "unsafe.tfplan"
    result = _run(_plan_command(var_file, plan), env)
    assert result.returncode == 1
    assert not plan.exists()


def test_live_change_during_plan_is_never_published(tmp_path: Path) -> None:
    env, var_file, _ = _harness(tmp_path)
    # fakeは全snapshotで同じdriftを返すため、templateとの比較で即座に拒否される。
    env["AWS_FAKE_DRIFT"] = "1"
    plan = tmp_path / "unsafe.tfplan"
    result = _run(_plan_command(var_file, plan), env)
    assert result.returncode == 1
    assert not plan.exists()


def test_wrong_account_and_unstable_service_fail_before_plan(tmp_path: Path) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    env["AWS_FAKE_ACCOUNT"] = "000000000000"
    result = _run(_plan_command(var_file, tmp_path / "account.tfplan"), env)
    assert result.returncode == 1
    assert "plan " not in tf_log.read_text(encoding="utf-8")

    env.pop("AWS_FAKE_ACCOUNT")
    env["AWS_FAKE_SERVICE_STATE"] = "in_progress"
    result = _run(_plan_command(var_file, tmp_path / "service.tfplan"), env)
    assert result.returncode == 1


def test_invalid_bool_value_is_not_echoed(tmp_path: Path) -> None:
    env, var_file, _ = _harness(tmp_path)
    secret_value = "DO_NOT_LOG_THIS_VALUE"
    env["AWS_FAKE_INVALID_BOOL"] = secret_value
    result = _run(_plan_command(var_file, tmp_path / "bool.tfplan"), env)
    assert result.returncode == 1
    assert secret_value not in result.stdout + result.stderr


def test_rollout_repository_digest_and_ecr_existence_are_fail_closed(
    tmp_path: Path,
) -> None:
    env, var_file, _ = _harness(tmp_path)
    bad = f"000000000000.dkr.ecr.{REGION}.amazonaws.com/teamagent-mcp@sha256:{'a' * 64}"
    command = _plan_command(var_file, tmp_path / "bad.tfplan", rollout=True)
    command[-1] = bad
    assert _run(command, env).returncode == 1

    env["AWS_FAKE_ECR_MISSING"] = "1"
    command = _plan_command(var_file, tmp_path / "missing.tfplan", rollout=True)
    assert _run(command, env).returncode == 1


def test_private_permissions_symlinks_existing_paths_and_arbitrary_target(
    tmp_path: Path,
) -> None:
    env, var_file, _ = _harness(tmp_path)
    var_file.chmod(0o644)
    result = _run(_plan_command(var_file, tmp_path / "mode.tfplan"), env)
    assert result.returncode == 1
    assert "group/other権限" in result.stdout + result.stderr

    var_file.chmod(0o600)
    link = tmp_path / "link.tfvars"
    link.symlink_to(var_file)
    result = _run(_plan_command(link, tmp_path / "link.tfplan"), env)
    assert result.returncode == 1
    assert "symlink" in result.stdout + result.stderr

    existing = tmp_path / "existing.tfplan"
    existing.write_text("do not overwrite", encoding="utf-8")
    existing.chmod(0o600)
    result = _run(_plan_command(var_file, existing), env)
    assert result.returncode == 1
    assert existing.read_text(encoding="utf-8") == "do not overwrite"

    target_command = _plan_command(var_file, tmp_path / "target.tfplan")
    target_command.extend(["--target", "aws_iam_policy.bad"])
    assert _run(target_command, env).returncode == 1


def test_atomic_publish_race_does_not_overwrite_or_delete_racer(tmp_path: Path) -> None:
    env, var_file, _ = _harness(tmp_path)
    plan = tmp_path / "race.tfplan"
    receipt = Path(f"{plan}.runtime-guard.json")
    env["TF_FAKE_PUBLISH_RACE_PATH"] = str(receipt)

    result = _run(_plan_command(var_file, plan), env)

    assert result.returncode == 1
    assert not plan.exists()
    assert receipt.read_text(encoding="utf-8") == "racer-owned"


def test_plan_tamper_pair_swap_live_drift_and_apply_are_rejected(tmp_path: Path) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    first = tmp_path / "first.tfplan"
    second = tmp_path / "second.tfplan"
    assert _run(_plan_command(var_file, first), env).returncode == 0
    assert _run(_plan_command(var_file, second), env).returncode == 0

    swapped = _run(
        [
            "bash",
            str(GUARD),
            "verify",
            "--plan",
            str(first),
            "--receipt",
            f"{second}.runtime-guard.json",
        ],
        env,
    )
    assert swapped.returncode == 1

    first.write_bytes(first.read_bytes() + b"tamper")
    tampered = _run(["bash", str(GUARD), "verify", "--plan", str(first)], env)
    assert tampered.returncode == 1

    env["AWS_FAKE_DRIFT"] = "1"
    live = _run(["bash", str(GUARD), "verify", "--plan", str(second)], env)
    assert live.returncode == 1

    apply = _run(["bash", str(GUARD), "apply", "--plan", str(second)], env)
    assert apply.returncode == 1
    assert "不明な command" in apply.stdout + apply.stderr
    assert "apply" not in tf_log.read_text(encoding="utf-8")
