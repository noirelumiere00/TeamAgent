from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from email.utils import formatdate
from pathlib import Path
from typing import Any

import pytest

import scripts.hmac_rollout_gate as rollout_gate_module
import scripts.terraform_hmac_gate as terraform_gate_module
import scripts.terraform_hmac_promotion_gate as promotion_gate_module
from scripts.hmac_rollout_gate import (
    DeploymentIntent,
    LiveRolloutGate,
    RolloutGateError,
    load_control,
)

_TEST_INTENT = DeploymentIntent(
    plan_sha256="e" * 64,
    apply_attempt_id="12345678-1234-4123-8123-123456789abc",
)

_NOW = 2_000_000_000
_DB_ARN = "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:teamagent/dev/database-url"
_MAIL_ARN = (
    "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:teamagent/dev/hmac/mail-action"
)
_REPORT_ARN = (
    "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:teamagent/dev/hmac/report-link"
)
_SLACK_ARN = (
    "arn:aws:secretsmanager:ap-northeast-1:123456789012:secret:teamagent/dev/slack/bot-token"
)
_DB_VERSION = "d" * 32
_MAIL_VERSION = "m" * 32
_REPORT_VERSION = "r" * 32
_SLACK_VERSION = "s" * 32
_DB_GENERATION = f"{_DB_ARN}@{_DB_VERSION}"
_MAIL_GENERATION = f"{_MAIL_ARN}@{_MAIL_VERSION}"
_REPORT_GENERATION = f"{_REPORT_ARN}@{_REPORT_VERSION}"
_SLACK_GENERATION = f"{_SLACK_ARN}@{_SLACK_VERSION}"
_EPOCH = "hmac-2026-07-18"
_TABLE = "teamagent-dev-hmac-state"
_SCOPE = "teamagent/dev"
_CLUSTER = "arn:aws:ecs:ap-northeast-1:123456789012:cluster/teamagent-dev"
_PROVENANCE = {
    "mcp": "",
    "mcp_rollback": "",
    "connect_web": "",
    "connect_web_rollback": "",
    "morning_digest": "",
    "morning_digest_rollback": "",
    "worker": "",
    "worker_rollback": "",
}
_TASK_ARNS = {
    "mcp_old": "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-mcp:55",
    "mcp_new": "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-mcp:56",
    "mcp_rollback": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-mcp:57"
    ),
    "mcp_cleanup": ("arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-mcp:58"),
    "connect_old": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-connect-web:53"
    ),
    "connect_new": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-connect-web:54"
    ),
    "connect_rollback": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-connect-web:55"
    ),
    "connect_cleanup": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-connect-web:56"
    ),
    "morning_old": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-morning-digest:44"
    ),
    "morning_new": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-morning-digest:45"
    ),
    "morning_rollback": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-morning-digest:46"
    ),
    "morning_cleanup": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-morning-digest:47"
    ),
    "canary": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-connect-canary:14"
    ),
}
_IMAGE = "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent@sha256:" + "1" * 64
_ROLLBACK_IMAGE = "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent@sha256:" + "9" * 64
_EVENT_ROLE = "arn:aws:iam::123456789012:role/teamagent-dev-events-morning-digest"


def _provenance(**values: str) -> str:
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


_PROVENANCE.update(
    {
        "mcp": _provenance(
            workload="mcp",
            image=_IMAGE,
            rotation_epoch=_EPOCH,
            mail_primary=_MAIL_GENERATION,
            mail_previous=_DB_GENERATION,
            mail_t0=str(_NOW),
            report_primary=_REPORT_GENERATION,
            report_previous=_DB_GENERATION,
            report_t0=str(_NOW),
            legacy_worker=_SLACK_GENERATION,
        ),
        "connect_web": _provenance(
            workload="connect_web",
            image=_IMAGE,
            rotation_epoch=_EPOCH,
            report_primary=_REPORT_GENERATION,
            report_previous=_DB_GENERATION,
            report_t0=str(_NOW),
        ),
        "morning_digest": _provenance(
            workload="morning_digest",
            image=_IMAGE,
            rotation_epoch=_EPOCH,
            mail_primary=_MAIL_GENERATION,
            mail_previous=_DB_GENERATION,
            mail_t0=str(_NOW),
            legacy_worker=_SLACK_GENERATION,
        ),
        "mcp_rollback": _provenance(
            workload="mcp",
            image=_ROLLBACK_IMAGE,
            rotation_epoch=_EPOCH,
            mail_primary=_MAIL_GENERATION,
            mail_previous=_DB_GENERATION,
            mail_t0=str(_NOW),
            report_primary=_REPORT_GENERATION,
            report_previous=_DB_GENERATION,
            report_t0=str(_NOW),
            legacy_worker=_SLACK_GENERATION,
        ),
        "connect_web_rollback": _provenance(
            workload="connect_web",
            image=_ROLLBACK_IMAGE,
            rotation_epoch=_EPOCH,
            report_primary=_REPORT_GENERATION,
            report_previous=_DB_GENERATION,
            report_t0=str(_NOW),
        ),
        "morning_digest_rollback": _provenance(
            workload="morning_digest",
            image=_ROLLBACK_IMAGE,
            rotation_epoch=_EPOCH,
            mail_primary=_MAIL_GENERATION,
            mail_previous=_DB_GENERATION,
            mail_t0=str(_NOW),
            legacy_worker=_SLACK_GENERATION,
        ),
    }
)


def _response(**values: object) -> dict[str, object]:
    return {
        **values,
        "ResponseMetadata": {"HTTPHeaders": {"date": formatdate(_NOW, usegmt=True)}},
    }


def _old_definition(task: str) -> dict[str, object]:
    if task == "morning_digest":
        secrets = [
            {
                "name": "MAIL_ACTION_HMAC_SECRET",
                "valueFrom": f"{_DB_ARN}:::{_DB_VERSION}",
            }
        ]
    else:
        secrets = [
            {
                "name": "MAIL_ACTION_HMAC_SECRET",
                "valueFrom": f"{_DB_ARN}:::{_DB_VERSION}",
            }
        ]
    task_definition = _TASK_ARNS[
        {"mcp": "mcp_old", "connect_web": "connect_old", "morning_digest": "morning_old"}[task]
    ]
    return {
        "taskDefinitionArn": task_definition,
        "family": task_definition.rsplit("/", maxsplit=1)[-1].rsplit(":", maxsplit=1)[0],
        "taskRoleArn": f"arn:aws:iam::123456789012:role/{task}-task",
        "executionRoleArn": f"arn:aws:iam::123456789012:role/{task}-execution",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "1024",
        "memory": "2048",
        "runtimePlatform": {
            "cpuArchitecture": "ARM64",
            "operatingSystemFamily": "LINUX",
        },
        "volumes": [],
        "containerDefinitions": [
            {
                "name": task,
                "image": _IMAGE,
                "environment": [],
                "secrets": secrets,
            }
        ],
    }


def _domain_entries(domain: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if domain == "mail_action":
        prefix = "MAIL_ACTION"
        primary_arn = _MAIL_ARN
        primary_version = _MAIL_VERSION
        primary_generation = _MAIL_GENERATION
        ttl = "86400"
    else:
        prefix = "REPORT_LINK"
        primary_arn = _REPORT_ARN
        primary_version = _REPORT_VERSION
        primary_generation = _REPORT_GENERATION
        ttl = "604800"
    environment = [
        {"name": f"{prefix}_HMAC_PRIMARY_GENERATION", "value": primary_generation},
        {"name": f"{prefix}_HMAC_PREVIOUS_GENERATION", "value": _DB_GENERATION},
        {"name": f"{prefix}_HMAC_PREVIOUS_ROTATION_STARTED_AT", "value": str(_NOW)},
        {"name": f"{prefix}_HMAC_PREVIOUS_IS_LEGACY", "value": "1"},
        {"name": f"{prefix}_TTL_S", "value": ttl},
    ]
    secrets = [
        {
            "name": f"{prefix}_HMAC_SECRET",
            "valueFrom": f"{primary_arn}:::{primary_version}",
        },
        {
            "name": f"{prefix}_HMAC_PREVIOUS_SECRET",
            "valueFrom": f"{_DB_ARN}:::{_DB_VERSION}",
        },
    ]
    if domain == "mail_action":
        environment.append(
            {
                "name": "MAIL_ACTION_HMAC_LEGACY_WORKER_GENERATION",
                "value": _SLACK_GENERATION,
            }
        )
        secrets.append(
            {
                "name": "MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET",
                "valueFrom": f"{_SLACK_ARN}:::{_SLACK_VERSION}",
            }
        )
    return environment, secrets


def _new_definition(task: str) -> dict[str, object]:
    environment = [
        {"name": "TEAMAGENT_HMAC_STATE_REQUIRED", "value": "1"},
        {"name": "TEAMAGENT_HMAC_STATE_TABLE", "value": _TABLE},
        {"name": "TEAMAGENT_HMAC_STATE_SCOPE", "value": _SCOPE},
        {"name": "TEAMAGENT_HMAC_ROTATION_EPOCH", "value": _EPOCH},
        {"name": "TEAMAGENT_HMAC_PROVENANCE", "value": _PROVENANCE[task]},
    ]
    secrets: list[dict[str, str]] = []
    domains = {
        "mcp": ("mail_action", "report_link"),
        "connect_web": ("report_link",),
        "morning_digest": ("mail_action",),
    }[task]
    for domain in domains:
        domain_environment, domain_secrets = _domain_entries(domain)
        environment.extend(domain_environment)
        secrets.extend(domain_secrets)
    arn = _TASK_ARNS[
        {"mcp": "mcp_new", "connect_web": "connect_new", "morning_digest": "morning_new"}[task]
    ]
    return {
        "taskDefinitionArn": arn,
        "family": arn.rsplit("/", maxsplit=1)[-1].rsplit(":", maxsplit=1)[0],
        "taskRoleArn": f"arn:aws:iam::123456789012:role/{task}-task",
        "executionRoleArn": f"arn:aws:iam::123456789012:role/{task}-execution",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "1024",
        "memory": "2048",
        "runtimePlatform": {
            "cpuArchitecture": "ARM64",
            "operatingSystemFamily": "LINUX",
        },
        "volumes": [],
        "containerDefinitions": [
            {
                "name": task,
                "image": _IMAGE,
                "environment": environment,
                "secrets": secrets,
            }
        ],
    }


def _rollback_definition(task: str) -> dict[str, object]:
    definition = copy.deepcopy(_new_definition(task))
    definition["taskDefinitionArn"] = _TASK_ARNS[
        {
            "mcp": "mcp_rollback",
            "connect_web": "connect_rollback",
            "morning_digest": "morning_rollback",
        }[task]
    ]
    container = definition["containerDefinitions"][0]  # type: ignore[index]
    container["image"] = _ROLLBACK_IMAGE
    for entry in container["environment"]:
        if entry["name"] == "TEAMAGENT_HMAC_PROVENANCE":
            entry["value"] = _PROVENANCE[f"{task}_rollback"]
    return definition


def _config(domain: str, *, deployed: bool) -> dict[str, object]:
    if deployed:
        return {
            "primary_generation": _DB_GENERATION,
            "previous_generation": None,
            "rotation_started_at": None,
        }
    return {
        "primary_generation": _MAIL_GENERATION if domain == "mail_action" else _REPORT_GENERATION,
        "previous_generation": _DB_GENERATION,
        "rotation_started_at": _NOW,
    }


def _manifest() -> dict[str, object]:
    proposed = {
        domain: _config(domain, deployed=False) for domain in ("mail_action", "report_link")
    }
    return {
        "now": _NOW,
        "legacy_database_generation": _DB_GENERATION,
        "legacy_worker_generation": _SLACK_GENERATION,
        "domains": {
            domain: {
                "deployed": _config(domain, deployed=True),
                "proposed": proposed[domain],
            }
            for domain in ("mail_action", "report_link")
        },
        "tasks": {
            "mcp": {
                "mail_action": proposed["mail_action"],
                "report_link": proposed["report_link"],
            },
            "morning_digest": {"mail_action": proposed["mail_action"]},
            "connect_web": {"report_link": proposed["report_link"]},
            "worker": {
                "mail_action": proposed["mail_action"],
                "report_link": proposed["report_link"],
            },
        },
    }


def _morning_target(task_definition: str) -> dict[str, object]:
    return {
        "Id": "morning",
        "Arn": _CLUSTER,
        "RoleArn": _EVENT_ROLE,
        "Input": "{}",
        "EcsParameters": {
            "TaskDefinitionArn": task_definition,
            "TaskCount": 1,
            "LaunchType": "FARGATE",
            "PlatformVersion": "LATEST",
            "NetworkConfiguration": {
                "awsvpcConfiguration": {
                    "Subnets": ["subnet-a", "subnet-b"],
                    "SecurityGroups": ["sg-morning"],
                    "AssignPublicIp": "ENABLED",
                }
            },
        },
        "RetryPolicy": {
            "MaximumEventAgeInSeconds": 3600,
            "MaximumRetryAttempts": 1,
        },
    }


def _target_digest(target: object) -> str:
    return hashlib.sha256(
        json.dumps(target, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def test_event_target_digest_canonicalizes_unordered_network_sets() -> None:
    target = _morning_target(_TASK_ARNS["morning_new"])
    reordered = copy.deepcopy(target)
    awsvpc = reordered["EcsParameters"]["NetworkConfiguration"][  # type: ignore[index]
        "awsvpcConfiguration"
    ]
    awsvpc["Subnets"] = list(reversed(awsvpc["Subnets"]))  # type: ignore[index]

    assert rollout_gate_module._canonical_target_digest(
        target
    ) == rollout_gate_module._canonical_target_digest(reordered)


def _control(rollback_hash: str, artifact_hash: str = "2" * 64) -> dict[str, object]:
    worker_provenance = _provenance(
        workload="worker",
        artifact=artifact_hash,
        rotation_epoch=_EPOCH,
        mail_primary=_MAIL_GENERATION,
        mail_previous=_DB_GENERATION,
        mail_t0=str(_NOW),
        report_primary=_REPORT_GENERATION,
        report_previous=_DB_GENERATION,
        report_t0=str(_NOW),
        legacy_worker=_SLACK_GENERATION,
    )
    worker_rollback_provenance = _provenance(
        workload="worker",
        artifact=rollback_hash,
        rotation_epoch=_EPOCH,
        mail_primary=_MAIL_GENERATION,
        mail_previous=_DB_GENERATION,
        mail_t0=str(_NOW),
        report_primary=_REPORT_GENERATION,
        report_previous=_DB_GENERATION,
        report_t0=str(_NOW),
        legacy_worker=_SLACK_GENERATION,
    )
    return {
        "schema": 1,
        "region": "ap-northeast-1",
        "scope": _SCOPE,
        "state_table": _TABLE,
        "rotation_epoch": _EPOCH,
        "services": {
            "mcp": {
                "cluster": "teamagent-dev",
                "service": "teamagent-dev-mcp",
                "legacy_task_definition": _TASK_ARNS["mcp_old"],
                "provenance": _PROVENANCE["mcp"],
                "rollback_provenance": _PROVENANCE["mcp_rollback"],
                "rollback_task_definition": _TASK_ARNS["mcp_rollback"],
                "rollback_image": _ROLLBACK_IMAGE,
            },
            "connect_web": {
                "cluster": "teamagent-dev",
                "service": "teamagent-dev-connect-web",
                "legacy_task_definition": _TASK_ARNS["connect_old"],
                "provenance": _PROVENANCE["connect_web"],
                "rollback_provenance": _PROVENANCE["connect_web_rollback"],
                "rollback_task_definition": _TASK_ARNS["connect_rollback"],
                "rollback_image": _ROLLBACK_IMAGE,
            },
        },
        "morning_digest": {
            "cluster": _CLUSTER,
            "rule": "teamagent-dev-morning-digest",
            "target_id": "morning",
            "legacy_task_definition": _TASK_ARNS["morning_old"],
            "provenance": _PROVENANCE["morning_digest"],
            "rollback_provenance": _PROVENANCE["morning_digest_rollback"],
            "rollback_task_definition": _TASK_ARNS["morning_rollback"],
            "rollback_image": _ROLLBACK_IMAGE,
            "legacy_target_digest": _target_digest(_morning_target(_TASK_ARNS["morning_old"])),
            "rollback_target_digest": _target_digest(
                _morning_target(_TASK_ARNS["morning_rollback"])
            ),
            "expected_rule_state": "DISABLED",
        },
        "worker": {
            "instance_id": "i-0123456789abcdef0",
            "provenance": worker_provenance,
            "artifact_sha256": artifact_hash,
            "rollback_provenance": worker_rollback_provenance,
            "rollback_artifact_sha256": rollback_hash,
        },
        "canary": {
            "rule": "teamagent-dev-connect-canary",
            "target_id": "canary",
            "task_definition": _TASK_ARNS["canary"],
        },
        "forbidden_signing_task_definitions": [
            _TASK_ARNS["mcp_old"],
            _TASK_ARNS["connect_old"],
        ],
    }


class _FakeEcs:
    def __init__(self) -> None:
        self.current = {
            "teamagent-dev-mcp": _TASK_ARNS["mcp_old"],
            "teamagent-dev-connect-web": _TASK_ARNS["connect_old"],
        }
        self.definitions = {
            _TASK_ARNS["mcp_old"]: _old_definition("mcp"),
            _TASK_ARNS["connect_old"]: _old_definition("connect_web"),
            _TASK_ARNS["morning_old"]: _old_definition("morning_digest"),
            _TASK_ARNS["mcp_new"]: _new_definition("mcp"),
            _TASK_ARNS["connect_new"]: _new_definition("connect_web"),
            _TASK_ARNS["morning_new"]: _new_definition("morning_digest"),
            _TASK_ARNS["mcp_rollback"]: _rollback_definition("mcp"),
            _TASK_ARNS["connect_rollback"]: _rollback_definition("connect_web"),
            _TASK_ARNS["morning_rollback"]: _rollback_definition("morning_digest"),
        }
        self.service_overrides: dict[str, dict[str, object]] = {}
        self.running_task_definition: dict[str, str] = {}
        self.list_tasks_next_token: str | None = None
        self.draining_service: str | None = None
        self.scheduled_running: list[str] = []
        self.scheduled_draining: list[str] = []

    def describe_services(self, **kwargs: object) -> dict[str, object]:
        service_name = kwargs["services"][0]  # type: ignore[index]
        task_definition = self.current[str(service_name)]
        service = {
            "status": "ACTIVE",
            "taskDefinition": task_definition,
            "desiredCount": 1,
            "runningCount": 1,
            "pendingCount": 0,
            "deployments": [
                {
                    "taskDefinition": task_definition,
                    "desiredCount": 1,
                    "runningCount": 1,
                    "pendingCount": 0,
                    "rolloutState": "COMPLETED",
                }
            ],
        }
        service.update(self.service_overrides.get(str(service_name), {}))
        return _response(services=[service], failures=[])

    def describe_task_definition(self, **kwargs: object) -> dict[str, object]:
        return _response(
            taskDefinition=copy.deepcopy(self.definitions[str(kwargs["taskDefinition"])])
        )

    def list_tasks(self, **kwargs: object) -> dict[str, object]:
        desired_status = str(kwargs["desiredStatus"])
        service_name = kwargs.get("serviceName")
        if service_name is not None:
            service = str(service_name)
            if desired_status == "RUNNING":
                task_arns = [f"arn:aws:ecs:region:account:task/{service}/running"]
            else:
                task_arns = [f"arn:aws:ecs:region:account:task/{service}/stopped"]
                if self.draining_service == service:
                    task_arns.append(f"arn:aws:ecs:region:account:task/{service}/draining")
        elif desired_status == "RUNNING":
            task_arns = [
                f"arn:aws:ecs:region:account:task/morning/{index}"
                for index, _task_definition in enumerate(self.scheduled_running)
            ]
        else:
            task_arns = [
                "arn:aws:ecs:region:account:task/morning/stopped",
                *[
                    f"arn:aws:ecs:region:account:task/morning/draining/{index}"
                    for index, _task_definition in enumerate(self.scheduled_draining)
                ],
            ]
        if kwargs.get("nextToken") is not None:
            return _response(taskArns=[], nextToken=self.list_tasks_next_token)
        return _response(
            taskArns=task_arns,
            **(
                {"nextToken": self.list_tasks_next_token}
                if self.list_tasks_next_token is not None
                else {}
            ),
        )

    def describe_tasks(self, **kwargs: object) -> dict[str, object]:
        tasks: list[dict[str, str]] = []
        for raw_task_arn in kwargs["tasks"]:  # type: ignore[union-attr]
            task_arn = str(raw_task_arn)
            if "/morning/" in task_arn:
                if task_arn.endswith("/stopped"):
                    task_definition = _TASK_ARNS["morning_old"]
                    desired_status = "STOPPED"
                    last_status = "STOPPED"
                elif "/draining/" in task_arn:
                    index = int(task_arn.rsplit("/", maxsplit=1)[-1])
                    task_definition = self.scheduled_draining[index]
                    desired_status = "STOPPED"
                    last_status = "RUNNING"
                else:
                    index = int(task_arn.rsplit("/", maxsplit=1)[-1])
                    task_definition = self.scheduled_running[index]
                    desired_status = "RUNNING"
                    last_status = "RUNNING"
            else:
                service = (
                    "teamagent-dev-connect-web"
                    if "connect-web" in task_arn
                    else "teamagent-dev-mcp"
                )
                task_definition = self.running_task_definition.get(
                    service,
                    self.current[service],
                )
                if task_arn.endswith("/running"):
                    desired_status = "RUNNING"
                    last_status = "RUNNING"
                elif task_arn.endswith("/draining"):
                    desired_status = "STOPPED"
                    last_status = "RUNNING"
                else:
                    desired_status = "STOPPED"
                    last_status = "STOPPED"
            tasks.append(
                {
                    "taskArn": task_arn,
                    "taskDefinitionArn": task_definition,
                    "desiredStatus": desired_status,
                    "lastStatus": last_status,
                }
            )
        return _response(tasks=tasks, failures=[])


class _FakeEvents:
    def __init__(self) -> None:
        self._morning_target = _morning_target(_TASK_ARNS["morning_old"])
        self.canary = _TASK_ARNS["canary"]
        self.rule_state = "DISABLED"
        self.additional_targets: list[dict[str, object]] = []
        self.put_failure: str | None = None
        self.put_history: list[dict[str, object]] = []
        self.disable_count = 0

    @property
    def morning(self) -> str:
        ecs_parameters = self._morning_target["EcsParameters"]
        assert isinstance(ecs_parameters, dict)
        return str(ecs_parameters["TaskDefinitionArn"])

    @morning.setter
    def morning(self, task_definition: str) -> None:
        self._morning_target = _morning_target(task_definition)

    def describe_rule(self, **kwargs: object) -> dict[str, object]:
        return _response(Name=kwargs["Name"], State=self.rule_state)

    def list_targets_by_rule(self, **kwargs: object) -> dict[str, object]:
        if kwargs["Rule"] == "teamagent-dev-connect-canary":
            return _response(
                Targets=[
                    {
                        "Id": "canary",
                        "EcsParameters": {"TaskDefinitionArn": self.canary},
                    }
                ]
            )
        return _response(
            Targets=[copy.deepcopy(self._morning_target), *copy.deepcopy(self.additional_targets)]
        )

    def put_targets(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["Rule"] == "teamagent-dev-morning-digest"
        targets = kwargs["Targets"]
        assert isinstance(targets, list) and len(targets) == 1
        target = copy.deepcopy(targets[0])
        assert isinstance(target, dict)
        self.put_history.append(target)
        self._morning_target = target
        if self.put_failure is not None:
            code = self.put_failure
            self.put_failure = None
            return _response(
                FailedEntryCount=1,
                FailedEntries=[{"ErrorCode": code, "ErrorMessage": "redacted"}],
            )
        return _response(FailedEntryCount=0, FailedEntries=[])

    def disable_rule(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["Name"] == "teamagent-dev-morning-digest"
        self.disable_count += 1
        self.rule_state = "DISABLED"
        return _response()


class _FakeSecrets:
    def __init__(self) -> None:
        self.extra_versions: dict[str, list[str]] = {}
        self.suppressed_versions: set[tuple[str, str]] = set()

    def list_secret_version_ids(self, **kwargs: object) -> dict[str, object]:
        versions = {
            _DB_ARN: _DB_VERSION,
            _MAIL_ARN: _MAIL_VERSION,
            _REPORT_ARN: _REPORT_VERSION,
            _SLACK_ARN: _SLACK_VERSION,
        }
        secret_id = str(kwargs["SecretId"])
        version_ids = [
            version_id
            for version_id in [versions[secret_id], *self.extra_versions.get(secret_id, [])]
            if (secret_id, version_id) not in self.suppressed_versions
        ]
        return _response(
            Versions=[
                {
                    "VersionId": version_id,
                    "VersionStages": ["AWSCURRENT"] if index == 0 else [],
                }
                for index, version_id in enumerate(version_ids)
            ]
        )


class _ConditionalDdbError(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "TransactionCanceledException"}}


class _FakeDdb:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.transactions: list[list[dict[str, Any]]] = []
        self.conditional_failures_remaining = 0
        self.conditional_failure_hook: Any | None = None
        self.before_transaction_hook: Any | None = None

    @staticmethod
    def _condition_matches(
        item: dict[str, Any] | None,
        operation: dict[str, Any],
    ) -> bool:
        expression = str(operation.get("ConditionExpression", "")).strip()
        if not expression:
            return True
        names = operation.get("ExpressionAttributeNames", {})
        values = operation.get("ExpressionAttributeValues", {})
        assert isinstance(names, dict) and isinstance(values, dict)
        tokens = re.findall(
            r"attribute_(?:not_)?exists|AND|OR|[()]|=|#[A-Za-z0-9_]+|"
            r":[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_]*",
            expression,
        )
        position = 0

        def attribute(token: str) -> str:
            return str(names.get(token, token))

        def factor() -> bool:
            nonlocal position
            token = tokens[position]
            if token == "(":
                position += 1
                result = disjunction()
                assert tokens[position] == ")"
                position += 1
                return result
            if token in {"attribute_not_exists", "attribute_exists"}:
                position += 1
                assert tokens[position] == "("
                name = attribute(tokens[position + 1])
                assert tokens[position + 2] == ")"
                position += 3
                exists = item is not None and name in item
                return not exists if token == "attribute_not_exists" else exists
            name = attribute(token)
            assert tokens[position + 1] == "="
            placeholder = tokens[position + 2]
            position += 3
            return item is not None and item.get(name) == values[placeholder]

        def conjunction() -> bool:
            nonlocal position
            result = factor()
            while position < len(tokens) and tokens[position] == "AND":
                position += 1
                result = factor() and result
            return result

        def disjunction() -> bool:
            nonlocal position
            result = conjunction()
            while position < len(tokens) and tokens[position] == "OR":
                position += 1
                result = conjunction() or result
            return result

        matched = disjunction()
        assert position == len(tokens), (expression, tokens[position:])
        return matched

    def transact_write_items(self, **kwargs: object) -> dict[str, object]:
        transaction = kwargs["TransactItems"]
        assert isinstance(transaction, list)
        with self.lock:
            self.transactions.append(copy.deepcopy(transaction))
            if self.before_transaction_hook is not None:
                hook = self.before_transaction_hook
                self.before_transaction_hook = None
                hook(self.items)
            if self.conditional_failures_remaining:
                self.conditional_failures_remaining -= 1
                if self.conditional_failure_hook is not None:
                    self.conditional_failure_hook(self.items)
                raise _ConditionalDdbError()
            next_items = copy.deepcopy(self.items)
            for operation in transaction:
                if "ConditionCheck" in operation:
                    check = operation["ConditionCheck"]
                    key = (check["Key"]["scope"]["S"], check["Key"]["record"]["S"])
                    item = next_items.get(key)
                    if not self._condition_matches(item, check):
                        raise _ConditionalDdbError()
                elif "Put" in operation:
                    put = operation["Put"]
                    item = copy.deepcopy(operation["Put"]["Item"])
                    key = (item["scope"]["S"], item["record"]["S"])
                    existing = next_items.get(key)
                    if not self._condition_matches(existing, put):
                        raise _ConditionalDdbError()
                    next_items[key] = item
                elif "Update" in operation:
                    update = operation["Update"]
                    key = (update["Key"]["scope"]["S"], update["Key"]["record"]["S"])
                    item = next_items.get(key)
                    if item is None or not self._condition_matches(item, update):
                        raise _ConditionalDdbError()
                    values = update["ExpressionAttributeValues"]
                    expression = str(update["UpdateExpression"])
                    names = update.get("ExpressionAttributeNames", {})
                    assert isinstance(names, dict)
                    if key[1].startswith("LEDGER#"):
                        if ":next" in values:
                            item["stage"] = copy.deepcopy(values[":next"])
                            item["updated_at"] = copy.deepcopy(values[":now"])
                        elif expression.startswith("SET #digest"):
                            item[str(names["#digest"])] = copy.deepcopy(values[":digest"])
                        elif expression.startswith("SET #arn"):
                            item[str(names["#arn"])] = copy.deepcopy(values[":arn"])
                        elif expression.startswith("SET #target"):
                            item[str(names["#target"])] = copy.deepcopy(values[":target"])
                            if "#rule_state" in names:
                                item[str(names["#rule_state"])] = copy.deepcopy(
                                    values[":rule_state"]
                                )
                        elif expression.startswith("SET #plan"):
                            item[str(names["#plan"])] = copy.deepcopy(values[":plan"])
                            item[str(names["#attempt"])] = copy.deepcopy(values[":attempt"])
                    elif "cleanup_stage = :authorized" in expression:
                        item["issuer_provenances"] = copy.deepcopy(values[":temporary"])
                        item["previous_retired"] = {"BOOL": True}
                        item["high_water"] = copy.deepcopy(values[":now"])
                        item["cleanup_stage"] = {"S": "authorized"}
                        existing_retired = set(item.get("retired_generations", {}).get("SS", []))
                        item["retired_generations"] = {
                            "SS": sorted(existing_retired | set(values[":retired"]["SS"]))
                        }
                    elif key[1].startswith("RESTART#"):
                        item["stage"] = {"S": "complete"}
                        item["completed_at"] = copy.deepcopy(values[":checked"])
                    elif key[1].startswith("CLEANUP#"):
                        if expression.startswith("SET #digest"):
                            item[str(names["#digest"])] = copy.deepcopy(values[":digest"])
                        elif expression.startswith("SET #arn"):
                            item[str(names["#arn"])] = copy.deepcopy(values[":arn"])
                        elif expression.startswith("SET #target"):
                            item[str(names["#target"])] = copy.deepcopy(values[":target"])
                            if "#rule_state" in names:
                                item[str(names["#rule_state"])] = copy.deepcopy(
                                    values[":rule_state"]
                                )
                        elif expression.startswith("SET #plan"):
                            item[str(names["#plan"])] = copy.deepcopy(values[":plan"])
                            item[str(names["#attempt"])] = copy.deepcopy(values[":attempt"])
                        else:
                            item["stage"] = {"S": "complete"}
                            item["completed_at"] = copy.deepcopy(values[":now"])
                    elif ":new_epoch" in values:
                        item.setdefault("clock_revision", {"N": "0"})
                        item["primary_generation"] = copy.deepcopy(values[":primary"])
                        item["rotation_epoch"] = copy.deepcopy(values[":new_epoch"])
                        item["previous_retired"] = copy.deepcopy(values[":false"])
                        item["stage"] = copy.deepcopy(values[":preload"])
                        for name in ("issuer_provenances", "cleanup_stage"):
                            item.pop(name, None)
                        if ":previous" in values:
                            item["previous_generation"] = copy.deepcopy(values[":previous"])
                            item["rotation_started_at"] = copy.deepcopy(values[":t0"])
                            item["deadline"] = copy.deepcopy(values[":deadline"])
                        else:
                            for name in (
                                "previous_generation",
                                "rotation_started_at",
                                "deadline",
                            ):
                                item.pop(name, None)
                        if ":legacy_worker" in values:
                            item["legacy_worker_generation"] = copy.deepcopy(
                                values[":legacy_worker"]
                            )
                            item["legacy_worker_deadline"] = copy.deepcopy(values[":deadline"])
                        else:
                            item.pop("legacy_worker_generation", None)
                            item.pop("legacy_worker_deadline", None)
                    elif "REMOVE previous_generation" in expression:
                        item["issuer_provenances"] = copy.deepcopy(values[":new"])
                        item["previous_retired"] = {"BOOL": True}
                        for name in (
                            "previous_generation",
                            "rotation_started_at",
                            "deadline",
                            "legacy_worker_generation",
                            "legacy_worker_deadline",
                            "cleanup_stage",
                        ):
                            item.pop(name, None)
                        if ":retired_provenances" in values:
                            existing_retired = set(
                                item.get("retired_provenances", {}).get("SS", [])
                            )
                            item["retired_provenances"] = {
                                "SS": sorted(
                                    existing_retired | set(values[":retired_provenances"]["SS"])
                                )
                            }
                    elif ":temporary" in values:
                        item["issuer_provenances"] = copy.deepcopy(values[":temporary"])
                    elif ":new" in values:
                        item["issuer_provenances"] = copy.deepcopy(values[":new"])
                        if ":retired_provenances" in values:
                            existing_retired = set(
                                item.get("retired_provenances", {}).get("SS", [])
                            )
                            item["retired_provenances"] = {
                                "SS": sorted(
                                    existing_retired | set(values[":retired_provenances"]["SS"])
                                )
                            }
                    elif ":retired" in values:
                        item["previous_retired"] = {"BOOL": True}
                        item["high_water"] = copy.deepcopy(values[":now"])
                        for name in (
                            "previous_generation",
                            "rotation_started_at",
                            "deadline",
                            "legacy_worker_generation",
                            "legacy_worker_deadline",
                        ):
                            item.pop(name, None)
                        existing_retired = set(item.get("retired_generations", {}).get("SS", []))
                        item["retired_generations"] = {
                            "SS": sorted(existing_retired | set(values[":retired"]["SS"]))
                        }
                    else:
                        item["stage"] = copy.deepcopy(values[":stage"])
                        if ":issuers" in values:
                            item["issuer_provenances"] = copy.deepcopy(values[":issuers"])
                    item["revision"] = {"N": str(int(item["revision"]["N"]) + 1)}
            self.items = next_items
        return _response()

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key_value = kwargs["Key"]
        assert isinstance(key_value, dict)
        key = (str(key_value["scope"]["S"]), str(key_value["record"]["S"]))
        item = copy.deepcopy(self.items.get(key))
        return _response(**({"Item": item} if item is not None else {}))


class _Factory:
    def __init__(self) -> None:
        self.ecs = _FakeEcs()
        self.events = _FakeEvents()
        self.secrets = _FakeSecrets()
        self.ddb = _FakeDdb()

    def client(self, service_name: str, *, region_name: str) -> object:
        assert region_name == "ap-northeast-1"
        return {
            "ecs": self.ecs,
            "events": self.events,
            "secretsmanager": self.secrets,
            "dynamodb": self.ddb,
        }[service_name]


def _gate(
    factory: _Factory,
    rollback_hash: str,
    *,
    artifact_hash: str = "2" * 64,
) -> LiveRolloutGate:
    return LiveRolloutGate(
        control=load_control(_control(rollback_hash, artifact_hash)),
        manifest=_manifest(),
        clients=factory,
        deployment_intent=_TEST_INTENT,
    )


def _bind_candidate_state(
    gate: LiveRolloutGate,
    factory: _Factory,
    *,
    definitions: dict[str, dict[str, object]] | None = None,
) -> None:
    selected = definitions or {
        task: factory.ecs.definitions[
            _TASK_ARNS[
                {
                    "mcp": "mcp_new",
                    "connect_web": "connect_new",
                    "morning_digest": "morning_new",
                }[task]
            ]
        ]
        for task in ("mcp", "connect_web", "morning_digest")
    }
    ledger = factory.ddb.items[(_SCOPE, f"LEDGER#{gate.control.rotation_epoch}")]
    for task, definition in selected.items():
        ledger[f"candidate_{task}_digest"] = {
            "S": rollout_gate_module._task_artifact_digest(definition)
        }
        ledger[f"candidate_{task}_arn"] = {"S": str(definition["taskDefinitionArn"])}
        ledger[f"candidate_{task}_plan_sha256"] = {"S": _TEST_INTENT.plan_sha256}
        ledger[f"candidate_{task}_apply_attempt_id"] = {"S": _TEST_INTENT.apply_attempt_id}
    ledger["candidate_morning_digest_target_digest"] = {
        "S": _target_digest(_morning_target(str(selected["morning_digest"]["taskDefinitionArn"])))
    }
    ledger["candidate_morning_digest_rule_state"] = {"S": "DISABLED"}


def test_initialize_uses_live_generations_and_creates_atomic_durable_state() -> None:
    factory = _Factory()
    gate = _gate(factory, "0" * 64)
    gate.initialize()

    assert (_SCOPE, "DOMAIN#mail_action") in factory.ddb.items
    assert (_SCOPE, "DOMAIN#report_link") in factory.ddb.items
    assert (_SCOPE, f"LEDGER#{_EPOCH}") in factory.ddb.items
    transaction_text = repr(factory.ddb.transactions)
    assert "SecretString" not in transaction_text
    assert _MAIL_GENERATION in transaction_text
    assert _REPORT_GENERATION in transaction_text


def test_initialize_rejects_manifest_deployed_generation_drift() -> None:
    factory = _Factory()
    manifest = _manifest()
    manifest["domains"]["mail_action"]["deployed"]["primary_generation"] = _MAIL_GENERATION  # type: ignore[index]
    gate = LiveRolloutGate(
        control=load_control(_control("0" * 64)),
        manifest=manifest,
        clients=factory,
        deployment_intent=_TEST_INTENT,
    )
    with pytest.raises(RolloutGateError, match="manifest_live_drift"):
        gate.initialize()
    assert not factory.ddb.transactions


def test_initialize_rejects_untrusted_stale_manifest_time() -> None:
    factory = _Factory()
    manifest = _manifest()
    manifest["now"] = _NOW - 61
    gate = LiveRolloutGate(
        control=load_control(_control("0" * 64)),
        manifest=manifest,
        clients=factory,
        deployment_intent=_TEST_INTENT,
    )
    with pytest.raises(RolloutGateError, match="manifest_time_stale"):
        gate.initialize()
    assert not factory.ddb.transactions


def test_trusted_time_compares_server_offsets_across_long_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moments = iter((1_000.0, 1_000.0, 1_120.0, 1_120.0))
    monkeypatch.setattr(rollout_gate_module.time, "monotonic", lambda: next(moments))
    gate = LiveRolloutGate(
        control=load_control(_control("0" * 64)),
        manifest=_manifest(),
        clients=_Factory(),
        deployment_intent=_TEST_INTENT,
    )
    gate._observe({"ResponseMetadata": {"HTTPHeaders": {"date": formatdate(_NOW, usegmt=True)}}})
    gate._observe(
        {"ResponseMetadata": {"HTTPHeaders": {"date": formatdate(_NOW + 120, usegmt=True)}}}
    )

    assert gate._now() == _NOW + 120


def test_trusted_time_rejects_inconsistent_server_offsets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moments = iter((1_000.0, 1_000.0, 1_120.0, 1_120.0))
    monkeypatch.setattr(rollout_gate_module.time, "monotonic", lambda: next(moments))
    gate = LiveRolloutGate(
        control=load_control(_control("0" * 64)),
        manifest=_manifest(),
        clients=_Factory(),
        deployment_intent=_TEST_INTENT,
    )
    for epoch in (_NOW, _NOW + 100):
        gate._observe(
            {"ResponseMetadata": {"HTTPHeaders": {"date": formatdate(epoch, usegmt=True)}}}
        )

    with pytest.raises(RolloutGateError, match="trusted_clock_disagreement"):
        gate._now()


def test_candidate_requires_exact_runtime_metadata() -> None:
    factory = _Factory()
    gate = _gate(factory, "0" * 64)
    gate.initialize()
    candidate = _new_definition("mcp")
    gate.validate_candidate(task="mcp", definition=candidate)

    drifted = copy.deepcopy(candidate)
    environment = drifted["containerDefinitions"][0]["environment"]  # type: ignore[index]
    for item in environment:
        if item["name"] == "TEAMAGENT_HMAC_PROVENANCE":
            item["value"] = "9" * 64
    with pytest.raises(RolloutGateError, match="runtime_metadata_drift"):
        gate.validate_candidate(task="mcp", definition=drifted)

    identity_alias = copy.deepcopy(candidate)
    identity_alias["taskDefinitionArn"] = gate.control.mcp.rollback_task_definition
    with pytest.raises(RolloutGateError, match="provenance_binding_drift"):
        gate.validate_candidate(task="mcp", definition=identity_alias)


def test_registration_rechecks_pinned_candidate_version_metadata() -> None:
    factory = _Factory()
    gate = _gate(factory, "0" * 64)
    gate.initialize()
    factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]["stage"] = {"S": "worker_verified"}
    factory.secrets.suppressed_versions.add((_MAIL_ARN, _MAIL_VERSION))

    with pytest.raises(RolloutGateError, match="secret_generation_unavailable"):
        gate.terraform_pre_register(
            task="mcp",
            definition=factory.ecs.definitions[_TASK_ARNS["mcp_new"]],
        )


def test_candidate_arn_is_fixed_once_even_for_same_full_artifact() -> None:
    factory = _Factory()
    gate = _gate(factory, "0" * 64)
    gate.initialize()
    candidate = copy.deepcopy(factory.ecs.definitions[_TASK_ARNS["connect_new"]])
    gate.terraform_pre_register(task="connect_web", definition=candidate)
    gate.pre_update(
        task="connect_web",
        task_definition=_TASK_ARNS["connect_new"],
        mode="candidate",
    )

    alternate_arn = (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-connect-web:99"
    )
    alternate = copy.deepcopy(candidate)
    alternate["taskDefinitionArn"] = alternate_arn
    factory.ecs.definitions[alternate_arn] = alternate
    assert rollout_gate_module._task_artifact_digest(
        alternate
    ) == rollout_gate_module._task_artifact_digest(candidate)

    with pytest.raises(RolloutGateError, match="candidate_identity_drift"):
        gate.pre_update(
            task="connect_web",
            task_definition=alternate_arn,
            mode="candidate",
        )


def test_candidate_gate_rejects_replayed_saved_plan_attempt() -> None:
    factory = _Factory()
    gate = _gate(factory, "0" * 64)
    gate.initialize()
    candidate = copy.deepcopy(factory.ecs.definitions[_TASK_ARNS["connect_new"]])
    gate.terraform_pre_register(task="connect_web", definition=candidate)

    replay = LiveRolloutGate(
        control=gate.control,
        manifest=_manifest(),
        clients=factory,
        deployment_intent=DeploymentIntent(
            plan_sha256=_TEST_INTENT.plan_sha256,
            apply_attempt_id="87654321-4321-4321-8321-cba987654321",
        ),
    )
    with pytest.raises(RolloutGateError, match="deployment_intent_drift"):
        replay.terraform_pre_register(task="connect_web", definition=candidate)


def test_registration_and_update_reject_stage_bypass() -> None:
    factory = _Factory()
    gate = _gate(factory, "0" * 64)
    gate.initialize()

    with pytest.raises(RolloutGateError, match="stage_order_violation"):
        gate.terraform_pre_register(task="mcp", definition=_new_definition("mcp"))
    with pytest.raises(RolloutGateError, match="stage_order_violation"):
        gate.pre_update(task="mcp", task_definition=_TASK_ARNS["mcp_new"])


def test_worker_stage_transition_requires_attestation_and_artifact(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "worker-rollback.tar.gz"
    artifact.write_bytes(b"prebuilt-hmac-compatible-worker")
    artifact_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
    factory = _Factory()
    gate = _gate(factory, artifact_hash)
    gate.initialize()
    _bind_candidate_state(gate, factory)
    ledger = factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]
    ledger["stage"] = {"S": "connect_web_preloaded"}
    worker_provenance = gate.control.worker.provenance
    factory.ddb.items[(_SCOPE, f"WORKER#{worker_provenance}")] = {
        "scope": {"S": _SCOPE},
        "record": {"S": f"WORKER#{worker_provenance}"},
        "provenance": {"S": worker_provenance},
        "worker_id": {"S": "i-0123456789abcdef0"},
        "rotation_epoch": {"S": _EPOCH},
        "config_digest": {"S": gate._worker_config_digest()},
        "loaded_domains": {"SS": ["mail_action", "report_link"]},
        "checked_at": {"N": str(_NOW)},
        "expires_at": {"N": str(_NOW + 3600)},
    }

    factory.ecs.scheduled_draining = [_TASK_ARNS["morning_old"]]
    with pytest.raises(RolloutGateError, match="old_tasks_not_drained"):
        gate.worker_verified(rollback_artifact=artifact)
    factory.ecs.scheduled_draining = []
    gate.worker_verified(rollback_artifact=artifact)
    assert factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]["stage"] == {"S": "worker_verified"}


def test_worker_attestation_rejects_effective_generation_t0_digest_drift(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "worker-rollback.tar.gz"
    artifact.write_bytes(b"prebuilt-hmac-compatible-worker")
    factory = _Factory()
    gate = _gate(factory, hashlib.sha256(artifact.read_bytes()).hexdigest())
    gate.initialize()
    factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]["stage"] = {"S": "connect_web_preloaded"}
    worker_provenance = gate.control.worker.provenance
    factory.ddb.items[(_SCOPE, f"WORKER#{worker_provenance}")] = {
        "scope": {"S": _SCOPE},
        "record": {"S": f"WORKER#{worker_provenance}"},
        "provenance": {"S": worker_provenance},
        "worker_id": {"S": "i-0123456789abcdef0"},
        "rotation_epoch": {"S": _EPOCH},
        "config_digest": {"S": "f" * 64},
        "loaded_domains": {"SS": ["mail_action", "report_link"]},
        "checked_at": {"N": str(_NOW)},
        "expires_at": {"N": str(_NOW + 300)},
    }

    with pytest.raises(RolloutGateError, match="worker_attestation_invalid"):
        gate.worker_verified(rollback_artifact=artifact)


def test_mcp_cutover_requires_post_worker_verified_attestation(tmp_path: Path) -> None:
    rollback = tmp_path / "worker-rollback.tar.gz"
    rollback.write_bytes(b"prebuilt-hmac-compatible-worker")
    factory = _Factory()
    gate = _gate(factory, hashlib.sha256(rollback.read_bytes()).hexdigest())
    gate.initialize()
    ledger = factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]
    ledger["stage"] = {"S": "worker_verified"}
    ledger["updated_at"] = {"N": str(_NOW)}
    factory.ecs.current["teamagent-dev-mcp"] = _TASK_ARNS["mcp_new"]
    worker_provenance = gate.control.worker.provenance
    attestation = {
        "scope": {"S": _SCOPE},
        "record": {"S": f"WORKER#{worker_provenance}"},
        "provenance": {"S": worker_provenance},
        "worker_id": {"S": "i-0123456789abcdef0"},
        "rotation_epoch": {"S": _EPOCH},
        "config_digest": {"S": gate._worker_config_digest()},
        "loaded_domains": {"SS": ["mail_action", "report_link"]},
        "checked_at": {"N": str(_NOW)},
        "expires_at": {"N": str(_NOW + 300)},
    }
    factory.ddb.items[(_SCOPE, f"WORKER#{worker_provenance}")] = attestation

    with pytest.raises(RolloutGateError, match="worker_restart_state_invalid"):
        gate.mcp_stable_and_old_drained()
    restart_nonce = "f" * 64
    factory.ddb.items[(_SCOPE, f"RESTART#{_EPOCH}#{worker_provenance}")] = {
        "scope": {"S": _SCOPE},
        "record": {"S": f"RESTART#{_EPOCH}#{worker_provenance}"},
        "rotation_epoch": {"S": _EPOCH},
        "provenance": {"S": worker_provenance},
        "artifact_sha256": {"S": gate.control.worker.artifact_sha256},
        "config_digest": {"S": gate._worker_config_digest()},
        "restart_nonce": {"S": restart_nonce},
        "stage": {"S": "complete"},
        "mode": {"S": "candidate"},
        "revision": {"N": "2"},
        "after_checked_at": {"N": str(_NOW - 1)},
        "requested_at": {"N": str(_NOW)},
        "completed_at": {"N": str(_NOW + 1)},
    }
    with pytest.raises(RolloutGateError, match="worker_attestation_invalid"):
        gate.mcp_stable_and_old_drained()

    attestation["checked_at"] = {"N": str(_NOW + 1)}
    config_digest = gate._worker_config_digest()
    for service, main_pid in (("bot", 111), ("connect", 222)):
        factory.ddb.items[(_SCOPE, f"WORKER_SERVICE#{worker_provenance}#{service}")] = {
            "scope": {"S": _SCOPE},
            "record": {"S": f"WORKER_SERVICE#{worker_provenance}#{service}"},
            "service": {"S": service},
            "provenance": {"S": worker_provenance},
            "worker_id": {"S": "i-0123456789abcdef0"},
            "rotation_epoch": {"S": _EPOCH},
            "restart_nonce": {"S": restart_nonce},
            "artifact_sha256": {"S": gate.control.worker.artifact_sha256},
            "config_digest": {"S": config_digest},
            "main_pid": {"N": str(main_pid)},
            "process_start_ticks": {"N": str(main_pid * 100)},
            "process_started_at": {"N": str(_NOW)},
            "health_verified": {"BOOL": True},
            "checked_at": {"N": str(_NOW + 1)},
            "expires_at": {"N": str(_NOW + 300)},
            **(
                {
                    "active_port": {"N": "8788"},
                    "port_owner_pid": {"N": str(main_pid)},
                    "health_endpoint": {"S": "http://127.0.0.1:8788/healthz"},
                }
                if service == "connect"
                else {}
            ),
        }
    gate.mcp_stable_and_old_drained()
    assert factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]["stage"] == {
        "S": "mcp_stable_and_old_drained"
    }


def test_worker_upload_binds_current_and_rollback_artifacts(tmp_path: Path) -> None:
    artifact = tmp_path / "worker.tar.gz"
    rollback = tmp_path / "worker-rollback.tar.gz"
    artifact.write_bytes(b"current-worker")
    rollback.write_bytes(b"rollback-worker")
    factory = _Factory()
    gate = _gate(
        factory,
        hashlib.sha256(rollback.read_bytes()).hexdigest(),
        artifact_hash=hashlib.sha256(artifact.read_bytes()).hexdigest(),
    )
    gate.initialize()
    factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]["stage"] = {"S": "connect_web_preloaded"}
    worker_env = tmp_path / "worker.env"
    rollback_env = tmp_path / "worker-rollback.env"
    manifest = _manifest()
    worker_env.write_text(
        _worker_env_text(
            manifest=manifest,
            provenance=gate.control.worker.provenance,
            artifact_sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        ),
        encoding="utf-8",
    )
    rollback_env.write_text(
        _worker_env_text(
            manifest=manifest,
            provenance=gate.control.worker.rollback_provenance,
            artifact_sha256=hashlib.sha256(rollback.read_bytes()).hexdigest(),
        ),
        encoding="utf-8",
    )

    gate.pre_worker_upload(
        artifact=artifact,
        rollback_artifact=rollback,
        worker_env=worker_env,
        rollback_env=rollback_env,
    )
    artifact.write_bytes(b"stale-worker")
    with pytest.raises(RolloutGateError, match="worker_artifact_drift"):
        gate.pre_worker_upload(
            artifact=artifact,
            rollback_artifact=rollback,
            worker_env=worker_env,
            rollback_env=rollback_env,
        )


def test_canary14_and_td53_contracts_fail_closed() -> None:
    factory = _Factory()
    factory.events.canary = _TASK_ARNS["morning_old"]
    gate = _gate(factory, "0" * 64)
    with pytest.raises(RolloutGateError, match="canary_anchor_changed"):
        gate.initialize()

    factory = _Factory()
    gate = _gate(factory, "0" * 64)
    gate.initialize()
    with pytest.raises(RolloutGateError, match="forbidden_signing_revision"):
        gate.pre_update(task="connect_web", task_definition=_TASK_ARNS["connect_old"])


def test_initialize_rejects_unpinned_or_unapproved_legacy_revisions() -> None:
    factory = _Factory()
    definition = factory.ecs.definitions[_TASK_ARNS["mcp_old"]]
    secrets = definition["containerDefinitions"][0]["secrets"]  # type: ignore[index]
    secrets[0]["valueFrom"] = _DB_ARN
    gate = _gate(factory, "0" * 64)
    with pytest.raises(RolloutGateError, match="secret_reference_unpinned"):
        gate.initialize()
    assert not factory.ddb.transactions

    factory = _Factory()
    factory.ecs.current["teamagent-dev-mcp"] = _TASK_ARNS["mcp_new"]
    gate = _gate(factory, "0" * 64)
    with pytest.raises(RolloutGateError, match="pinned_legacy_revision_required"):
        gate.initialize()
    assert not factory.ddb.transactions


@pytest.mark.parametrize(
    "override",
    [
        {"pendingCount": 1},
        {
            "deployments": [
                {"rolloutState": "COMPLETED"},
                {"rolloutState": "COMPLETED"},
            ]
        },
    ],
)
def test_initialize_requires_pinned_legacy_service_stability_and_full_drain(
    override: dict[str, object],
) -> None:
    factory = _Factory()
    factory.ecs.service_overrides["teamagent-dev-mcp"] = override
    gate = _gate(factory, "0" * 64)

    with pytest.raises(RolloutGateError, match="service_not_stable"):
        gate.initialize()
    assert not factory.ddb.transactions

    factory = _Factory()
    factory.ecs.running_task_definition["teamagent-dev-mcp"] = _TASK_ARNS["mcp_new"]
    gate = _gate(factory, "0" * 64)
    with pytest.raises(RolloutGateError, match="old_tasks_not_drained"):
        gate.initialize()
    assert not factory.ddb.transactions


def test_connect_stage_advancement_requires_stable_drained_service() -> None:
    factory = _Factory()
    gate = _gate(factory, "0" * 64)
    gate.initialize()
    factory.ecs.current["teamagent-dev-connect-web"] = _TASK_ARNS["connect_new"]
    factory.ecs.service_overrides["teamagent-dev-connect-web"] = {"pendingCount": 1}

    with pytest.raises(RolloutGateError, match="service_not_stable"):
        gate.connect_web_preloaded()
    ledger = factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]
    assert ledger["stage"] == {"S": "initialized"}


def test_complete_requires_connect_service_stable_and_fully_drained() -> None:
    factory = _Factory()
    gate = _gate(factory, "0" * 64)
    gate.initialize()
    _bind_candidate_state(gate, factory)
    ledger = factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]
    ledger["stage"] = {"S": "mcp_stable_and_old_drained"}
    factory.ecs.current["teamagent-dev-mcp"] = _TASK_ARNS["mcp_new"]
    factory.ecs.current["teamagent-dev-connect-web"] = _TASK_ARNS["connect_new"]
    factory.events.morning = _TASK_ARNS["morning_new"]
    factory.ecs.running_task_definition["teamagent-dev-connect-web"] = _TASK_ARNS["connect_old"]

    with pytest.raises(RolloutGateError, match="old_tasks_not_drained"):
        gate.complete()
    assert ledger["stage"] == {"S": "mcp_stable_and_old_drained"}

    factory.ecs.running_task_definition["teamagent-dev-connect-web"] = _TASK_ARNS["connect_new"]
    factory.ecs.running_task_definition["teamagent-dev-mcp"] = _TASK_ARNS["mcp_old"]
    with pytest.raises(RolloutGateError, match="old_tasks_not_drained"):
        gate.complete()
    assert ledger["stage"] == {"S": "mcp_stable_and_old_drained"}

    factory.ecs.running_task_definition["teamagent-dev-mcp"] = _TASK_ARNS["mcp_new"]
    gate.complete()
    assert factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]["stage"] == {"S": "complete"}


def test_service_drain_proof_fails_closed_on_repeated_inventory_page_token() -> None:
    factory = _Factory()
    gate = _gate(factory, "0" * 64)
    factory.ecs.list_tasks_next_token = "another-page"

    with pytest.raises(RolloutGateError, match="task_inventory_incomplete"):
        gate.initialize()
    assert not factory.ddb.transactions


def test_service_inventory_reconciles_describe_and_list_counts() -> None:
    factory = _Factory()
    factory.ecs.service_overrides["teamagent-dev-mcp"] = {
        "desiredCount": 2,
        "runningCount": 2,
        "pendingCount": 0,
        "deployments": [
            {
                "taskDefinition": _TASK_ARNS["mcp_old"],
                "desiredCount": 2,
                "runningCount": 2,
                "pendingCount": 0,
                "rolloutState": "COMPLETED",
            }
        ],
    }
    gate = _gate(factory, "0" * 64)

    with pytest.raises(RolloutGateError, match="task_inventory_count_drift"):
        gate.initialize()
    assert not factory.ddb.transactions


def test_service_inventory_rejects_zero_capacity_and_describe_failures() -> None:
    factory = _Factory()
    factory.ecs.service_overrides["teamagent-dev-mcp"] = {
        "desiredCount": 0,
        "runningCount": 0,
        "pendingCount": 0,
        "deployments": [
            {
                "taskDefinition": _TASK_ARNS["mcp_old"],
                "desiredCount": 0,
                "runningCount": 0,
                "pendingCount": 0,
                "rolloutState": "COMPLETED",
            }
        ],
    }
    gate = _gate(factory, "0" * 64)
    with pytest.raises(RolloutGateError, match="service_not_stable"):
        gate._service_stable_and_drained(
            gate.control.mcp,
            _TASK_ARNS["mcp_old"],
        )

    def failed_describe(**_kwargs: object) -> dict[str, object]:
        return _response(
            services=[
                {
                    "status": "ACTIVE",
                    "taskDefinition": _TASK_ARNS["mcp_old"],
                    "desiredCount": 1,
                    "runningCount": 1,
                    "pendingCount": 0,
                    "deployments": [],
                }
            ],
            failures=[{"reason": "MISSING"}],
        )

    factory.ecs.describe_services = failed_describe  # type: ignore[method-assign]
    with pytest.raises(RolloutGateError, match="service_not_stable"):
        gate._service_stable_and_drained(
            gate.control.mcp,
            _TASK_ARNS["mcp_old"],
        )


def test_inventory_rejects_service_and_scheduled_tasks_still_draining() -> None:
    factory = _Factory()
    factory.ecs.draining_service = "teamagent-dev-mcp"
    gate = _gate(factory, "0" * 64)
    with pytest.raises(RolloutGateError, match="old_tasks_not_drained"):
        gate.initialize()

    factory = _Factory()
    factory.ecs.scheduled_draining = [_TASK_ARNS["morning_old"]]
    gate = _gate(factory, "0" * 64)
    with pytest.raises(RolloutGateError, match="old_tasks_not_drained"):
        gate.initialize()


def _install_distinct_rollbacks(
    factory: _Factory,
    control: dict[str, object],
) -> dict[str, str]:
    del factory, control
    return {
        "mcp": _TASK_ARNS["mcp_rollback"],
        "connect_web": _TASK_ARNS["connect_rollback"],
        "morning_digest": _TASK_ARNS["morning_rollback"],
    }


def test_distinct_exact_rollback_passes_after_cutover_and_rejects_wrong_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _Factory()
    control = _control("0" * 64)
    rollback_arns = _install_distinct_rollbacks(factory, control)
    gate = LiveRolloutGate(
        control=load_control(control),
        manifest=_manifest(),
        clients=factory,
        deployment_intent=_TEST_INTENT,
    )
    gate.initialize()
    factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]["stage"] = {"S": "complete"}
    after_cutover = _NOW + 901
    monkeypatch.setattr(rollout_gate_module, "_trusted_epoch", lambda _response: after_cutover)
    manifest = _manifest()
    manifest["now"] = after_cutover
    gate = LiveRolloutGate(
        control=load_control(control),
        manifest=manifest,
        clients=factory,
        deployment_intent=_TEST_INTENT,
    )

    gate.pre_update(
        task="mcp",
        task_definition=rollback_arns["mcp"],
        mode="rollback",
    )
    with pytest.raises(RolloutGateError, match="rollback_task_not_approved"):
        gate.pre_update(
            task="mcp",
            task_definition=_TASK_ARNS["mcp_new"],
            mode="rollback",
        )
    with pytest.raises(RolloutGateError, match="issuer_cutover_deadline"):
        gate.pre_update(
            task="mcp",
            task_definition=_TASK_ARNS["mcp_new"],
            mode="candidate",
        )

    factory.ecs.current["teamagent-dev-mcp"] = rollback_arns["mcp"]
    gate.post_update(
        task="mcp",
        task_definition=rollback_arns["mcp"],
        mode="rollback",
    )


def _ready_for_scheduled_promotion(
    factory: _Factory,
) -> LiveRolloutGate:
    gate = _gate(factory, "0" * 64)
    gate.initialize()
    factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]["stage"] = {"S": "complete"}
    gate.terraform_pre_register(
        task="morning_digest",
        definition=factory.ecs.definitions[_TASK_ARNS["morning_new"]],
    )
    return gate


def test_eventbridge_transaction_binds_full_target_and_rule_state() -> None:
    factory = _Factory()
    gate = _ready_for_scheduled_promotion(factory)
    target = _morning_target(_TASK_ARNS["morning_new"])

    gate.event_target_transaction(
        task_definition=_TASK_ARNS["morning_new"],
        target=target,
        mode="candidate",
    )

    assert factory.events.morning == _TASK_ARNS["morning_new"]
    assert factory.events.put_history == [target]
    ledger = factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]
    assert ledger["candidate_morning_digest_target_digest"] == {"S": _target_digest(target)}
    assert ledger["candidate_morning_digest_rule_state"] == {"S": "DISABLED"}
    assert ledger["candidate_morning_digest_plan_sha256"] == {"S": _TEST_INTENT.plan_sha256}
    assert ledger["candidate_morning_digest_apply_attempt_id"] == {
        "S": _TEST_INTENT.apply_attempt_id
    }

    changed_input = copy.deepcopy(target)
    changed_input["Input"] = '{"unexpected":true}'
    with pytest.raises(RolloutGateError, match="scheduled_target_drift"):
        gate.event_target_transaction(
            task_definition=_TASK_ARNS["morning_new"],
            target=changed_input,
            mode="candidate",
        )
    assert factory.events.put_history == [target]


def test_eventbridge_partial_failure_restores_exact_target_and_disabled_rule() -> None:
    factory = _Factory()
    gate = _ready_for_scheduled_promotion(factory)
    baseline = _morning_target(_TASK_ARNS["morning_old"])
    candidate = _morning_target(_TASK_ARNS["morning_new"])
    factory.events.put_failure = "InternalException"

    with pytest.raises(RolloutGateError, match="scheduled_target_partial_failure"):
        gate.event_target_transaction(
            task_definition=_TASK_ARNS["morning_new"],
            target=candidate,
            mode="candidate",
        )

    assert factory.events.put_history == [candidate, baseline]
    assert factory.events._morning_target == baseline
    assert factory.events.rule_state == "DISABLED"
    assert factory.events.disable_count == 1


@pytest.mark.parametrize("drift", ["additional-target", "enabled-rule"])
def test_eventbridge_wrong_baseline_fails_before_mutation(drift: str) -> None:
    factory = _Factory()
    gate = _ready_for_scheduled_promotion(factory)
    if drift == "additional-target":
        factory.events.additional_targets.append(
            {
                "Id": "unexpected",
                "Arn": _CLUSTER,
                "RoleArn": _EVENT_ROLE,
                "Input": "{}",
                "EcsParameters": copy.deepcopy(
                    _morning_target(_TASK_ARNS["morning_old"])["EcsParameters"]
                ),
                "RetryPolicy": {
                    "MaximumEventAgeInSeconds": 3600,
                    "MaximumRetryAttempts": 1,
                },
            }
        )
        expected = "scheduled_target_unavailable"
    else:
        factory.events.rule_state = "ENABLED"
        expected = "scheduled_rule_state_drift"

    with pytest.raises(RolloutGateError, match=expected):
        gate.event_target_transaction(
            task_definition=_TASK_ARNS["morning_new"],
            target=_morning_target(_TASK_ARNS["morning_new"]),
            mode="candidate",
        )
    assert factory.events.put_history == []


def test_eventbridge_rollback_uses_distinct_full_approved_target() -> None:
    factory = _Factory()
    gate = _ready_for_scheduled_promotion(factory)
    candidate = _morning_target(_TASK_ARNS["morning_new"])
    gate.event_target_transaction(
        task_definition=_TASK_ARNS["morning_new"],
        target=candidate,
        mode="candidate",
    )
    rollback = _morning_target(_TASK_ARNS["morning_rollback"])

    gate.event_target_transaction(
        task_definition=_TASK_ARNS["morning_rollback"],
        target=rollback,
        mode="rollback",
    )

    assert candidate != rollback
    assert _target_digest(candidate) != _target_digest(rollback)
    assert factory.events.put_history == [candidate, rollback]
    assert factory.events.morning == _TASK_ARNS["morning_rollback"]
    ledger = factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]
    assert ledger["rollback_morning_digest_plan_sha256"] == {"S": _TEST_INTENT.plan_sha256}
    assert ledger["rollback_morning_digest_apply_attempt_id"] == {
        "S": _TEST_INTENT.apply_attempt_id
    }


def test_worker_rollback_mode_uses_exact_approved_artifact_and_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = tmp_path / "current.tar.gz"
    rollback = tmp_path / "rollback.tar.gz"
    current.write_bytes(b"current-worker")
    rollback.write_bytes(b"approved-rollback-worker")
    current_hash = hashlib.sha256(current.read_bytes()).hexdigest()
    rollback_hash = hashlib.sha256(rollback.read_bytes()).hexdigest()
    factory = _Factory()
    gate = _gate(factory, rollback_hash, artifact_hash=current_hash)
    gate.initialize()
    factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]["stage"] = {"S": "complete"}
    after_cutover = _NOW + 901
    monkeypatch.setattr(rollout_gate_module, "_trusted_epoch", lambda _response: after_cutover)
    manifest = _manifest()
    manifest["now"] = after_cutover
    gate = LiveRolloutGate(
        control=gate.control,
        manifest=manifest,
        clients=factory,
        deployment_intent=_TEST_INTENT,
    )
    rollback_env = tmp_path / "rollback.env"
    rollback_env.write_text(
        _worker_env_text(
            manifest=manifest,
            provenance=gate.control.worker.rollback_provenance,
            artifact_sha256=rollback_hash,
        ),
        encoding="utf-8",
    )

    gate.pre_worker_upload(
        artifact=rollback,
        rollback_artifact=rollback,
        worker_env=rollback_env,
        rollback_env=rollback_env,
        mode="rollback",
    )
    with pytest.raises(RolloutGateError, match="worker_rollback_artifact_drift"):
        gate.pre_worker_upload(
            artifact=current,
            rollback_artifact=rollback,
            worker_env=rollback_env,
            rollback_env=rollback_env,
            mode="rollback",
        )

    rollback_provenance = gate.control.worker.rollback_provenance
    factory.ddb.items[(_SCOPE, f"WORKER#{rollback_provenance}")] = {
        "scope": {"S": _SCOPE},
        "record": {"S": f"WORKER#{rollback_provenance}"},
        "provenance": {"S": rollback_provenance},
        "worker_id": {"S": "i-0123456789abcdef0"},
        "rotation_epoch": {"S": _EPOCH},
        "config_digest": {"S": gate._worker_config_digest(provenance=rollback_provenance)},
        "loaded_domains": {"SS": ["mail_action", "report_link"]},
        "checked_at": {"N": str(after_cutover)},
        "expires_at": {"N": str(after_cutover + 300)},
    }
    restart_nonce = gate.pre_restart(rollback_artifact=rollback, mode="rollback")
    with pytest.raises(RolloutGateError, match="worker_attestation_invalid"):
        gate.post_restart(mode="rollback")
    stored = factory.ddb.items[(_SCOPE, f"WORKER#{rollback_provenance}")]
    stored["checked_at"] = {"N": str(after_cutover + 1)}
    stored["expires_at"] = {"N": str(after_cutover + 301)}
    config_digest = gate._worker_config_digest(provenance=rollback_provenance)
    for service, main_pid in (("bot", 101), ("connect", 202)):
        factory.ddb.items[(_SCOPE, f"WORKER_SERVICE#{rollback_provenance}#{service}")] = {
            "scope": {"S": _SCOPE},
            "record": {"S": f"WORKER_SERVICE#{rollback_provenance}#{service}"},
            "service": {"S": service},
            "provenance": {"S": rollback_provenance},
            "worker_id": {"S": "i-0123456789abcdef0"},
            "rotation_epoch": {"S": _EPOCH},
            "restart_nonce": {"S": restart_nonce},
            "artifact_sha256": {"S": rollback_hash},
            "config_digest": {"S": config_digest},
            "main_pid": {"N": str(main_pid)},
            "process_start_ticks": {"N": str(main_pid * 100)},
            "process_started_at": {"N": str(after_cutover)},
            "health_verified": {"BOOL": True},
            "checked_at": {"N": str(after_cutover + 1)},
            "expires_at": {"N": str(after_cutover + 301)},
            **(
                {
                    "active_port": {"N": "8788"},
                    "port_owner_pid": {"N": str(main_pid)},
                    "health_endpoint": {"S": "http://127.0.0.1:8788/healthz"},
                }
                if service == "connect"
                else {}
            ),
        }
    bot_record = factory.ddb.items[(_SCOPE, f"WORKER_SERVICE#{rollback_provenance}#bot")]
    connect_record = factory.ddb.items[(_SCOPE, f"WORKER_SERVICE#{rollback_provenance}#connect")]
    bot_record["health_verified"] = {"BOOL": False}
    with pytest.raises(RolloutGateError, match="worker_attestation_invalid"):
        gate.post_restart(mode="rollback")
    bot_record["health_verified"] = {"BOOL": True}
    connect_pid = connect_record["main_pid"]
    connect_record["main_pid"] = copy.deepcopy(bot_record["main_pid"])
    with pytest.raises(RolloutGateError, match="worker_attestation_invalid"):
        gate.post_restart(mode="rollback")
    connect_record["main_pid"] = connect_pid
    connect_port_owner = connect_record["port_owner_pid"]
    connect_record["port_owner_pid"] = copy.deepcopy(bot_record["main_pid"])
    with pytest.raises(RolloutGateError, match="worker_attestation_invalid"):
        gate.post_restart(mode="rollback")
    connect_record["port_owner_pid"] = connect_port_owner
    connect_started_at = connect_record["process_started_at"]
    connect_record["process_started_at"] = {"N": str(after_cutover - 1)}
    with pytest.raises(RolloutGateError, match="worker_attestation_invalid"):
        gate.post_restart(mode="rollback")
    connect_record["process_started_at"] = connect_started_at
    gate.post_restart(mode="rollback")


def _retirement_manifest(now: int, *, domain: str = "mail_action") -> dict[str, object]:
    manifest = _manifest()
    manifest["now"] = now
    configs: dict[str, dict[str, object]] = {}
    for item_domain in ("mail_action", "report_link"):
        active = _config(item_domain, deployed=False)
        proposed = (
            {
                "primary_generation": active["primary_generation"],
                "previous_generation": None,
                "rotation_started_at": None,
            }
            if item_domain == domain
            else copy.deepcopy(active)
        )
        manifest["domains"][item_domain] = {  # type: ignore[index]
            "deployed": active,
            "proposed": proposed,
        }
        configs[item_domain] = proposed
    manifest["legacy_worker_generation"] = None if domain == "mail_action" else _SLACK_GENERATION
    manifest["tasks"] = {
        "mcp": {
            "mail_action": configs["mail_action"],
            "report_link": configs["report_link"],
        },
        "morning_digest": {"mail_action": configs["mail_action"]},
        "connect_web": {"report_link": configs["report_link"]},
        "worker": {
            "mail_action": configs["mail_action"],
            "report_link": configs["report_link"],
        },
    }
    return manifest


def _steady_manifest(now: int) -> dict[str, object]:
    manifest = _retirement_manifest(now, domain="mail_action")
    for domain in ("mail_action", "report_link"):
        proposed = {
            "primary_generation": (
                _MAIL_GENERATION if domain == "mail_action" else _REPORT_GENERATION
            ),
            "previous_generation": None,
            "rotation_started_at": None,
        }
        manifest["domains"][domain]["proposed"] = copy.deepcopy(proposed)  # type: ignore[index]
        manifest["domains"][domain]["deployed"] = proposed  # type: ignore[index]
    manifest["legacy_worker_generation"] = None
    configs = {
        domain: copy.deepcopy(manifest["domains"][domain]["proposed"])  # type: ignore[index]
        for domain in ("mail_action", "report_link")
    }
    manifest["tasks"] = {
        "mcp": configs,
        "morning_digest": {"mail_action": configs["mail_action"]},
        "connect_web": {"report_link": configs["report_link"]},
        "worker": configs,
    }
    return manifest


def _primary_only_definition(
    task: str,
    *,
    epoch: str,
    provenance: str,
) -> dict[str, object]:
    definition = copy.deepcopy(_new_definition(task))
    environment = definition["containerDefinitions"][0]["environment"]  # type: ignore[index]
    secrets = definition["containerDefinitions"][0]["secrets"]  # type: ignore[index]
    environment[:] = [
        entry
        for entry in environment
        if entry["name"]
        not in {
            "MAIL_ACTION_HMAC_PREVIOUS_GENERATION",
            "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
            "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY",
            "MAIL_ACTION_HMAC_LEGACY_WORKER_GENERATION",
            "REPORT_LINK_HMAC_PREVIOUS_GENERATION",
            "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT",
            "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY",
        }
    ]
    secrets[:] = [
        entry
        for entry in secrets
        if entry["name"]
        not in {
            "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
            "MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET",
            "REPORT_LINK_HMAC_PREVIOUS_SECRET",
        }
    ]
    for entry in environment:
        if entry["name"] == "TEAMAGENT_HMAC_ROTATION_EPOCH":
            entry["value"] = epoch
        elif entry["name"] == "TEAMAGENT_HMAC_PROVENANCE":
            entry["value"] = provenance
    return definition


def _remove_domain_previous(
    definition: dict[str, object],
    *,
    domain: str,
) -> None:
    prefix = "MAIL_ACTION" if domain == "mail_action" else "REPORT_LINK"
    environment = definition["containerDefinitions"][0]["environment"]  # type: ignore[index]
    secrets = definition["containerDefinitions"][0]["secrets"]  # type: ignore[index]
    environment[:] = [
        entry
        for entry in environment
        if entry["name"]
        not in {
            f"{prefix}_HMAC_PREVIOUS_GENERATION",
            f"{prefix}_HMAC_PREVIOUS_ROTATION_STARTED_AT",
            f"{prefix}_HMAC_PREVIOUS_IS_LEGACY",
            *({"MAIL_ACTION_HMAC_LEGACY_WORKER_GENERATION"} if domain == "mail_action" else set()),
        }
    ]
    secrets[:] = [
        entry
        for entry in secrets
        if entry["name"]
        not in {
            f"{prefix}_HMAC_PREVIOUS_SECRET",
            *({"MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET"} if domain == "mail_action" else set()),
        }
    ]


def _artifact_provenance(
    task: str,
    *,
    manifest: dict[str, object],
    image: str | None = None,
    artifact: str | None = None,
) -> str:
    domains = manifest["domains"]  # type: ignore[assignment]
    mail = domains["mail_action"]["proposed"]  # type: ignore[index]
    report = domains["report_link"]["proposed"]  # type: ignore[index]
    values = {
        "workload": task,
        "rotation_epoch": _EPOCH,
    }
    if image is not None:
        values["image"] = image
    if artifact is not None:
        values["artifact"] = artifact
    if task in {"mcp", "morning_digest", "worker"}:
        values.update(
            {
                "mail_primary": str(mail["primary_generation"]),
                "mail_previous": str(mail["previous_generation"] or ""),
                "mail_t0": (
                    str(mail["rotation_started_at"])
                    if mail["rotation_started_at"] is not None
                    else ""
                ),
                "legacy_worker": str(manifest["legacy_worker_generation"] or ""),
            }
        )
    if task in {"mcp", "connect_web", "worker"}:
        values.update(
            {
                "report_primary": str(report["primary_generation"]),
                "report_previous": str(report["previous_generation"] or ""),
                "report_t0": (
                    str(report["rotation_started_at"])
                    if report["rotation_started_at"] is not None
                    else ""
                ),
            }
        )
    return _provenance(**values)


def _cleanup_definition(
    task: str,
    *,
    manifest: dict[str, object],
    image: str,
    provenance: str,
    rollback: bool,
) -> dict[str, object]:
    definition = copy.deepcopy(_new_definition(task))
    definition["taskDefinitionArn"] = _TASK_ARNS[
        (
            {
                "mcp": "mcp_rollback",
                "connect_web": "connect_rollback",
                "morning_digest": "morning_rollback",
            }
            if rollback
            else {
                "mcp": "mcp_cleanup",
                "connect_web": "connect_cleanup",
                "morning_digest": "morning_cleanup",
            }
        )[task]
    ]
    for domain in ("mail_action", "report_link"):
        if (
            domain in {"mail_action", "report_link"}
            and domain
            in {
                "mcp": {"mail_action", "report_link"},
                "connect_web": {"report_link"},
                "morning_digest": {"mail_action"},
            }[task]
            and manifest["domains"][domain]["proposed"]["previous_generation"] is None  # type: ignore[index]
        ):
            _remove_domain_previous(definition, domain=domain)
    container = definition["containerDefinitions"][0]  # type: ignore[index]
    container["image"] = image
    for entry in container["environment"]:
        if entry["name"] == "TEAMAGENT_HMAC_PROVENANCE":
            entry["value"] = provenance
    return definition


def _worker_env_text(
    *,
    manifest: dict[str, object],
    provenance: str,
    artifact_sha256: str,
) -> str:
    values = {
        "TEAMAGENT_HMAC_STATE_REQUIRED": "1",
        "TEAMAGENT_HMAC_STATE_TABLE": _TABLE,
        "TEAMAGENT_HMAC_STATE_SCOPE": _SCOPE,
        "TEAMAGENT_HMAC_ROTATION_EPOCH": _EPOCH,
        "TEAMAGENT_HMAC_PROVENANCE": provenance,
        "TEAMAGENT_HMAC_ARTIFACT_SHA256": artifact_sha256,
        "TEAMAGENT_HMAC_WORKER_ID": "i-0123456789abcdef0",
        "MAIL_ACTION_HMAC_SECRET_NAME": "teamagent/dev/hmac/mail-action",
        "MAIL_ACTION_HMAC_PRIMARY_VERSION_ID": _MAIL_VERSION,
        "MAIL_ACTION_HMAC_PRIMARY_GENERATION": _MAIL_GENERATION,
        "MAIL_ACTION_TTL_S": "86400",
        "REPORT_LINK_HMAC_SECRET_NAME": "teamagent/dev/hmac/report-link",
        "REPORT_LINK_HMAC_PRIMARY_VERSION_ID": _REPORT_VERSION,
        "REPORT_LINK_HMAC_PRIMARY_GENERATION": _REPORT_GENERATION,
        "REPORT_LINK_TTL_S": "604800",
    }
    for domain, prefix in (
        ("mail_action", "MAIL_ACTION"),
        ("report_link", "REPORT_LINK"),
    ):
        proposed = manifest["domains"][domain]["proposed"]  # type: ignore[index]
        previous = proposed["previous_generation"]
        if previous is None:
            values.update(
                {
                    f"{prefix}_HMAC_PREVIOUS_SECRET_NAME": "",
                    f"{prefix}_HMAC_PREVIOUS_VERSION_ID": "",
                    f"{prefix}_HMAC_PREVIOUS_GENERATION": "",
                    f"{prefix}_HMAC_PREVIOUS_ROTATION_STARTED_AT": "",
                    f"{prefix}_HMAC_PREVIOUS_IS_LEGACY": "",
                }
            )
        else:
            values.update(
                {
                    f"{prefix}_HMAC_PREVIOUS_SECRET_NAME": "teamagent/dev/database-url",
                    f"{prefix}_HMAC_PREVIOUS_VERSION_ID": _DB_VERSION,
                    f"{prefix}_HMAC_PREVIOUS_GENERATION": str(previous),
                    f"{prefix}_HMAC_PREVIOUS_ROTATION_STARTED_AT": str(
                        proposed["rotation_started_at"]
                    ),
                    f"{prefix}_HMAC_PREVIOUS_IS_LEGACY": "1",
                }
            )
    legacy = manifest["legacy_worker_generation"]
    values.update(
        {
            "MAIL_ACTION_HMAC_LEGACY_WORKER_SECRET_NAME": (
                "teamagent/dev/slack/bot-token" if legacy is not None else ""
            ),
            "MAIL_ACTION_HMAC_LEGACY_WORKER_VERSION_ID": (
                _SLACK_VERSION if legacy is not None else ""
            ),
            "MAIL_ACTION_HMAC_LEGACY_WORKER_GENERATION": str(legacy or ""),
        }
    )
    return "\n".join(f"export {name}='{value}'" for name, value in values.items()) + "\n"


def _cleanup_bundle(
    tmp_path: Path,
    *,
    factory: _Factory,
    manifest: dict[str, object],
) -> tuple[
    LiveRolloutGate,
    dict[str, dict[str, object]],
    Path,
    Path,
    Path,
    Path,
]:
    candidate_artifact = tmp_path / "worker-cleanup.tar.gz"
    rollback_artifact = tmp_path / "worker-cleanup-rollback.tar.gz"
    candidate_artifact.write_bytes(b"primary-only-candidate-worker")
    rollback_artifact.write_bytes(b"primary-only-rollback-worker")
    candidate_hash = hashlib.sha256(candidate_artifact.read_bytes()).hexdigest()
    rollback_hash = hashlib.sha256(rollback_artifact.read_bytes()).hexdigest()
    control = _control(rollback_hash, candidate_hash)
    services = control["services"]  # type: ignore[assignment]
    candidate_definitions: dict[str, dict[str, object]] = {}
    for task in ("mcp", "connect_web", "morning_digest"):
        candidate_provenance = _artifact_provenance(
            task,
            manifest=manifest,
            image=_IMAGE,
        )
        rollback_provenance = _artifact_provenance(
            task,
            manifest=manifest,
            image=_ROLLBACK_IMAGE,
        )
        candidate = _cleanup_definition(
            task,
            manifest=manifest,
            image=_IMAGE,
            provenance=candidate_provenance,
            rollback=False,
        )
        rollback = _cleanup_definition(
            task,
            manifest=manifest,
            image=_ROLLBACK_IMAGE,
            provenance=rollback_provenance,
            rollback=True,
        )
        candidate_definitions[task] = candidate
        candidate_arn = str(candidate["taskDefinitionArn"])
        rollback_arn = str(rollback["taskDefinitionArn"])
        factory.ecs.definitions[candidate_arn] = candidate
        factory.ecs.definitions[rollback_arn] = rollback
        task_control = (
            control["morning_digest"] if task == "morning_digest" else services[task]  # type: ignore[index]
        )
        task_control["provenance"] = candidate_provenance  # type: ignore[index]
        task_control["rollback_provenance"] = rollback_provenance  # type: ignore[index]
        task_control["rollback_task_definition"] = rollback_arn  # type: ignore[index]
        task_control["rollback_image"] = _ROLLBACK_IMAGE  # type: ignore[index]
    worker_provenance = _artifact_provenance(
        "worker",
        manifest=manifest,
        artifact=candidate_hash,
    )
    worker_rollback_provenance = _artifact_provenance(
        "worker",
        manifest=manifest,
        artifact=rollback_hash,
    )
    control["worker"]["provenance"] = worker_provenance  # type: ignore[index]
    control["worker"]["rollback_provenance"] = worker_rollback_provenance  # type: ignore[index]
    candidate_env = tmp_path / "worker-cleanup.env"
    rollback_env = tmp_path / "worker-cleanup-rollback.env"
    candidate_env.write_text(
        _worker_env_text(
            manifest=manifest,
            provenance=worker_provenance,
            artifact_sha256=candidate_hash,
        ),
        encoding="utf-8",
    )
    rollback_env.write_text(
        _worker_env_text(
            manifest=manifest,
            provenance=worker_rollback_provenance,
            artifact_sha256=rollback_hash,
        ),
        encoding="utf-8",
    )
    return (
        LiveRolloutGate(
            control=load_control(control),
            manifest=manifest,
            clients=factory,
            deployment_intent=_TEST_INTENT,
        ),
        candidate_definitions,
        candidate_env,
        rollback_env,
        candidate_artifact,
        rollback_artifact,
    )


def test_retirement_cleanup_is_cas_durable_and_preserves_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _Factory()
    initial = _gate(factory, "0" * 64)
    initial.initialize()
    _bind_candidate_state(initial, factory)
    factory.ecs.current["teamagent-dev-mcp"] = _TASK_ARNS["mcp_new"]
    factory.ecs.current["teamagent-dev-connect-web"] = _TASK_ARNS["connect_new"]
    factory.events.morning = _TASK_ARNS["morning_new"]
    ledger = factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]
    ledger["stage"] = {"S": "complete"}
    initial_issuers = initial._issuer_provenances(initial.control)
    for domain in ("mail_action", "report_link"):
        item = factory.ddb.items[(_SCOPE, f"DOMAIN#{domain}")]
        item["stage"] = {"S": "complete"}
        item["issuer_provenances"] = {"SS": sorted(initial_issuers[domain])}

    retirement_now = _NOW + 900 + 86_400
    monkeypatch.setattr(
        rollout_gate_module,
        "_trusted_epoch",
        lambda _response: retirement_now,
    )
    manifest = _retirement_manifest(retirement_now, domain="mail_action")
    (
        gate,
        candidates,
        worker_env,
        worker_rollback_env,
        worker_artifact,
        worker_rollback_artifact,
    ) = _cleanup_bundle(
        tmp_path,
        factory=factory,
        manifest=manifest,
    )

    with pytest.raises(RolloutGateError, match="cleanup_staging_required"):
        gate.retire_previous(domain="mail_action")
    aliased_candidates = copy.deepcopy(candidates)
    aliased_candidates["connect_web"]["taskDefinitionArn"] = aliased_candidates["mcp"][
        "taskDefinitionArn"
    ]
    with pytest.raises(RolloutGateError, match="cleanup_artifacts_not_distinct"):
        gate.prepare_cleanup(
            domain="mail_action",
            candidate_definitions=aliased_candidates,
            worker_env=worker_env,
            worker_rollback_env=worker_rollback_env,
            worker_artifact=worker_artifact,
            worker_rollback_artifact=worker_rollback_artifact,
            prepared_plan_sha256="e" * 64,
        )
    gate.prepare_cleanup(
        domain="mail_action",
        candidate_definitions=candidates,
        worker_env=worker_env,
        worker_rollback_env=worker_rollback_env,
        worker_artifact=worker_artifact,
        worker_rollback_artifact=worker_rollback_artifact,
        prepared_plan_sha256="e" * 64,
    )
    cleanup = gate._cleanup_ledger("mail_action")
    assert cleanup.candidate_worker_env_digest != cleanup.rollback_worker_env_digest
    exact_worker_env = worker_env.read_bytes()
    worker_env.write_bytes(exact_worker_env + b"# drift\n")
    with pytest.raises(RolloutGateError, match="worker_env_drift"):
        gate._verify_prepared_worker_env(
            cleanup=cleanup,
            path=worker_env,
            rollback=False,
        )
    worker_env.write_bytes(exact_worker_env)
    mail = factory.ddb.items[(_SCOPE, "DOMAIN#mail_action")]
    assert mail["cleanup_stage"] == {"S": "authorized"}
    assert mail["previous_generation"] == {"S": _DB_GENERATION}
    assert mail["previous_retired"] == {"BOOL": True}
    assert set(mail["issuer_provenances"]["SS"]) > initial_issuers["mail_action"]

    drifted_candidate = copy.deepcopy(candidates["mcp"])
    drifted_candidate["containerDefinitions"][0]["environment"].append(  # type: ignore[index]
        {"name": "UNREVIEWED_RUNTIME_FLAG", "value": "1"}
    )
    with pytest.raises(RolloutGateError, match="cleanup_artifact_drift"):
        gate.terraform_pre_register(
            task="mcp",
            definition=drifted_candidate,
            mode="cleanup",
        )
    for task in ("connect_web", "mcp", "morning_digest"):
        gate.terraform_pre_register(
            task=task,
            definition=candidates[task],
            mode="cleanup",
        )
    with pytest.raises(RolloutGateError, match="cleanup_mode_required"):
        gate.pre_update(
            task="mcp",
            task_definition=_TASK_ARNS["mcp_cleanup"],
            mode="candidate",
        )
    gate.pre_update(
        task="connect_web",
        task_definition=_TASK_ARNS["connect_cleanup"],
        mode="cleanup",
    )
    gate.pre_update(
        task="mcp",
        task_definition=_TASK_ARNS["mcp_cleanup"],
        mode="cleanup",
    )
    gate.pre_event_update(
        task_definition=_TASK_ARNS["morning_cleanup"],
        target=_morning_target(_TASK_ARNS["morning_cleanup"]),
        mode="cleanup",
    )
    factory.ecs.current["teamagent-dev-mcp"] = _TASK_ARNS["mcp_cleanup"]
    factory.ecs.current["teamagent-dev-connect-web"] = _TASK_ARNS["connect_cleanup"]
    factory.events.morning = _TASK_ARNS["morning_cleanup"]
    factory.ecs.running_task_definition["teamagent-dev-mcp"] = _TASK_ARNS["mcp_cleanup"]
    factory.ecs.running_task_definition["teamagent-dev-connect-web"] = _TASK_ARNS["connect_cleanup"]

    gate.pre_worker_upload(
        artifact=worker_artifact,
        rollback_artifact=worker_rollback_artifact,
        worker_env=worker_env,
        rollback_env=worker_rollback_env,
        mode="cleanup",
    )
    worker_provenance = gate.control.worker.provenance
    cleanup = gate._cleanup_ledger("mail_action")
    attestation = {
        "scope": {"S": _SCOPE},
        "record": {"S": f"WORKER#{worker_provenance}"},
        "provenance": {"S": worker_provenance},
        "worker_id": {"S": "i-0123456789abcdef0"},
        "rotation_epoch": {"S": _EPOCH},
        "config_digest": {
            "S": gate._worker_config_digest(
                provenance=worker_provenance,
                configs=gate._cleanup_proposed_from_ledger(cleanup),
            )
        },
        "loaded_domains": {"SS": ["mail_action", "report_link"]},
        "checked_at": {"N": str(retirement_now)},
        "expires_at": {"N": str(retirement_now + 300)},
    }
    factory.ddb.items[(_SCOPE, f"WORKER#{worker_provenance}")] = attestation
    restart_nonce = gate.pre_restart(
        rollback_artifact=worker_rollback_artifact,
        mode="cleanup",
    )
    stored_attestation = factory.ddb.items[(_SCOPE, f"WORKER#{worker_provenance}")]
    stored_attestation["checked_at"] = {"N": str(retirement_now + 1)}
    stored_attestation["expires_at"] = {"N": str(retirement_now + 301)}
    config_digest = str(stored_attestation["config_digest"]["S"])
    for service, main_pid in (("bot", 303), ("connect", 404)):
        factory.ddb.items[(_SCOPE, f"WORKER_SERVICE#{worker_provenance}#{service}")] = {
            "scope": {"S": _SCOPE},
            "record": {"S": f"WORKER_SERVICE#{worker_provenance}#{service}"},
            "service": {"S": service},
            "provenance": {"S": worker_provenance},
            "worker_id": {"S": "i-0123456789abcdef0"},
            "rotation_epoch": {"S": _EPOCH},
            "restart_nonce": {"S": restart_nonce},
            "artifact_sha256": {"S": gate.control.worker.artifact_sha256},
            "config_digest": {"S": config_digest},
            "main_pid": {"N": str(main_pid)},
            "process_start_ticks": {"N": str(main_pid * 100)},
            "process_started_at": {"N": str(retirement_now)},
            "health_verified": {"BOOL": True},
            "checked_at": {"N": str(retirement_now + 1)},
            "expires_at": {"N": str(retirement_now + 301)},
            **(
                {
                    "active_port": {"N": "8788"},
                    "port_owner_pid": {"N": str(main_pid)},
                    "health_endpoint": {"S": "http://127.0.0.1:8788/healthz"},
                }
                if service == "connect"
                else {}
            ),
        }
    gate.post_restart(mode="cleanup")

    current_attestation = factory.ddb.items[(_SCOPE, f"WORKER#{worker_provenance}")]
    current_attestation["checked_at"] = {"N": str(retirement_now)}
    with pytest.raises(RolloutGateError, match="worker_attestation_invalid"):
        gate.complete_cleanup(domain="mail_action")
    current_attestation["checked_at"] = {"N": str(retirement_now + 1)}

    factory.ecs.scheduled_running = [_TASK_ARNS["morning_new"]]
    with pytest.raises(RolloutGateError, match="old_tasks_not_drained"):
        gate.complete_cleanup(domain="mail_action")
    factory.ecs.scheduled_running = [_TASK_ARNS["morning_cleanup"]]
    gate.complete_cleanup(domain="mail_action")

    mail = factory.ddb.items[(_SCOPE, "DOMAIN#mail_action")]
    assert "previous_generation" not in mail
    assert "rotation_started_at" not in mail
    assert "deadline" not in mail
    assert "legacy_worker_generation" not in mail
    assert "cleanup_stage" not in mail
    assert mail["previous_retired"] == {"BOOL": True}
    assert _SLACK_GENERATION in mail["retired_generations"]["SS"]
    assert _DB_GENERATION in mail["retired_generations"]["SS"]
    assert (_SCOPE, f"RETIREMENT#mail_action#{_EPOCH}") in factory.ddb.items
    assert initial_issuers["mail_action"] <= set(mail["retired_provenances"]["SS"])
    expected_issuers = gate._issuer_provenances(gate.control)
    assert set(mail["issuer_provenances"]["SS"]) == expected_issuers["mail_action"]
    assert gate.control.mcp.rollback_provenance in mail["issuer_provenances"]["SS"]
    assert gate._assert_manifest_matches_durable() == {
        "mail_action": {
            "primary_generation": _MAIL_GENERATION,
            "previous_generation": None,
            "rotation_started_at": None,
        },
        "report_link": {
            "primary_generation": _REPORT_GENERATION,
            "previous_generation": _DB_GENERATION,
            "rotation_started_at": _NOW,
        },
    }
    gate.pre_update(
        task="mcp",
        task_definition=_TASK_ARNS["mcp_rollback"],
        mode="rollback",
    )


@pytest.mark.parametrize(
    ("domain", "deadline"),
    [
        ("mail_action", _NOW + 900 + 86_400),
        ("report_link", _NOW + 900 + 604_800),
    ],
)
def test_cleanup_deadline_boundary_is_exact_and_retries_hot_clock_cas(
    domain: str,
    deadline: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _Factory()
    initial = _gate(factory, "0" * 64)
    initial.initialize()
    _bind_candidate_state(initial, factory)
    factory.ecs.current["teamagent-dev-mcp"] = _TASK_ARNS["mcp_new"]
    factory.ecs.current["teamagent-dev-connect-web"] = _TASK_ARNS["connect_new"]
    factory.events.morning = _TASK_ARNS["morning_new"]
    ledger = factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")]
    ledger["stage"] = {"S": "complete"}
    issuers = initial._issuer_provenances(initial.control)
    for item_domain in ("mail_action", "report_link"):
        item = factory.ddb.items[(_SCOPE, f"DOMAIN#{item_domain}")]
        item["stage"] = {"S": "complete"}
        item["issuer_provenances"] = {"SS": sorted(issuers[item_domain])}
    if domain == "report_link":
        # The shorter-lived domain is already expired. Its independent runtime clock has
        # durably retired the key, so it must not make report-link cleanup unrecoverable.
        mail = factory.ddb.items[(_SCOPE, "DOMAIN#mail_action")]
        mail["previous_retired"] = {"BOOL": True}
        mail["high_water"] = {"N": str(_NOW + 900 + 86_400)}
        mail["retired_generations"] = {"SS": sorted({_DB_GENERATION, _SLACK_GENERATION})}

    monkeypatch.setattr(
        rollout_gate_module,
        "_trusted_epoch",
        lambda _response: deadline,
    )
    manifest = _retirement_manifest(deadline, domain=domain)
    (
        gate,
        candidates,
        worker_env,
        worker_rollback_env,
        worker_artifact,
        worker_rollback_artifact,
    ) = _cleanup_bundle(
        tmp_path,
        factory=factory,
        manifest=manifest,
    )

    hot_high_water = deadline + 5

    def advance_runtime_clock(
        items: dict[tuple[str, str], dict[str, Any]],
    ) -> None:
        item = items[(_SCOPE, f"DOMAIN#{domain}")]
        item["clock_revision"] = {"N": str(int(item.get("clock_revision", {"N": "0"})["N"]) + 1)}
        item["high_water"] = {"N": str(hot_high_water)}

    factory.ddb.conditional_failures_remaining = 1
    factory.ddb.conditional_failure_hook = advance_runtime_clock
    gate.prepare_cleanup(
        domain=domain,
        candidate_definitions=candidates,
        worker_env=worker_env,
        worker_rollback_env=worker_rollback_env,
        worker_artifact=worker_artifact,
        worker_rollback_artifact=worker_rollback_artifact,
        prepared_plan_sha256=_TEST_INTENT.plan_sha256,
    )

    cleanup = gate._cleanup_ledger(domain)
    saved_proposal = gate._cleanup_proposed_from_ledger(cleanup)
    expected = {
        item_domain: copy.deepcopy(manifest["domains"][item_domain]["proposed"])  # type: ignore[index]
        for item_domain in ("mail_action", "report_link")
    }
    assert saved_proposal == expected
    assert factory.ddb.items[(_SCOPE, f"DOMAIN#{domain}")]["high_water"] == {
        "N": str(hot_high_water)
    }
    assert len(factory.ddb.transactions) >= 3

    monkeypatch.setattr(
        rollout_gate_module,
        "_trusted_epoch",
        lambda _response: deadline + 700_000,
    )
    manifest["domains"][domain]["proposed"]["primary_generation"] = "drifted"  # type: ignore[index]
    for task, definition in candidates.items():
        gate._validate_prepared_cleanup_task(
            cleanup=cleanup,
            task=task,
            definition=definition,
            rollback=False,
        )

    assert gate._cleanup_proposed_from_ledger(gate._cleanup_ledger(domain)) == expected


def test_completed_primary_only_epoch_can_initialize_next_rotation_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _Factory()
    retired_generations = {_DB_GENERATION, _SLACK_GENERATION}
    prior_high_water = _NOW + 120
    for domain, primary in (
        ("mail_action", _MAIL_GENERATION),
        ("report_link", _REPORT_GENERATION),
    ):
        factory.ddb.items[(_SCOPE, f"DOMAIN#{domain}")] = {
            "scope": {"S": _SCOPE},
            "record": {"S": f"DOMAIN#{domain}"},
            "domain": {"S": domain},
            "revision": {"N": "9"},
            "clock_revision": {"N": "4"},
            "primary_generation": {"S": primary},
            "rotation_epoch": {"S": _EPOCH},
            "high_water": {"N": str(prior_high_water)},
            "previous_retired": {"BOOL": True},
            "stage": {"S": "complete"},
            "retired_generations": {"SS": sorted(retired_generations)},
        }
    factory.ddb.items[(_SCOPE, f"LEDGER#{_EPOCH}")] = {
        "scope": {"S": _SCOPE},
        "record": {"S": f"LEDGER#{_EPOCH}"},
        "rotation_epoch": {"S": _EPOCH},
        "stage": {"S": "complete"},
        "revision": {"N": "5"},
        "updated_at": {"N": str(_NOW)},
    }

    next_epoch = "hmac-2026-08-rotation"
    for task, arn in (
        ("mcp", _TASK_ARNS["mcp_new"]),
        ("connect_web", _TASK_ARNS["connect_new"]),
        ("morning_digest", _TASK_ARNS["morning_new"]),
    ):
        factory.ecs.definitions[arn] = _primary_only_definition(
            task,
            epoch=_EPOCH,
            provenance=_PROVENANCE[task],
        )
    factory.ecs.current["teamagent-dev-mcp"] = _TASK_ARNS["mcp_new"]
    factory.ecs.current["teamagent-dev-connect-web"] = _TASK_ARNS["connect_new"]
    factory.events.morning = _TASK_ARNS["morning_new"]

    next_mail = f"{_MAIL_ARN}@{'n' * 32}"
    next_report = f"{_REPORT_ARN}@{'p' * 32}"
    factory.secrets.extra_versions[_MAIL_ARN] = ["n" * 32]
    factory.secrets.extra_versions[_REPORT_ARN] = ["p" * 32]
    next_t0 = _NOW
    manifest = {
        "now": _NOW,
        "legacy_database_generation": _DB_GENERATION,
        "legacy_worker_generation": None,
        "domains": {
            "mail_action": {
                "deployed": {
                    "primary_generation": _MAIL_GENERATION,
                    "previous_generation": None,
                    "rotation_started_at": None,
                },
                "proposed": {
                    "primary_generation": next_mail,
                    "previous_generation": _MAIL_GENERATION,
                    "rotation_started_at": next_t0,
                },
            },
            "report_link": {
                "deployed": {
                    "primary_generation": _REPORT_GENERATION,
                    "previous_generation": None,
                    "rotation_started_at": None,
                },
                "proposed": {
                    "primary_generation": next_report,
                    "previous_generation": _REPORT_GENERATION,
                    "rotation_started_at": next_t0,
                },
            },
        },
    }
    proposed = {
        domain: copy.deepcopy(config["proposed"]) for domain, config in manifest["domains"].items()
    }
    manifest["tasks"] = {
        "mcp": {
            "mail_action": proposed["mail_action"],
            "report_link": proposed["report_link"],
        },
        "morning_digest": {"mail_action": proposed["mail_action"]},
        "connect_web": {"report_link": proposed["report_link"]},
        "worker": {
            "mail_action": proposed["mail_action"],
            "report_link": proposed["report_link"],
        },
    }
    control = _control("0" * 64)
    control["rotation_epoch"] = next_epoch
    control["services"]["mcp"]["legacy_task_definition"] = _TASK_ARNS["mcp_new"]  # type: ignore[index]
    control["services"]["connect_web"]["legacy_task_definition"] = _TASK_ARNS[  # type: ignore[index]
        "connect_new"
    ]
    control["morning_digest"]["legacy_task_definition"] = _TASK_ARNS["morning_new"]  # type: ignore[index]
    control["morning_digest"]["legacy_target_digest"] = _target_digest(  # type: ignore[index]
        _morning_target(_TASK_ARNS["morning_new"])
    )
    gate = LiveRolloutGate(
        control=load_control(control),
        manifest=manifest,
        clients=factory,
        deployment_intent=_TEST_INTENT,
    )
    monkeypatch.setattr(rollout_gate_module, "_trusted_epoch", lambda _response: _NOW)
    concurrent_high_water = prior_high_water + 30

    def advance_runtime_high_water(
        items: dict[tuple[str, str], dict[str, Any]],
    ) -> None:
        for domain in ("mail_action", "report_link"):
            item = items[(_SCOPE, f"DOMAIN#{domain}")]
            item["clock_revision"] = {"N": "5"}
            item["high_water"] = {"N": str(concurrent_high_water)}

    factory.ddb.before_transaction_hook = advance_runtime_high_water

    gate.initialize()

    for domain, expected_primary, expected_previous in (
        ("mail_action", next_mail, _MAIL_GENERATION),
        ("report_link", next_report, _REPORT_GENERATION),
    ):
        item = factory.ddb.items[(_SCOPE, f"DOMAIN#{domain}")]
        assert item["rotation_epoch"] == {"S": next_epoch}
        assert item["primary_generation"] == {"S": expected_primary}
        assert item["previous_generation"] == {"S": expected_previous}
        assert item["high_water"] == {"N": str(concurrent_high_water)}
        assert item["clock_revision"] == {"N": "5"}
        assert set(item["retired_generations"]["SS"]) == retired_generations
        history = factory.ddb.items[(_SCOPE, f"EPOCH_HISTORY#{domain}#{_EPOCH}")]
        assert history["high_water"] == {"N": str(prior_high_water)}
        assert set(history["retired_generations"]["SS"]) == retired_generations
    assert factory.ddb.items[(_SCOPE, f"LEDGER#{next_epoch}")]["previous_rotation_epoch"] == {
        "S": _EPOCH
    }
    assert factory.ddb.items[(_SCOPE, f"LEDGER#{next_epoch}")]["updated_at"] == {
        "N": str(prior_high_water)
    }


def test_control_rejects_candidate_rollback_aliases() -> None:
    same_worker_digest = _control("2" * 64, "2" * 64)
    with pytest.raises(RolloutGateError, match="invalid_control"):
        load_control(same_worker_digest)

    same_ecs_provenance = _control("0" * 64)
    same_ecs_provenance["services"]["mcp"]["rollback_provenance"] = (  # type: ignore[index]
        same_ecs_provenance["services"]["mcp"]["provenance"]  # type: ignore[index]
    )
    with pytest.raises(RolloutGateError, match="invalid_control"):
        load_control(same_ecs_provenance)

    duplicate_rollback_identity = _control("0" * 64)
    duplicate_rollback_identity["services"]["connect_web"]["rollback_task_definition"] = (  # type: ignore[index]
        duplicate_rollback_identity["services"]["mcp"]["rollback_task_definition"]  # type: ignore[index]
    )
    with pytest.raises(RolloutGateError, match="invalid_control"):
        load_control(duplicate_rollback_identity)


def test_cli_redacts_ordinary_client_exceptions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "manifest.json"
    control_path = tmp_path / "control.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    control_path.write_text(json.dumps(_control("0" * 64)), encoding="utf-8")
    factory = _Factory()

    def explode(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("sensitive-client-detail")

    factory.ecs.describe_services = explode  # type: ignore[method-assign]
    result = rollout_gate_module.main(
        [
            "--manifest",
            str(manifest_path),
            "--control",
            str(control_path),
            "--action",
            "inspect",
        ],
        clients=factory,
    )

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == '{"code":"gate_client_error","ok":false}\n'
    assert captured.err == ""
    assert "sensitive-client-detail" not in captured.out


def test_terraform_bridge_redacts_ordinary_client_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HMAC_GATE_ENABLED", "true")
    monkeypatch.setenv("HMAC_PREFLIGHT_MANIFEST", "/protected/manifest.json")
    monkeypatch.setenv("HMAC_ROLLOUT_CONTROL", "/protected/control.json")
    monkeypatch.setenv("HMAC_GATE_TASK", "mcp")
    plan = tmp_path / "saved.tfplan"
    plan.write_bytes(b"opaque-plan")
    monkeypatch.setenv("TEAMAGENT_SAVED_PLAN_PATH", str(plan))
    monkeypatch.setenv("TEAMAGENT_APPLY_ATTEMPT_ID", "attempt")
    monkeypatch.setattr(
        terraform_gate_module,
        "show_saved_plan",
        lambda _path: {},
    )
    monkeypatch.setattr(
        terraform_gate_module,
        "validate_saved_plan_hmac_files",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        terraform_gate_module,
        "validate_saved_plan_runtime_mutations",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        terraform_gate_module,
        "candidate_change_from_plan",
        lambda _plan, **_kwargs: ({}, ("create",)),
    )

    def explode(_path: str) -> dict[str, object]:
        raise RuntimeError("sensitive-client-detail")

    monkeypatch.setattr(terraform_gate_module, "_load_mapping", explode)
    assert terraform_gate_module.main() == 2

    captured = capsys.readouterr()
    assert captured.out == '{"code":"gate_client_error","ok":false}\n'
    assert captured.err == ""
    assert "sensitive-client-detail" not in captured.out


def test_promotion_bridge_redacts_ordinary_client_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    plan = tmp_path / "saved.tfplan"
    plan.write_bytes(b"opaque-plan")
    monkeypatch.setenv("HMAC_GATE_ENABLED", "true")
    monkeypatch.setenv("TEAMAGENT_HMAC_PROMOTION_FROM_TERRAFORM", "1")
    monkeypatch.setenv("TEAMAGENT_SAVED_PLAN_PATH", str(plan))
    monkeypatch.setenv("TEAMAGENT_APPLY_ATTEMPT_ID", _TEST_INTENT.apply_attempt_id)
    monkeypatch.setenv("HMAC_GATE_TASK", "mcp")
    monkeypatch.setenv("HMAC_GATE_ACTION", "pre-update")
    monkeypatch.setenv("HMAC_GATE_MODE", "candidate")
    monkeypatch.setenv("HMAC_REGISTERED_TASK_ARN", _TASK_ARNS["mcp_new"])
    monkeypatch.setenv("HMAC_PREFLIGHT_MANIFEST", "/protected/manifest.json")
    monkeypatch.setenv("HMAC_ROLLOUT_CONTROL", "/protected/control.json")
    monkeypatch.setattr(promotion_gate_module, "show_saved_plan", lambda _path: {})
    monkeypatch.setattr(
        promotion_gate_module,
        "validate_saved_plan_hmac_files",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        promotion_gate_module,
        "validate_saved_plan_runtime_mutations",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        promotion_gate_module,
        "validate_saved_plan_event_target",
        lambda *_args, **_kwargs: None,
    )

    def explode(_path: str) -> dict[str, object]:
        raise RuntimeError("sensitive-client-detail")

    monkeypatch.setattr(promotion_gate_module, "_mapping_file", explode)
    assert promotion_gate_module.main() == 2

    captured = capsys.readouterr()
    assert captured.out == '{"code":"gate_client_error","ok":false}\n'
    assert captured.err == ""
    assert "sensitive-client-detail" not in captured.out
