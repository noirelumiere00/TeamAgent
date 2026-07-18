"""Terraform runtime validatorをAWS書込みなしの敵対fixtureで検証する。"""

from __future__ import annotations

import copy
import json
import os
import re
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
X_IMAGE = f"{REPOSITORY}@sha256:{'d' * 64}"
TIKTOK_REPOSITORY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/teamagent-dev-tiktok-acquire"
TIKTOK_IMAGE = f"{TIKTOK_REPOSITORY}@sha256:{'e' * 64}"
OPENCLAW_REPOSITORY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/teamagent-openclaw"
OPENCLAW_IMAGE = f"{OPENCLAW_REPOSITORY}@sha256:{'c' * 64}"
APP_VAULT_MANIFEST_SHA256 = "a" * 64
APP_BUILD_INPUTS_SHA256 = "b" * 64
APP_HTML = (
    "<!doctype html>\n<script>\nconst DATA="
    + json.dumps(
        {
            "manifest_sha256": APP_VAULT_MANIFEST_SHA256,
            "build_inputs_sha256": APP_BUILD_INPUTS_SHA256,
        },
        separators=(",", ":"),
    )
    + ";\n</script>\n"
).encode()
MAIL_HMAC_SECRET = (
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:teamagent/dev/hmac/mail-action-AbC123"
)
REPORT_HMAC_SECRET = (
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:teamagent/dev/hmac/report-link-XyZ789"
)

COMPONENTS = {
    "openclaw": ("openclaw", "teamagent-dev-openclaw", 25),
    "mcp": ("teamagent-mcp", "teamagent-dev-mcp", 55),
    "connect_web": ("connect-web", "teamagent-dev-connect-web", 53),
    "ingest": ("ingest", "teamagent-dev-ingest", 42),
    "morning": ("morning-digest", "teamagent-dev-morning-digest", 44),
    "canary": ("canary", "teamagent-dev-canary", 14),
    "tiktok": ("acquire", "teamagent-dev-tiktok-acquire", 25),
    "x_buzz": ("worker", "teamagent-dev-x-buzz-worker", 19),
}
TASK_ADDRESSES = {
    "openclaw": "aws_ecs_task_definition.openclaw[0]",
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
        "code_sha256": "ERERERERERERERERERERERERERERERERERERERERERE=",
    },
    "x_buzz": {
        "component": "x_buzz",
        "function_name": "teamagent-dev-x-buzz-dispatch",
        "container": "worker",
        "cluster": f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/teamagent-dev",
        "security_group": "sg-x-buzz",
        "queue_arn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:teamagent-dev-x-buzz-jobs",
        "code_sha256": "IiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiIiI=",
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
    if component == "openclaw":
        image = OPENCLAW_IMAGE
    elif component == "tiktok":
        image = TIKTOK_IMAGE
    elif component == "x_buzz":
        image = X_IMAGE
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
    elif component in {"mcp", "connect_web", "morning"}:
        container["secrets"].append(
            {"name": "MAIL_ACTION_HMAC_SECRET", "valueFrom": MAIL_HMAC_SECRET}
        )
        if component in {"mcp", "connect_web"}:
            container["secrets"].append(
                {"name": "REPORT_LINK_HMAC_SECRET", "valueFrom": REPORT_HMAC_SECRET}
            )
        if component == "connect_web":
            container["environment"].append(
                {
                    "name": "CONNECT_APP_HTML_S3_URI",
                    "value": ("s3://teamagent-dev-raw-files/codebuild/connect-web-app.html"),
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
        "skip_destroy": True,
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
    suffix = {"connect_web": "connect-web", "openclaw": "openclaw"}.get(component, "mcp")
    name = f"teamagent-dev-{suffix}"
    security_group = {
        "connect_web": "sg-connect",
        "openclaw": "sg-openclaw",
    }.get(component, "sg-mcp")
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
        "availability_zone_rebalancing": "ENABLED",
        "deployment_maximum_percent": 100 if component == "openclaw" else 200,
        "deployment_minimum_healthy_percent": 0 if component == "openclaw" else 100,
        "deployment_circuit_breaker": [
            {
                "enable": True,
                "rollback": True,
            }
        ],
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
        ("openclaw", "aws_ecs_service.openclaw[0]"),
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
                    "environment": [{"variables": {"references": [f"{task_address}.arn"]}}],
                    "filename": {
                        "references": [f"data.archive_file.{function_name}[0].output_path"]
                    },
                    "source_code_hash": {
                        "references": [
                            (f"data.archive_file.{function_name}[0].output_base64sha256")
                        ]
                    },
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

    exact_policy = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["logs:PutLogEvents"],
                    "Resource": (
                        f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/teamagent/dev/exact:*"
                    ),
                }
            ],
        },
        separators=(",", ":"),
    )
    for address in (
        "aws_iam_role_policy.worker_app",
        "aws_iam_role_policy.lambda_app",
        "aws_iam_role_policy.mcp_task",
        "aws_iam_role_policy.connect_web_task[0]",
        "aws_iam_role_policy.ingest_task[0]",
        "aws_iam_role_policy.morning_digest_task[0]",
    ):
        value = {"policy": exact_policy}
        changes.append(
            _change(
                address,
                "aws_iam_role_policy",
                ["no-op"],
                copy.deepcopy(value),
                value,
            )
        )

    return {
        "format_version": "1.2",
        "terraform_version": "1.12.2",
        "timestamp": "2026-07-16T00:00:00Z",
        "applyable": True,
        "complete": True,
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
    dispatcher_config = next(
        item
        for item in plan["configuration"]["root_module"]["resources"]
        if item["address"] == "aws_lambda_function.tiktok_dispatch"
    )

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
        task["change"]["after"]["skip_destroy"] = False
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
    elif scenario == "lambda_handler":
        dispatcher["change"]["after"]["handler"] = "handler.unsafe"
    elif scenario == "lambda_runtime":
        dispatcher["change"]["after"]["runtime"] = "python3.13"
    elif scenario == "lambda_timeout":
        dispatcher["change"]["after"]["timeout"] = 900
    elif scenario == "lambda_kms":
        dispatcher["change"]["after"]["kms_key_arn"] = "arn:aws:kms:wrong"
    elif scenario == "lambda_vpc":
        dispatcher["change"]["after"]["vpc_config"] = [
            {"security_group_ids": ["sg-wrong"], "subnet_ids": ["subnet-wrong"]}
        ]
    elif scenario == "lambda_source_reference":
        dispatcher_config["expressions"]["source_code_hash"]["references"] = [
            "data.archive_file.unsafe.output_base64sha256"
        ]
    elif scenario == "lambda_filename_reference":
        dispatcher_config["expressions"]["filename"]["references"] = [
            "data.archive_file.unsafe.output_path"
        ]
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
    elif scenario == "incomplete":
        plan["complete"] = False
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
        import pathlib
        import sys

        ACCOUNT = {ACCOUNT!r}
        REGION = {REGION!r}
        LIVE_IMAGE = {LIVE_IMAGE!r}
        APP_HTML = {APP_HTML!r}
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
        if (
            len(args) >= 2
            and args[0] == "--region"
            and args[1] in (REGION, "us-east-1")
        ):
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
            identity = {{"Account": account}}
            if os.environ.get("AWS_FAKE_TRUSTED_AUTOMATION"):
                identity["Arn"] = (
                    "arn:aws:sts::718959508629:assumed-role/"
                    "teamagent-dev-terraform-runtime-automation/test"
                )
            print(json.dumps(identity))
        elif args[:2] in (
            ["dynamodb", "put-item"],
            ["dynamodb", "delete-item"],
        ):
            print(json.dumps({{}}))
        elif args[:2] == ["cloudtrail", "get-trail"]:
            print(json.dumps({{
                "Trail": {{
                    "Name": "teamagent-dev-trail",
                    "S3BucketName": (
                        "teamagent-dev-cloudtrail-718959508629"
                    ),
                    "IsMultiRegionTrail": True,
                    "IncludeGlobalServiceEvents": True,
                    "LogFileValidationEnabled": True,
                    "KmsKeyId": (
                        "arn:aws:kms:ap-northeast-1:718959508629:key/"
                        "11111111-2222-3333-4444-555555555555"
                    ),
                }}
            }}))
        elif args[:2] == ["cloudtrail", "get-trail-status"]:
            print(json.dumps({{
                "IsLogging": True,
                "LatestDeliveryTime": "2026-07-18T00:00:00+00:00",
                "LatestDigestDeliveryTime": "2026-07-18T00:00:00+00:00",
            }}))
        elif args[:2] == [
            "bedrock",
            "get-model-invocation-logging-configuration",
        ]:
            print(json.dumps({{
                "loggingConfig": {{
                    "textDataDeliveryEnabled": True,
                    "embeddingDataDeliveryEnabled": True,
                    "imageDataDeliveryEnabled": False,
                    "videoDataDeliveryEnabled": False,
                    "s3Config": {{
                        "bucketName": (
                            "teamagent-dev-bedrock-logs-718959508629"
                        ),
                        "keyPrefix": "bedrock/",
                    }},
                }}
            }}))
        elif args[:2] == ["sns", "list-topics"]:
            topics = [{{
                "TopicArn": (
                    "arn:aws:sns:ap-northeast-1:718959508629:"
                    "teamagent-dev-openclaw-alarms"
                )
            }}]
            if os.environ.get("AWS_FAKE_LEGACY_ALARM_TOPIC"):
                topics.append({{
                    "TopicArn": (
                        "arn:aws:sns:ap-northeast-1:718959508629:"
                        "teamagent-dev-alarms"
                    )
                }})
            print(json.dumps({{"Topics": topics}}))
        elif args[:2] == ["sns", "list-subscriptions-by-topic"]:
            subscriptions = []
            if not os.environ.get("AWS_FAKE_NO_ALARM_DELIVERY"):
                subscriptions.append({{
                    "SubscriptionArn": (
                        "arn:aws:sns:ap-northeast-1:718959508629:"
                        "teamagent-dev-openclaw-alarms:confirmed"
                    ),
                    "Owner": ACCOUNT,
                    "Protocol": "email",
                    "Endpoint": "alerts@example.com",
                    "TopicArn": (
                        "arn:aws:sns:ap-northeast-1:718959508629:"
                        "teamagent-dev-openclaw-alarms"
                    ),
                }})
            print(json.dumps({{"Subscriptions": subscriptions}}))
        elif args[:2] == ["sns", "get-subscription-attributes"]:
            subscription_arn = args[args.index("--subscription-arn") + 1]
            attributes = {{
                "SubscriptionArn": subscription_arn,
                "TopicArn": (
                    "arn:aws:sns:ap-northeast-1:718959508629:"
                    "teamagent-dev-openclaw-alarms"
                ),
                "Protocol": "email",
                "Endpoint": "alerts@example.com",
                "PendingConfirmation": "false",
                "ConfirmationWasAuthenticated": "true",
                "RawMessageDelivery": "false",
            }}
            if os.environ.get("AWS_FAKE_SUBSCRIPTION_FILTER"):
                attributes["FilterPolicy"] = '{{"severity":["critical"]}}'
            print(json.dumps({{"Attributes": attributes}}))
        elif args[:2] == ["chatbot", "describe-slack-channel-configurations"]:
            print(json.dumps({{"SlackChannelConfigurations": []}}))
        elif args[:2] == ["chatbot", "list-microsoft-teams-channel-configurations"]:
            print(json.dumps({{"TeamChannelConfigurations": []}}))
        elif args[:2] == ["cloudwatch", "describe-alarms"]:
            actions = []
            if os.environ.get("AWS_FAKE_LEGACY_ALARM_ACTION"):
                actions = [
                    "arn:aws:sns:ap-northeast-1:718959508629:"
                    "teamagent-dev-alarms"
                ]
            print(json.dumps({{
                "MetricAlarms": [{{"AlarmActions": actions}}],
                "CompositeAlarms": [],
            }}))
        elif args[:2] == ["budgets", "describe-budgets"]:
            print(json.dumps({{
                "Budgets": [{{"BudgetName": "teamagent-dev-monthly-cost"}}],
            }}))
        elif args[:2] == ["budgets", "describe-notifications-for-budget"]:
            print(json.dumps({{
                "Notifications": [{{
                    "NotificationType": "ACTUAL",
                    "ComparisonOperator": "GREATER_THAN",
                    "Threshold": 80,
                    "ThresholdType": "PERCENTAGE",
                    "NotificationState": "OK",
                }}],
            }}))
        elif args[:2] == [
            "budgets",
            "describe-subscribers-for-notification",
        ]:
            address = (
                "arn:aws:sns:ap-northeast-1:718959508629:"
                "teamagent-dev-openclaw-alarms"
            )
            if os.environ.get("AWS_FAKE_LEGACY_BUDGET_ACTION"):
                address = (
                    "arn:aws:sns:ap-northeast-1:718959508629:"
                    "teamagent-dev-alarms"
                )
            print(json.dumps({{
                "Subscribers": [{{
                    "SubscriptionType": "SNS",
                    "Address": address,
                }}],
            }}))
        elif args[:2] == ["ce", "get-anomaly-subscriptions"]:
            address = (
                "arn:aws:sns:ap-northeast-1:718959508629:"
                "teamagent-dev-openclaw-alarms"
            )
            if os.environ.get("AWS_FAKE_LEGACY_ANOMALY_ACTION"):
                address = (
                    "arn:aws:sns:ap-northeast-1:718959508629:"
                    "teamagent-dev-alarms"
                )
            print(json.dumps({{
                "AnomalySubscriptions": [{{
                    "SubscriptionArn": (
                        "arn:aws:ce::718959508629:"
                        "anomalysubscription/fake"
                    ),
                    "Subscribers": [{{
                        "Type": "SNS",
                        "Address": address,
                    }}],
                }}],
            }}))
        elif args[:2] == ["s3api", "get-bucket-versioning"]:
            bucket = args[args.index("--bucket") + 1]
            state_path = os.environ.get("AWS_FAKE_VERSIONING_STATE")
            state = {{}}
            if state_path and pathlib.Path(state_path).exists():
                state = json.loads(
                    pathlib.Path(state_path).read_text(encoding="utf-8")
                )
            status = state.get(bucket)
            print(json.dumps({{"Status": status}} if status else {{}}))
        elif args[:2] == ["s3api", "put-bucket-versioning"]:
            bucket = args[args.index("--bucket") + 1]
            assert (
                args[args.index("--versioning-configuration") + 1]
                == "Status=Enabled"
            )
            state_path = pathlib.Path(
                os.environ["AWS_FAKE_VERSIONING_STATE"]
            )
            state = {{}}
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
            state[bucket] = "Enabled"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            print(json.dumps({{}}))
        elif args[:2] == ["s3api", "head-object"]:
            app_html = (
                b"fake current app html\\n"
                if os.environ.get("AWS_FAKE_APP_PROVENANCE_MISSING")
                else APP_HTML
            )
            print(json.dumps({{
                "ContentLength": len(app_html),
                "VersionId": "fake-current-version-1",
            }}))
        elif args[:2] == ["s3api", "get-object"]:
            version = args[args.index("--version-id") + 1]
            app_html = (
                b"fake current app html\\n"
                if os.environ.get("AWS_FAKE_APP_PROVENANCE_MISSING")
                else APP_HTML
            )
            pathlib.Path(args[-1]).write_bytes(app_html)
            print(json.dumps({{"VersionId": version}}))
        elif args[:2] == ["apigatewayv2", "get-api"]:
            print(json.dumps({{
                "ApiId": "esk97z9grh",
                "Name": "teamagent-connectweb-api",
                "ProtocolType": "HTTP",
                "DisableExecuteApiEndpoint": True,
                "ApiEndpoint": (
                    "https://esk97z9grh.execute-api."
                    "ap-northeast-1.amazonaws.com"
                ),
            }}))
        elif args[:2] == ["apigatewayv2", "get-stage"]:
            print(json.dumps({{
                "StageName": "$default",
                "AutoDeploy": True,
                "AccessLogSettings": {{
                    "DestinationArn": (
                        "arn:aws:logs:ap-northeast-1:718959508629:"
                        "log-group:/aws/apigateway/teamagent-dev-connect-web"
                    ),
                    "Format": json.dumps({{
                        "requestId": "$context.requestId",
                        "routeKey": "$context.routeKey",
                        "status": "$context.status",
                        "responseLength": "$context.responseLength",
                        "integrationStatus": "$context.integration.status",
                        "integrationLatency": "$context.integrationLatency",
                        "responseType": "$context.error.responseType",
                    }}),
                }},
                "DefaultRouteSettings": {{"DetailedMetricsEnabled": False}},
            }}))
        elif args[:2] == ["apigatewayv2", "get-api-mappings"]:
            print(json.dumps({{
                "Items": [{{
                    "ApiId": "esk97z9grh",
                    "Stage": "$default",
                    "ApiMappingKey": "",
                }}]
            }}))
        elif args[:2] == ["ecs", "describe-services"]:
            state = os.environ.get("AWS_FAKE_SERVICE_STATE", "stable")
            services = []
            for component in ("openclaw", "mcp", "connect_web"):
                suffix = {{
                    "openclaw": "openclaw",
                    "mcp": "mcp",
                    "connect_web": "connect-web",
                }}[component]
                name = f"teamagent-dev-{{suffix}}"
                td = task_arn(component)
                deployments = [{{
                    "status": "PRIMARY",
                    "rolloutState": "IN_PROGRESS" if state == "in_progress" else "COMPLETED",
                    "taskDefinition": td,
                }}]
                if state == "two_primary":
                    deployments.append(dict(deployments[0]))
                sg = {{
                    "openclaw": "sg-openclaw",
                    "mcp": "sg-mcp",
                    "connect_web": "sg-connect",
                }}[component]
                lbs = [] if component != "connect_web" else [{{
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
                    "availabilityZoneRebalancing": "ENABLED",
                    "deploymentConfiguration": {{
                        "maximumPercent": 100 if component == "openclaw" else 200,
                        "minimumHealthyPercent": 0 if component == "openclaw" else 100,
                        "deploymentCircuitBreaker": {{
                            "enable": True,
                            "rollback": True,
                        }},
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
        elif args[:2] == ["ecs", "list-tasks"]:
            task_arns = []
            if os.environ.get("AWS_FAKE_ACTIVE_INGEST_TASK"):
                task_arns = [
                    (
                        f"arn:aws:ecs:{{REGION}}:{{ACCOUNT}}:"
                        "task/teamagent-dev/active-ingest"
                    )
                ]
            print(json.dumps({{"taskArns": task_arns}}))
        elif args[:2] == ["ecs", "describe-tasks"]:
            requested = args[
                args.index("--tasks") + 1 : args.index("--output")
            ]
            print(json.dumps({{
                "failures": [],
                "tasks": [
                    {{
                        "taskArn": arn,
                        "desiredStatus": "RUNNING",
                        "lastStatus": "RUNNING",
                    }}
                    for arn in requested
                ],
            }}))
        elif args[:2] == ["ecs", "describe-clusters"]:
            print(json.dumps({{
                "failures": [],
                "clusters": [{{
                    "clusterName": "teamagent-dev",
                    "settings": [
                        {{"name": "containerInsights", "value": "enabled"}}
                    ],
                    "tags": [],
                }}],
            }}))
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
            image_override = os.environ.get(
                {{
                    "connect_web": "AWS_FAKE_CONNECT_IMAGE",
                    "ingest": "AWS_FAKE_INGEST_IMAGE",
                }}.get(component, "")
            )
            if image_override:
                task["containerDefinitions"][0]["image"] = image_override
            if component not in ("tiktok", "x_buzz"):
                task_environment = environment()
                if component == "connect_web":
                    task_environment.append({{
                        "name": "CONNECT_APP_HTML_S3_URI",
                        "value": (
                            "s3://teamagent-dev-raw-files/"
                            "codebuild/connect-web-app.html"
                        ),
                    }})
                task["containerDefinitions"][0]["environment"] = task_environment
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
        elif args[:2] == ["secretsmanager", "describe-secret"]:
            secret_id = args[args.index("--secret-id") + 1]
            print(json.dumps({{
                "ARN": secret_id,
                "Name": secret_id.split(":secret:", 1)[1],
            }}))
        elif args[:2] == ["secretsmanager", "list-secret-version-ids"]:
            print(json.dumps({{
                "Versions": [{{
                    "VersionId": "01234567-89ab-cdef-0123-456789abcdef",
                    "VersionStages": ["AWSCURRENT"],
                }}]
            }}))
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

        def state_data():
            state_path = os.environ.get("TF_FAKE_STATE")
            if state_path:
                return json.loads(pathlib.Path(state_path).read_text(encoding="utf-8"))
            return {
                "version": 4,
                "terraform_version": "1.12.2",
                "serial": 42,
                "lineage": "01234567-89ab-cdef-0123-456789abcdef",
                "outputs": {},
                "resources": [],
            }

        def state_addresses(state):
            addresses = []
            for resource in state.get("resources", []):
                prefix = resource.get("module", "")
                if prefix:
                    prefix += "."
                base = prefix + resource["type"] + "." + resource["name"]
                for instance in resource.get("instances", []):
                    if "index_key" not in instance:
                        addresses.append(base)
                    elif isinstance(instance["index_key"], int):
                        addresses.append(f"{base}[{instance['index_key']}]")
                    else:
                        addresses.append(
                            base
                            + "["
                            + json.dumps(instance["index_key"], separators=(",", ":"))
                            + "]"
                        )
            return sorted(addresses)

        if args[:2] == ["workspace", "show"]:
            print(os.environ.get("TF_FAKE_WORKSPACE", "default"))
        elif args[:2] == ["state", "pull"]:
            print(json.dumps(state_data(), sort_keys=True))
        elif args[:2] == ["state", "list"]:
            state_list_path = os.environ.get("TF_FAKE_STATE_LIST")
            if state_list_path:
                sys.stdout.write(
                    pathlib.Path(state_list_path).read_text(encoding="utf-8")
                )
            else:
                for address in state_addresses(state_data()):
                    print(address)
        elif args[0] == "plan":
            out = next(arg.split("=", 1)[1] for arg in args if arg.startswith("-out="))
            core_arg = next(arg for arg in args if arg.startswith("-var=runtime_guard_live="))
            core = json.loads(core_arg.split("=", 2)[2])
            desired = core["desired_mcp_image"]
            plan = json.loads(pathlib.Path(os.environ["TF_FAKE_TEMPLATE"]).read_text())
            plan["variables"] = {
                "openclaw_image": {"value": core["desired_openclaw_image"]},
                "mcp_image": {"value": desired},
                "x_buzz_image": {"value": core["desired_x_image"]},
                "tiktok_acquire_image": {"value": core["desired_tiktok_image"]},
                "ingest_rule_enabled": {"value": core["ingest_rule_enabled"]},
                "morning_digest_rule_enabled": {
                    "value": core["morning_digest_rule_enabled"]
                },
                "canary_rule_enabled": {"value": core["canary_rule_enabled"]},
                "require_alarm_delivery": {"value": True},
                "bedrock_logs_retention_days": {"value": 60},
                "mail_action_hmac_secret_arn": {
                    "value": "arn:aws:secretsmanager:ap-northeast-1:718959508629:"
                    "secret:teamagent/dev/hmac/mail-action-AbC123"
                },
                "mail_action_hmac_previous_secret_arn": {"value": ""},
                "mail_action_hmac_previous_rotation_started_at": {"value": None},
                "report_link_hmac_secret_arn": {
                    "value": "arn:aws:secretsmanager:ap-northeast-1:718959508629:"
                    "secret:teamagent/dev/hmac/report-link-XyZ789"
                },
                "report_link_hmac_previous_secret_arn": {"value": ""},
                "report_link_hmac_previous_rotation_started_at": {"value": None},
                "runtime_guard_live": {"value": core},
            }
            for change in plan["resource_changes"]:
                if change["type"] == "aws_ecs_task_definition":
                    containers = json.loads(change["change"]["after"]["container_definitions"])
                    image_by_address = {
                        "aws_ecs_task_definition.openclaw[0]": core[
                            "desired_openclaw_image"
                        ],
                        "aws_ecs_task_definition.tiktok_acquire[0]": core[
                            "desired_tiktok_image"
                        ],
                        "aws_ecs_task_definition.x_buzz_worker[0]": core[
                            "desired_x_image"
                        ],
                    }
                    containers[0]["image"] = image_by_address.get(change["address"], desired)
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
    var_file.write_text(
        'alarm_email_endpoints = ["alerts@example.com"]\n',
        encoding="utf-8",
    )
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


def _plan_command(var_file: Path, output: Path) -> list[str]:
    return [
        "bash",
        str(GUARD),
        "plan",
        "--var-file",
        str(var_file),
        "--out",
        str(output),
        "--runtime-sync",
    ]


def _run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _normalize_lambda_tf(value: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        [
            "jq",
            "-L",
            str(GUARD.parent),
            "-c",
            'include "terraform_runtime_guard"; guard_lambda_from_tf',
        ],
        input=json.dumps(value),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _run_dispatcher_migration_validator(
    tmp_path: Path,
    scenario: str = "safe",
) -> subprocess.CompletedProcess[str]:
    plan = _safe_plan()
    _mutate_plan(plan, scenario)
    snapshot = {
        "dispatchers": {
            component: {"critical": _normalize_lambda_tf(_lambda_tf(component))}
            for component in ("tiktok", "x_buzz")
        },
        "taskdefs": {
            component: {"arn": _task_arn(component)} for component in ("tiktok", "x_buzz")
        },
    }
    core = {
        "tiktok_dispatch_static_environment": {
            key: value
            for key, value in _dispatcher_environment("tiktok").items()
            if key != "TASKDEF_ARN"
        },
        "x_dispatch_static_environment": {
            key: value
            for key, value in _dispatcher_environment("x_buzz").items()
            if key != "TASKDEF_ARN"
        },
    }
    migration = {
        "to": {
            "dispatcher_code_sha256": {
                component: DISPATCHERS[component]["code_sha256"]
                for component in ("tiktok", "x_buzz")
            }
        }
    }
    paths: list[Path] = []
    for name, value in (
        ("plan", plan),
        ("snapshot", snapshot),
        ("core", core),
        ("migration", migration),
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        paths.append(path)

    guard = GUARD.read_text(encoding="utf-8")
    function = re.search(
        r"validate_dispatcher_migration_plan\(\) \{.*?"
        r"(?=\nvalidate_runtime_rule_staging\(\))",
        guard,
        flags=re.DOTALL,
    )
    assert function is not None
    script = "\n".join(
        (
            "set -euo pipefail",
            f"GUARD_JQ_DIR={str(GUARD.parent)!r}",
            'die() { echo "★ $*" >&2; return 1; }',
            function.group(0),
            'validate_dispatcher_migration_plan "$1" "$2" "$3" "$4"',
        )
    )
    return subprocess.run(
        ["bash", "-c", script, "validator", *(str(path) for path in paths)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_dispatcher_migration_validator_accepts_exact_archive_and_taskdef_only(
    tmp_path: Path,
) -> None:
    result = _run_dispatcher_migration_validator(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "scenario",
    [
        "lambda_static_env",
        "lambda_role",
        "lambda_code",
        "lambda_handler",
        "lambda_runtime",
        "lambda_timeout",
        "lambda_kms",
        "lambda_vpc",
        "lambda_source_reference",
        "lambda_filename_reference",
    ],
)
def test_dispatcher_migration_validator_rejects_non_allowlisted_changes(
    tmp_path: Path,
    scenario: str,
) -> None:
    result = _run_dispatcher_migration_validator(tmp_path, scenario)
    assert result.returncode == 1
    assert "destination code hash/taskdef参照以外" in result.stderr


def test_safe_sync_publishes_private_fully_bound_artifacts(tmp_path: Path) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    plan = tmp_path / "runtime.tfplan"
    result = _run(_plan_command(var_file, plan), env)

    assert result.returncode == 0, result.stdout + result.stderr
    receipt = Path(f"{plan}.runtime-guard.json")
    assert plan.is_file() and receipt.is_file()
    assert stat.S_IMODE(plan.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    data = json.loads(receipt.read_text(encoding="utf-8"))
    assert data["plan_path"] == str(plan)
    assert data["receipt_path"] == str(receipt)
    assert data["var_file"] == str(var_file)
    assert data["images"]["desired"] == {
        "openclaw": OPENCLAW_IMAGE,
        "mcp": LIVE_IMAGE,
        "x_buzz": X_IMAGE,
        "tiktok": TIKTOK_IMAGE,
    }
    assert data["rule_states"]["desired"] == {
        "ingest": False,
        "morning": True,
        "canary": False,
    }
    assert data["mode"] == "sync"
    assert data["migration_id"] == ""
    assert data["preflight_receipt_sha256"] == ""
    assert data["versioning_receipt_path"] == ""
    assert data["versioning_receipt_sha256"] == ""
    assert len(data["guard_script_sha256"]) == 64
    assert len(data["guard_jq_sha256"]) == 64
    assert len(data["config_manifest_sha256"]) == 64
    assert len(data["hmac_transition_sha256"]) == 64
    assert data["state_contract"]["backend"] == {
        "bucket": "teamagent-tfstate-718959508629",
        "key": "teamagent/terraform.tfstate",
        "region": REGION,
        "workspace": "default",
    }
    assert data["state_contract"]["state"] == {
        "address_count": 0,
        "address_set_sha256": ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
        "lineage": "01234567-89ab-cdef-0123-456789abcdef",
        "serial": 42,
    }
    assert set(data["state_contract"]["imports"]) == {
        "aws_cloudwatch_log_group.codebuild_aiia_image_builder",
        "aws_cloudwatch_log_group.codebuild_image_builder",
        "aws_cloudwatch_log_group.reminder_notify",
        "aws_cloudwatch_log_group.tiktok_dispatch",
        "aws_cloudwatch_log_group.x_dispatch",
    }
    assert all(item["present"] is False for item in data["state_contract"]["imports"].values())
    assert "apply" not in tf_log.read_text(encoding="utf-8")

    verify = _run(["bash", str(GUARD), "verify", "--plan", str(plan)], env)
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert "read-only検証完了" in verify.stdout
    assert "apply" not in tf_log.read_text(encoding="utf-8")


def test_versioning_stage_is_fail_closed_while_review_manifest_is_disabled(
    tmp_path: Path,
) -> None:
    env, _, tf_log = _harness(tmp_path)
    env["AWS_FAKE_TRUSTED_AUTOMATION"] = "1"
    versioning_state = tmp_path / "versioning-state.json"
    env["AWS_FAKE_VERSIONING_STATE"] = str(versioning_state)

    receipt = tmp_path / "log-versioning.json"
    result = _run(
        [
            "bash",
            str(GUARD),
            "enable-log-versioning",
            "--out",
            str(receipt),
        ],
        env,
    )
    assert result.returncode == 1
    assert "review済みmanifest" in result.stdout + result.stderr
    assert not receipt.exists()
    assert not versioning_state.exists()
    assert "plan " not in tf_log.read_text(encoding="utf-8")


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
        "lambda_handler",
        "lambda_runtime",
        "lambda_timeout",
        "lambda_kms",
        "lambda_vpc",
        "lambda_source_reference",
        "lambda_filename_reference",
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
        "incomplete",
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


def test_connect_app_without_bound_provenance_fails_before_plan(tmp_path: Path) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    env["AWS_FAKE_APP_PROVENANCE_MISSING"] = "1"
    plan = tmp_path / "missing-app-provenance.tfplan"

    result = _run(_plan_command(var_file, plan), env)

    assert result.returncode == 1
    assert "Vault manifest/build inputs provenance" in result.stdout + result.stderr
    assert "plan " not in tf_log.read_text(encoding="utf-8")
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


def test_divergent_live_core_images_require_exact_migration_without_rollback(
    tmp_path: Path,
) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    env["AWS_FAKE_CONNECT_IMAGE"] = f"{REPOSITORY}@sha256:{'a' * 64}"
    env["AWS_FAKE_INGEST_IMAGE"] = f"{REPOSITORY}@sha256:{'b' * 64}"

    result = _run(_plan_command(var_file, tmp_path / "divergent.tfplan"), env)

    assert result.returncode == 1
    assert "divergent live" in result.stdout + result.stderr
    assert "plan " not in tf_log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "marker",
    [
        "AWS_FAKE_NO_ALARM_DELIVERY",
        "AWS_FAKE_LEGACY_ALARM_TOPIC",
        "AWS_FAKE_LEGACY_ALARM_ACTION",
        "AWS_FAKE_LEGACY_BUDGET_ACTION",
        "AWS_FAKE_LEGACY_ANOMALY_ACTION",
    ],
)
def test_alarm_delivery_and_legacy_topic_contract_fail_before_plan(
    tmp_path: Path,
    marker: str,
) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    env[marker] = "1"

    result = _run(_plan_command(var_file, tmp_path / "alarm.tfplan"), env)

    assert result.returncode == 1
    assert "alarm delivery" in result.stdout + result.stderr
    assert "plan " not in tf_log.read_text(encoding="utf-8")


def test_alarm_subscription_filter_policy_fails_before_plan(tmp_path: Path) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    env["AWS_FAKE_SUBSCRIPTION_FILTER"] = "1"

    result = _run(_plan_command(var_file, tmp_path / "filtered.tfplan"), env)

    assert result.returncode == 1
    assert "no-filter exact" in result.stdout + result.stderr
    assert "plan " not in tf_log.read_text(encoding="utf-8")


def test_invalid_bool_value_is_not_echoed(tmp_path: Path) -> None:
    env, var_file, _ = _harness(tmp_path)
    secret_value = "DO_NOT_LOG_THIS_VALUE"
    env["AWS_FAKE_INVALID_BOOL"] = secret_value
    result = _run(_plan_command(var_file, tmp_path / "bool.tfplan"), env)
    assert result.returncode == 1
    assert secret_value not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "variable_name",
    [
        "TF_CLI_ARGS",
        "TF_CLI_ARGS_plan",
        "TF_WORKSPACE",
        "TF_DATA_DIR",
        "TF_VAR_MAIL_ACTION_HMAC_SECRET",
        "TF_CLI_CONFIG_FILE",
        "TF_REATTACH_PROVIDERS",
        "TF_LOG_PATH",
    ],
)
def test_terraform_environment_injection_is_removed_and_rejected_without_value_leak(
    tmp_path: Path,
    variable_name: str,
) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    secret_value = "DO_NOT_LOG_TERRAFORM_ENV_VALUE"
    env[variable_name] = secret_value

    result = _run(_plan_command(var_file, tmp_path / "env-injected.tfplan"), env)

    assert result.returncode == 1
    assert variable_name in result.stdout + result.stderr
    assert secret_value not in result.stdout + result.stderr
    assert "plan " not in tf_log.read_text(encoding="utf-8")


def test_workspace_state_list_and_import_ownership_collisions_fail_closed(
    tmp_path: Path,
) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    env["TF_FAKE_WORKSPACE"] = "production"
    result = _run(_plan_command(var_file, tmp_path / "workspace.tfplan"), env)
    assert result.returncode == 1
    assert "workspace" in result.stdout + result.stderr
    assert "plan " not in tf_log.read_text(encoding="utf-8")

    env.pop("TF_FAKE_WORKSPACE")
    state_list = tmp_path / "state-list.txt"
    state_list.write_text("aws_cloudwatch_log_group.untracked\n", encoding="utf-8")
    state_list.chmod(0o600)
    env["TF_FAKE_STATE_LIST"] = str(state_list)
    result = _run(_plan_command(var_file, tmp_path / "state-list.tfplan"), env)
    assert result.returncode == 1
    assert "state pull/list" in result.stdout + result.stderr
    assert "plan " not in tf_log.read_text(encoding="utf-8")

    env.pop("TF_FAKE_STATE_LIST")
    state = tmp_path / "collision-state.json"
    state.write_text(
        json.dumps(
            {
                "version": 4,
                "terraform_version": "1.12.2",
                "serial": 42,
                "lineage": "01234567-89ab-cdef-0123-456789abcdef",
                "outputs": {},
                "resources": [
                    {
                        "mode": "managed",
                        "type": "aws_cloudwatch_log_group",
                        "name": "unrelated_owner",
                        "provider": ('provider["registry.terraform.io/hashicorp/aws"]'),
                        "instances": [
                            {
                                "schema_version": 0,
                                "attributes": {
                                    "id": ("/aws/lambda/teamagent-dev-reminders-notify"),
                                    "name": ("/aws/lambda/teamagent-dev-reminders-notify"),
                                },
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state.chmod(0o600)
    env["TF_FAKE_STATE"] = str(state)
    result = _run(_plan_command(var_file, tmp_path / "collision.tfplan"), env)
    assert result.returncode == 1
    assert "state lineage/serial/address/import ownership" in (result.stdout + result.stderr)
    assert "plan " not in tf_log.read_text(encoding="utf-8")


def test_ad_hoc_rollout_is_rejected_and_migration_requires_preflight(
    tmp_path: Path,
) -> None:
    env, var_file, _ = _harness(tmp_path)
    command = _plan_command(var_file, tmp_path / "bad.tfplan")
    command[-1:] = ["--runtime-rollout-image", LIVE_IMAGE]
    result = _run(command, env)
    assert result.returncode == 1
    assert "不明な引数" in result.stdout + result.stderr

    command = _plan_command(var_file, tmp_path / "migration.tfplan")
    command[-1:] = ["--runtime-migration", "2026-07-wolfi-runtime-v1"]
    result = _run(command, env)
    assert result.returncode == 1
    assert "--preflight-receipt" in result.stdout + result.stderr


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

    apply = _run(
        [
            "bash",
            str(GUARD),
            "apply",
            "--plan",
            str(second),
            "--out",
            str(tmp_path / "apply-receipt.json"),
        ],
        env,
    )
    assert apply.returncode == 1
    assert "exact trusted automation role" in apply.stdout + apply.stderr
    assert "apply" not in tf_log.read_text(encoding="utf-8")
