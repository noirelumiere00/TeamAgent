from __future__ import annotations

import copy
import hashlib
import json
import threading
from email.utils import formatdate
from pathlib import Path
from typing import Any

import pytest

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
_DB_VERSION = "d" * 32
_MAIL_VERSION = "m" * 32
_REPORT_VERSION = "r" * 32
_DB_GENERATION = f"{_DB_ARN}@{_DB_VERSION}"
_MAIL_GENERATION = f"{_MAIL_ARN}@{_MAIL_VERSION}"
_REPORT_GENERATION = f"{_REPORT_ARN}@{_REPORT_VERSION}"
_EPOCH = "hmac-2026-07-18"
_TABLE = "teamagent-dev-hmac-state"
_SCOPE = "teamagent/dev"
_PROVENANCE = {
    "mcp": "",
    "connect_web": "",
    "morning_digest": "",
    "worker": "",
    "worker_rollback": "",
}
_TASK_ARNS = {
    "mcp_old": "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-mcp:55",
    "mcp_new": "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-mcp:56",
    "connect_old": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-connect-web:53"
    ),
    "connect_new": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-connect-web:54"
    ),
    "morning_old": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-morning-digest:44"
    ),
    "morning_new": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-morning-digest:45"
    ),
    "canary": (
        "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/teamagent-dev-connect-canary:14"
    ),
}
_IMAGE = "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent@sha256:" + "1" * 64


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
            mail=_MAIL_GENERATION,
            report=_REPORT_GENERATION,
        ),
        "connect_web": _provenance(
            workload="connect_web",
            image=_IMAGE,
            rotation_epoch=_EPOCH,
            report=_REPORT_GENERATION,
        ),
        "morning_digest": _provenance(
            workload="morning_digest",
            image=_IMAGE,
            rotation_epoch=_EPOCH,
            mail=_MAIL_GENERATION,
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
        secrets = [{"name": "MAIL_ACTION_HMAC_SECRET", "valueFrom": _DB_ARN}]
    else:
        secrets = [{"name": "MAIL_ACTION_HMAC_SECRET", "valueFrom": _DB_ARN}]
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
        mail=_MAIL_GENERATION,
        report=_REPORT_GENERATION,
    )
    worker_rollback_provenance = _provenance(
        workload="worker",
        artifact=rollback_hash,
        rotation_epoch=_EPOCH,
        mail=_MAIL_GENERATION,
        report=_REPORT_GENERATION,
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
                "provenance": _PROVENANCE["mcp"],
                "rollback_provenance": _PROVENANCE["mcp"],
                "rollback_task_definition": _TASK_ARNS["mcp_new"],
                "rollback_image": _IMAGE,
            },
            "connect_web": {
                "cluster": "teamagent-dev",
                "service": "teamagent-dev-connect-web",
                "provenance": _PROVENANCE["connect_web"],
                "rollback_provenance": _PROVENANCE["connect_web"],
                "rollback_task_definition": _TASK_ARNS["connect_new"],
                "rollback_image": _IMAGE,
            },
        },
        "morning_digest": {
            "rule": "teamagent-dev-morning-digest",
            "target_id": "morning",
            "provenance": _PROVENANCE["morning_digest"],
            "rollback_provenance": _PROVENANCE["morning_digest"],
            "rollback_task_definition": _TASK_ARNS["morning_new"],
            "rollback_image": _IMAGE,
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
        }

    def describe_services(self, **kwargs: object) -> dict[str, object]:
        service_name = kwargs["services"][0]  # type: ignore[index]
        task_definition = self.current[str(service_name)]
        return _response(
            services=[
                {
                    "taskDefinition": task_definition,
                    "desiredCount": 1,
                    "runningCount": 1,
                    "pendingCount": 0,
                    "deployments": [{"rolloutState": "COMPLETED"}],
                }
            ],
            failures=[],
        )

    def describe_task_definition(self, **kwargs: object) -> dict[str, object]:
        return _response(
            taskDefinition=copy.deepcopy(self.definitions[str(kwargs["taskDefinition"])])
        )

    def list_tasks(self, **_kwargs: object) -> dict[str, object]:
        return _response(taskArns=["arn:aws:ecs:region:account:task/one"])

    def describe_tasks(self, **kwargs: object) -> dict[str, object]:
        service = str(kwargs["cluster"])
        task_definition = (
            self.current["teamagent-dev-mcp"]
            if service == "teamagent-dev"
            else self.current["teamagent-dev-connect-web"]
        )
        return _response(tasks=[{"taskDefinitionArn": task_definition, "lastStatus": "RUNNING"}])


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
                    "EcsParameters": {"TaskDefinitionArn": self.morning},
                }
            ]
        )


class _FakeSecrets:
    def list_secret_version_ids(self, **kwargs: object) -> dict[str, object]:
        versions = {
            _DB_ARN: _DB_VERSION,
            _MAIL_ARN: _MAIL_VERSION,
            _REPORT_ARN: _REPORT_VERSION,
        }
        return _response(
            Versions=[
                {
                    "VersionId": versions[str(kwargs["SecretId"])],
                    "VersionStages": ["AWSCURRENT"],
                }
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
            for operation in transaction:
                if "Put" in operation:
                    item = copy.deepcopy(operation["Put"]["Item"])
                    key = (item["scope"]["S"], item["record"]["S"])
                    if key in self.items:
                        raise RuntimeError("conditional")
                    self.items[key] = item
                elif "Update" in operation:
                    update = operation["Update"]
                    key = (update["Key"]["scope"]["S"], update["Key"]["record"]["S"])
                    item = self.items[key]
                    values = update["ExpressionAttributeValues"]
                    if key[1].startswith("LEDGER#"):
                        item["stage"] = copy.deepcopy(values[":next"])
                        item["updated_at"] = copy.deepcopy(values[":now"])
                    else:
                        item["stage"] = copy.deepcopy(values[":stage"])
                        if ":issuers" in values:
                            item["issuer_provenances"] = copy.deepcopy(values[":issuers"])
                    item["revision"] = {"N": str(int(item["revision"]["N"]) + 1)}
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
    assert ledger["stage"] == {"S": "worker_verified"}


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
    assert ledger["stage"] == {"S": "mcp_stable_and_old_drained"}


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
