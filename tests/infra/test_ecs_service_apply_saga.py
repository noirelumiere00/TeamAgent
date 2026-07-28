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
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "terraform" / "ecs_service_apply_saga.py"
FINALIZER_PATH = ROOT / "infra" / "deploy" / "deployment_apply_finalizer.py"
REGISTRY_PATH = ROOT / "infra" / "codebuild" / "image_deployment_consumers.json"
ATTEMPT = "12345678-1234-4123-8123-123456789abc"
PLAN_SHA256 = "a" * 64
CLUSTER_ARN = "arn:aws:ecs:ap-northeast-1:718959508629:cluster/teamagent-dev"
ECR_REGISTRY = "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com"
EXPECTED_CONSUMER_IDS = frozenset(
    {
        "mcp",
        "connect_web",
        "openclaw",
        "canary",
        "ingest",
        "morning_digest",
        "x_buzz_worker",
        "tiktok_acquire",
    }
)
CONSUMER_REGISTRY = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
CONSUMERS = {consumer["consumer_id"]: consumer for consumer in CONSUMER_REGISTRY["consumers"]}
assert frozenset(CONSUMERS) == EXPECTED_CONSUMER_IDS

MCP_SERVICE_ARN = "arn:aws:ecs:ap-northeast-1:718959508629:service/teamagent-dev/teamagent-dev-mcp"
CONNECT_SERVICE_ARN = (
    "arn:aws:ecs:ap-northeast-1:718959508629:service/teamagent-dev/teamagent-dev-connect-web"
)
OPENCLAW_SERVICE_ARN = (
    "arn:aws:ecs:ap-northeast-1:718959508629:service/teamagent-dev/teamagent-dev-openclaw"
)
OLD_MCP_TASK = "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-mcp:50"
NEW_MCP_TASK = "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-mcp:51"
OLD_CONNECT_TASK = (
    "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-connect-web:60"
)
NEW_CONNECT_TASK = (
    "arn:aws:ecs:ap-northeast-1:718959508629:task-definition/teamagent-dev-connect-web:61"
)
_TASK_REVISIONS = {
    "mcp": (50, 51),
    "connect_web": (60, 61),
    "openclaw": (70, 71),
    "canary": (80, 81),
    "ingest": (90, 91),
    "morning_digest": (100, 101),
    "x_buzz_worker": (110, 111),
    "tiktok_acquire": (120, 121),
}


def _task_arn(consumer_id: str, revision: int) -> str:
    family = CONSUMERS[consumer_id]["ecs_family"]
    return f"arn:aws:ecs:ap-northeast-1:718959508629:task-definition/{family}:{revision}"


OLD_TASKS = {
    consumer_id: _task_arn(consumer_id, revisions[0])
    for consumer_id, revisions in _TASK_REVISIONS.items()
}
NEW_TASKS = {
    consumer_id: _task_arn(consumer_id, revisions[1])
    for consumer_id, revisions in _TASK_REVISIONS.items()
}
OLD_TASKS["mcp"] = OLD_MCP_TASK
NEW_TASKS["mcp"] = NEW_MCP_TASK
OLD_TASKS["connect_web"] = OLD_CONNECT_TASK
NEW_TASKS["connect_web"] = NEW_CONNECT_TASK
_OLD_REPOSITORY_DIGESTS = {
    "teamagent-mcp": "b" * 64,
    "teamagent-openclaw": "d" * 64,
    "teamagent-media-worker": "e" * 64,
}
_NEW_REPOSITORY_DIGESTS = {
    "teamagent-mcp": "c" * 64,
    "teamagent-openclaw": "a" * 64,
    "teamagent-media-worker": "f" * 64,
}
OLD_IMAGE_DIGESTS = {
    consumer_id: _OLD_REPOSITORY_DIGESTS[consumer["release_repository"]]
    for consumer_id, consumer in CONSUMERS.items()
}
NEW_IMAGE_DIGESTS = {
    consumer_id: _NEW_REPOSITORY_DIGESTS[consumer["release_repository"]]
    for consumer_id, consumer in CONSUMERS.items()
}
SERVICE_ARNS = {
    "mcp": MCP_SERVICE_ARN,
    "connect_web": CONNECT_SERVICE_ARN,
    "openclaw": OPENCLAW_SERVICE_ARN,
}
RULE_STATES = {
    "canary": "DISABLED",
    "ingest": "DISABLED",
    "morning_digest": "ENABLED",
}


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ecs_service_apply_saga_under_test",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SAGA = _load_module()


def _load_finalizer() -> Any:
    spec = importlib.util.spec_from_file_location(
        "deployment_apply_finalizer_for_ecs_integration",
        FINALIZER_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FINALIZER = _load_finalizer()


def _resource(
    *,
    address: str,
    resource_type: str,
    name: str,
    after: dict[str, Any],
    actions: list[str],
    index: int | None = None,
    before: dict[str, Any] | None = None,
    after_unknown: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "address": address,
        "mode": "managed",
        "type": resource_type,
        "name": name,
        "change": {
            "actions": actions,
            "before": copy.deepcopy(after if before is None else before),
            "after": copy.deepcopy(after),
            "after_unknown": copy.deepcopy(after_unknown or {}),
        },
    }
    if index is not None:
        value["index"] = index
    return value


def _task_after(
    family: str,
    container_name: str,
    *,
    repository: str | None = None,
    image_digest: str = "c" * 64,
) -> dict[str, Any]:
    image_repository = repository or family
    return {
        "container_definitions": json.dumps(
            [
                {
                    "essential": True,
                    "image": (f"{ECR_REGISTRY}/{image_repository}@sha256:{image_digest}"),
                    "name": container_name,
                }
            ],
            separators=(",", ":"),
            sort_keys=True,
        ),
        "cpu": "1024",
        "enable_fault_injection": False,
        "execution_role_arn": (f"arn:aws:iam::718959508629:role/{family}-execution"),
        "family": family,
        "memory": "2048",
        "network_mode": "awsvpc",
        "requires_compatibilities": ["FARGATE"],
        "runtime_platform": [
            {
                "cpu_architecture": "ARM64",
                "operating_system_family": "LINUX",
            }
        ],
        "tags_all": {
            "Environment": "dev",
            "ManagedBy": "Terraform",
            "Project": "TeamAgent",
            "Version": "v3.0",
        },
        "task_role_arn": f"arn:aws:iam::718959508629:role/{family}-task",
        "volume": [],
    }


def _task_change(
    consumer_id: str,
    *,
    image_digest: str,
    actions: list[str],
) -> dict[str, Any]:
    consumer = CONSUMERS[consumer_id]
    address = consumer["terraform_task_definition_address"]
    return _resource(
        address=address,
        resource_type="aws_ecs_task_definition",
        name=address.split(".", maxsplit=1)[1].split("[", maxsplit=1)[0],
        index=0 if address.endswith("[0]") else None,
        actions=actions,
        after=_task_after(
            consumer["ecs_family"],
            consumer["container_name"],
            repository=consumer["release_repository"],
            image_digest=image_digest,
        ),
    )


def _service_change(
    consumer_id: str,
    *,
    task_definition: str | None,
) -> dict[str, Any]:
    address = {
        "mcp": "aws_ecs_service.mcp[0]",
        "connect_web": "aws_ecs_service.connect_web[0]",
        "openclaw": "aws_ecs_service.openclaw[0]",
    }[consumer_id]
    unknown = {"task_definition": True} if task_definition is None else {}
    return _resource(
        address=address,
        resource_type="aws_ecs_service",
        name=address.split(".", maxsplit=1)[1].split("[", maxsplit=1)[0],
        index=0,
        actions=(["no-op"] if task_definition == OLD_TASKS[consumer_id] else ["update"]),
        before={
            "cluster": CLUSTER_ARN,
            "name": CONSUMERS[consumer_id]["activator"]["identity"],
            "desired_count": 1,
            "task_definition": OLD_TASKS[consumer_id],
        },
        after={
            "cluster": CLUSTER_ARN,
            "name": CONSUMERS[consumer_id]["activator"]["identity"],
            "desired_count": 1,
            "task_definition": task_definition,
        },
        after_unknown=unknown,
    )


def _event_rule_change(
    consumer_id: str,
    *,
    before_state: str,
    after_state: str,
) -> dict[str, Any]:
    address = {
        "canary": "aws_cloudwatch_event_rule.canary_hourly[0]",
        "ingest": "aws_cloudwatch_event_rule.ingest_weekly[0]",
        "morning_digest": "aws_cloudwatch_event_rule.morning_digest_weekday[0]",
    }[consumer_id]
    identity = CONSUMERS[consumer_id]["activator"]["identity"]
    return _resource(
        address=address,
        resource_type="aws_cloudwatch_event_rule",
        name=address.split(".", maxsplit=1)[1].split("[", maxsplit=1)[0],
        index=0,
        actions=["update"] if before_state != after_state else ["no-op"],
        before={
            "event_bus_name": "default",
            "name": identity,
            "state": before_state,
        },
        after={
            "event_bus_name": "default",
            "name": identity,
            "state": after_state,
        },
    )


def _event_target_change(
    consumer_id: str,
    *,
    task_definition: str | None,
) -> dict[str, Any]:
    address = {
        "canary": "aws_cloudwatch_event_target.canary_run_task[0]",
        "ingest": "aws_cloudwatch_event_target.ingest_run_task[0]",
        "morning_digest": "aws_cloudwatch_event_target.morning_digest_run_task[0]",
    }[consumer_id]
    identity = CONSUMERS[consumer_id]["activator"]["identity"]
    unknown = {"ecs_target": [{"task_definition_arn": True}]} if task_definition is None else {}
    return _resource(
        address=address,
        resource_type="aws_cloudwatch_event_target",
        name=address.split(".", maxsplit=1)[1].split("[", maxsplit=1)[0],
        index=0,
        actions=(["no-op"] if task_definition == OLD_TASKS[consumer_id] else ["update"]),
        before={
            "rule": identity,
            "event_bus_name": "default",
            "arn": CLUSTER_ARN,
            "ecs_target": [
                {
                    "task_definition_arn": OLD_TASKS[consumer_id],
                }
            ],
        },
        after={
            "rule": identity,
            "event_bus_name": "default",
            "arn": CLUSTER_ARN,
            "ecs_target": [{"task_definition_arn": task_definition}],
        },
        after_unknown=unknown,
    )


def _lambda_change(
    consumer_id: str,
    *,
    task_definition: str | None,
) -> dict[str, Any]:
    address = {
        "x_buzz_worker": "aws_lambda_function.x_dispatch[0]",
        "tiktok_acquire": "aws_lambda_function.tiktok_dispatch[0]",
    }[consumer_id]
    identity = CONSUMERS[consumer_id]["activator"]["identity"]
    variables: dict[str, Any] = {
        "CLUSTER_ARN": CLUSTER_ARN,
        "SUBNETS": "subnet-a,subnet-b",
        "SG_ID": f"sg-{consumer_id}",
        "CONTAINER": CONSUMERS[consumer_id]["container_name"],
        "TASKDEF_ARN": task_definition,
    }
    unknown = (
        {"environment": [{"variables": {"TASKDEF_ARN": True}}]} if task_definition is None else {}
    )
    return _resource(
        address=address,
        resource_type="aws_lambda_function",
        name=address.split(".", maxsplit=1)[1].split("[", maxsplit=1)[0],
        index=0,
        actions=(["no-op"] if task_definition == OLD_TASKS[consumer_id] else ["update"]),
        before={
            "function_name": identity,
            "environment": [
                {
                    "variables": {
                        **variables,
                        "TASKDEF_ARN": OLD_TASKS[consumer_id],
                    }
                }
            ],
        },
        after={
            "function_name": identity,
            "environment": [{"variables": variables}],
        },
        after_unknown=unknown,
    )


def _plan(
    *,
    mcp_task: str | None = NEW_MCP_TASK,
    connect_task: str | None = NEW_CONNECT_TASK,
    task_definitions: dict[str, str | None] | None = None,
    rule_states: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    planned_tasks: dict[str, str | None] = {
        **OLD_TASKS,
        "mcp": mcp_task,
        "connect_web": connect_task,
    }
    if task_definitions is not None:
        planned_tasks.update(task_definitions)
    planned_rule_states = {key: (state, state) for key, state in RULE_STATES.items()}
    if rule_states is not None:
        planned_rule_states.update(rule_states)
    task_changes = [
        _task_change(
            consumer_id,
            image_digest=(
                NEW_IMAGE_DIGESTS[consumer_id]
                if planned_tasks[consumer_id] in {None, NEW_TASKS[consumer_id]}
                else OLD_IMAGE_DIGESTS[consumer_id]
            ),
            actions=(
                ["create", "delete"]
                if planned_tasks[consumer_id] in {None, NEW_TASKS[consumer_id]}
                else ["no-op"]
            ),
        )
        for consumer_id in sorted(EXPECTED_CONSUMER_IDS)
    ]
    return {
        "format_version": "1.2",
        "terraform_version": "1.12.2",
        "complete": True,
        "errored": False,
        "resource_changes": [
            *task_changes,
            *(
                _service_change(
                    consumer_id,
                    task_definition=planned_tasks[consumer_id],
                )
                for consumer_id in ("mcp", "connect_web", "openclaw")
            ),
            *(
                change
                for consumer_id in ("canary", "ingest", "morning_digest")
                for change in (
                    _event_rule_change(
                        consumer_id,
                        before_state=planned_rule_states[consumer_id][0],
                        after_state=planned_rule_states[consumer_id][1],
                    ),
                    _event_target_change(
                        consumer_id,
                        task_definition=planned_tasks[consumer_id],
                    ),
                )
            ),
            *(
                _lambda_change(
                    consumer_id,
                    task_definition=planned_tasks[consumer_id],
                )
                for consumer_id in ("x_buzz_worker", "tiktok_acquire")
            ),
            {
                "address": "aws_s3_bucket.unrelated",
                "mode": "managed",
                "type": "aws_s3_bucket",
                "name": "unrelated",
                "change": {"actions": ["update"], "before": {}, "after": {}},
            },
        ],
    }


def _deployment_configuration(
    *,
    maximum: int = 200,
    minimum: int = 100,
    enable: bool = True,
    rollback: bool = True,
) -> dict[str, Any]:
    return {
        "deploymentCircuitBreaker": {
            "enable": enable,
            "rollback": rollback,
        },
        "maximumPercent": maximum,
        "minimumHealthyPercent": minimum,
    }


def _network(
    *,
    subnet: str,
    security_group: str,
    public_ip: str = "ENABLED",
) -> dict[str, Any]:
    return {
        "awsvpcConfiguration": {
            "assignPublicIp": public_ip,
            "securityGroups": [security_group],
            "subnets": [subnet],
        }
    }


def _service(
    *,
    service_arn: str,
    service_name: str,
    task_definition: str,
    network: dict[str, Any],
    deployment: dict[str, Any] | None = None,
    desired_count: int = 1,
) -> dict[str, Any]:
    deployment_configuration = deployment or _deployment_configuration()
    return {
        "serviceArn": service_arn,
        "serviceName": service_name,
        "clusterArn": CLUSTER_ARN,
        "status": "ACTIVE",
        "launchType": "FARGATE",
        "schedulingStrategy": "REPLICA",
        "deploymentController": {"type": "ECS"},
        "taskDefinition": task_definition,
        "deploymentConfiguration": copy.deepcopy(deployment_configuration),
        "networkConfiguration": copy.deepcopy(network),
        "desiredCount": desired_count,
        "runningCount": desired_count,
        "pendingCount": 0,
        "deployments": [
            {
                "status": "PRIMARY",
                "taskDefinition": task_definition,
                "desiredCount": desired_count,
                "runningCount": desired_count,
                "pendingCount": 0,
                "rolloutState": "COMPLETED",
            }
        ],
    }


def _aws_task_definition(
    consumer_id: str,
    *,
    task_definition: str,
    image_digest: str,
) -> dict[str, Any]:
    consumer = CONSUMERS[consumer_id]
    after = _task_after(
        consumer["ecs_family"],
        consumer["container_name"],
        repository=consumer["release_repository"],
        image_digest=image_digest,
    )
    payload = SAGA.task_from_change(after, task=consumer_id)
    tags = payload.pop("tags")
    return {
        "taskDefinition": {
            **copy.deepcopy(payload),
            "compatibilities": ["EC2", "FARGATE"],
            "registeredAt": "2026-07-28T00:00:00+00:00",
            "registeredBy": (
                "arn:aws:sts::718959508629:assumed-role/teamagent-dev-runtime-automation/test"
            ),
            "requiresAttributes": [],
            "placementConstraints": [],
            "taskDefinitionArn": task_definition,
            "revision": int(task_definition.rsplit(":", maxsplit=1)[1]),
            "status": "ACTIVE",
        },
        "tags": tags,
    }


def _rule(consumer_id: str, *, state: str) -> dict[str, Any]:
    name = CONSUMERS[consumer_id]["activator"]["identity"]
    return {
        "Name": name,
        "Arn": f"arn:aws:events:ap-northeast-1:718959508629:rule/{name}",
        "EventBusName": "default",
        "State": state,
        "ScheduleExpression": "cron(0 0 * * ? *)",
        "Description": f"{consumer_id} schedule",
    }


def _target(
    consumer_id: str,
    *,
    task_definition: str,
) -> dict[str, Any]:
    target_id = "morning" if consumer_id == "morning_digest" else f"target-{consumer_id}"
    return {
        "Id": target_id,
        "Arn": CLUSTER_ARN,
        "RoleArn": (f"arn:aws:iam::718959508629:role/teamagent-dev-events-{consumer_id}"),
        **({"Input": "{}"} if consumer_id == "morning_digest" else {}),
        "EcsParameters": {
            "TaskDefinitionArn": task_definition,
            "TaskCount": 1,
            "LaunchType": "FARGATE",
            "PlatformVersion": "LATEST",
            "EnableECSManagedTags": False,
            "EnableExecuteCommand": False,
            "NetworkConfiguration": {
                "awsvpcConfiguration": {
                    "AssignPublicIp": "ENABLED",
                    "SecurityGroups": [f"sg-{consumer_id}"],
                    "Subnets": ["subnet-a", "subnet-b"],
                }
            },
        },
        "RetryPolicy": {
            "MaximumEventAgeInSeconds": 3600,
            "MaximumRetryAttempts": 1,
        },
    }


def _lambda_configuration(
    consumer_id: str,
    *,
    task_definition: str,
) -> dict[str, Any]:
    consumer = CONSUMERS[consumer_id]
    name = consumer["activator"]["identity"]
    return {
        "FunctionName": name,
        "FunctionArn": (f"arn:aws:lambda:ap-northeast-1:718959508629:function:{name}"),
        "State": "Active",
        "LastUpdateStatus": "Successful",
        "RevisionId": f"revision-{consumer_id}",
        "Role": f"arn:aws:iam::718959508629:role/{name}",
        "Runtime": "python3.12",
        "Handler": "handler.handler",
        "Architectures": ["arm64"],
        "CodeSha256": "Y29kZS1zaGEyNTY=",
        "Description": "",
        "Timeout": 30,
        "MemorySize": 512 if consumer_id == "tiktok_acquire" else 128,
        "PackageType": "Zip",
        "Environment": {
            "Variables": {
                "CLUSTER_ARN": CLUSTER_ARN,
                "SUBNETS": "subnet-a,subnet-b",
                "SG_ID": f"sg-{consumer_id}",
                "CONTAINER": consumer["container_name"],
                "TASKDEF_ARN": task_definition,
            }
        },
        "TracingConfig": {"Mode": "PassThrough"},
        "EphemeralStorage": {"Size": 512},
        "SnapStart": {"ApplyOn": "None"},
    }


def _running_task(
    consumer_id: str,
    *,
    task_definition: str,
    image_digest: str,
) -> dict[str, Any]:
    consumer = CONSUMERS[consumer_id]
    task_arn = f"arn:aws:ecs:ap-northeast-1:718959508629:task/teamagent-dev/running-{consumer_id}"
    image = f"{ECR_REGISTRY}/{consumer['release_repository']}@sha256:{image_digest}"
    return {
        "taskArn": task_arn,
        "clusterArn": CLUSTER_ARN,
        "taskDefinitionArn": task_definition,
        "group": f"service:{consumer['activator']['identity']}",
        "desiredStatus": "RUNNING",
        "lastStatus": "RUNNING",
        "launchType": "FARGATE",
        "containers": [
            {
                "taskArn": task_arn,
                "name": consumer["container_name"],
                "image": image,
                "imageDigest": f"sha256:{image_digest}",
                "lastStatus": "RUNNING",
            }
        ],
    }


def _argument(arguments: Sequence[str], name: str) -> str:
    index = arguments.index(name)
    return arguments[index + 1]


def _argument_values(arguments: Sequence[str], name: str) -> list[str]:
    index = arguments.index(name) + 1
    values: list[str] = []
    while index < len(arguments) and not arguments[index].startswith("--"):
        values.append(arguments[index])
        index += 1
    return values


class _FakeCli:
    def __init__(self) -> None:
        self.services = {
            MCP_SERVICE_ARN: _service(
                service_arn=MCP_SERVICE_ARN,
                service_name="teamagent-dev-mcp",
                task_definition=OLD_MCP_TASK,
                network=_network(subnet="subnet-b", security_group="sg-mcp"),
            ),
            CONNECT_SERVICE_ARN: _service(
                service_arn=CONNECT_SERVICE_ARN,
                service_name="teamagent-dev-connect-web",
                task_definition=OLD_CONNECT_TASK,
                network=_network(
                    subnet="subnet-a",
                    security_group="sg-connect",
                ),
            ),
            OPENCLAW_SERVICE_ARN: _service(
                service_arn=OPENCLAW_SERVICE_ARN,
                service_name="teamagent-dev-openclaw",
                task_definition=OLD_TASKS["openclaw"],
                network=_network(
                    subnet="subnet-c",
                    security_group="sg-openclaw",
                ),
            ),
        }
        self.list_pages: dict[str, dict[str, Any]] = {
            "": {
                "serviceArns": [
                    OPENCLAW_SERVICE_ARN,
                    MCP_SERVICE_ARN,
                ],
                "nextToken": "page-2",
            },
            "page-2": {"serviceArns": [CONNECT_SERVICE_ARN]},
        }
        self.items: dict[str, dict[str, Any]] = {}
        self.task_definitions: dict[str, dict[str, Any]] = {}
        for consumer_id in EXPECTED_CONSUMER_IDS:
            self.task_definitions[OLD_TASKS[consumer_id]] = _aws_task_definition(
                consumer_id,
                task_definition=OLD_TASKS[consumer_id],
                image_digest=OLD_IMAGE_DIGESTS[consumer_id],
            )
            self.task_definitions[NEW_TASKS[consumer_id]] = _aws_task_definition(
                consumer_id,
                task_definition=NEW_TASKS[consumer_id],
                image_digest=NEW_IMAGE_DIGESTS[consumer_id],
            )
        self.rules = {
            consumer_id: _rule(consumer_id, state=RULE_STATES[consumer_id])
            for consumer_id in ("canary", "ingest", "morning_digest")
        }
        self.targets = {
            consumer_id: [
                _target(
                    consumer_id,
                    task_definition=OLD_TASKS[consumer_id],
                )
            ]
            for consumer_id in ("canary", "ingest", "morning_digest")
        }
        self.lambda_configurations = {
            consumer_id: _lambda_configuration(
                consumer_id,
                task_definition=OLD_TASKS[consumer_id],
            )
            for consumer_id in ("x_buzz_worker", "tiktok_acquire")
        }
        self.service_task_arns: dict[str, list[str]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        for consumer_id, service_arn in SERVICE_ARNS.items():
            task = _running_task(
                consumer_id,
                task_definition=OLD_TASKS[consumer_id],
                image_digest=OLD_IMAGE_DIGESTS[consumer_id],
            )
            task_arn = task["taskArn"]
            self.service_task_arns[service_arn] = [task_arn]
            self.tasks[task_arn] = task
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.wait_count = 0
        self.fail_wait_once = False

    @property
    def item(self) -> dict[str, Any] | None:
        return self.items.get(f"ecs-service-apply#{ATTEMPT}")

    @item.setter
    def item(self, value: dict[str, Any] | None) -> None:
        record_id = f"ecs-service-apply#{ATTEMPT}"
        if value is None:
            self.items.pop(record_id, None)
        else:
            self.items[record_id] = value

    def json(
        self,
        service: str,
        operation: str,
        arguments: Sequence[str] = (),
        *,
        timeout_seconds: float = 120,
    ) -> dict[str, Any]:
        del timeout_seconds
        self.calls.append((service, operation, tuple(arguments)))
        if (service, operation) == ("ecs", "list-services"):
            token = _argument(arguments, "--next-token") if "--next-token" in arguments else ""
            return copy.deepcopy(self.list_pages[token])
        if (service, operation) == ("ecs", "describe-services"):
            requested = _argument_values(arguments, "--services")
            return {
                "services": [
                    copy.deepcopy(self.services[service_arn])
                    for service_arn in requested
                    if service_arn in self.services
                ],
                "failures": [],
            }
        if (service, operation) == ("ecs", "update-service"):
            service_arn = _argument(arguments, "--service")
            current = self.services[service_arn]
            task_definition = _argument(arguments, "--task-definition")
            desired_count = int(_argument(arguments, "--desired-count"))
            current["taskDefinition"] = task_definition
            current["desiredCount"] = desired_count
            current["runningCount"] = desired_count
            current["pendingCount"] = 0
            current["deploymentConfiguration"] = json.loads(
                _argument(arguments, "--deployment-configuration")
            )
            current["networkConfiguration"] = json.loads(
                _argument(arguments, "--network-configuration")
            )
            current["deployments"] = [
                {
                    "status": "PRIMARY",
                    "taskDefinition": task_definition,
                    "desiredCount": desired_count,
                    "runningCount": desired_count,
                    "pendingCount": 0,
                    "rolloutState": "COMPLETED",
                }
            ]
            consumer_id = next(key for key, arn in SERVICE_ARNS.items() if arn == service_arn)
            running_task_arns = self.service_task_arns[service_arn]
            image_digest = self._task_image_digest(
                consumer_id,
                task_definition,
            )
            self.tasks = {
                **self.tasks,
                **{
                    task_arn: _running_task(
                        consumer_id,
                        task_definition=task_definition,
                        image_digest=image_digest,
                    )
                    for task_arn in running_task_arns
                },
            }
            for task_arn in running_task_arns:
                self.tasks[task_arn]["taskArn"] = task_arn
                self.tasks[task_arn]["containers"][0]["taskArn"] = task_arn
            return {"service": copy.deepcopy(current)}
        if (service, operation) == ("ecs", "list-tasks"):
            service_value = (
                _argument(arguments, "--service-name")
                if "--service-name" in arguments
                else _argument(arguments, "--service")
            )
            service_arn = next(
                (
                    arn
                    for consumer_id, arn in SERVICE_ARNS.items()
                    if service_value
                    in {
                        arn,
                        CONSUMERS[consumer_id]["activator"]["identity"],
                    }
                ),
                None,
            )
            assert service_arn is not None
            return {
                "taskArns": copy.deepcopy(self.service_task_arns[service_arn]),
            }
        if (service, operation) == ("ecs", "describe-tasks"):
            requested = _argument_values(arguments, "--tasks")
            return {
                "tasks": [
                    copy.deepcopy(self.tasks[task_arn])
                    for task_arn in requested
                    if task_arn in self.tasks
                ],
                "failures": [],
            }
        if (service, operation) == ("ecs", "describe-task-definition"):
            task_definition = _argument(arguments, "--task-definition")
            assert _argument(arguments, "--include") == "TAGS"
            return copy.deepcopy(self.task_definitions[task_definition])
        if (service, operation) == ("events", "describe-rule"):
            rule_name = _argument(arguments, "--name")
            assert _argument(arguments, "--event-bus-name") == "default"
            consumer_id = next(key for key, rule in self.rules.items() if rule["Name"] == rule_name)
            return copy.deepcopy(self.rules[consumer_id])
        if (service, operation) == ("events", "list-targets-by-rule"):
            request = json.loads(_argument(arguments, "--cli-input-json"))
            assert request["Limit"] == 100
            assert request["EventBusName"] == "default"
            assert set(request) <= {
                "EventBusName",
                "Limit",
                "NextToken",
                "Rule",
            }
            rule_name = request["Rule"]
            consumer_id = next(key for key, rule in self.rules.items() if rule["Name"] == rule_name)
            return {"Targets": copy.deepcopy(self.targets[consumer_id])}
        if (service, operation) == ("lambda", "get-function-configuration"):
            function_name = _argument(arguments, "--function-name")
            consumer_id = next(
                key
                for key, configuration in self.lambda_configurations.items()
                if configuration["FunctionName"] == function_name
            )
            return copy.deepcopy(self.lambda_configurations[consumer_id])
        if (service, operation) == ("dynamodb", "get-item"):
            record_id = json.loads(_argument(arguments, "--key"))["record_id"]["S"]
            item = self.items.get(record_id)
            return {"Item": copy.deepcopy(item)} if item is not None else {}
        raise AssertionError((service, operation, arguments))

    def _task_image_digest(
        self,
        consumer_id: str,
        task_definition: str,
    ) -> str:
        response = self.task_definitions[task_definition]
        containers = response["taskDefinition"]["containerDefinitions"]
        container_name = CONSUMERS[consumer_id]["container_name"]
        container = next(item for item in containers if item["name"] == container_name)
        return str(container["image"]).rsplit("@sha256:", maxsplit=1)[1]

    def run(
        self,
        service: str,
        operation: str,
        arguments: Sequence[str] = (),
        *,
        timeout_seconds: float = 120,
    ) -> None:
        self.calls.append((service, operation, tuple(arguments)))
        if (service, operation) == ("dynamodb", "transact-write-items"):
            transaction = json.loads(_argument(arguments, "--transact-items"))
            staged = copy.deepcopy(self.items)
            for request in transaction:
                if "Put" in request:
                    put = request["Put"]
                    item = put["Item"]
                    record_id = item["record_id"]["S"]
                    current = staged.get(record_id)
                    if current is not None and (
                        put["ConditionExpression"] == "attribute_not_exists(record_id)"
                        or current["stage"]["S"] not in {"APPLIED", "RESTORED"}
                    ):
                        raise SAGA.SagaError("ConditionalCheckFailedException")
                    staged[record_id] = copy.deepcopy(item)
                    continue
                update = request["Update"]
                record_id = update["Key"]["record_id"]["S"]
                current = staged.get(record_id)
                values = update["ExpressionAttributeValues"]
                expected_stage = values.get(":applying") or values[":active"]
                if current is None or current["stage"] != expected_stage:
                    raise SAGA.SagaError("ConditionalCheckFailedException")
                for item_name, value_name in (
                    ("plan_sha256", ":plan"),
                    ("apply_attempt_id", ":attempt"),
                    ("baseline_sha256", ":baseline"),
                    ("planned_sha256", ":planned"),
                ):
                    if current[item_name] != values[value_name]:
                        raise SAGA.SagaError("ConditionalCheckFailedException")
                if (
                    "attempt_record_id" in current
                    and current["attempt_record_id"] != values[":attempt_record"]
                ):
                    raise SAGA.SagaError("ConditionalCheckFailedException")
                current["stage"] = copy.deepcopy(values[":desired"])
            self.items = staged
            return
        if (service, operation) == ("ecs", "wait services-stable"):
            assert timeout_seconds == 900
            self.wait_count += 1
            if self.fail_wait_once:
                self.fail_wait_once = False
                raise SAGA.SagaError("waiter failed")
            return
        raise AssertionError((service, operation, arguments))


class _FinalizerLedger:
    """Finalizer adapter over the same fake DynamoDB records used by the ECS saga."""

    def __init__(self, cli: _FakeCli) -> None:
        self.items = cli.items

    def seed(self, item: dict[str, Any]) -> None:
        self.items[item["record_id"]["S"]] = copy.deepcopy(item)

    def get_item(
        self,
        *,
        table_name: str,
        key: dict[str, dict[str, str]],
    ) -> dict[str, Any] | None:
        assert table_name == FINALIZER._IMAGE_LEDGER_TABLE
        item = self.items.get(key["record_id"]["S"])
        return copy.deepcopy(item) if item is not None else None

    def transact_write(
        self,
        *,
        items: Sequence[dict[str, Any]],
        client_request_token: str,
    ) -> None:
        assert client_request_token == FINALIZER._client_request_token(ATTEMPT)
        staged = copy.deepcopy(self.items)
        for operation in items:
            if "Put" in operation:
                request = operation["Put"]
                record_id = request["Item"]["record_id"]["S"]
                assert record_id not in staged
                staged[record_id] = copy.deepcopy(request["Item"])
            elif "Delete" in operation:
                record_id = operation["Delete"]["Key"]["record_id"]["S"]
                assert record_id in staged
                del staged[record_id]
            else:
                request = operation["Update"]
                record_id = request["Key"]["record_id"]["S"]
                current = staged[record_id]
                values = request["ExpressionAttributeValues"]
                if current["record_type"]["S"] == FINALIZER._EVENTBRIDGE_ACTIVE_RECORD_TYPE:
                    current["stage"] = copy.deepcopy(values[":complete"])
                    current["finished_at"] = copy.deepcopy(values[":finished"])
                    current["revision"] = {"N": str(int(current["revision"]["N"]) + 1)}
                elif record_id.startswith("intent#"):
                    current["state"] = copy.deepcopy(values[":applied"])
                    current["outcome_recorded_at"] = copy.deepcopy(values[":recorded"])
                else:
                    current["stage"] = copy.deepcopy(values[":applied"])
        self.items.clear()
        self.items.update(staged)


def _finalizer_draft(intent_id: str) -> dict[str, Any]:
    return {
        "kind": "terraform-runtime-apply-receipt-draft",
        "schema_version": 7,
        "guard_version": "24",
        "account_id": "718959508629",
        "region": "ap-northeast-1",
        "git_commit": "d" * 40,
        "status": "verified_pending_finalization",
        "migration_kind": "runtime",
        "migration_id": "test-migration",
        "required_migration_id": "",
        "provenance_outcome": "pending",
        "image_deployment_intent_id": intent_id,
        "apply_attempt_id": ATTEMPT,
        "source_receipt_sha256": "1" * 64,
        "migration_contract_sha256": "2" * 64,
        "reviewed_plan_sha256": "3" * 64,
        "plan_sha256": PLAN_SHA256,
        "openclaw_rollout_result_sha256": "4" * 64,
        "post_apply_service_probe_sha256": "5" * 64,
        "post_state_contract_sha256": "6" * 64,
        "post_live_fingerprint_sha256": "7" * 64,
        "post_runtime_inventory_sha256": "8" * 64,
        "shared_deployment_lock_record_id": FINALIZER._LOCK_RECORD_ID,
        "shared_deployment_lock_receipt_sha256": "9" * 64,
    }


def _saga(
    cli: _FakeCli,
    *,
    plan: dict[str, Any] | None = None,
    attempt: str = ATTEMPT,
) -> Any:
    return SAGA.EcsServiceApplySaga(
        plan=SAGA._analyze_plan(plan or _plan()),
        plan_sha256=PLAN_SHA256,
        apply_attempt_id=attempt,
        cli=cli,
    )


def _set_live_task(
    cli: _FakeCli,
    *,
    service_arn: str,
    task_definition: str,
) -> None:
    service = cli.services[service_arn]
    service["taskDefinition"] = task_definition
    service["deployments"][0]["taskDefinition"] = task_definition
    consumer_id = next(
        key for key, candidate_arn in SERVICE_ARNS.items() if candidate_arn == service_arn
    )
    image_digest = cli._task_image_digest(consumer_id, task_definition)
    for task_arn in cli.service_task_arns[service_arn]:
        replacement = _running_task(
            consumer_id,
            task_definition=task_definition,
            image_digest=image_digest,
        )
        replacement["taskArn"] = task_arn
        replacement["containers"][0]["taskArn"] = task_arn
        cli.tasks[task_arn] = replacement


def _set_live_consumer_task(
    cli: _FakeCli,
    *,
    consumer_id: str,
    task_definition: str,
) -> None:
    activator_type = CONSUMERS[consumer_id]["activator"]["type"]
    if activator_type == "ecs_service":
        _set_live_task(
            cli,
            service_arn=SERVICE_ARNS[consumer_id],
            task_definition=task_definition,
        )
        return
    if activator_type == "eventbridge_rule_ecs_target":
        cli.targets[consumer_id][0]["EcsParameters"]["TaskDefinitionArn"] = task_definition
        return
    cli.lambda_configurations[consumer_id]["Environment"]["Variables"]["TASKDEF_ARN"] = (
        task_definition
    )


def _promote_default_plan(cli: _FakeCli) -> None:
    _set_live_consumer_task(
        cli,
        consumer_id="mcp",
        task_definition=NEW_MCP_TASK,
    )
    _set_live_consumer_task(
        cli,
        consumer_id="connect_web",
        task_definition=NEW_CONNECT_TASK,
    )


def _rewrite_durable_baseline(
    cli: _FakeCli,
    baseline: dict[str, Any],
) -> None:
    assert cli.item is not None
    baseline_json = SAGA._canonical_bytes(baseline).decode("utf-8")
    baseline_sha256 = SAGA._digest(baseline)
    cli.item["baseline_json"] = {"S": baseline_json}
    cli.item["baseline_sha256"] = {"S": baseline_sha256}
    cli.items[SAGA._ACTIVE_RECORD_ID]["baseline_sha256"] = {
        "S": baseline_sha256,
    }


@pytest.mark.parametrize(
    ("address", "resource_type", "name", "index"),
    [
        ("aws_ecs_service.shadow[0]", "aws_ecs_service", "shadow", 0),
        (
            "aws_ecs_task_definition.shadow[0]",
            "aws_ecs_task_definition",
            "shadow",
            0,
        ),
        (
            'module.hostile.aws_ecs_service.mcp["shadow"]',
            "aws_ecs_service",
            "mcp",
            "shadow",
        ),
    ],
)
def test_plan_rejects_every_mutating_ecs_address_outside_exact_scope(
    address: str,
    resource_type: str,
    name: str,
    index: int | str,
) -> None:
    plan = _plan()
    hostile = _resource(
        address=address,
        resource_type=resource_type,
        name=name,
        index=index if isinstance(index, int) else None,
        actions=["update"],
        after={"name": "hostile"},
    )
    if isinstance(index, str):
        hostile["index"] = index
    plan["resource_changes"].append(hostile)

    with pytest.raises(SAGA.SagaError, match="outside the saga scope"):
        SAGA._analyze_plan(plan)


def test_plan_rejects_address_alias_even_when_metadata_claims_allowed_identity() -> None:
    plan = _plan()
    service_change = next(
        change
        for change in plan["resource_changes"]
        if change["address"] == "aws_ecs_service.mcp[0]"
    )
    service_change["address"] = "aws_ecs_service.mcp"

    with pytest.raises(SAGA.SagaError, match="outside the saga scope"):
        SAGA._analyze_plan(plan)


def test_saga_scope_and_planned_binding_exactly_match_all_eight_registry_consumers() -> None:
    specs, registry_sha256 = SAGA._load_consumer_specs(copy.deepcopy(CONSUMER_REGISTRY))
    analysis = SAGA._analyze_plan(_plan())

    assert frozenset(specs) == EXPECTED_CONSUMER_IDS
    assert len(specs) == 8
    assert frozenset(analysis.binding["consumers"]) == EXPECTED_CONSUMER_IDS
    assert analysis.binding["consumerRegistrySha256"] == registry_sha256
    assert analysis.binding["consumers"]["mcp"]["activationChanged"] is True
    assert analysis.binding["consumers"]["openclaw"]["activationChanged"] is False
    assert {spec.activator_type for spec in specs.values()} == {
        "ecs_service",
        "eventbridge_rule_ecs_target",
        "lambda_taskdef_arn_environment",
    }


def test_saga_rejects_consumer_registry_activator_partition_drift() -> None:
    registry = copy.deepcopy(CONSUMER_REGISTRY)
    mcp = next(consumer for consumer in registry["consumers"] if consumer["consumer_id"] == "mcp")
    mcp["activator"]["type"] = "lambda_taskdef_arn_environment"

    with pytest.raises(
        SAGA.SagaError,
        match="consumer registry activator partition differs",
    ):
        SAGA._load_consumer_specs(registry)


def test_saga_fails_closed_when_registry_adds_a_valid_ninth_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = copy.deepcopy(CONSUMER_REGISTRY)
    registry["consumers"].append(
        {
            "consumer_id": "shadow",
            "terraform_task_definition_address": ("aws_ecs_task_definition.shadow[0]"),
            "ecs_family": "teamagent-dev-shadow",
            "container_name": "shadow",
            "activator": {
                "type": "ecs_service",
                "identity": "teamagent-dev-shadow",
            },
            "release_repository": "teamagent-shadow",
            "receipt": {"pipeline": "shadow", "subject": "shadow"},
            "provisional": False,
            "provisional_reason": None,
        }
    )
    monkeypatch.setattr(
        SAGA,
        "validate_consumer_registry",
        lambda value: copy.deepcopy(value),
    )

    with pytest.raises(
        SAGA.SagaError,
        match=r"registry|scope|exactly",
    ):
        SAGA._load_consumer_specs(registry)

    cli = _FakeCli()
    saga = _saga(cli)
    monkeypatch.setattr(
        SAGA,
        "load_consumer_registry",
        lambda: copy.deepcopy(registry),
    )
    with pytest.raises(SAGA.SagaError, match=r"registry|scope"):
        saga.begin()
    assert cli.calls == []


def test_begin_reads_manual_pagination_and_persists_exact_canonical_baseline() -> None:
    cli = _FakeCli()
    saga = _saga(cli)

    receipt = saga.begin()

    assert cli.item is not None
    assert receipt["kind"] == "teamagent-ecs-service-apply-saga-receipt"
    assert receipt["stage"] == "APPLYING"
    assert receipt["record_id"] == f"ecs-service-apply#{ATTEMPT}"
    assert receipt["plan_sha256"] == PLAN_SHA256
    assert receipt["apply_attempt_id"] == ATTEMPT
    assert re.fullmatch(r"[0-9a-f]{64}", receipt["receipt_sha256"])
    assert cli.item["record_id"] == {"S": f"ecs-service-apply#{ATTEMPT}"}
    assert cli.item["plan_sha256"] == {"S": PLAN_SHA256}
    assert cli.item["apply_attempt_id"] == {"S": ATTEMPT}
    assert cli.item["stage"] == {"S": "APPLYING"}
    baseline = json.loads(cli.item["baseline_json"]["S"])
    assert baseline["mcp"] == {
        "taskDefinition": OLD_MCP_TASK,
        "taskDefinitionSha256": SAGA._task_artifact_sha256(cli.task_definitions[OLD_MCP_TASK]),
        "image": (f"{ECR_REGISTRY}/teamagent-mcp@sha256:{OLD_IMAGE_DIGESTS['mcp']}"),
        "imageDigest": f"sha256:{OLD_IMAGE_DIGESTS['mcp']}",
        "activation": {
            "type": "ecs_service",
            "identity": "teamagent-dev-mcp",
            "status": "ACTIVE",
            "taskDefinition": OLD_MCP_TASK,
            "deploymentConfiguration": _deployment_configuration(),
            "networkConfiguration": _network(
                subnet="subnet-b",
                security_group="sg-mcp",
            ),
            "desiredCount": 1,
        },
    }
    list_calls = [
        arguments
        for service, operation, arguments in cli.calls
        if (service, operation) == ("ecs", "list-services")
    ]
    assert len(list_calls) == 2
    assert "--next-token" not in list_calls[0]
    assert list_calls[1][-2:] == ("--next-token", "page-2")


def test_begin_dispatches_each_activator_without_treating_nonservices_as_services() -> None:
    cli = _FakeCli()

    _saga(cli).begin()

    describe_service_calls = [
        arguments
        for service, operation, arguments in cli.calls
        if (service, operation) == ("ecs", "describe-services")
    ]
    assert len(describe_service_calls) == 1
    assert set(_argument_values(describe_service_calls[0], "--services")) == set(
        SERVICE_ARNS.values()
    )
    assert {
        _argument(arguments, "--name")
        for service, operation, arguments in cli.calls
        if (service, operation) == ("events", "describe-rule")
    } == {CONSUMERS[key]["activator"]["identity"] for key in ("canary", "ingest", "morning_digest")}
    assert {
        _argument(arguments, "--function-name")
        for service, operation, arguments in cli.calls
        if (service, operation)
        == (
            "lambda",
            "get-function-configuration",
        )
    } == {CONSUMERS[key]["activator"]["identity"] for key in ("x_buzz_worker", "tiktok_acquire")}
    task_service_values = {
        _argument(arguments, "--service-name")
        for service, operation, arguments in cli.calls
        if (service, operation) == ("ecs", "list-tasks")
    }
    assert task_service_values == {CONSUMERS[key]["activator"]["identity"] for key in SERVICE_ARNS}
    assert {
        _argument(arguments, "--task-definition")
        for service, operation, arguments in cli.calls
        if (service, operation) == ("ecs", "describe-task-definition")
    } == set(OLD_TASKS.values())


def test_lambda_steady_uses_taskdef_environment_not_ecs_or_idle_runtime_state() -> None:
    cli = _FakeCli()
    cli.lambda_configurations["x_buzz_worker"]["State"] = "Inactive"

    _saga(cli).begin()

    assert cli.item is not None
    baseline = json.loads(cli.item["baseline_json"]["S"])
    activation = baseline["x_buzz_worker"]["activation"]
    assert frozenset(activation) == {
        "type",
        "identity",
        "functionArn",
        "taskDefinition",
        "environmentVariables",
    }
    assert activation["taskDefinition"] == OLD_TASKS["x_buzz_worker"]
    assert all(
        CONSUMERS["x_buzz_worker"]["activator"]["identity"] not in arguments
        for service, operation, arguments in cli.calls
        if (service, operation)
        in {
            ("ecs", "describe-services"),
            ("ecs", "list-tasks"),
        }
    )


def test_begin_baseline_contains_task_digest_and_activation_for_all_consumers() -> None:
    cli = _FakeCli()

    _saga(cli).begin()

    assert cli.item is not None
    baseline = json.loads(cli.item["baseline_json"]["S"])
    assert frozenset(baseline) == EXPECTED_CONSUMER_IDS
    assert len(baseline) == 8
    for consumer_id, consumer in CONSUMERS.items():
        value = baseline[consumer_id]
        assert frozenset(value) == {
            "taskDefinition",
            "taskDefinitionSha256",
            "image",
            "imageDigest",
            "activation",
        }
        assert value["taskDefinition"] == OLD_TASKS[consumer_id]
        assert value["taskDefinitionSha256"] == SAGA._task_artifact_sha256(
            cli.task_definitions[OLD_TASKS[consumer_id]]
        )
        assert value["image"] == (
            f"{ECR_REGISTRY}/{consumer['release_repository']}"
            f"@sha256:{OLD_IMAGE_DIGESTS[consumer_id]}"
        )
        assert value["imageDigest"] == (f"sha256:{OLD_IMAGE_DIGESTS[consumer_id]}")
        activation = value["activation"]
        assert activation["type"] == consumer["activator"]["type"]
        assert activation["identity"] == consumer["activator"]["identity"]
        assert activation["taskDefinition"] == OLD_TASKS[consumer_id]
        expected_activation_fields = {
            "ecs_service": {
                "type",
                "identity",
                "status",
                "taskDefinition",
                "deploymentConfiguration",
                "networkConfiguration",
                "desiredCount",
            },
            "eventbridge_rule_ecs_target": {
                "type",
                "identity",
                "ruleArn",
                "state",
                "taskDefinition",
                "target",
            },
            "lambda_taskdef_arn_environment": {
                "type",
                "identity",
                "functionArn",
                "taskDefinition",
                "environmentVariables",
            },
        }[consumer["activator"]["type"]]
        assert frozenset(activation) == expected_activation_fields

    assert baseline["canary"]["activation"]["state"] == "DISABLED"
    assert baseline["ingest"]["activation"]["state"] == "DISABLED"
    assert baseline["morning_digest"]["activation"]["state"] == "ENABLED"
    assert baseline["canary"]["activation"]["target"] == cli.targets["canary"][0]
    for consumer_id in ("mcp", "connect_web", "openclaw"):
        assert baseline[consumer_id]["activation"]["status"] == "ACTIVE"
        assert baseline[consumer_id]["activation"]["desiredCount"] == 1
    assert (
        baseline["x_buzz_worker"]["activation"]["environmentVariables"]
        == cli.lambda_configurations["x_buzz_worker"]["Environment"]["Variables"]
    )


def test_disabled_eventbridge_rules_are_observed_and_never_excluded() -> None:
    cli = _FakeCli()

    _saga(cli).begin()

    assert cli.item is not None
    baseline = json.loads(cli.item["baseline_json"]["S"])
    for consumer_id in ("canary", "ingest"):
        identity = CONSUMERS[consumer_id]["activator"]["identity"]
        assert baseline[consumer_id]["activation"]["state"] == "DISABLED"
        assert any(
            (service, operation) == ("events", "describe-rule")
            and _argument(arguments, "--name") == identity
            for service, operation, arguments in cli.calls
        )
        assert any(
            (service, operation) == ("events", "list-targets-by-rule")
            and json.loads(_argument(arguments, "--cli-input-json"))["Rule"] == identity
            for service, operation, arguments in cli.calls
        )


def test_begin_rejects_replay_without_recapturing_or_overwriting_baseline() -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    assert cli.item is not None
    original_item = copy.deepcopy(cli.item)
    _set_live_task(
        cli,
        service_arn=MCP_SERVICE_ARN,
        task_definition=NEW_MCP_TASK,
    )

    with pytest.raises(SAGA.SagaError, match="already exists"):
        saga.begin()

    assert cli.item == original_item


@pytest.mark.parametrize("scenario", ["repeat-token", "duplicate-arn", "missing-service"])
def test_begin_rejects_truncated_or_ambiguous_service_inventory(
    scenario: str,
) -> None:
    cli = _FakeCli()
    if scenario == "repeat-token":
        cli.list_pages = {
            "": {"serviceArns": [MCP_SERVICE_ARN], "nextToken": "repeat"},
            "repeat": {
                "serviceArns": [CONNECT_SERVICE_ARN],
                "nextToken": "repeat",
            },
        }
    elif scenario == "duplicate-arn":
        cli.list_pages = {
            "": {"serviceArns": [MCP_SERVICE_ARN], "nextToken": "second"},
            "second": {
                "serviceArns": [MCP_SERVICE_ARN, CONNECT_SERVICE_ARN],
            },
        }
    else:
        cli.list_pages = {"": {"serviceArns": [MCP_SERVICE_ARN]}}

    with pytest.raises(SAGA.SagaError):
        _saga(cli).begin()

    assert cli.item is None


def test_begin_rejects_describe_response_identity_substitution() -> None:
    cli = _FakeCli()
    cli.services[MCP_SERVICE_ARN]["serviceName"] = "teamagent-dev-openclaw"

    with pytest.raises(SAGA.SagaError, match="identity"):
        _saga(cli).begin()

    assert cli.item is None


def test_applied_requires_both_exact_planned_task_definitions_and_stability() -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    _set_live_task(
        cli,
        service_arn=MCP_SERVICE_ARN,
        task_definition=NEW_MCP_TASK,
    )

    with pytest.raises(SAGA.SagaError, match="planned task definition"):
        saga.finish(outcome="applied")

    assert cli.item is not None
    assert cli.item["stage"] == {"S": "APPLYING"}

    _set_live_task(
        cli,
        service_arn=CONNECT_SERVICE_ARN,
        task_definition=NEW_CONNECT_TASK,
    )
    cli.services[CONNECT_SERVICE_ARN]["pendingCount"] = 1
    with pytest.raises(SAGA.SagaError, match="not exactly stable"):
        saga.finish(outcome="applied")
    cli.services[CONNECT_SERVICE_ARN]["pendingCount"] = 0

    receipt = saga.finish(outcome="applied")

    assert cli.item["stage"] == {"S": "APPLIED"}
    assert receipt["stage"] == "APPLIED"
    assert saga.finish(outcome="applied") == receipt


def test_verify_accepts_planned_task_changes_for_each_activator_type() -> None:
    cli = _FakeCli()
    plan = _plan(
        task_definitions={
            consumer_id: NEW_TASKS[consumer_id] for consumer_id in EXPECTED_CONSUMER_IDS
        }
    )
    saga = _saga(cli, plan=plan)
    saga.begin()
    for consumer_id in EXPECTED_CONSUMER_IDS:
        _set_live_consumer_task(
            cli,
            consumer_id=consumer_id,
            task_definition=NEW_TASKS[consumer_id],
        )

    receipt = saga.verify()

    assert receipt["stage"] == "VERIFIED_APPLIED"
    assert all(
        consumer["activationChanged"] is True
        for consumer in saga.plan.binding["consumers"].values()
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "eventbridge-task-definition",
        "eventbridge-rule-state",
        "eventbridge-two-targets",
        "lambda-task-definition",
        "lambda-missing-task-definition",
    ],
)
def test_verify_applies_only_the_steady_contract_for_each_activator_type(
    scenario: str,
) -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    _promote_default_plan(cli)
    if scenario == "eventbridge-task-definition":
        cli.task_definitions[NEW_TASKS["canary"]] = copy.deepcopy(
            cli.task_definitions[OLD_TASKS["canary"]]
        )
        cli.task_definitions[NEW_TASKS["canary"]]["taskDefinition"].update(
            {
                "taskDefinitionArn": NEW_TASKS["canary"],
                "revision": _TASK_REVISIONS["canary"][1],
            }
        )
        cli.targets["canary"][0]["EcsParameters"]["TaskDefinitionArn"] = NEW_TASKS["canary"]
    elif scenario == "eventbridge-rule-state":
        cli.rules["canary"]["State"] = "ENABLED"
    elif scenario == "eventbridge-two-targets":
        duplicate = copy.deepcopy(cli.targets["canary"][0])
        duplicate["Id"] = "second-canary-target"
        cli.targets["canary"].append(duplicate)
    elif scenario == "lambda-task-definition":
        cli.task_definitions[NEW_TASKS["x_buzz_worker"]] = copy.deepcopy(
            cli.task_definitions[OLD_TASKS["x_buzz_worker"]]
        )
        cli.task_definitions[NEW_TASKS["x_buzz_worker"]]["taskDefinition"].update(
            {
                "taskDefinitionArn": NEW_TASKS["x_buzz_worker"],
                "revision": _TASK_REVISIONS["x_buzz_worker"][1],
            }
        )
        cli.lambda_configurations["x_buzz_worker"]["Environment"]["Variables"]["TASKDEF_ARN"] = (
            NEW_TASKS["x_buzz_worker"]
        )
    else:
        cli.lambda_configurations["x_buzz_worker"]["Environment"]["Variables"].pop("TASKDEF_ARN")

    with pytest.raises(SAGA.SagaError):
        saga.verify()


def test_disabled_to_enabled_eventbridge_transition_is_a_verified_change() -> None:
    cli = _FakeCli()
    plan = _plan(
        rule_states={
            "canary": ("DISABLED", "ENABLED"),
        }
    )
    saga = _saga(cli, plan=plan)

    saga.begin()
    _promote_default_plan(cli)

    planned = saga.plan.binding["consumers"]["canary"]
    assert planned["activationChanged"] is True
    assert planned["activation"]["state"] == "ENABLED"
    assert cli.item is not None
    baseline = json.loads(cli.item["baseline_json"]["S"])
    assert baseline["canary"]["activation"]["state"] == "DISABLED"
    with pytest.raises(SAGA.SagaError, match="EventBridge"):
        saga.verify()

    cli.rules["canary"]["State"] = "ENABLED"
    assert saga.verify()["stage"] == "VERIFIED_APPLIED"


def test_verify_emits_finalizer_bound_receipt_and_active_pointer() -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    _set_live_task(cli, service_arn=MCP_SERVICE_ARN, task_definition=NEW_MCP_TASK)
    _set_live_task(
        cli,
        service_arn=CONNECT_SERVICE_ARN,
        task_definition=NEW_CONNECT_TASK,
    )

    receipt = saga.verify()

    active = cli.items[SAGA._ACTIVE_RECORD_ID]
    assert receipt["stage"] == "VERIFIED_APPLIED"
    assert receipt["ledger_item_sha256"] == SAGA._digest(cli.item)
    assert active == SAGA._ddb_item(
        {
            "record_id": SAGA._ACTIVE_RECORD_ID,
            "record_type": SAGA._ACTIVE_RECORD_TYPE,
            "schema_version": 1,
            "scope_id": SAGA._SCOPE_ID,
            "stage": "APPLYING",
            "apply_attempt_id": ATTEMPT,
            "attempt_record_id": f"ecs-service-apply#{ATTEMPT}",
            "plan_sha256": PLAN_SHA256,
            "baseline_sha256": cli.item["baseline_sha256"]["S"],
            "planned_sha256": cli.item["planned_sha256"]["S"],
        }
    )


def test_guard_verify_contract_produces_records_the_finalizer_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exercise the guard's verify action through its real ECS producer and consumer."""
    parsed = SAGA._parser().parse_args(
        [
            "verify",
            "--aws-bin",
            "/trusted/aws",
            "--terraform-bin",
            "/trusted/terraform",
            "--plan",
            "/trusted/saved.tfplan",
            "--plan-sha256",
            PLAN_SHA256,
            "--apply-attempt-id",
            ATTEMPT,
        ]
    )
    assert parsed.action == "verify"
    assert parsed.outcome is None

    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    _set_live_task(cli, service_arn=MCP_SERVICE_ARN, task_definition=NEW_MCP_TASK)
    _set_live_task(
        cli,
        service_arn=CONNECT_SERVICE_ARN,
        task_definition=NEW_CONNECT_TASK,
    )
    monkeypatch.setattr(SAGA, "_trusted_executable", lambda path, **_kwargs: path)
    monkeypatch.setattr(SAGA, "_load_saved_plan", lambda *_args, **_kwargs: _plan())
    assert (
        SAGA.main(
            [
                "verify",
                "--aws-bin",
                "/trusted/aws",
                "--terraform-bin",
                "/trusted/terraform",
                "--plan",
                "/trusted/saved.tfplan",
                "--plan-sha256",
                PLAN_SHA256,
                "--apply-attempt-id",
                ATTEMPT,
            ],
            cli=cli,
        )
        == 0
    )
    ecs_verification = json.loads(capsys.readouterr().out)

    ledger = _FinalizerLedger(cli)
    intent_id = "11111111-1111-4111-8111-111111111111"
    verified_at = 1_784_500_000
    eventbridge_id = f"{FINALIZER._EVENTBRIDGE_RECORD_PREFIX}hmac-2026-07"
    eventbridge = FINALIZER._ddb_item(
        {
            "record_id": eventbridge_id,
            "record_type": FINALIZER._EVENTBRIDGE_ACTIVE_RECORD_TYPE,
            "schema_version": 2,
            "stage": "applying",
            "revision": 1,
            "rotation_epoch": "hmac-2026-07",
            "plan_sha256": PLAN_SHA256,
            "apply_attempt_id": ATTEMPT,
            "baseline_json": '{"schema_version":2,"rules":{}}',
            "baseline_sha256": "b" * 64,
            "planned_json": '{"schema_version":2,"rules":{}}',
            "planned_sha256": "c" * 64,
            "started_at": verified_at - 1,
        }
    )
    eventbridge_verification = {
        "kind": "teamagent-eventbridge-apply-saga-receipt",
        "schema_version": 2,
        "record_id": eventbridge_id,
        "rotation_epoch": "hmac-2026-07",
        "stage": "verified_applied",
        "plan_sha256": PLAN_SHA256,
        "apply_attempt_id": ATTEMPT,
        "baseline_sha256": "b" * 64,
        "planned_sha256": "c" * 64,
        "ledger_item_sha256": FINALIZER._digest(eventbridge),
        "verified_at": verified_at,
    }
    eventbridge_verification["receipt_sha256"] = FINALIZER._digest(eventbridge_verification)
    ledger.seed(eventbridge)
    ledger.seed(
        FINALIZER._ddb_item(
            {
                "record_id": f"intent#{intent_id}",
                "record_type": "teamagent.image-deployment-intent",
                "schema_version": 1,
                "intent_id": intent_id,
                "state": "CONSUMED",
                "plan_sha256": PLAN_SHA256,
                "apply_attempt_id": ATTEMPT,
                "audit_expires_at": verified_at + 7_776_000,
            }
        )
    )
    ledger.seed(
        FINALIZER._ddb_item(
            {
                "record_id": FINALIZER._LOCK_RECORD_ID,
                "record_type": "teamagent.image-release-apply-lock",
                "schema_version": 1,
                "state": "LOCKED",
                "intent_id": intent_id,
                "plan_sha256": PLAN_SHA256,
                "apply_attempt_id": ATTEMPT,
                "lease_expires_at": verified_at + 300,
            }
        )
    )

    output = tmp_path / "apply.json"
    result = FINALIZER.ApplyFinalizer(
        client=ledger,
        intent_id=intent_id,
        plan_sha256=PLAN_SHA256,
        apply_attempt_id=ATTEMPT,
    ).commit(
        draft_raw=_finalizer_draft(intent_id),
        eventbridge_raw=eventbridge_verification,
        ecs_raw=ecs_verification,
        output=output,
    )

    assert result["state"] == "COMMITTED"
    assert cli.items[f"ecs-service-apply#{ATTEMPT}"]["stage"] == {"S": "APPLIED"}
    assert cli.items[SAGA._ACTIVE_RECORD_ID]["stage"] == {"S": "APPLIED"}
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "applied"


def test_begin_requires_exact_completed_single_primary_deployment() -> None:
    cli = _FakeCli()
    cli.services[MCP_SERVICE_ARN]["deployments"][0]["rolloutState"] = "IN_PROGRESS"

    with pytest.raises(SAGA.SagaError, match="not exactly stable"):
        _saga(cli).begin()

    assert cli.item is None


@pytest.mark.parametrize(
    "scenario",
    [
        "running-count",
        "pending-count",
        "two-primary",
        "rollout-incomplete",
    ],
)
def test_service_steady_non_regression_rejects_each_independent_condition(
    scenario: str,
) -> None:
    cli = _FakeCli()
    service = cli.services[MCP_SERVICE_ARN]
    if scenario == "running-count":
        service["runningCount"] = 0
    elif scenario == "pending-count":
        service["pendingCount"] = 1
    elif scenario == "two-primary":
        service["deployments"].append(copy.deepcopy(service["deployments"][0]))
    else:
        service["deployments"][0]["rolloutState"] = "IN_PROGRESS"

    with pytest.raises(SAGA.SagaError, match="not exactly stable"):
        _saga(cli).begin()

    assert cli.item is None


@pytest.mark.parametrize(
    "scenario",
    ["task-definition", "image-digest"],
)
def test_service_steady_rejects_running_task_pointer_or_digest_drift(
    scenario: str,
) -> None:
    cli = _FakeCli()
    task_arn = cli.service_task_arns[MCP_SERVICE_ARN][0]
    task = cli.tasks[task_arn]
    if scenario == "task-definition":
        task["taskDefinitionArn"] = NEW_MCP_TASK
    else:
        task["containers"][0]["imageDigest"] = f"sha256:{'f' * 64}"

    with pytest.raises(SAGA.SagaError, match=r"running task|differs"):
        _saga(cli).begin()

    assert cli.item is None


def test_applied_resolves_unknown_planned_arns_by_exact_task_artifact() -> None:
    cli = _FakeCli()
    plan = _plan(mcp_task=None, connect_task=None)
    saga = _saga(cli, plan=plan)
    saga.begin()
    for key, service_arn, task_definition in (
        ("mcp", MCP_SERVICE_ARN, NEW_MCP_TASK),
        ("connect_web", CONNECT_SERVICE_ARN, NEW_CONNECT_TASK),
    ):
        _set_live_task(
            cli,
            service_arn=service_arn,
            task_definition=task_definition,
        )
        task_address = SAGA._SERVICE_SPECS[key].task_address
        task_change = next(
            change for change in plan["resource_changes"] if change["address"] == task_address
        )
        payload = SAGA.task_from_change(task_change["change"]["after"], task=key)
        tags = payload.pop("tags")
        cli.task_definitions[task_definition] = {
            "taskDefinition": {
                **copy.deepcopy(payload),
                "compatibilities": ["EC2", "FARGATE"],
                "registeredAt": "2026-07-28T00:00:00+00:00",
                "registeredBy": (
                    "arn:aws:sts::718959508629:assumed-role/teamagent-dev-runtime-automation/test"
                ),
                "requiresAttributes": [],
                "placementConstraints": [],
                "taskDefinitionArn": task_definition,
                "revision": int(task_definition.rsplit(":", maxsplit=1)[1]),
                "status": "ACTIVE",
            },
            "tags": tags,
        }

    saga.finish(outcome="applied")

    assert cli.item is not None
    assert cli.item["stage"] == {"S": "APPLIED"}


def test_failed_restores_every_bound_field_waits_and_verifies_exactly() -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    for service_arn, task_definition in (
        (MCP_SERVICE_ARN, NEW_MCP_TASK),
        (CONNECT_SERVICE_ARN, NEW_CONNECT_TASK),
        (OPENCLAW_SERVICE_ARN, NEW_TASKS["openclaw"]),
    ):
        service = cli.services[service_arn]
        service["taskDefinition"] = task_definition
        service["desiredCount"] = 3
        service["runningCount"] = 2
        service["pendingCount"] = 1
        service["deploymentConfiguration"] = _deployment_configuration(
            maximum=50,
            minimum=0,
            enable=False,
            rollback=False,
        )
        service["networkConfiguration"] = _network(
            subnet="subnet-hostile",
            security_group="sg-hostile",
            public_ip="DISABLED",
        )
        service["deployments"] = [
            {
                "status": "PRIMARY",
                "taskDefinition": task_definition,
                "desiredCount": 3,
                "runningCount": 2,
                "pendingCount": 1,
                "rolloutState": "IN_PROGRESS",
            }
        ]
    saga.finish(outcome="failed")

    assert cli.wait_count == 1
    assert cli.item is not None
    assert cli.item["stage"] == {"S": "RESTORED"}
    assert cli.services[MCP_SERVICE_ARN]["taskDefinition"] == OLD_MCP_TASK
    assert cli.services[MCP_SERVICE_ARN]["deploymentConfiguration"] == (_deployment_configuration())
    assert cli.services[MCP_SERVICE_ARN]["networkConfiguration"] == _network(
        subnet="subnet-b",
        security_group="sg-mcp",
    )
    assert cli.services[MCP_SERVICE_ARN]["desiredCount"] == 1
    update_calls = [
        arguments
        for service, operation, arguments in cli.calls
        if (service, operation) == ("ecs", "update-service")
    ]
    assert len(update_calls) == 3
    assert all("--task-definition" in arguments for arguments in update_calls)
    assert all("--deployment-configuration" in arguments for arguments in update_calls)
    assert all("--network-configuration" in arguments for arguments in update_calls)
    assert all("--desired-count" in arguments for arguments in update_calls)


def test_failed_nonservice_drift_remains_applying_for_controller_reconciliation() -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    _set_live_consumer_task(
        cli,
        consumer_id="canary",
        task_definition=NEW_TASKS["canary"],
    )

    with pytest.raises(SAGA.SagaError, match=r"baseline|rollback|verified"):
        saga.finish(outcome="failed")

    assert cli.item is not None
    assert cli.item["stage"] == {"S": "APPLYING"}
    assert all(
        (service, operation)
        not in {
            ("events", "put-targets"),
            ("events", "enable-rule"),
            ("events", "disable-rule"),
            ("lambda", "update-function-configuration"),
        }
        for service, operation, _arguments in cli.calls
    )


def test_failed_partial_restore_remains_reconcilable_until_wait_and_verify() -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    _set_live_task(
        cli,
        service_arn=MCP_SERVICE_ARN,
        task_definition=NEW_MCP_TASK,
    )
    _set_live_task(
        cli,
        service_arn=CONNECT_SERVICE_ARN,
        task_definition=NEW_CONNECT_TASK,
    )
    cli.fail_wait_once = True

    with pytest.raises(SAGA.SagaError, match="waiter"):
        saga.finish(outcome="failed")

    assert cli.item is not None
    assert cli.item["stage"] == {"S": "APPLYING"}

    saga.finish(outcome="failed")

    assert cli.wait_count == 2
    assert cli.item["stage"] == {"S": "RESTORED"}


@pytest.mark.parametrize("field", ["baseline_sha256", "planned_sha256", "plan_sha256"])
def test_finish_rejects_tampered_durable_binding_before_any_ecs_write(
    field: str,
) -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    assert cli.item is not None
    cli.item[field] = {"S": "f" * 64}
    calls_before = len(cli.calls)

    with pytest.raises(SAGA.SagaError, match=r"differs|digest"):
        saga.finish(outcome="failed")

    new_calls = cli.calls[calls_before:]
    assert all(
        (service, operation) != ("ecs", "update-service")
        for service, operation, _arguments in new_calls
    )


@pytest.mark.parametrize(
    ("consumer_id", "field"),
    [
        ("tiktok_acquire", None),
        ("canary", "taskDefinition"),
        ("mcp", "taskDefinitionSha256"),
        ("openclaw", "image"),
        ("ingest", "imageDigest"),
        ("morning_digest", "activation"),
        ("connect_web", "activation.type"),
        ("connect_web", "activation.identity"),
        ("connect_web", "activation.status"),
        ("connect_web", "activation.taskDefinition"),
        ("connect_web", "activation.deploymentConfiguration"),
        ("connect_web", "activation.networkConfiguration"),
        ("connect_web", "activation.desiredCount"),
        ("canary", "activation.type"),
        ("canary", "activation.identity"),
        ("canary", "activation.ruleArn"),
        ("canary", "activation.state"),
        ("canary", "activation.taskDefinition"),
        ("canary", "activation.target"),
        ("x_buzz_worker", "activation.type"),
        ("x_buzz_worker", "activation.identity"),
        ("x_buzz_worker", "activation.functionArn"),
        ("x_buzz_worker", "activation.taskDefinition"),
        ("x_buzz_worker", "activation.environmentVariables"),
    ],
)
def test_finish_rejects_any_missing_consumer_baseline_field_before_aws_writes(
    consumer_id: str,
    field: str | None,
) -> None:
    cli = _FakeCli()
    saga = _saga(cli)
    saga.begin()
    assert cli.item is not None
    baseline = json.loads(cli.item["baseline_json"]["S"])
    if field is None:
        baseline.pop(consumer_id)
    elif field.startswith("activation."):
        baseline[consumer_id]["activation"].pop(field.split(".", maxsplit=1)[1])
    else:
        baseline[consumer_id].pop(field)
    _rewrite_durable_baseline(cli, baseline)
    calls_before = len(cli.calls)

    with pytest.raises(SAGA.SagaError, match=r"baseline|rollback|activation"):
        saga.finish(outcome="failed")

    mutating_operations = {
        ("ecs", "update-service"),
        ("events", "put-targets"),
        ("events", "enable-rule"),
        ("events", "disable-rule"),
        ("lambda", "update-function-configuration"),
    }
    assert all(
        (service, operation) not in mutating_operations
        for service, operation, _arguments in cli.calls[calls_before:]
    )


def test_private_saved_plan_is_held_and_remeasured_by_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "saved.tfplan"
    payload = b"opaque exact plan"
    path.write_bytes(payload)
    path.chmod(0o600)
    observed_descriptors: list[int] = []

    def show(descriptor: int, *, terraform_bin: Path) -> dict[str, Any]:
        assert terraform_bin == Path("/trusted/terraform")
        observed_descriptors.append(descriptor)
        assert os.read(descriptor, len(payload)) == payload
        os.lseek(descriptor, 0, os.SEEK_SET)
        return _plan()

    monkeypatch.setattr(SAGA, "_terraform_show_descriptor", show)

    assert (
        SAGA._load_saved_plan(
            path,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            terraform_bin=Path("/trusted/terraform"),
        )
        == _plan()
    )
    assert len(observed_descriptors) == 1

    path.chmod(0o644)
    with pytest.raises(SAGA.SagaError, match="private"):
        SAGA._load_saved_plan(
            path,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            terraform_bin=Path("/trusted/terraform"),
        )


def test_aws_cli_pins_endpoint_scrubs_ambient_authority_and_never_uses_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AWS_ENDPOINT_URL_ECS", "http://attacker.invalid")
    monkeypatch.setenv("AWS_ENDPOINT_URL_EVENTS", "http://attacker.invalid")
    monkeypatch.setenv("AWS_ENDPOINT_URL_LAMBDA", "http://attacker.invalid")
    monkeypatch.setenv("AWS_PROFILE", "attacker")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/tmp/attacker.pem")
    observed: dict[str, Any] = {}

    def run(
        command: Sequence[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        observed["command"] = list(command)
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(SAGA.subprocess, "run", run)
    aws_bin = tmp_path / "aws"
    aws_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    aws_bin.chmod(0o500)

    aws_cli = SAGA._SubprocessAwsCli(aws_bin)
    assert aws_cli.json("ecs", "list-services") == {}

    command = observed["command"]
    kwargs = observed["kwargs"]
    assert command[:7] == [
        str(aws_bin.resolve()),
        "--region",
        "ap-northeast-1",
        "--endpoint-url",
        "https://ecs.ap-northeast-1.amazonaws.com",
        "--no-cli-pager",
        "--no-paginate",
    ]
    assert command[7:9] == ["ecs", "list-services"]
    assert "shell" not in kwargs
    environment = kwargs["env"]
    assert environment["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] == "true"
    assert environment["AWS_CONFIG_FILE"] == "/dev/null"
    assert environment["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    assert "AWS_ENDPOINT_URL_ECS" not in environment
    assert "AWS_ENDPOINT_URL_EVENTS" not in environment
    assert "AWS_ENDPOINT_URL_LAMBDA" not in environment
    assert "AWS_PROFILE" not in environment
    assert "HTTPS_PROXY" not in environment
    assert "REQUESTS_CA_BUNDLE" not in environment
    for service in ("events", "lambda"):
        service_command = aws_cli._command(service, "test-operation", ())
        assert service_command[4] == (f"https://{service}.ap-northeast-1.amazonaws.com")
        assert service_command[7:9] == [service, "test-operation"]


def test_saved_plan_file_mode_check_uses_owner_read_bit() -> None:
    assert stat.S_IRUSR == 0o400
