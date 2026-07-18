from __future__ import annotations

import copy
import hashlib
import json
import threading
from email.utils import formatdate
from pathlib import Path
from typing import Any

import pytest

import scripts.hmac_rollout_gate as rollout_gate_module
import scripts.terraform_hmac_gate as terraform_gate_module
from scripts.hmac_rollout_gate import (
    LiveRolloutGate,
    RolloutGateError,
    load_control,
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
    return {
        "taskDefinitionArn": _TASK_ARNS[
            {"mcp": "mcp_old", "connect_web": "connect_old", "morning_digest": "morning_old"}[task]
        ],
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
        self.morning = _TASK_ARNS["morning_old"]
        self.canary = _TASK_ARNS["canary"]

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
            Targets=[
                {
                    "Id": "morning",
                    "Arn": _CLUSTER,
                    "EcsParameters": {"TaskDefinitionArn": self.morning},
                }
            ]
        )


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


class _FakeDdb:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.transactions: list[list[dict[str, Any]]] = []

    def transact_write_items(self, **kwargs: object) -> dict[str, object]:
        transaction = kwargs["TransactItems"]
        assert isinstance(transaction, list)
        with self.lock:
            self.transactions.append(copy.deepcopy(transaction))
            next_items = copy.deepcopy(self.items)
            for operation in transaction:
                if "ConditionCheck" in operation:
                    check = operation["ConditionCheck"]
                    key = (check["Key"]["scope"]["S"], check["Key"]["record"]["S"])
                    item = next_items.get(key)
                    if item is None or item.get("stage") != {"S": "complete"}:
                        raise RuntimeError("conditional")
                elif "Put" in operation:
                    put = operation["Put"]
                    item = copy.deepcopy(operation["Put"]["Item"])
                    key = (item["scope"]["S"], item["record"]["S"])
                    condition = str(put.get("ConditionExpression", ""))
                    existing = next_items.get(key)
                    if "attribute_not_exists" in condition:
                        if existing is not None:
                            raise RuntimeError("conditional")
                    elif "revision = :revision" in condition:
                        values = put["ExpressionAttributeValues"]
                        invalid = (
                            existing is None or existing.get("revision") != values[":revision"]
                        )
                        if ":old_epoch" in values:
                            invalid = invalid or (
                                existing.get("rotation_epoch") != values[":old_epoch"]
                                or existing.get("stage") != values[":complete"]
                            )
                        if invalid:
                            raise RuntimeError("conditional")
                    next_items[key] = item
                elif "Update" in operation:
                    update = operation["Update"]
                    key = (update["Key"]["scope"]["S"], update["Key"]["record"]["S"])
                    item = next_items[key]
                    values = update["ExpressionAttributeValues"]
                    expression = str(update["UpdateExpression"])
                    if key[1].startswith("LEDGER#"):
                        item["stage"] = copy.deepcopy(values[":next"])
                        item["updated_at"] = copy.deepcopy(values[":now"])
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
                        item["stage"] = {"S": "complete"}
                        item["completed_at"] = copy.deepcopy(values[":now"])
                    elif "REMOVE previous_generation" in expression:
                        item["issuer_provenances"] = copy.deepcopy(values[":new"])
                        item["previous_retired"] = {"BOOL": True}
                        item["high_water"] = copy.deepcopy(values[":now"])
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
    )


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

    with pytest.raises(RolloutGateError, match="worker_attestation_invalid"):
        gate.mcp_stable_and_old_drained()

    attestation["checked_at"] = {"N": str(_NOW + 1)}
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

    gate.pre_worker_upload(artifact=artifact, rollback_artifact=rollback)
    artifact.write_bytes(b"stale-worker")
    with pytest.raises(RolloutGateError, match="worker_artifact_drift"):
        gate.pre_worker_upload(artifact=artifact, rollback_artifact=rollback)


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
    )

    gate.pre_worker_upload(
        artifact=rollback,
        rollback_artifact=rollback,
        mode="rollback",
    )
    with pytest.raises(RolloutGateError, match="worker_rollback_artifact_drift"):
        gate.pre_worker_upload(
            artifact=current,
            rollback_artifact=rollback,
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
    gate.pre_restart(rollback_artifact=rollback, mode="rollback")
    with pytest.raises(RolloutGateError, match="worker_attestation_invalid"):
        gate.post_restart(mode="rollback")
    stored = factory.ddb.items[(_SCOPE, f"WORKER#{rollback_provenance}")]
    stored["checked_at"] = {"N": str(after_cutover + 1)}
    stored["expires_at"] = {"N": str(after_cutover + 301)}
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
        )
    gate.prepare_cleanup(
        domain="mail_action",
        candidate_definitions=candidates,
        worker_env=worker_env,
        worker_rollback_env=worker_rollback_env,
        worker_artifact=worker_artifact,
        worker_rollback_artifact=worker_rollback_artifact,
    )
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
    gate.terraform_pre_register(
        task="mcp",
        definition=candidates["mcp"],
        mode="cleanup",
    )
    with pytest.raises(RolloutGateError, match="cleanup_mode_required"):
        gate.pre_update(
            task="mcp",
            task_definition=_TASK_ARNS["mcp_cleanup"],
            mode="candidate",
        )
    gate.pre_update(
        task="mcp",
        task_definition=_TASK_ARNS["mcp_cleanup"],
        mode="cleanup",
    )
    factory.ecs.current["teamagent-dev-mcp"] = _TASK_ARNS["mcp_cleanup"]
    factory.events.morning = _TASK_ARNS["morning_cleanup"]
    factory.ecs.running_task_definition["teamagent-dev-mcp"] = _TASK_ARNS["mcp_cleanup"]

    gate.pre_worker_upload(
        artifact=worker_artifact,
        rollback_artifact=worker_rollback_artifact,
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
                configs=gate._cleanup_proposed_from_live(cleanup),
            )
        },
        "loaded_domains": {"SS": ["mail_action", "report_link"]},
        "checked_at": {"N": str(retirement_now)},
        "expires_at": {"N": str(retirement_now + 300)},
    }
    factory.ddb.items[(_SCOPE, f"WORKER#{worker_provenance}")] = attestation
    gate.pre_restart(
        rollback_artifact=worker_rollback_artifact,
        mode="cleanup",
    )
    stored_attestation = factory.ddb.items[(_SCOPE, f"WORKER#{worker_provenance}")]
    stored_attestation["checked_at"] = {"N": str(retirement_now + 1)}
    stored_attestation["expires_at"] = {"N": str(retirement_now + 301)}
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
    gate = LiveRolloutGate(
        control=load_control(control),
        manifest=manifest,
        clients=factory,
    )
    monkeypatch.setattr(rollout_gate_module, "_trusted_epoch", lambda _response: _NOW)

    gate.initialize()

    for domain, expected_primary, expected_previous in (
        ("mail_action", next_mail, _MAIL_GENERATION),
        ("report_link", next_report, _REPORT_GENERATION),
    ):
        item = factory.ddb.items[(_SCOPE, f"DOMAIN#{domain}")]
        assert item["rotation_epoch"] == {"S": next_epoch}
        assert item["primary_generation"] == {"S": expected_primary}
        assert item["previous_generation"] == {"S": expected_previous}
        assert item["high_water"] == {"N": str(prior_high_water)}
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
) -> None:
    monkeypatch.setenv("HMAC_GATE_ENABLED", "true")
    monkeypatch.setenv("HMAC_PREFLIGHT_MANIFEST", "/protected/manifest.json")
    monkeypatch.setenv("HMAC_ROLLOUT_CONTROL", "/protected/control.json")
    monkeypatch.setenv("HMAC_GATE_TASK", "mcp")
    monkeypatch.setenv("HMAC_GATE_CANDIDATE_JSON", "{}")

    def explode(_path: str) -> dict[str, object]:
        raise RuntimeError("sensitive-client-detail")

    monkeypatch.setattr(terraform_gate_module, "_load_mapping", explode)
    assert terraform_gate_module.main() == 2

    captured = capsys.readouterr()
    assert captured.out == '{"code":"gate_client_error","ok":false}\n'
    assert captured.err == ""
