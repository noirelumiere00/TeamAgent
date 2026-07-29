"""Terraform runtime validatorをAWS書込みなしの敵対fixtureで検証する。"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
GUARD = PROJECT_ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh"
CODEBUILD_DIR = PROJECT_ROOT / "infra" / "codebuild"
if str(CODEBUILD_DIR) not in sys.path:
    sys.path.insert(0, str(CODEBUILD_DIR))
CONSUMER_REGISTRY = CODEBUILD_DIR / "image_deployment_consumers.json"
CONSUMER_REGISTRY_MODULE = CODEBUILD_DIR / "image_deployment_consumers.py"
RELEASE_EVIDENCE = CODEBUILD_DIR / "release_evidence.py"
RELEASE_CONTRACTS = {
    "mcp": CODEBUILD_DIR / "teamagent_core_media_release_contract.json",
    "openclaw": CODEBUILD_DIR / "openclaw_bundle_contract.json",
}
ACCOUNT = "718959508629"
REGION = "ap-northeast-1"
REPOSITORY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/teamagent-mcp"
LIVE_IMAGE = f"{REPOSITORY}@sha256:fb44f7cdb19c7f683768fe074aa85ba3a99fdefe7b6c9e49422e46055bb458b5"
CONNECT_WEB_IMAGE = (
    f"{REPOSITORY}@sha256:0f23860dc382e29d2051f3e6e415a427c853182d90ef05cce0935c3c7cecc144"
)
X_IMAGE = f"{REPOSITORY}@sha256:1747d2d0729d2c30ae04ab4d21dc9dc10c1351553684eb10303e157f58a227e8"
CANARY_IMAGE = f"{REPOSITORY}@sha256:{'5' * 64}"
INGEST_IMAGE = f"{REPOSITORY}@sha256:{'6' * 64}"
MORNING_IMAGE = f"{REPOSITORY}@sha256:{'7' * 64}"
LEGACY_TIKTOK_REPOSITORY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/teamagent-dev-tiktok-acquire"
LEGACY_TIKTOK_IMAGE = f"{LEGACY_TIKTOK_REPOSITORY}@sha256:{'e' * 64}"
MEDIA_WORKER_REPOSITORY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/teamagent-media-worker"
MEDIA_WORKER_IMAGE = f"{MEDIA_WORKER_REPOSITORY}@sha256:{'9' * 64}"
OPENCLAW_REPOSITORY = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/teamagent-openclaw"
OPENCLAW_IMAGE = (
    f"{OPENCLAW_REPOSITORY}@sha256:9cde4c829335ba5196186df0460db29eb6dbe31d3f212d095f6367d1b98be8af"
)
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
APP_VERSION_ID = "fake-current-version-1"
APP_SHA256 = hashlib.sha256(APP_HTML).hexdigest()
MCP_APPLICATION_PROVENANCE = {
    "bucket": "teamagent-dev-raw-files",
    "key": "codebuild/connect-web-app.html",
    "version_id": APP_VERSION_ID,
    "sha256": APP_SHA256,
    "vault_manifest_sha256": APP_VAULT_MANIFEST_SHA256,
    "build_inputs_sha256": APP_BUILD_INPUTS_SHA256,
    "baked_fallback_version_id": APP_VERSION_ID,
    "baked_fallback_sha256": APP_SHA256,
}
MAIL_HMAC_SECRET = (
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:teamagent/dev/hmac/mail-action-AbC123"
)
REPORT_HMAC_SECRET = (
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:teamagent/dev/hmac/report-link-XyZ789"
)
HMAC_MANIFEST_SHA256 = "3" * 64
HMAC_CONTROL_SHA256 = "4" * 64
HMAC_RELEASE = {
    "rotation_epoch": "hmac-2026-07",
    "gate_mode": "candidate",
    "cleanup_domain": "",
    "manifest_sha256": HMAC_MANIFEST_SHA256,
    "rollout_control_sha256": HMAC_CONTROL_SHA256,
    "worker_enabled": False,
    "worker_mode": "candidate",
    "worker_artifacts": {},
    "worker_provenance_key_arn": "",
}


def _load_consumer_registry_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "runtime_guard_consumer_registry_under_test",
        CONSUMER_REGISTRY_MODULE,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CONSUMERS = _load_consumer_registry_module()
CONSUMER_REGISTRY_DATA = CONSUMERS.load_consumer_registry()


def _release_contract_bindings() -> tuple[dict[str, str], dict[str, bool]]:
    contracts: dict[str, str] = {}
    ready: dict[str, bool] = {}
    for pipeline, path in RELEASE_CONTRACTS.items():
        contracts[pipeline] = hashlib.sha256(path.read_bytes()).hexdigest()
        value = json.loads(path.read_text(encoding="utf-8"))
        release_ready = value["release"]["ready"]
        assert isinstance(release_ready, bool)
        ready[pipeline] = release_ready
    return contracts, ready


@pytest.fixture(scope="module", autouse=True)
def _canonical_backend_metadata_for_guard_tests() -> None:
    """Guard subprocesses must not depend on a developer's prior terraform init."""
    metadata = PROJECT_ROOT / "infra" / "terraform" / ".terraform" / "terraform.tfstate"
    previous = metadata.read_bytes() if metadata.exists() else None
    previous_mode = stat.S_IMODE(metadata.stat().st_mode) if metadata.exists() else None
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(
        json.dumps(
            {
                "backend": {
                    "type": "s3",
                    "config": {
                        "bucket": "teamagent-tfstate-718959508629",
                        "key": "teamagent/terraform.tfstate",
                        "region": REGION,
                        "dynamodb_table": "teamagent-tflock",
                        "encrypt": True,
                        "access_key": None,
                        "secret_key": None,
                        "token": None,
                    },
                }
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    metadata.chmod(0o600)
    try:
        yield
    finally:
        if previous is None:
            metadata.unlink(missing_ok=True)
        else:
            metadata.write_bytes(previous)
            assert previous_mode is not None
            metadata.chmod(previous_mode)


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
HMAC_TASK_ADDRESSES = {
    workload: TASK_ADDRESSES[workload] for workload in ("mcp", "connect_web", "morning")
}
HMAC_WORKLOADS = {
    "mcp": "mcp",
    "connect_web": "connect_web",
    "morning": "morning_digest",
}
HMAC_LIVE_GATE_ADDRESSES = {
    workload: f'terraform_data.hmac_live_task_gate["{name}"]'
    for workload, name in HMAC_WORKLOADS.items()
}
HMAC_SERVICE_GATE_ADDRESSES = {
    "mcp": (
        "terraform_data.hmac_mcp_pre_update[0]",
        "terraform_data.hmac_mcp_post_update[0]",
    ),
    "connect_web": (
        "terraform_data.hmac_connect_web_pre_update[0]",
        "terraform_data.hmac_connect_web_post_update[0]",
    ),
}
HMAC_MORNING_GATE_ADDRESSES = (
    "terraform_data.hmac_morning_digest_pre_update[0]",
    "terraform_data.hmac_morning_digest_post_update[0]",
)
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


def _task_state_attributes(component: str) -> dict[str, Any]:
    _, family, revision = COMPONENTS[component]
    return {
        "arn": _task_arn(component),
        "family": family,
        "revision": revision,
    }


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
    elif component == "connect_web":
        image = CONNECT_WEB_IMAGE
    elif component == "canary":
        image = CANARY_IMAGE
    elif component == "ingest":
        image = INGEST_IMAGE
    elif component == "morning":
        image = MORNING_IMAGE
    elif component == "tiktok":
        image = MEDIA_WORKER_IMAGE
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
        "reserved_concurrent_executions": 2 if component == "tiktok" else -1,
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
        "wait_for_steady_state": component in {"mcp", "connect_web"},
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
        "target_id": "morning" if component == "morning" else f"target-{component}",
        "arn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/teamagent-dev",
        "role_arn": f"arn:aws:iam::{ACCOUNT}:role/events-{component}",
        "input": "{}" if component == "morning" else "",
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


def _hmac_common_gate_input(action: str, workload: str) -> dict[str, Any]:
    return {
        "action": action,
        "workload": workload,
        "mode": HMAC_RELEASE["gate_mode"],
        "rotation_epoch": HMAC_RELEASE["rotation_epoch"],
        "cleanup_domain": HMAC_RELEASE["cleanup_domain"],
        "manifest_sha256": HMAC_MANIFEST_SHA256,
        "rollout_control_sha256": HMAC_CONTROL_SHA256,
    }


def _hmac_target_from_tf(value: dict[str, Any]) -> dict[str, Any]:
    ecs = value["ecs_target"][0]
    network = ecs["network_configuration"][0]
    retry = value["retry_policy"][0]
    return {
        "Id": value["target_id"],
        "Arn": value["arn"],
        "RoleArn": value["role_arn"],
        "Input": value["input"],
        "EcsParameters": {
            "TaskDefinitionArn": ecs["task_definition_arn"],
            "TaskCount": ecs["task_count"],
            "LaunchType": ecs["launch_type"],
            "PlatformVersion": ecs["platform_version"],
            "NetworkConfiguration": {
                "awsvpcConfiguration": {
                    "Subnets": sorted(network["subnets"]),
                    "SecurityGroups": sorted(network["security_groups"]),
                    "AssignPublicIp": (
                        "ENABLED" if network["assign_public_ip"] is True else "DISABLED"
                    ),
                }
            },
        },
        "RetryPolicy": {
            "MaximumEventAgeInSeconds": retry["maximum_event_age_in_seconds"],
            "MaximumRetryAttempts": retry["maximum_retry_attempts"],
        },
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
        "provider_name": "registry.terraform.io/hashicorp/aws",
        "change": {
            "actions": actions,
            "before": before,
            "after": after,
            "after_unknown": {},
        },
    }


CONSUMER_COMPONENTS = {
    "mcp": "mcp",
    "connect_web": "connect_web",
    "openclaw": "openclaw",
    "canary": "canary",
    "ingest": "ingest",
    "morning_digest": "morning",
    "x_buzz_worker": "x_buzz",
    "tiktok_acquire": "tiktok",
}
CONSUMER_MANIFEST_IDENTITY_KEYS = (
    "consumer_id",
    "terraform_task_definition_address",
    "ecs_family",
    "container_name",
    "activator",
    "release_repository",
    "receipt",
)
CONTAINER_DEFINITION_COMPARE_FIELDS = (
    "name",
    "image",
    "command",
    "entryPoint",
    "environment",
    "secrets",
    "user",
    "privileged",
    "readonlyRootFilesystem",
    "linuxParameters",
    "mountPoints",
    "logConfiguration",
)
TASK_DEFINITION_COMPARE_FIELDS = (
    "container_definitions",
    "task_role_arn",
    "execution_role_arn",
    "network_mode",
    "cpu",
    "memory",
    "volumes",
)


def _consumer_task_definition(component: str) -> dict[str, Any]:
    task = _task_after(component)
    container_definitions = json.loads(task["container_definitions"])
    for container in container_definitions:
        for field in CONTAINER_DEFINITION_COMPARE_FIELDS:
            container.setdefault(field, None)
    return {
        "container_definitions": container_definitions,
        "task_role_arn": task["task_role_arn"],
        "execution_role_arn": task["execution_role_arn"],
        "network_mode": task["network_mode"],
        "cpu": task["cpu"],
        "memory": task["memory"],
        "volumes": task.get("volumes"),
    }


def _consumer_snapshot(consumer: dict[str, Any]) -> dict[str, Any]:
    consumer_id = consumer["consumer_id"]
    component = CONSUMER_COMPONENTS[consumer_id]
    task_definition_arn = _task_arn(component)
    task_definition = _consumer_task_definition(component)
    named_containers = [
        container
        for container in task_definition["container_definitions"]
        if container["name"] == consumer["container_name"]
    ]
    assert len(named_containers) == 1
    activator_type = consumer["activator"]["type"]
    if activator_type == "ecs_service":
        activation = {
            "desired_count": 1,
            "task_definition_arn": task_definition_arn,
        }
    elif activator_type == "eventbridge_rule_ecs_target":
        activation = {
            "state": RULES[component][2],
            "task_definition_arn": task_definition_arn,
        }
    else:
        activation = {
            "event_source_mapping_enabled": True,
            "task_definition_arn": task_definition_arn,
        }
    return {
        "image": named_containers[0]["image"],
        "task_definition_arn": task_definition_arn,
        "task_definition": task_definition,
        "activation": activation,
    }


def _consumer_manifest() -> dict[str, Any]:
    consumers: list[dict[str, Any]] = []
    for consumer in CONSUMER_REGISTRY_DATA["consumers"]:
        live = _consumer_snapshot(consumer)
        consumers.append(
            {
                **{key: copy.deepcopy(consumer[key]) for key in CONSUMER_MANIFEST_IDENTITY_KEYS},
                "live": copy.deepcopy(live),
                "before": copy.deepcopy(live),
                "after": copy.deepcopy(live),
            }
        )
    return {
        "schema_version": 1,
        "registry_sha256": CONSUMERS.consumer_registry_sha256(),
        "mode": "no-image-transition",
        "consumers": consumers,
    }


def _state_address_parts(address: str) -> tuple[str, int | str | None]:
    match = re.fullmatch(r"(?P<base>.+?)(?:\[(?P<index>.+)\])?", address)
    assert match is not None
    base = match.group("base")
    encoded_index = match.group("index")
    if encoded_index is None:
        return base, None
    if encoded_index.startswith('"'):
        index = json.loads(encoded_index)
        assert isinstance(index, str)
        return base, index
    return base, int(encoded_index)


def _fake_state_from_plan(plan: dict[str, Any]) -> dict[str, Any]:
    resources: dict[str, dict[str, Any]] = {}
    component_by_task_address = {
        address: component for component, address in TASK_ADDRESSES.items()
    }
    for change in plan["resource_changes"]:
        if change.get("mode", "managed") != "managed":
            continue
        before = change["change"].get("before")
        if before is None:
            continue
        address = change["address"]
        base, index_key = _state_address_parts(address)
        resource_type, name = base.split(".", 1)
        resource = resources.setdefault(
            base,
            {
                "mode": "managed",
                "type": resource_type,
                "name": name,
                "provider": (
                    'provider["terraform.io/builtin/terraform"]'
                    if resource_type == "terraform_data"
                    else 'provider["registry.terraform.io/hashicorp/aws"]'
                ),
                "instances": [],
            },
        )
        attributes = copy.deepcopy(before)
        component = component_by_task_address.get(address)
        if component is not None:
            attributes.update(_task_state_attributes(component))
            attributes["id"] = _task_arn(component)
        instance: dict[str, Any] = {
            "schema_version": 0,
            "attributes": attributes,
        }
        if index_key is not None:
            instance["index_key"] = index_key
        resource["instances"].append(instance)
    return {
        "version": 4,
        "terraform_version": "1.12.2",
        "serial": 42,
        "lineage": "01234567-89ab-cdef-0123-456789abcdef",
        "outputs": {},
        "resources": list(resources.values()),
    }


def _safe_plan() -> dict[str, Any]:
    changes: list[dict[str, Any]] = [
        _change(
            "terraform_data.runtime_guard",
            "terraform_data",
            ["no-op"],
            {"input": {"verified": True}},
            {"input": {"verified": True}},
        ),
        _change(
            "terraform_data.production_image_release_gate",
            "terraform_data",
            ["create", "delete"],
            {"input": {"intent": "previous"}},
            {"input": {"intent": "current"}},
        ),
    ]
    for component, address in TASK_ADDRESSES.items():
        after = _task_after(component)
        before = copy.deepcopy(after)
        before["arn"] = _task_arn(component)
        before["id"] = _task_arn(component)
        if component == "morning":
            after["arn"] = _task_arn(component)
            after["id"] = _task_arn(component)
        change = _change(
            address,
            "aws_ecs_task_definition",
            ["no-op"] if component == "morning" else ["create", "delete"],
            before,
            after,
        )
        if component != "morning":
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
        if component != "morning":
            after["ecs_target"][0]["task_definition_arn"] = None
        change = _change(
            address,
            "aws_cloudwatch_event_target",
            ["no-op"] if component == "morning" else ["update"],
            before,
            after,
        )
        if component != "morning":
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

    configured_addresses = {item["address"] for item in configurations}
    for change in changes:
        if change["mode"] != "managed":
            continue
        address = change["address"].split("[", 1)[0]
        if address not in configured_addresses:
            configurations.append({"address": address, "expressions": {}})
            configured_addresses.add(address)

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


def _activate_hmac_plan(plan: dict[str, Any]) -> None:
    plan["variables"]["hmac_runtime_promotion_tasks"] = {
        "value": ["connect_web", "mcp", "morning_digest"]
    }
    production_gate = _find(plan, "terraform_data.production_image_release_gate")
    production_gate["change"]["after"]["input"]["hmac_release_bindings"] = copy.deepcopy(
        HMAC_RELEASE
    )

    morning_task = _find(plan, TASK_ADDRESSES["morning"])
    morning_task["change"]["actions"] = ["create", "delete"]
    morning_task["change"]["after"].pop("arn")
    morning_task["change"]["after"].pop("id")
    morning_task["change"]["after_unknown"] = {
        "arn": True,
        "arn_without_revision": True,
        "enable_fault_injection": True,
        "id": True,
        "revision": True,
    }

    morning_target = _find(
        plan,
        "aws_cloudwatch_event_target.morning_digest_run_task[0]",
    )
    morning_target["change"]["actions"] = ["update"]
    morning_target["change"]["after"]["ecs_target"][0]["task_definition_arn"] = None
    morning_target["change"]["after_unknown"] = {"ecs_target": [{"task_definition_arn": True}]}

    gate_changes: list[dict[str, Any]] = []
    for component, workload in HMAC_WORKLOADS.items():
        live_input = {
            **_hmac_common_gate_input("pre-register", workload),
            "task_address": HMAC_TASK_ADDRESSES[component],
        }
        gate_changes.append(
            _change(
                HMAC_LIVE_GATE_ADDRESSES[component],
                "terraform_data",
                ["create", "delete"],
                None,
                {"input": live_input},
            )
        )

    for component, addresses in HMAC_SERVICE_GATE_ADDRESSES.items():
        for action, address in zip(
            ("pre-update", "post-update"),
            addresses,
            strict=True,
        ):
            gate_changes.append(
                _change(
                    address,
                    "terraform_data",
                    ["create", "delete"],
                    None,
                    {
                        "input": {
                            **_hmac_common_gate_input(action, component),
                            "task_definition_arn": None,
                        }
                    },
                )
            )

    morning_after = morning_target["change"]["after"]
    _, rule_name, _, schedule = RULES["morning"]
    expected_rule = {
        "Name": rule_name,
        "Arn": f"arn:aws:events:{REGION}:{ACCOUNT}:rule/{rule_name}",
        "State": "DISABLED",
        "ScheduleExpression": schedule,
        "Description": "morning schedule",
        "EventBusName": "default",
    }
    target = _hmac_target_from_tf(morning_after)
    for action, address in zip(
        ("pre-update", "post-update"),
        HMAC_MORNING_GATE_ADDRESSES,
        strict=True,
    ):
        gate_changes.append(
            _change(
                address,
                "terraform_data",
                ["create", "delete"],
                None,
                {
                    "input": {
                        **_hmac_common_gate_input(action, "morning_digest"),
                        "expected_rule": expected_rule,
                        "target": target,
                        "task_definition_arn": None,
                    }
                },
            )
        )

    plan["resource_changes"].extend(gate_changes)
    configurations = plan["configuration"]["root_module"]["resources"]
    configured_addresses = {item["address"] for item in configurations}
    for change in gate_changes:
        address = change["address"].split("[", 1)[0]
        if address not in configured_addresses:
            configurations.append({"address": address, "expressions": {}})
            configured_addresses.add(address)


def _stabilize_no_image_plan(
    plan: dict[str, Any],
    *,
    hmac_active: bool,
) -> None:
    # HMAC-selected task actions remain mutations so the exact promotion-gate
    # coverage is exercised; the image-consumer ARN/body/activation stays stable.
    hmac_task_addresses = set(HMAC_TASK_ADDRESSES.values()) if hmac_active else set()
    for address in TASK_ADDRESSES.values():
        task_change = _find(plan, address)["change"]
        task_change["actions"] = (
            ["create", "delete"] if address in hmac_task_addresses else ["no-op"]
        )
        task_change["after"] = copy.deepcopy(task_change["before"])
        task_change["after_unknown"] = {}

    activation_addresses = (
        "aws_ecs_service.openclaw[0]",
        "aws_ecs_service.mcp[0]",
        "aws_ecs_service.connect_web[0]",
        "aws_cloudwatch_event_target.ingest_run_task[0]",
        "aws_cloudwatch_event_target.morning_digest_run_task[0]",
        "aws_cloudwatch_event_target.canary_run_task[0]",
        "aws_lambda_function.tiktok_dispatch[0]",
        "aws_lambda_function.x_dispatch[0]",
    )
    for address in activation_addresses:
        activation_change = _find(plan, address)["change"]
        activation_change["actions"] = ["no-op"]
        activation_change["after"] = copy.deepcopy(activation_change["before"])
        activation_change["after_unknown"] = {}

    if hmac_active:
        morning_target = _find(
            plan,
            "aws_cloudwatch_event_target.morning_digest_run_task[0]",
        )["change"]["after"]
        target = _hmac_target_from_tf(morning_target)
        for address in HMAC_MORNING_GATE_ADDRESSES:
            gate_input = _find(plan, address)["change"]["after"]["input"]
            gate_input["target"] = copy.deepcopy(target)


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
        service["change"]["after"]["wait_for_steady_state"] = False
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
        task["change"]["actions"] = ["delete"]
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
        #!{sys.executable}
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
        if args == ["--version"]:
            print("aws-cli/2.27.0 Python/3.13.5 Darwin/24.5.0")
            raise SystemExit(0)
        if args[:2] == ["sts", "get-caller-identity"]:
            if "--query" not in args or args[-2:] != ["--output", "text"]:
                raise SystemExit("malformed deployment gate identity query")
            arn = (
                "arn:aws:sts::718959508629:assumed-role/"
                "teamagent-dev-terraform-runtime-automation/"
                "teamagent-terraform-worker"
            )
            print(f"{{ACCOUNT}}\\t{{arn}}")
            raise SystemExit(0)
        if args[:2] == ["sts", "assume-role"]:
            raise SystemExit("deployment gate must not chain into another role")
        if args[:2] == ["dynamodb", "put-item"]:
            if (
                "--region" not in args
                or args[args.index("--region") + 1] != REGION
                or os.environ.get("AWS_FAKE_TRUSTED_AUTOMATION") != "1"
            ):
                raise SystemExit("malformed deployment intent write")
            print(json.dumps({{}}))
            raise SystemExit(0)
        if (
            len(args) >= 2
            and args[0] == "--region"
            and args[1] in (REGION, "us-east-1", "us-east-2")
        ):
            command_region = args[1]
            args = args[2:]
        else:
            raise SystemExit("missing exact fake AWS region")
        if len(args) < 4 or args[0] != "--endpoint-url":
            raise SystemExit("missing exact fake AWS endpoint")
        endpoint = args[1]
        if args[2] != "--no-cli-pager":
            raise SystemExit("fake AWS pager was not disabled")
        command_index = 3
        debug = False
        if args[command_index] == "--debug":
            debug = True
            command_index += 1
        service = args[command_index]
        expected_endpoint = {{
            "apigatewayv2": f"https://apigateway.{{REGION}}.amazonaws.com",
            "ecr": f"https://api.ecr.{{REGION}}.amazonaws.com",
            "efs": f"https://elasticfilesystem.{{REGION}}.amazonaws.com",
            "iam": "https://iam.amazonaws.com",
            "s3api": f"https://s3.{{REGION}}.amazonaws.com",
            "cloudwatch": f"https://monitoring.{{REGION}}.amazonaws.com",
            "budgets": "https://budgets.amazonaws.com",
            "ce": "https://ce.us-east-1.amazonaws.com",
            "chatbot": "https://chatbot.us-east-2.amazonaws.com",
        }}.get(service, f"https://{{service}}.{{REGION}}.amazonaws.com")
        if endpoint != expected_endpoint:
            raise SystemExit("fake AWS endpoint differs")
        if service in ("budgets", "ce") and command_region != "us-east-1":
            raise SystemExit("fake AWS billing region differs")
        # Chatbot is absent from ap-northeast-1 (regional DNS does not exist), so
        # the guard reads the account-global configurations via us-east-2 -- the
        # fake must demand the same pairing production requires.
        if service == "chatbot" and command_region != "us-east-2":
            raise SystemExit("fake AWS chatbot region differs")
        if service not in ("budgets", "ce", "chatbot") and command_region != REGION:
            raise SystemExit("fake AWS regional service region differs")
        args = args[command_index:]
        if debug:
            import email.utils

            print(
                "TEAMAGENT_HTTP_METADATA:"
                + json.dumps(
                    {{
                        "date": email.utils.formatdate(usegmt=True),
                        "x-amzn-requestid": f"fake-{{os.getpid()}}-request",
                    }}
                ),
                file=sys.stderr,
            )

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
            drift_marker = os.environ.get("TF_FAKE_DRIFT_AFTER_PLAN_MARKER")
            if os.environ.get("AWS_FAKE_DRIFT") or (
                drift_marker and pathlib.Path(drift_marker).exists()
            ):
                values.append({{"name": "LIVE_DRIFT", "value": "1"}})
            return values

        if args[:2] == ["sts", "get-caller-identity"]:
            account = os.environ.get("AWS_FAKE_ACCOUNT", ACCOUNT)
            gate_session = os.environ.get("AWS_ACCESS_KEY_ID") == "ASIAFAKEGATE"
            arn = (
                "arn:aws:sts::718959508629:assumed-role/"
                "teamagent-dev-image-deployment-gate/"
                "teamagent-image-deployment-gate"
                if gate_session
                else (
                    "arn:aws:sts::718959508629:assumed-role/"
                    "teamagent-dev-terraform-runtime-automation/"
                    "teamagent-terraform-worker"
                )
            )
            identity = {{
                "UserId": "AROATEST:teamagent-terraform-worker",
                "Account": account,
                "Arn": arn,
            }}
            if "--output" in args and args[args.index("--output") + 1] == "text":
                print(f"{{account}}\\t{{arn}}")
            else:
                print(json.dumps(identity))
        elif args[:2] == ["sts", "assume-role"]:
            if "--output" in args and args[args.index("--output") + 1] == "text":
                print("ASIAFAKEGATE\\tfake-secret\\tfake-session-token")
            else:
                print(json.dumps({{
                    "Credentials": {{
                        "AccessKeyId": "ASIAFAKEGATE",
                        "SecretAccessKey": "fake-secret",
                        "SessionToken": "fake-session-token",
                    }}
                }}))
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
                protocol = "email-json" if os.environ.get("AWS_FAKE_EMAIL_JSON") else "email"
                endpoint = (
                    "other@example.com"
                    if os.environ.get("AWS_FAKE_DIFFERENT_ALARM_EMAIL")
                    else "s-komata@vectorinc.co.jp"
                )
                subscriptions.append({{
                    "SubscriptionArn": (
                        "arn:aws:sns:ap-northeast-1:718959508629:"
                        "teamagent-dev-openclaw-alarms:"
                        "11111111-2222-4333-8444-555555555555"
                    ),
                    "Owner": ACCOUNT,
                    "Protocol": protocol,
                    "Endpoint": endpoint,
                    "TopicArn": (
                        "arn:aws:sns:ap-northeast-1:718959508629:"
                        "teamagent-dev-openclaw-alarms"
                    ),
                }})
            if os.environ.get("AWS_FAKE_PENDING_SUBSCRIPTION"):
                subscriptions.append({{
                    "SubscriptionArn": "PendingConfirmation",
                    "Owner": ACCOUNT,
                    "Protocol": "email",
                    "Endpoint": "pending@example.com",
                    "TopicArn": (
                        "arn:aws:sns:ap-northeast-1:718959508629:"
                        "teamagent-dev-openclaw-alarms"
                    ),
                }})
            if os.environ.get("AWS_FAKE_EXTRA_SUBSCRIPTION"):
                subscriptions.append({{
                    "SubscriptionArn": (
                        "arn:aws:sns:ap-northeast-1:718959508629:"
                        "teamagent-dev-openclaw-alarms:"
                        "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                    ),
                    "Owner": ACCOUNT,
                    "Protocol": "sms",
                    "Endpoint": "+819012345678",
                    "TopicArn": (
                        "arn:aws:sns:ap-northeast-1:718959508629:"
                        "teamagent-dev-openclaw-alarms"
                    ),
                }})
            print(json.dumps({{"Subscriptions": subscriptions}}))
        elif args[:2] == ["sns", "get-subscription-attributes"]:
            subscription_arn = args[args.index("--subscription-arn") + 1]
            protocol = "email-json" if os.environ.get("AWS_FAKE_EMAIL_JSON") else "email"
            endpoint = (
                "other@example.com"
                if os.environ.get("AWS_FAKE_DIFFERENT_ALARM_EMAIL")
                else "s-komata@vectorinc.co.jp"
            )
            attributes = {{
                "SubscriptionArn": subscription_arn,
                "Owner": ACCOUNT,
                "TopicArn": (
                    "arn:aws:sns:ap-northeast-1:718959508629:"
                    "teamagent-dev-openclaw-alarms"
                ),
                "Protocol": protocol,
                "Endpoint": endpoint,
                "Owner": ACCOUNT,
                "PendingConfirmation": "false",
                "ConfirmationWasAuthenticated": "true",
                "RawMessageDelivery": "false",
            }}
            if os.environ.get("AWS_FAKE_SUBSCRIPTION_FILTER"):
                attributes["FilterPolicy"] = '{{"severity":["critical"]}}'
            print(json.dumps({{"Attributes": attributes}}))
        elif args[:2] == ["chatbot", "describe-slack-channel-configurations"]:
            configurations = []
            if os.environ.get("AWS_FAKE_CHATBOT"):
                configurations.append({{
                    "ChatConfigurationArn": (
                        "arn:aws:chatbot::718959508629:chat-configuration/"
                        "slack-channel/teamagent-dev-alerts"
                    ),
                    "SnsTopicArns": [(
                        "arn:aws:sns:ap-northeast-1:718959508629:"
                        "teamagent-dev-openclaw-alarms"
                    )],
                    "State": "ENABLED",
                }})
            print(json.dumps({{"SlackChannelConfigurations": configurations}}))
        elif args[:2] == ["chatbot", "list-microsoft-teams-channel-configurations"]:
            print(json.dumps({{"TeamChannelConfigurations": []}}))
        elif args[:2] == ["chatbot", "describe-chime-webhook-configurations"]:
            print(json.dumps({{"WebhookConfigurations": []}}))
        elif args[:2] == ["cloudwatch", "describe-alarms"]:
            actions = []
            if os.environ.get("AWS_FAKE_LEGACY_ALARM_ACTION"):
                actions = [
                    "arn:aws:sns:ap-northeast-1:718959508629:"
                    "teamagent-dev-alarms"
                ]
            print(json.dumps({{
                "MetricAlarms": [{{
                    "AlarmName": "teamagent-dev-errors",
                    "AlarmActions": actions,
                }}],
                "CompositeAlarms": [],
            }}))
        elif args[:2] == ["logs", "describe-metric-filters"]:
            print(json.dumps({{"metricFilters": []}}))
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
        elif args[:2] == ["events", "list-event-buses"]:
            print(json.dumps({{
                "EventBuses": [{{"Name": "default"}}],
            }}))
        elif args[:2] == ["events", "list-rules"]:
            print(json.dumps({{"Rules": []}}))
        elif args[:2] == ["scheduler", "list-schedule-groups"]:
            print(json.dumps({{
                "ScheduleGroups": [{{"Name": "default"}}],
            }}))
        elif args[:2] == ["scheduler", "list-schedules"]:
            print(json.dumps({{"Schedules": []}}))
        elif args[:2] == ["lambda", "list-functions"]:
            print(json.dumps({{"Functions": []}}))
        elif args[:2] == ["s3api", "list-buckets"]:
            print(json.dumps({{
                "Owner": {{"ID": "canonical-owner-id"}},
                "Buckets": [],
            }}))
        elif args[:2] == [
            "autoscaling",
            "describe-notification-configurations",
        ]:
            print(json.dumps({{"NotificationConfigurations": []}}))
        elif args[:2] == [
            "codestar-notifications",
            "list-notification-rules",
        ]:
            print(json.dumps({{"NotificationRules": []}}))
        elif args[:2] == ["rds", "describe-event-subscriptions"]:
            print(json.dumps({{"EventSubscriptionsList": []}}))
        elif args[:2] == [
            "chatbot",
            "describe-chime-webhook-configurations",
        ]:
            print(json.dumps({{"WebhookConfigurations": []}}))
        elif args[:2] == ["s3api", "get-bucket-versioning"]:
            bucket = args[args.index("--bucket") + 1]
            state_path = os.environ.get("AWS_FAKE_VERSIONING_STATE")
            state = {{}}
            if state_path and pathlib.Path(state_path).exists():
                state = json.loads(
                    pathlib.Path(state_path).read_text(encoding="utf-8")
                )
            status = state.get(bucket)
            # Real AWS prints nothing at all for a bucket that has never had a
            # versioning configuration. Printing {{}} here made every test green
            # while production died on the empty document, so mirror the CLI.
            if status:
                print(json.dumps({{"Status": status}}))
        elif args[:2] == ["s3api", "get-bucket-lifecycle-configuration"]:
            lifecycle = os.environ.get("AWS_FAKE_CLOUDTRAIL_LIFECYCLE")
            if lifecycle:
                print(lifecycle)
            else:
                print(
                    "An error occurred (NoSuchLifecycleConfiguration) "
                    "when calling GetBucketLifecycleConfiguration",
                    file=sys.stderr,
                )
                raise SystemExit(254)
        elif args[:2] == ["s3api", "head-object"]:
            app_html = (
                b"fake current app html\\n"
                if os.environ.get("AWS_FAKE_APP_PROVENANCE_MISSING")
                else APP_HTML
            )
            print(json.dumps({{
                "ContentLength": len(app_html),
                "VersionId": {APP_VERSION_ID!r},
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
        elif args[:2] == ["events", "list-event-buses"]:
            print(json.dumps({{
                "EventBuses": [{{
                    "Name": "default",
                    "Arn": f"arn:aws:events:{{REGION}}:{{ACCOUNT}}:event-bus/default",
                }}]
            }}))
        elif args[:2] == ["events", "list-rules"]:
            print(json.dumps({{
                "Rules": [{{
                    "Name": "teamagent-dev-ingest-weekly",
                    "EventBusName": "default",
                    "State": "DISABLED",
                }}]
            }}))
        elif args[:2] == ["scheduler", "list-schedule-groups"]:
            print(json.dumps({{"ScheduleGroups": [{{"Name": "default"}}]}}))
        elif args[:2] == ["scheduler", "list-schedules"]:
            print(json.dumps({{
                "Schedules": [{{
                    "Name": "teamagent-dev-benign-schedule",
                    "GroupName": "default",
                }}]
            }}))
        elif args[:2] == ["scheduler", "get-schedule"]:
            print(json.dumps({{
                "Name": "teamagent-dev-benign-schedule",
                "GroupName": "default",
                "Target": {{
                    "Arn": f"arn:aws:sqs:{{REGION}}:{{ACCOUNT}}:benign",
                    "RoleArn": f"arn:aws:iam::{{ACCOUNT}}:role/benign-scheduler",
                }},
            }}))
        elif args[:2] == ["lambda", "list-functions"]:
            print(json.dumps({{
                "Functions": [{{
                    "FunctionName": "teamagent-dev-tiktok-acquire-dispatch",
                }}]
            }}))
        elif args[:2] == ["lambda", "list-function-event-invoke-configs"]:
            print(json.dumps({{"FunctionEventInvokeConfigs": []}}))
        elif args[:2] == ["s3api", "list-buckets"]:
            print(json.dumps({{"Buckets": [{{"Name": "teamagent-dev-raw-files"}}]}}))
        elif args[:2] == ["s3api", "get-bucket-notification-configuration"]:
            print(json.dumps({{}}))
        elif args[:2] == ["autoscaling", "describe-notification-configurations"]:
            print(json.dumps({{"NotificationConfigurations": []}}))
        elif args[:2] == ["codestar-notifications", "list-notification-rules"]:
            print(json.dumps({{
                "NotificationRules": [{{
                    "Arn": (
                        f"arn:aws:codestar-notifications:{{REGION}}:{{ACCOUNT}}:"
                        "notificationrule/benign"
                    )
                }}]
            }}))
        elif args[:2] == ["codestar-notifications", "describe-notification-rule"]:
            print(json.dumps({{
                "Arn": (
                    f"arn:aws:codestar-notifications:{{REGION}}:{{ACCOUNT}}:"
                    "notificationrule/benign"
                ),
                "Targets": [],
            }}))
        elif args[:2] == ["rds", "describe-event-subscriptions"]:
            print(json.dumps({{"EventSubscriptionsList": []}}))
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
                "Id": "morning" if component == "morning" else f"target-{{component}}",
                "Arn": f"arn:aws:ecs:{{REGION}}:{{ACCOUNT}}:cluster/teamagent-dev",
                "RoleArn": f"arn:aws:iam::{{ACCOUNT}}:role/events-{{component}}",
                **({{"Input": "{{}}"}} if component == "morning" else {{}}),
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
            name = args[args.index("--function-name") + 1]
            component = next(
                key for key, value in dispatchers.items()
                if value["function_name"] == name
            )
            print(json.dumps(
                {{"ReservedConcurrentExecutions": 2}}
                if component == "tiktok"
                else {{}}
            ))
        elif args[:2] == ["lambda", "list-event-source-mappings"]:
            if "--function-name" in args:
                name = args[args.index("--function-name") + 1]
                component = next(
                    key for key, value in dispatchers.items()
                    if value["function_name"] == name
                )
                observed_mappings = [mappings[component]]
            else:
                observed_mappings = []
            print(json.dumps({{"EventSourceMappings": observed_mappings}}))
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
        import subprocess
        import sys

        args = [arg for arg in sys.argv[1:] if not arg.startswith("-chdir=")]
        if log_path := os.environ.get("TF_FAKE_LOG"):
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(" ".join(args) + "\\n")

        def state_data():
            state_path = os.environ.get(
                "TF_FAKE_STATE",
                os.environ["TF_FAKE_DEFAULT_STATE"],
            )
            state = json.loads(
                pathlib.Path(state_path).read_text(encoding="utf-8")
            )
            drifted_task = os.environ.get("TF_FAKE_TASK_REVISION_DRIFT")
            if drifted_task:
                resource = next(
                    item
                    for item in state["resources"]
                    if item["type"] == "aws_ecs_task_definition"
                    and item["name"] == drifted_task
                )
                for instance in resource["instances"]:
                    attributes = instance["attributes"]
                    attributes["revision"] += 1
                    arn = attributes["arn"].rsplit(":", 1)[0]
                    attributes["arn"] = f"{arn}:{attributes['revision']}"
                    if "id" in attributes:
                        attributes["id"] = attributes["arn"]
            return state

        def state_addresses(state):
            addresses = []
            for resource in state.get("resources", []):
                prefix = resource.get("module", "")
                if prefix:
                    prefix += "."
                mode = resource.get("mode")
                if mode == "managed":
                    mode_prefix = ""
                elif mode == "data":
                    mode_prefix = "data."
                else:
                    raise ValueError("unsupported state resource mode")
                base = (
                    prefix
                    + mode_prefix
                    + resource["type"]
                    + "."
                    + resource["name"]
                )
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
            intent_arg = next(
                arg for arg in args
                if arg.startswith("-var=image_deployment_intent_id=")
            )
            image_deployment_intent_id = intent_arg.split("=", 2)[2]
            desired = core["desired_mcp_image"]
            plan = json.loads(pathlib.Path(os.environ["TF_FAKE_TEMPLATE"]).read_text())
            consumer_manifest = json.loads(
                pathlib.Path(os.environ["TF_FAKE_CONSUMER_MANIFEST"]).read_text(
                    encoding="utf-8"
                )
            )
            receipt_catalog = {}
            consumer_receipt_bindings = {}
            force_ready_false = os.environ.get("TF_FAKE_GATE_READY_FALSE") == "1"
            if force_ready_false:
                canary = next(
                    consumer
                    for consumer in consumer_manifest["consumers"]
                    if consumer["consumer_id"] == "canary"
                )
                canary["after"]["activation"]["state"] = "ENABLED"
                consumer_manifest["mode"] = "receipt-required"
                claim_id = "d" * 64
                receipt_key = (
                    "release-receipts/mcp/"
                    + "a" * 40
                    + "/"
                    + claim_id
                    + ".json"
                )
                receipt_catalog = {
                    claim_id: {
                        "bucket": "teamagent-dev-image-release-evidence",
                        "key": receipt_key,
                        "version_id": "receipt-version-1",
                        "signature_key": receipt_key + ".sig",
                        "signature_version_id": "signature-version-1",
                    }
                }
                consumer_receipt_bindings = {"canary": claim_id}
            hmac_active = os.environ.get("TF_FAKE_HMAC_ACTIVE") == "1"
            plan["variables"] = {
                "openclaw_image": {"value": core["desired_openclaw_image"]},
                "mcp_image": {"value": desired},
                "x_buzz_image": {"value": core["desired_x_image"]},
                "media_worker_image": {"value": core["desired_tiktok_image"]},
                "tiktok_acquire_image": {"value": ""},
                "enable_connect_web": {"value": True},
                "enable_canary_health": {"value": True},
                "enable_ingest_schedule": {"value": True},
                "enable_morning_digest": {"value": True},
                "enable_x_research": {"value": True},
                "enable_media_worker": {"value": True},
                "enable_tiktok_acquire": {"value": True},
                "ingest_rule_enabled": {"value": core["ingest_rule_enabled"]},
                "morning_digest_rule_enabled": {
                    "value": core["morning_digest_rule_enabled"]
                },
                "canary_rule_enabled": {"value": core["canary_rule_enabled"]},
                "require_alarm_delivery": {"value": True},
                "bedrock_logs_retention_days": {"value": 60},
                "image_deployment_consumer_manifest": {
                    "value": consumer_manifest
                },
                "image_release_receipt_catalog": {
                    "value": receipt_catalog
                },
                "image_release_consumer_receipt_bindings": {
                    "value": consumer_receipt_bindings
                },
                "image_deployment_intent_id": {
                    "value": image_deployment_intent_id
                },
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
                "hmac_runtime_promotion_tasks": {
                    "value": (
                        ["connect_web", "mcp", "morning_digest"]
                        if hmac_active
                        else []
                    )
                },
                "runtime_guard_live": {"value": core},
            }
            consumer_by_address = {
                consumer["terraform_task_definition_address"]: consumer["consumer_id"]
                for consumer in consumer_manifest["consumers"]
            }
            for change in plan["resource_changes"]:
                if change["type"] == "aws_ecs_task_definition":
                    containers = json.loads(change["change"]["after"]["container_definitions"])
                    consumer_id = consumer_by_address[change["address"]]
                    containers[0]["image"] = core["desired_consumer_images"][consumer_id]
                    change["change"]["after"]["container_definitions"] = json.dumps(containers)
            contracts = json.loads(os.environ["TF_FAKE_RELEASE_CONTRACTS"])
            contract_ready = json.loads(os.environ["TF_FAKE_RELEASE_READY"])
            if force_ready_false:
                contracts = {"mcp": contracts["mcp"]}
                contract_ready = {"mcp": False}
            application_provenance = {
                "mcp": json.loads(os.environ["TF_FAKE_MCP_APPLICATION"])
            }
            shared_generation_ledger = {}
            gate_query = {
                "consumer_manifest_json": json.dumps(
                    consumer_manifest,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "receipt_catalog_json": json.dumps(
                    receipt_catalog,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "consumer_receipt_bindings_json": json.dumps(
                    consumer_receipt_bindings,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "contracts_json": json.dumps(
                    contracts,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "contract_ready_json": json.dumps(
                    contract_ready,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "application_json": json.dumps(
                    application_provenance,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "shared_generation_ledger_json": json.dumps(
                    shared_generation_ledger,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "signing_key_arn": (
                    "arn:aws:kms:ap-northeast-1:718959508629:"
                    "key/11111111-1111-4111-8111-111111111111"
                ),
                "encryption_key_arn": (
                    "arn:aws:kms:ap-northeast-1:718959508629:"
                    "key/22222222-2222-4222-8222-222222222222"
                ),
                "deployment_intent_id": image_deployment_intent_id,
            }
            gate = subprocess.run(
                [
                    sys.executable,
                    os.environ["TF_FAKE_RELEASE_EVIDENCE"],
                    "terraform-gate",
                ],
                input=json.dumps(gate_query),
                capture_output=True,
                text=True,
            )
            if gate.returncode != 0:
                sys.stdout.write(gate.stdout)
                sys.stderr.write(gate.stderr)
                raise SystemExit(gate.returncode)
            verified_gate = json.loads(gate.stdout)
            assert verified_gate["verified"] == "true"
            if marker := os.environ.get("TF_FAKE_GATE_VERIFIED_MARKER"):
                pathlib.Path(marker).touch()
            gate_input = {
                "deployment_intent_id": image_deployment_intent_id,
                "deployment_context_sha256": verified_gate[
                    "deployment_context_sha256"
                ],
                "receipt_claims_sha256": verified_gate[
                    "receipt_claims_sha256"
                ],
                "consumer_manifest": consumer_manifest,
                "receipt_catalog": receipt_catalog,
                "consumer_receipt_bindings": consumer_receipt_bindings,
                "release_channels": json.loads(
                    verified_gate["release_channels_json"]
                ),
                "application_provenance": application_provenance,
                "shared_generation_ledger": shared_generation_ledger,
                "hmac_release_bindings": (
                    {
                        "rotation_epoch": "hmac-2026-07",
                        "gate_mode": "candidate",
                        "cleanup_domain": "",
                        "manifest_sha256": "3" * 64,
                        "rollout_control_sha256": "4" * 64,
                        "worker_enabled": False,
                        "worker_mode": "candidate",
                        "worker_artifacts": {},
                        "worker_provenance_key_arn": "",
                    }
                    if hmac_active
                    else {}
                ),
                "deployment_gate_query": gate_query,
                "receipt_authorization_expires_at": verified_gate[
                    "receipt_authorization_expires_at"
                ],
                "deployment_mode": verified_gate["deployment_mode"],
            }
            if capture := os.environ.get("TF_FAKE_GATE_INPUT_CAPTURE"):
                pathlib.Path(capture).write_text(
                    json.dumps(gate_input, sort_keys=True),
                    encoding="utf-8",
                )
            mismatched_consumer_id = os.environ.get(
                "TF_FAKE_POST_GATE_IMAGE_MISMATCH"
            )
            if mismatched_consumer_id:
                task_address = next(
                    address
                    for address, consumer_id in consumer_by_address.items()
                    if consumer_id == mismatched_consumer_id
                )
                task_change = next(
                    change
                    for change in plan["resource_changes"]
                    if change["address"] == task_address
                )
                task_change["change"]["actions"] = ["create", "delete"]
                containers = json.loads(
                    task_change["change"]["after"]["container_definitions"]
                )
                image = containers[0]["image"]
                containers[0]["image"] = image[:-1] + (
                    "0" if image[-1] != "0" else "1"
                )
                task_change["change"]["after"]["container_definitions"] = (
                    json.dumps(containers)
                )
            gate_change = next(
                change
                for change in plan["resource_changes"]
                if change["address"]
                == "terraform_data.production_image_release_gate"
            )
            gate_change["change"]["after"] = {"input": gate_input}
            plan["planned_values"] = {
                "root_module": {
                    "resources": [
                        {
                            "address": change["address"],
                            "mode": change["mode"],
                            "type": change["type"],
                            "name": change["name"],
                            "values": change["change"]["after"],
                        }
                        for change in plan["resource_changes"]
                        if change["change"]["after"] is not None
                    ],
                    "child_modules": [],
                }
            }
            pathlib.Path(out).write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
            if marker := os.environ.get("TF_FAKE_DRIFT_AFTER_PLAN_MARKER"):
                pathlib.Path(marker).touch()
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


def _harness(
    tmp_path: Path,
    scenario: str = "safe",
    *,
    hmac_active: bool = False,
) -> tuple[dict[str, str], Path, Path]:
    tmp_path.chmod(0o700)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(mode=0o700)
    _fake_aws(fake_bin / "aws")
    _fake_terraform(fake_bin / "terraform")

    plan_data = _safe_plan()
    if hmac_active:
        _activate_hmac_plan(plan_data)
    if scenario == "safe":
        _stabilize_no_image_plan(plan_data, hmac_active=hmac_active)
    state_data = _fake_state_from_plan(plan_data)
    consumer_manifest = _consumer_manifest()
    _mutate_plan(plan_data, scenario)
    template = tmp_path / "template.json"
    template.write_text(json.dumps(plan_data), encoding="utf-8")
    template.chmod(0o600)
    default_state = tmp_path / "terraform-state.json"
    default_state.write_text(json.dumps(state_data), encoding="utf-8")
    default_state.chmod(0o600)
    manifest = tmp_path / "image-deployment-consumer-manifest.json"
    manifest.write_text(json.dumps(consumer_manifest), encoding="utf-8")
    manifest.chmod(0o600)
    var_file = tmp_path / "terraform.tfvars"
    var_file.write_text(
        'alarm_email_endpoints = ["s-komata@vectorinc.co.jp"]\n',
        encoding="utf-8",
    )
    var_file.chmod(0o600)
    tf_log = tmp_path / "terraform.log"
    tf_log.write_text("", encoding="utf-8")
    tf_log.chmod(0o600)
    release_contracts, _ = _release_contract_bindings()
    release_ready = {pipeline: True for pipeline in release_contracts}
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "TF_FAKE_LOG": str(tf_log),
            "TF_FAKE_TEMPLATE": str(template),
            "TF_FAKE_DEFAULT_STATE": str(default_state),
            "TF_FAKE_CONSUMER_MANIFEST": str(manifest),
            "TF_FAKE_RELEASE_EVIDENCE": str(RELEASE_EVIDENCE),
            "TF_FAKE_RELEASE_CONTRACTS": json.dumps(
                release_contracts,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "TF_FAKE_RELEASE_READY": json.dumps(
                release_ready,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "TF_FAKE_MCP_APPLICATION": json.dumps(
                MCP_APPLICATION_PROVENANCE,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "AWS_FAKE_TRUSTED_AUTOMATION": "1",
        }
    )
    if hmac_active:
        env["TF_FAKE_HMAC_ACTIVE"] = "1"
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


SYNC_CONSUMER_SNAPSHOT_KEYS = {
    "mcp": "mcp",
    "connect_web": "connect_web",
    "openclaw": "openclaw",
    "canary": "canary",
    "ingest": "ingest",
    "morning_digest": "morning",
    "x_buzz_worker": "x_buzz",
    "tiktok_acquire": "tiktok",
}


def _strict_sync_expected_images() -> dict[str, str]:
    return {
        "mcp": LIVE_IMAGE,
        "connect_web": CONNECT_WEB_IMAGE,
        "openclaw": OPENCLAW_IMAGE,
        "canary": CANARY_IMAGE,
        "ingest": INGEST_IMAGE,
        "morning_digest": MORNING_IMAGE,
        "x_buzz_worker": X_IMAGE,
        "tiktok_acquire": MEDIA_WORKER_IMAGE,
    }


def _strict_sync_snapshot(expected: dict[str, str]) -> dict[str, Any]:
    return {
        "taskdefs": {
            snapshot_key: {"image": expected[consumer_id]}
            for consumer_id, snapshot_key in SYNC_CONSUMER_SNAPSHOT_KEYS.items()
        }
    }


def _write_consumer_registry(
    tmp_path: Path,
    registry: dict[str, Any],
) -> Path:
    path = tmp_path / "image-deployment-consumers.json"
    path.write_text(
        json.dumps(registry, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _run_sync_consumer_image_validator(
    tmp_path: Path,
    *,
    snapshot: dict[str, Any],
    expected: dict[str, str],
    registry: Path = CONSUMER_REGISTRY,
) -> subprocess.CompletedProcess[str]:
    snapshot_path = tmp_path / "sync-consumer-snapshot.json"
    snapshot_path.write_text(
        json.dumps(snapshot, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    guard = GUARD.read_text(encoding="utf-8")
    function = re.search(
        r"validate_sync_consumer_images\(\) \{.*?"
        r"(?=\n# Terraform precondition)",
        guard,
        flags=re.DOTALL,
    )
    assert function is not None
    script = "\n".join(
        (
            "set -euo pipefail",
            f"EXPECTED_ACCOUNT_ID={ACCOUNT!r}",
            f"REGION={REGION!r}",
            'IMAGE_DEPLOYMENT_CONSUMER_REGISTRY="$3"',
            'die() { echo "★ $*" >&2; return 1; }',
            function.group(0),
            'validate_sync_consumer_images "$1" "$2"',
        )
    )
    return subprocess.run(
        [
            "bash",
            "-c",
            script,
            "validator",
            str(snapshot_path),
            json.dumps(expected, sort_keys=True, separators=(",", ":")),
            str(registry),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _assert_sync_consumer_image_rejected(
    result: subprocess.CompletedProcess[str],
) -> None:
    assert result.returncode == 1
    assert "registryと完全一致する8 consumer" in result.stderr


def _release_gate_query(
    manifest: dict[str, Any],
    *,
    receipt_catalog: dict[str, Any] | None = None,
    consumer_receipt_bindings: dict[str, str] | None = None,
    contract_ready: dict[str, bool] | None = None,
    application: dict[str, Any] | None = None,
) -> dict[str, str]:
    contracts, _ = _release_contract_bindings()
    default_ready = {pipeline: True for pipeline in contracts}
    catalog = receipt_catalog or {}
    bindings = consumer_receipt_bindings or {}
    application_provenance = (
        {"mcp": copy.deepcopy(MCP_APPLICATION_PROVENANCE)} if application is None else application
    )
    return {
        "consumer_manifest_json": json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "receipt_catalog_json": json.dumps(
            catalog,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "consumer_receipt_bindings_json": json.dumps(
            bindings,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "contracts_json": json.dumps(
            contracts,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "contract_ready_json": json.dumps(
            default_ready if contract_ready is None else contract_ready,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "application_json": json.dumps(
            application_provenance,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "shared_generation_ledger_json": "{}",
        "signing_key_arn": (
            f"arn:aws:kms:{REGION}:{ACCOUNT}:key/11111111-1111-4111-8111-111111111111"
        ),
        "encryption_key_arn": (
            f"arn:aws:kms:{REGION}:{ACCOUNT}:key/22222222-2222-4222-8222-222222222222"
        ),
        "deployment_intent_id": "11111111-1111-4111-8111-111111111111",
    }


def _run_release_gate(
    query: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RELEASE_EVIDENCE), "terraform-gate"],
        input=json.dumps(query),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _manifest_consumer(
    manifest: dict[str, Any],
    consumer_id: str,
) -> dict[str, Any]:
    return next(
        consumer for consumer in manifest["consumers"] if consumer["consumer_id"] == consumer_id
    )


def _receipt_required_canary_manifest() -> dict[str, Any]:
    manifest = _consumer_manifest()
    canary = _manifest_consumer(manifest, "canary")
    canary["after"]["activation"]["state"] = "ENABLED"
    manifest["mode"] = "receipt-required"
    return manifest


def test_receipt_required_canary_fixture_has_only_the_intended_activation_change() -> None:
    manifest = _receipt_required_canary_manifest()

    assert manifest["mode"] == "receipt-required"
    for consumer in manifest["consumers"]:
        before = consumer["before"]
        after = consumer["after"]
        assert consumer["live"] == before
        assert before["image"] == after["image"]
        assert before["task_definition_arn"] == after["task_definition_arn"]
        assert before["task_definition"] == after["task_definition"]
        if consumer["consumer_id"] == "canary":
            assert before["activation"] == {
                "state": "DISABLED",
                "task_definition_arn": before["task_definition_arn"],
            }
            assert after["activation"] == {
                "state": "ENABLED",
                "task_definition_arn": after["task_definition_arn"],
            }
        else:
            assert before["activation"] == after["activation"]


def test_consumer_image_map_preserves_exact_consumer_argument_wiring() -> None:
    expected = _strict_sync_expected_images()
    guard = GUARD.read_text(encoding="utf-8")
    function = re.search(
        r"consumer_image_map\(\) \{.*?"
        r"(?=\nvalidate_sync_consumer_images\(\))",
        guard,
        flags=re.DOTALL,
    )
    assert function is not None
    script = "\n".join(
        (
            "set -euo pipefail",
            function.group(0),
            'consumer_image_map "$@"',
        )
    )

    result = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "mapper",
            expected["openclaw"],
            expected["mcp"],
            expected["x_buzz_worker"],
            expected["tiktok_acquire"],
            expected["connect_web"],
            expected["ingest"],
            expected["morning_digest"],
            expected["canary"],
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == expected


def test_core_strict_sync_path_invokes_consumer_image_validator() -> None:
    guard = GUARD.read_text(encoding="utf-8")
    function = re.search(
        r"core_from_snapshot\(\) \{.*?"
        r"(?=\nprint_hcl_snapshot\(\))",
        guard,
        flags=re.DOTALL,
    )
    assert function is not None
    core = function.group(0)
    assert re.search(
        r'if \[ "\$mode" = "sync" \]; then\s+'
        r'validate_sync_consumer_images "\$snapshot" '
        r'"\$desired_consumer_images"\s+fi',
        core,
    )
    assert "unique | length == 1" not in core


def test_strict_sync_accepts_distinct_expected_images_per_consumer(
    tmp_path: Path,
) -> None:
    expected = _strict_sync_expected_images()
    assert {
        expected[consumer_id].split("@", 1)[0]
        for consumer_id in ("mcp", "connect_web", "x_buzz_worker")
    } == {REPOSITORY}
    assert (
        len(
            {
                expected["mcp"],
                expected["connect_web"],
                expected["x_buzz_worker"],
            }
        )
        == 3
    )

    result = _run_sync_consumer_image_validator(
        tmp_path,
        snapshot=_strict_sync_snapshot(expected),
        expected=expected,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("consumer_id", "snapshot_key"),
    tuple(SYNC_CONSUMER_SNAPSHOT_KEYS.items()),
)
def test_strict_sync_rejects_one_consumer_outside_its_expected_image(
    tmp_path: Path,
    consumer_id: str,
    snapshot_key: str,
) -> None:
    expected = _strict_sync_expected_images()
    snapshot = _strict_sync_snapshot(expected)
    repository = expected[consumer_id].split("@", 1)[0]
    snapshot["taskdefs"][snapshot_key]["image"] = f"{repository}@sha256:{'a' * 64}"
    if snapshot["taskdefs"][snapshot_key]["image"] == expected[consumer_id]:
        snapshot["taskdefs"][snapshot_key]["image"] = f"{repository}@sha256:{'b' * 64}"

    result = _run_sync_consumer_image_validator(
        tmp_path,
        snapshot=snapshot,
        expected=expected,
    )

    _assert_sync_consumer_image_rejected(result)


def test_strict_sync_rejects_repository_outside_consumer_registry(
    tmp_path: Path,
) -> None:
    expected = _strict_sync_expected_images()
    snapshot = _strict_sync_snapshot(expected)
    unexpected = f"{LEGACY_TIKTOK_REPOSITORY}@sha256:{'e' * 64}"
    expected["tiktok_acquire"] = unexpected
    snapshot["taskdefs"]["tiktok"]["image"] = unexpected

    result = _run_sync_consumer_image_validator(
        tmp_path,
        snapshot=snapshot,
        expected=expected,
    )

    _assert_sync_consumer_image_rejected(result)


@pytest.mark.parametrize(
    "invalid_image",
    (
        f"{REPOSITORY}:latest",
        f"{REPOSITORY}@sha256:abc123",
    ),
    ids=("tag", "short-digest"),
)
def test_strict_sync_rejects_noncanonical_digest_reference(
    tmp_path: Path,
    invalid_image: str,
) -> None:
    expected = _strict_sync_expected_images()
    snapshot = _strict_sync_snapshot(expected)
    expected["mcp"] = invalid_image
    snapshot["taskdefs"]["mcp"]["image"] = invalid_image

    result = _run_sync_consumer_image_validator(
        tmp_path,
        snapshot=snapshot,
        expected=expected,
    )

    _assert_sync_consumer_image_rejected(result)


def test_strict_sync_rejects_missing_snapshot_consumer(tmp_path: Path) -> None:
    expected = _strict_sync_expected_images()
    snapshot = _strict_sync_snapshot(expected)
    del snapshot["taskdefs"]["canary"]

    result = _run_sync_consumer_image_validator(
        tmp_path,
        snapshot=snapshot,
        expected=expected,
    )

    _assert_sync_consumer_image_rejected(result)


@pytest.mark.parametrize("mutation", ("missing", "extra"))
def test_strict_sync_rejects_nonexact_expected_consumer_set(
    tmp_path: Path,
    mutation: str,
) -> None:
    expected = _strict_sync_expected_images()
    snapshot = _strict_sync_snapshot(expected)
    if mutation == "missing":
        del expected["morning_digest"]
    else:
        expected["unexpected_consumer"] = f"{REPOSITORY}@sha256:{'8' * 64}"

    result = _run_sync_consumer_image_validator(
        tmp_path,
        snapshot=snapshot,
        expected=expected,
    )

    _assert_sync_consumer_image_rejected(result)


def test_strict_sync_rejects_ninth_registry_consumer(tmp_path: Path) -> None:
    registry = json.loads(CONSUMER_REGISTRY.read_text(encoding="utf-8"))
    ninth = copy.deepcopy(registry["consumers"][0])
    ninth.update(
        {
            "consumer_id": "future_consumer",
            "terraform_task_definition_address": ("aws_ecs_task_definition.future_consumer[0]"),
            "ecs_family": "teamagent-dev-future-consumer",
            "container_name": "future-consumer",
            "activator": {
                "type": "ecs_service",
                "identity": "teamagent-dev-future-consumer",
            },
        }
    )
    registry["consumers"].append(ninth)
    registry_path = _write_consumer_registry(tmp_path, registry)
    expected = _strict_sync_expected_images()

    result = _run_sync_consumer_image_validator(
        tmp_path,
        snapshot=_strict_sync_snapshot(expected),
        expected=expected,
        registry=registry_path,
    )

    _assert_sync_consumer_image_rejected(result)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("ecs_family", "teamagent-dev-wrong-family"),
        ("container_name", "wrong-container"),
        (
            "activator",
            {"type": "ecs_service", "identity": "teamagent-dev-wrong-service"},
        ),
        (
            "terraform_task_definition_address",
            "aws_ecs_task_definition.wrong",
        ),
    ),
)
def test_strict_sync_rejects_registry_identity_drift(
    tmp_path: Path,
    field: str,
    invalid_value: object,
) -> None:
    registry = json.loads(CONSUMER_REGISTRY.read_text(encoding="utf-8"))
    mcp = next(consumer for consumer in registry["consumers"] if consumer["consumer_id"] == "mcp")
    mcp[field] = invalid_value
    registry_path = _write_consumer_registry(tmp_path, registry)
    expected = _strict_sync_expected_images()

    result = _run_sync_consumer_image_validator(
        tmp_path,
        snapshot=_strict_sync_snapshot(expected),
        expected=expected,
        registry=registry_path,
    )

    _assert_sync_consumer_image_rejected(result)


def test_strict_sync_rejects_provisional_registry_consumer(
    tmp_path: Path,
) -> None:
    registry = json.loads(CONSUMER_REGISTRY.read_text(encoding="utf-8"))
    registry["consumers"][0]["provisional"] = True
    registry_path = _write_consumer_registry(tmp_path, registry)
    expected = _strict_sync_expected_images()

    result = _run_sync_consumer_image_validator(
        tmp_path,
        snapshot=_strict_sync_snapshot(expected),
        expected=expected,
        registry=registry_path,
    )

    _assert_sync_consumer_image_rejected(result)


def test_verified_gate_does_not_bypass_guard_consumer_digest_binding(
    tmp_path: Path,
) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    gate_verified = tmp_path / "gate-verified"
    env["TF_FAKE_GATE_VERIFIED_MARKER"] = str(gate_verified)
    env["TF_FAKE_POST_GATE_IMAGE_MISMATCH"] = "connect_web"
    plan = tmp_path / "post-gate-mismatch.tfplan"

    result = _run(_plan_command(var_file, plan), env)

    assert result.returncode == 1
    assert gate_verified.is_file()
    assert (
        "aws_ecs_task_definition.connect_web[0] は期待container "
        "connect-web・候補image・unknown allowlistを満たしません"
    ) in result.stdout + result.stderr
    assert not plan.exists()
    assert not Path(f"{plan}.runtime-guard.json").exists()
    commands = tf_log.read_text(encoding="utf-8").splitlines()
    assert any(command.startswith("plan ") for command in commands)
    assert not any(command == "apply" or command.startswith("apply ") for command in commands)


def test_release_ready_false_stops_gate_after_guard_image_validation(
    tmp_path: Path,
) -> None:
    expected = _strict_sync_expected_images()
    guard = _run_sync_consumer_image_validator(
        tmp_path,
        snapshot=_strict_sync_snapshot(expected),
        expected=expected,
    )
    assert guard.returncode == 0, guard.stdout + guard.stderr

    env, var_file, tf_log = _harness(tmp_path)
    env["TF_FAKE_GATE_READY_FALSE"] = "1"
    plan = tmp_path / "release-not-ready.tfplan"

    result = _run(_plan_command(var_file, plan), env)

    assert result.returncode == 2
    assert "FATAL: mcp release.ready is false" in result.stderr
    assert not plan.exists()
    assert not Path(f"{plan}.runtime-guard.json").exists()
    commands = tf_log.read_text(encoding="utf-8").splitlines()
    assert any(command.startswith("plan ") for command in commands)
    assert not any(command == "apply" or command.startswith("apply ") for command in commands)


def test_no_image_transition_empty_receipts_still_requires_guard_image_match(
    tmp_path: Path,
) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    gate_verified = tmp_path / "no-image-transition-gate-verified"
    gate_input_capture = tmp_path / "no-image-transition-gate-input.json"
    env["TF_FAKE_GATE_VERIFIED_MARKER"] = str(gate_verified)
    env["TF_FAKE_GATE_INPUT_CAPTURE"] = str(gate_input_capture)
    env["TF_FAKE_POST_GATE_IMAGE_MISMATCH"] = "canary"
    plan = tmp_path / "no-image-transition-mismatch.tfplan"

    result = _run(_plan_command(var_file, plan), env)

    assert result.returncode == 1
    assert gate_verified.is_file()
    gate_input = json.loads(gate_input_capture.read_text(encoding="utf-8"))
    assert gate_input["deployment_mode"] == "no-image-transition"
    assert gate_input["receipt_catalog"] == {}
    assert gate_input["consumer_receipt_bindings"] == {}
    assert gate_input["release_channels"] == {}
    assert (
        "aws_ecs_task_definition.canary[0] は期待container "
        "canary・候補image・unknown allowlistを満たしません"
    ) in result.stdout + result.stderr
    assert not plan.exists()
    assert not Path(f"{plan}.runtime-guard.json").exists()
    assert not any(
        command == "apply" or command.startswith("apply ")
        for command in tf_log.read_text(encoding="utf-8").splitlines()
    )


def test_registry_identity_drift_is_rejected_independently_by_gate_and_guard(
    tmp_path: Path,
) -> None:
    manifest = _consumer_manifest()
    _manifest_consumer(manifest, "mcp")["ecs_family"] = "teamagent-dev-mcp-registry-drift"
    gate = _run_release_gate(_release_gate_query(manifest))
    assert gate.returncode == 2
    assert "FATAL: Terraform image consumer manifest is invalid" in gate.stderr

    registry = copy.deepcopy(CONSUMER_REGISTRY_DATA)
    registry["consumers"][0]["ecs_family"] = "teamagent-dev-mcp-registry-drift"
    registry_path = _write_consumer_registry(tmp_path, registry)
    expected = _strict_sync_expected_images()
    guard = _run_sync_consumer_image_validator(
        tmp_path,
        snapshot=_strict_sync_snapshot(expected),
        expected=expected,
        registry=registry_path,
    )
    _assert_sync_consumer_image_rejected(guard)


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


def _run_media_cutover_gate(
    tmp_path: Path,
    scenario: str = "safe",
) -> subprocess.CompletedProcess[str]:
    snapshot = {
        "taskdefs": {
            "tiktok": {"image": LEGACY_TIKTOK_IMAGE},
        }
    }
    snapshot_path = tmp_path / "cutover-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    tmp_root = tmp_path / "guard-tmp"
    tmp_root.mkdir()
    intent_id = "11111111-1111-4111-8111-111111111111"
    migration_sha = "6" * 64
    reviewed_plan_sha = "7" * 64
    receipt_path = tmp_path / "media-cutover-receipt.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    verification = {
        "kind": "teamagent-media-envelope-cutover-verification",
        "schema_version": 2,
        "account_id": ACCOUNT,
        "region": REGION,
        "record_id": f"media-cutover#{intent_id}",
        "status": "READY",
        "desired_image": MEDIA_WORKER_IMAGE,
        "image_deployment_intent_id": intent_id,
        "migration_contract_sha256": migration_sha,
        "reviewed_plan_sha256": reviewed_plan_sha,
        "claims_sha256": "2" * 64,
        "signature_sha256": "8" * 64,
        "kms_key_arn": (f"arn:aws:kms:{REGION}:{ACCOUNT}:key/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "ledger_item_sha256": "3" * 64,
        "verification_sha256": "4" * 64,
        "current_observation": {
            "state_sha256": "5" * 64,
            "state": {
                "legacy_runtime": {"image": LEGACY_TIKTOK_IMAGE},
                "event_source_mapping": {"state": "Disabled"},
                "tasks": {"pending": [], "running": []},
            },
        },
    }
    if scenario == "mapping-enabled":
        verification["current_observation"]["state"]["event_source_mapping"]["state"] = "Enabled"
    elif scenario == "legacy-mismatch":
        verification["current_observation"]["state"]["legacy_runtime"]["image"] = (
            f"{LEGACY_TIKTOK_REPOSITORY}@sha256:{'0' * 64}"
        )
    elif scenario == "invalid-hash":
        verification["claims_sha256"] = "not-a-hash"
    verification_path = tmp_path / "verification.json"
    verification_path.write_text(json.dumps(verification), encoding="utf-8")

    guard = GUARD.read_text(encoding="utf-8")
    function = re.search(
        r"validate_media_envelope_cutover_gate\(\) \{.*?"
        r"(?=\n# Terraform precondition)",
        guard,
        flags=re.DOTALL,
    )
    assert function is not None
    script = "\n".join(
        (
            "set -euo pipefail",
            f"TMP_ROOT={str(tmp_root)!r}",
            f"EXPECTED_ACCOUNT_ID={ACCOUNT!r}",
            f"REGION={REGION!r}",
            "PROJECT=teamagent",
            "ENVIRONMENT=dev",
            'die() { echo "★ $*" >&2; return 1; }',
            """
run_evidence_helper() {
  [ "$1" = "verify-media-cutover" ] || return 98
  shift
  local desired="" output="" receipt="" intent="" migration_sha=""
  local reviewed_sha="" status=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --receipt) receipt="$2"; shift 2 ;;
      --desired-image) desired="$2"; shift 2 ;;
      --image-deployment-intent-id) intent="$2"; shift 2 ;;
      --migration-contract-sha256) migration_sha="$2"; shift 2 ;;
      --reviewed-plan-sha256) reviewed_sha="$2"; shift 2 ;;
      --expected-status) status="$2"; shift 2 ;;
      --output) output="$2"; shift 2 ;;
      *) return 97 ;;
    esac
  done
  [ "$receipt" = "$EXPECTED_RECEIPT" ] || return 95
  [ "$desired" = "$EXPECTED_DESIRED_IMAGE" ] || return 96
  [ "$intent" = "$EXPECTED_INTENT" ] || return 94
  [ "$migration_sha" = "$EXPECTED_MIGRATION_SHA" ] || return 93
  [ "$reviewed_sha" = "$EXPECTED_REVIEWED_SHA" ] || return 92
  [ "$status" = "READY" ] || return 91
  cp "$MEDIA_VERIFICATION" "$output"
}
""",
            function.group(0),
            ('validate_media_envelope_cutover_gate "$1" "$2" "$3" "$4" "$5" "$6"'),
        )
    )
    environment = os.environ.copy()
    environment["EXPECTED_DESIRED_IMAGE"] = MEDIA_WORKER_IMAGE
    environment["EXPECTED_RECEIPT"] = str(receipt_path)
    environment["EXPECTED_INTENT"] = intent_id
    environment["EXPECTED_MIGRATION_SHA"] = migration_sha
    environment["EXPECTED_REVIEWED_SHA"] = reviewed_plan_sha
    environment["MEDIA_VERIFICATION"] = str(verification_path)
    return subprocess.run(
        [
            "bash",
            "-c",
            script,
            "validator",
            str(snapshot_path),
            MEDIA_WORKER_IMAGE,
            str(receipt_path),
            intent_id,
            migration_sha,
            reviewed_plan_sha,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )


def test_media_cutover_gate_requires_durable_exact_live_verification(
    tmp_path: Path,
) -> None:
    result = _run_media_cutover_gate(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("mapping-enabled", "900秒証跡"),
        ("legacy-mismatch", "900秒証跡"),
        ("invalid-hash", "900秒証跡"),
    ],
)
def test_media_cutover_gate_rejects_tampered_or_drifted_evidence(
    tmp_path: Path,
    scenario: str,
    message: str,
) -> None:
    result = _run_media_cutover_gate(tmp_path, scenario)
    assert result.returncode == 1
    assert message in result.stderr


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
    saved_plan = json.loads(plan.read_text(encoding="utf-8"))
    manifest = saved_plan["variables"]["image_deployment_consumer_manifest"]["value"]
    task_changes = {
        change["address"]: change["change"]
        for change in saved_plan["resource_changes"]
        if change["address"] in TASK_ADDRESSES.values()
    }
    assert set(task_changes) == set(TASK_ADDRESSES.values())
    assert all(
        change["actions"] == ["no-op"]
        and change["before"] == change["after"]
        and change["after_unknown"] == {}
        for change in task_changes.values()
    )
    assert manifest["schema_version"] == 1
    assert manifest["registry_sha256"] == CONSUMERS.consumer_registry_sha256()
    assert manifest["mode"] == "no-image-transition"
    assert len(manifest["consumers"]) == len(CONSUMER_REGISTRY_DATA["consumers"]) == 8
    for row, registry_consumer in zip(
        manifest["consumers"],
        CONSUMER_REGISTRY_DATA["consumers"],
        strict=True,
    ):
        assert {key: row[key] for key in CONSUMER_MANIFEST_IDENTITY_KEYS} == {
            key: registry_consumer[key] for key in CONSUMER_MANIFEST_IDENTITY_KEYS
        }
        for phase in ("live", "before", "after"):
            assert set(row[phase]) == {
                "image",
                "task_definition_arn",
                "task_definition",
                "activation",
            }
            task_definition = row[phase]["task_definition"]
            assert set(task_definition) == set(TASK_DEFINITION_COMPARE_FIELDS)
            named_containers = [
                container
                for container in task_definition["container_definitions"]
                if container["name"] == row["container_name"]
            ]
            assert len(named_containers) == 1
            assert set(CONTAINER_DEFINITION_COMPARE_FIELDS) <= set(named_containers[0])
            assert named_containers[0]["image"] == row[phase]["image"]
        assert (
            row["live"]["task_definition_arn"]
            == row["before"]["task_definition_arn"]
            == row["after"]["task_definition_arn"]
        )
        assert (
            row["live"]["task_definition"]
            == row["before"]["task_definition"]
            == row["after"]["task_definition"]
        )
        assert (
            row["live"]["activation"] == row["before"]["activation"] == row["after"]["activation"]
        )
    assert saved_plan["variables"]["image_release_receipt_catalog"]["value"] == {}
    assert saved_plan["variables"]["image_release_consumer_receipt_bindings"]["value"] == {}
    manifest_images = {row["consumer_id"]: row["after"]["image"] for row in manifest["consumers"]}
    assert {
        manifest_images[consumer_id].split("@", 1)[0]
        for consumer_id in ("mcp", "connect_web", "x_buzz_worker")
    } == {REPOSITORY}
    assert (
        len(
            {
                manifest_images["mcp"],
                manifest_images["connect_web"],
                manifest_images["x_buzz_worker"],
            }
        )
        == 3
    )
    for consumer_id in ("canary", "ingest"):
        row = _manifest_consumer(manifest, consumer_id)
        assert {row[phase]["activation"]["state"] for phase in ("live", "before", "after")} == {
            "DISABLED"
        }
    gate_input = next(
        change["change"]["after"]["input"]
        for change in saved_plan["resource_changes"]
        if change["address"] == "terraform_data.production_image_release_gate"
    )
    assert gate_input["deployment_mode"] == "no-image-transition"
    assert gate_input["receipt_catalog"] == {}
    assert gate_input["consumer_receipt_bindings"] == {}
    assert gate_input["release_channels"] == {}
    assert gate_input["consumer_manifest"] == manifest
    assert gate_input["application_provenance"] == {"mcp": MCP_APPLICATION_PROVENANCE}
    gate_verification = _run_release_gate(gate_input["deployment_gate_query"])
    assert gate_verification.returncode == 0, gate_verification.stdout + gate_verification.stderr
    verified_gate = json.loads(gate_verification.stdout)
    assert verified_gate["verified"] == "true"
    assert gate_input["deployment_context_sha256"] == verified_gate["deployment_context_sha256"]
    assert gate_input["receipt_claims_sha256"] == verified_gate["receipt_claims_sha256"]
    fake_state = json.loads(Path(env["TF_FAKE_DEFAULT_STATE"]).read_text(encoding="utf-8"))
    state_instances = {
        address: record
        for resource in fake_state["resources"]
        for record in resource["instances"]
        for address in [
            (
                f"{resource['type']}.{resource['name']}"
                + (
                    ""
                    if "index_key" not in record
                    else (
                        f"[{record['index_key']}]"
                        if isinstance(record["index_key"], int)
                        else f"[{json.dumps(record['index_key'])}]"
                    )
                )
            )
        ]
    }
    for consumer in CONSUMER_REGISTRY_DATA["consumers"]:
        attributes = state_instances[consumer["terraform_task_definition_address"]]["attributes"]
        assert {
            "container_definitions",
            "task_role_arn",
            "execution_role_arn",
            "network_mode",
            "cpu",
            "memory",
            "volume",
            "arn",
            "id",
        } <= set(attributes)
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
        "tiktok": MEDIA_WORKER_IMAGE,
    }
    assert data["images"]["consumers"]["desired"] == manifest_images
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
        "dynamodb_table": "teamagent-tflock",
        "encrypt": True,
        "identity_sha256": data["state_contract"]["backend"]["identity_sha256"],
        "key": "teamagent/terraform.tfstate",
        "region": REGION,
        "type": "s3",
        "workspace": "default",
    }
    assert len(data["state_contract"]["backend"]["identity_sha256"]) == 64
    template = json.loads(Path(env["TF_FAKE_TEMPLATE"]).read_text(encoding="utf-8"))
    managed_addresses = sorted(
        change["address"]
        for change in template["resource_changes"]
        if change.get("mode", "managed") == "managed"
    )
    address_set_sha256 = hashlib.sha256(
        "".join(f"{address}\n" for address in managed_addresses).encode()
    ).hexdigest()
    assert data["state_contract"]["state"] == {
        "address_count": len(managed_addresses),
        "address_set_sha256": address_set_sha256,
        "lineage": "01234567-89ab-cdef-0123-456789abcdef",
        "serial": 42,
    }
    assert data["state_contract"]["task_revisions"] == {}
    assert set(data["state_contract"]["imports"]) == {
        "aws_cloudwatch_log_group.codebuild_aiia_image_builder",
        "aws_cloudwatch_log_group.codebuild_image",
        "aws_cloudwatch_log_group.ecs_containerinsights_teamagent",
        "aws_cloudwatch_log_group.ecs_containerinsights_tiktok",
        "aws_cloudwatch_log_group.reminder_notify",
        "aws_cloudwatch_log_group.tiktok_dispatch",
        "aws_cloudwatch_log_group.x_dispatch",
    }
    assert all(item["present"] is False for item in data["state_contract"]["imports"].values())
    assert not any(
        command == "apply" or command.startswith("apply ")
        for command in tf_log.read_text(encoding="utf-8").splitlines()
    )

    verify = _run(["bash", str(GUARD), "verify", "--plan", str(plan)], env)
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert "read-only検証完了" in verify.stdout
    assert not any(
        command == "apply" or command.startswith("apply ")
        for command in tf_log.read_text(encoding="utf-8").splitlines()
    )


def test_exact_hmac_runtime_gate_set_is_accepted_by_sync_guard(tmp_path: Path) -> None:
    env, var_file, tf_log = _harness(tmp_path, hmac_active=True)
    plan = tmp_path / "hmac-runtime.tfplan"

    result = _run(_plan_command(var_file, plan), env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert plan.is_file()
    assert Path(f"{plan}.runtime-guard.json").is_file()
    saved_plan = json.loads(plan.read_text(encoding="utf-8"))
    manifest = saved_plan["variables"]["image_deployment_consumer_manifest"]["value"]
    assert manifest["mode"] == "no-image-transition"
    assert all(
        consumer["live"] == consumer["before"] == consumer["after"]
        for consumer in manifest["consumers"]
    )
    assert not any(
        command == "apply" or command.startswith("apply ")
        for command in tf_log.read_text(encoding="utf-8").splitlines()
    )


def test_runtime_migrations_require_exact_reviewed_plan_before_enablement() -> None:
    migrations = json.loads(
        (PROJECT_ROOT / "infra" / "deploy" / "terraform_runtime_migrations.json").read_text(
            encoding="utf-8"
        )
    )
    for migration in migrations["migrations"].values():
        assert migration["enabled"] is False
        assert migration["reviewed_plan"] is None
        assert set(migration["reviewed_inputs"]) == {"image_deployment_intent_id"}
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            migration["reviewed_inputs"]["image_deployment_intent_id"],
        )
        assert "allowed_changes" not in migration


def test_hmac_runtime_sync_rejects_missing_morning_post_gate(tmp_path: Path) -> None:
    env, var_file, _ = _harness(tmp_path, hmac_active=True)
    template_path = Path(env["TF_FAKE_TEMPLATE"])
    template = json.loads(template_path.read_text(encoding="utf-8"))
    post = _find(template, HMAC_MORNING_GATE_ADDRESSES[1])
    template["resource_changes"].remove(post)
    template_path.write_text(json.dumps(template), encoding="utf-8")
    plan = tmp_path / "hmac-bypass.tfplan"

    result = _run(_plan_command(var_file, plan), env)

    assert result.returncode == 1
    assert "promotion gate" in result.stdout + result.stderr
    assert not plan.exists()
    assert not Path(f"{plan}.runtime-guard.json").exists()


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
            "attest-log-versioning",
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
    "deletion",
    [
        {"Expiration": {"Days": 365}},
        {"NoncurrentVersionExpiration": {"NoncurrentDays": 365}},
    ],
)
def test_cloudtrail_live_lifecycle_deletion_is_rejected_before_plan(
    tmp_path: Path,
    deletion: dict[str, object],
) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    env["AWS_FAKE_CLOUDTRAIL_LIFECYCLE"] = json.dumps(
        {
            "Rules": [
                {
                    "ID": "delete-audit-logs",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    **deletion,
                }
            ]
        }
    )

    result = _run(
        _plan_command(var_file, tmp_path / "cloudtrail-lifecycle.tfplan"),
        env,
    )

    assert result.returncode == 1
    assert "CloudTrail監査bucketにexpiration/noncurrent deletion" in (result.stdout + result.stderr)
    assert "plan " not in tf_log.read_text(encoding="utf-8")


RUNTIME_ATTRIBUTE_FAILURES = {
    **dict.fromkeys(
        [
            "env_add",
            "env_change",
            "env_delete",
            "secret_add",
            "secret_change",
            "secret_delete",
        ],
        "aws_ecs_task_definition.mcp のenv/secretsがliveと完全一致しません",
    ),
    **dict.fromkeys(
        [
            "wrong_container",
            "duplicate_container",
            "task_skip_destroy",
        ],
        "aws_ecs_task_definition.mcp は期待container "
        "teamagent-mcp・候補image・unknown allowlistを満たしません",
    ),
    **dict.fromkeys(
        [
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
            "task_fault_injection",
            "task_restart_policy",
            "task_version_consistency",
            "task_credential_specs",
        ],
        "aws_ecs_task_definition.mcp のrole/cpu/memory/runtime/container/"
        "port/health/log/volume等がliveから変化します",
    ),
    **dict.fromkeys(
        [
            "service_desired",
            "service_network",
            "service_lb",
            "service_deployment",
            "service_force_deployment",
            "service_wait",
            "service_triggers",
            "service_connect",
        ],
        "aws_ecs_service.mcp[0] はliveからtask_definition参照以外も変更します",
    ),
    **dict.fromkeys(
        [
            "target_role",
            "target_cluster",
            "target_network",
            "target_retry",
            "target_input",
        ],
        "aws_cloudwatch_event_target.ingest_run_task[0] "
        "はliveからtask_definition参照以外も変更します",
    ),
    "rule_schedule": (
        "aws_cloudwatch_event_rule.ingest_weekly[0] "
        "のstate/schedule/description等を変更するruntime planは禁止です"
    ),
    **dict.fromkeys(
        [
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
        ],
        "aws_lambda_function.tiktok_dispatch[0] は所定taskdef参照以外のdispatcher設定を変更します",
    ),
    **dict.fromkeys(
        [
            "mapping_disabled",
            "mapping_queue",
        ],
        "aws_lambda_event_source_mapping.tiktok_dispatch[0] "
        "のqueue/function/enabled/batch/retry/filter等を変更するruntime planは禁止です",
    ),
}


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    tuple(RUNTIME_ATTRIBUTE_FAILURES.items()),
    ids=tuple(RUNTIME_ATTRIBUTE_FAILURES),
)
def test_runtime_attribute_regressions_fail_closed(
    tmp_path: Path,
    scenario: str,
    expected_error: str,
) -> None:
    env, var_file, _ = _harness(tmp_path, scenario)
    plan = tmp_path / "unsafe.tfplan"
    result = _run(_plan_command(var_file, plan), env)
    assert result.returncode == 1
    assert expected_error in result.stdout + result.stderr
    assert not plan.exists()
    assert not Path(f"{plan}.runtime-guard.json").exists()


PLAN_SCHEMA_FAILURES = {
    "unknown": (
        "aws_ecs_task_definition.mcp は期待container "
        "teamagent-mcp・候補image・unknown allowlistを満たしません"
    ),
    "arbitrary_update": "runtime planに許可外の変更を検出しました",
    "arbitrary_create": "非許可の destroy/replace を検出しました",
    "missing": (
        "runtime planに必須addressがありません: aws_cloudwatch_event_rule.ingest_weekly[0]"
    ),
    "schema": "plan JSON schema/check/image/rule/runtime guard bindingが不正です",
    "incomplete": "plan JSON schema/check/image/rule/runtime guard bindingが不正です",
    "deferred": "plan JSON schema/check/image/rule/runtime guard bindingが不正です",
    "invocation": "plan JSON schema/check/image/rule/runtime guard bindingが不正です",
    "checks": "plan JSON schema/check/image/rule/runtime guard bindingが不正です",
    "bad_action": "plan JSONのschema/action/check/runtime_guard束縛が不正です",
    "arbitrary_drift": "runtime planに許可外resourceのdriftを検出しました",
    "data_write": "plan JSONのschema/action/check/runtime_guard束縛が不正です",
}


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    tuple(PLAN_SCHEMA_FAILURES.items()),
    ids=tuple(PLAN_SCHEMA_FAILURES),
)
def test_plan_schema_action_and_allowlist_regressions_fail_closed(
    tmp_path: Path,
    scenario: str,
    expected_error: str,
) -> None:
    env, var_file, _ = _harness(tmp_path, scenario)
    plan = tmp_path / "unsafe.tfplan"
    result = _run(_plan_command(var_file, plan), env)
    assert result.returncode == 1
    assert expected_error in result.stdout + result.stderr
    assert not plan.exists()


@pytest.mark.parametrize(
    ("show_mode", "expected_error"),
    [
        ("malformed", "planからHMAC metadataを一意に取得できません"),
        ("race", "terraform show中のplan差替えを検出しました"),
    ],
)
def test_malformed_or_replaced_plan_is_never_published(
    tmp_path: Path,
    show_mode: str,
    expected_error: str,
) -> None:
    env, var_file, _ = _harness(tmp_path)
    env["TF_FAKE_SHOW_MODE"] = show_mode
    plan = tmp_path / "unsafe.tfplan"
    result = _run(_plan_command(var_file, plan), env)
    assert result.returncode == 1
    assert expected_error in result.stdout + result.stderr
    assert not plan.exists()


def test_live_change_during_plan_is_never_published(tmp_path: Path) -> None:
    env, var_file, _ = _harness(tmp_path)
    drift_marker = tmp_path / "terraform-plan-completed"
    env["TF_FAKE_DRIFT_AFTER_PLAN_MARKER"] = str(drift_marker)
    plan = tmp_path / "unsafe.tfplan"
    result = _run(_plan_command(var_file, plan), env)
    assert result.returncode == 1
    assert "plan 作成中に live runtime が変化しました" in (result.stdout + result.stderr)
    assert drift_marker.exists()
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
    assert "想定外のAWS accountです: 000000000000" in result.stdout + result.stderr
    assert "plan " not in tf_log.read_text(encoding="utf-8")

    env.pop("AWS_FAKE_ACCOUNT")
    env["AWS_FAKE_SERVICE_STATE"] = "in_progress"
    result = _run(_plan_command(var_file, tmp_path / "service.tfplan"), env)
    assert result.returncode == 1
    assert "teamagent-dev-mcp が安定稼働中ではありません" in (result.stdout + result.stderr)


@pytest.mark.parametrize(
    "marker",
    [
        "AWS_FAKE_NO_ALARM_DELIVERY",
        "AWS_FAKE_LEGACY_ALARM_TOPIC",
        "AWS_FAKE_LEGACY_ALARM_ACTION",
        "AWS_FAKE_LEGACY_BUDGET_ACTION",
        "AWS_FAKE_LEGACY_ANOMALY_ACTION",
        "AWS_FAKE_PENDING_SUBSCRIPTION",
        "AWS_FAKE_EXTRA_SUBSCRIPTION",
        "AWS_FAKE_DIFFERENT_ALARM_EMAIL",
        "AWS_FAKE_EMAIL_JSON",
        "AWS_FAKE_CHATBOT",
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
    assert "live runtimeのboolean/env契約が不正です" in result.stdout + result.stderr
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


def test_state_address_reconstruction_binds_mixed_exact_address_set(
    tmp_path: Path,
) -> None:
    env, var_file, _ = _harness(tmp_path)
    state = tmp_path / "module-state.json"
    extra_addresses = [
        "aws_cloudwatch_log_group.audit",
        "data.aws_caller_identity.current",
        'module.example.aws_s3_bucket.logs["blue"]',
        "module.example.data.aws_subnets.private[0]",
    ]
    state_payload = json.loads(Path(env["TF_FAKE_DEFAULT_STATE"]).read_text(encoding="utf-8"))
    state_payload["serial"] = 43
    state_payload["resources"].extend(
        [
            {
                "mode": "managed",
                "type": "aws_cloudwatch_log_group",
                "name": "audit",
                "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                "instances": [
                    {
                        "schema_version": 0,
                        "attributes": {"id": "/teamagent/dev/audit"},
                    }
                ],
            },
            {
                "mode": "data",
                "type": "aws_caller_identity",
                "name": "current",
                "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                "instances": [
                    {
                        "schema_version": 0,
                        "attributes": {"id": ACCOUNT},
                    }
                ],
            },
            {
                "module": "module.example",
                "mode": "managed",
                "type": "aws_s3_bucket",
                "name": "logs",
                "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                "instances": [
                    {
                        "index_key": "blue",
                        "schema_version": 0,
                        "attributes": {"id": "teamagent-test-module-logs"},
                    }
                ],
            },
            {
                "module": "module.example",
                "mode": "data",
                "type": "aws_subnets",
                "name": "private",
                "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                "instances": [
                    {
                        "index_key": 0,
                        "schema_version": 0,
                        "attributes": {"id": REGION},
                    }
                ],
            },
        ]
    )
    template = json.loads(Path(env["TF_FAKE_TEMPLATE"]).read_text(encoding="utf-8"))
    addresses = extra_addresses + [
        change["address"]
        for change in template["resource_changes"]
        if change.get("mode", "managed") == "managed" and change["change"].get("before") is not None
    ]
    state.write_text(json.dumps(state_payload), encoding="utf-8")
    state.chmod(0o600)
    env["TF_FAKE_STATE"] = str(state)

    plan = tmp_path / "module-state.tfplan"
    result = _run(_plan_command(var_file, plan), env)

    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(Path(f"{plan}.runtime-guard.json").read_text(encoding="utf-8"))
    state_contract = receipt["state_contract"]["state"]
    assert state_contract["serial"] == 43
    assert state_contract["address_count"] == len(addresses)
    assert (
        state_contract["address_set_sha256"]
        == hashlib.sha256(
            "".join(f"{address}\n" for address in sorted(addresses)).encode()
        ).hexdigest()
    )


def test_scoped_state_revision_is_derived_independently_from_live(
    tmp_path: Path,
) -> None:
    env, _, _ = _harness(tmp_path)
    body = GUARD.read_text(encoding="utf-8")
    function = re.search(
        r"capture_state_contract\(\) \{.*?"
        r"(?=\nverify_alarm_delivery_test_receipt_legacy_retired\(\))",
        body,
        flags=re.DOTALL,
    )
    assert function is not None
    tmp_root = tmp_path / "state-contract-tmp"
    tmp_root.mkdir(mode=0o700)
    live_contract = tmp_path / "live-contract.json"
    live_contract.write_text(
        json.dumps(
            {
                "resources": [
                    {
                        "activation": {
                            "identity": "teamagent-dev-mcp",
                            "state": 1,
                            "type": "ecs_service",
                        },
                        "consumer_id": "mcp",
                        "image": LIVE_IMAGE,
                        "pipeline": "mcp",
                        "subject": "core",
                        "task_definition_arn": _task_arn("mcp"),
                        "terraform_address": TASK_ADDRESSES["mcp"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    live_contract.chmod(0o600)
    output = tmp_path / "scoped-state-contract.json"
    script = "\n".join(
        (
            "set -euo pipefail",
            f"TMP_ROOT={str(tmp_root)!r}",
            f"TF_DIR={str(PROJECT_ROOT / 'infra' / 'terraform')!r}",
            f"IMAGE_DEPLOYMENT_CONSUMER_REGISTRY={str(PROJECT_ROOT / 'infra' / 'codebuild' / 'image_deployment_consumers.json')!r}",
            f"EXPECTED_ACCOUNT_ID={ACCOUNT!r}",
            f"REGION={REGION!r}",
            'EXPECTED_WORKSPACE="default"',
            'die() { echo "★ $*" >&2; return 1; }',
            "sha256_file() { openssl dgst -sha256 \"$1\" | awk '{print $NF}'; }",
            """
capture_backend_identity() {
  jq -n -S '{
    type:"s3",
    identity_sha256:"0000000000000000000000000000000000000000000000000000000000000000"
  }' > "$1"
}
""",
            function.group(0),
            'capture_state_contract "$1" "$2"',
        )
    )

    accepted = subprocess.run(
        ["bash", "-c", script, "validator", str(output), str(live_contract)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["task_revisions"] == {
        "mcp": COMPONENTS["mcp"][2]
    }

    drifted_env = env.copy()
    drifted_env["TF_FAKE_TASK_REVISION_DRIFT"] = "mcp"
    rejected = subprocess.run(
        ["bash", "-c", script, "validator", str(output), str(live_contract)],
        capture_output=True,
        text=True,
        check=False,
        env=drifted_env,
    )
    assert rejected.returncode != 0
    assert "state task definition binding" in rejected.stdout + rejected.stderr


def test_state_address_reconstruction_rejects_unknown_resource_mode(
    tmp_path: Path,
) -> None:
    env, var_file, tf_log = _harness(tmp_path)
    state = tmp_path / "unknown-mode-state.json"
    state.write_text(
        json.dumps(
            {
                "version": 4,
                "terraform_version": "1.12.2",
                "serial": 44,
                "lineage": "01234567-89ab-cdef-0123-456789abcdef",
                "outputs": {},
                "resources": [
                    {
                        "mode": "ephemeral",
                        "type": "terraform_data",
                        "name": "bad",
                        "instances": [{"schema_version": 0, "attributes": {"id": "bad"}}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state_list = tmp_path / "unknown-mode-list.txt"
    state_list.write_text("terraform_data.bad\n", encoding="utf-8")
    state.chmod(0o600)
    state_list.chmod(0o600)
    env["TF_FAKE_STATE"] = str(state)
    env["TF_FAKE_STATE_LIST"] = str(state_list)

    result = _run(_plan_command(var_file, tmp_path / "unknown-mode.tfplan"), env)

    assert result.returncode == 1
    assert "state pullからaddress ownershipを再構成できません" in (result.stdout + result.stderr)
    assert "plan " not in tf_log.read_text(encoding="utf-8")


def test_backend_identity_is_observed_from_normalized_initialized_metadata(
    tmp_path: Path,
) -> None:
    body = GUARD.read_text(encoding="utf-8")
    function = re.search(
        r"capture_backend_identity\(\) \{.*?(?=\ncapture_state_contract\(\))",
        body,
        flags=re.DOTALL,
    )
    assert function is not None
    tmp_path.chmod(0o700)
    metadata = tmp_path / "backend.tfstate"
    output = tmp_path / "backend.json"
    config = {
        "bucket": "teamagent-tfstate-718959508629",
        "key": "teamagent/terraform.tfstate",
        "region": REGION,
        "dynamodb_table": "teamagent-tflock",
        "encrypt": True,
        "access_key": None,
        "secret_key": None,
        "token": None,
        "endpoint": None,
        "workspace_key_prefix": None,
    }
    metadata.write_text(
        json.dumps({"backend": {"type": "s3", "config": config}}),
        encoding="utf-8",
    )
    metadata.chmod(0o600)
    script = "\n".join(
        (
            "set -euo pipefail",
            'EXPECTED_BACKEND_BUCKET="teamagent-tfstate-718959508629"',
            'EXPECTED_BACKEND_KEY="teamagent/terraform.tfstate"',
            'EXPECTED_BACKEND_DYNAMODB_TABLE="teamagent-tflock"',
            f'REGION="{REGION}"',
            'die() { echo "★ $*" >&2; return 1; }',
            "assert_owned() { :; }",
            "assert_not_shared_writable() { :; }",
            'assert_regular_nonwritable() { [ ! -L "$1" ] && [ -f "$1" ]; }',
            "sha256_file() { openssl dgst -sha256 \"$1\" | awk '{print $NF}'; }",
            function.group(0),
            'capture_backend_identity "$1" "$2"',
        )
    )

    valid = subprocess.run(
        ["bash", "-c", script, "validator", str(output), str(metadata)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert valid.returncode == 0, valid.stderr
    identity = json.loads(output.read_text(encoding="utf-8"))
    assert identity == {
        "bucket": "teamagent-tfstate-718959508629",
        "dynamodb_table": "teamagent-tflock",
        "encrypt": True,
        "identity_sha256": identity["identity_sha256"],
        "key": "teamagent/terraform.tfstate",
        "region": REGION,
        "type": "s3",
    }
    assert re.fullmatch(r"[0-9a-f]{64}", identity["identity_sha256"])

    config["endpoint"] = "https://attacker.invalid"
    metadata.write_text(
        json.dumps({"backend": {"type": "s3", "config": config}}),
        encoding="utf-8",
    )
    rejected = subprocess.run(
        ["bash", "-c", script, "validator", str(output), str(metadata)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert (
        "初期化済みTerraform backend metadataがreview済みS3設定と一致しません"
        in rejected.stdout + rejected.stderr
    )


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
    assert "既存pathへの上書きを拒否します" in result.stdout + result.stderr
    assert existing.read_text(encoding="utf-8") == "do not overwrite"

    target_command = _plan_command(var_file, tmp_path / "target.tfplan")
    target_command.extend(["--target", "aws_iam_policy.bad"])
    result = _run(target_command, env)
    assert result.returncode == 1
    assert "不明な引数: --target" in result.stdout + result.stderr


def test_atomic_publish_race_does_not_overwrite_or_delete_racer(tmp_path: Path) -> None:
    env, var_file, _ = _harness(tmp_path)
    plan = tmp_path / "race.tfplan"
    receipt = Path(f"{plan}.runtime-guard.json")
    env["TF_FAKE_PUBLISH_RACE_PATH"] = str(receipt)

    result = _run(_plan_command(var_file, plan), env)

    assert result.returncode == 1
    assert "publish先receipt pathを原子的に確保できません" in (result.stdout + result.stderr)
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
    assert "receiptが別plan pathに束縛されています" in (swapped.stdout + swapped.stderr)

    first.write_bytes(first.read_bytes() + b"tamper")
    tampered = _run(["bash", str(GUARD), "verify", "--plan", str(first)], env)
    assert tampered.returncode == 1
    assert "plan SHA256がreceiptと不一致です" in tampered.stdout + tampered.stderr

    env["AWS_FAKE_DRIFT"] = "1"
    live = _run(["bash", str(GUARD), "verify", "--plan", str(second)], env)
    assert live.returncode == 1
    assert "plan作成後にlive runtimeが変化しました" in live.stdout + live.stderr

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
    assert apply.returncode != 0
    assert "review済みTerraform v1だけを使用できます" in (apply.stdout + apply.stderr)
    assert not any(
        command == "apply" or command.startswith("apply ")
        for command in tf_log.read_text(encoding="utf-8").splitlines()
    )
