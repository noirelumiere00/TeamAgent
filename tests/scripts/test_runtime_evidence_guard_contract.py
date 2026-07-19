"""Adversarial unit tests for runtime evidence receipts and file bindings."""

from __future__ import annotations

import copy
import datetime as dt
import email.utils
import hashlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = PROJECT_ROOT / "infra/deploy/runtime_evidence_guard.py"
SPEC = importlib.util.spec_from_file_location("runtime_evidence_guard", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
evidence = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = evidence
SPEC.loader.exec_module(evidence)

HEX = "a" * 64
KEY_ARN = (
    "arn:aws:kms:ap-northeast-1:718959508629:key/"
    "12345678-1234-1234-1234-123456789abc"
)
SUBSCRIPTION_ARN = (
    f"{evidence.CANONICAL_TOPIC}:12345678-1234-1234-1234-123456789abc"
)


def _rehash_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    workflow.pop("workflow_sha256", None)
    workflow["workflow_sha256"] = evidence.canonical_sha256(workflow)
    return workflow


def _aws_date(epoch: int) -> str:
    return email.utils.format_datetime(
        dt.datetime.fromtimestamp(epoch, tz=dt.UTC),
        usegmt=True,
    )


def _quiescence(observed_at: int) -> dict[str, Any]:
    return {
        "inventory_sha256": HEX,
        "raw_reference_set_sha256": "b" * 64,
        "eventbridge_all_disabled": True,
        "scheduler_all_disabled": True,
        "lambda_mappings_all_disabled": True,
        "writer_controls": {
            "eventbridge": ["default/teamagent-rule"],
            "scheduler": ["default/teamagent-schedule"],
            "lambda_mappings": ["12345678-1234-1234-1234-123456789abc"],
        },
        "ecs_families": {
            family: {
                "cluster": evidence.WRITER_FAMILY_CLUSTERS[family],
                "running": 0,
                "pending": 0,
            }
            for family in evidence.WRITER_FAMILIES
        },
        "ecs_services": {
            service: {
                "status": "ACTIVE",
                "desired": 0,
                "running": 0,
                "pending": 0,
            }
            for service in evidence.WRITER_SERVICES
        },
        "queues": {
            (
                "arn:aws:sqs:ap-northeast-1:718959508629:"
                "teamagent-dev-tiktok-acquire-jobs"
            ): {
                "ApproximateNumberOfMessages": 0,
                "ApproximateNumberOfMessagesNotVisible": 0,
                "ApproximateNumberOfMessagesDelayed": 0,
            }
        },
        "log_producers": {
            "cloudtrail": {
                "trail_name": "teamagent-dev-trail",
                "bucket": "teamagent-dev-cloudtrail-718959508629",
                "identity": {
                    "trail_name": "teamagent-dev-trail",
                    "trail_arn": (
                        "arn:aws:cloudtrail:ap-northeast-1:718959508629:"
                        "trail/teamagent-dev-trail"
                    ),
                    "home_region": evidence.REGION,
                    "bucket": "teamagent-dev-cloudtrail-718959508629",
                    "is_multi_region": True,
                    "include_global_service_events": True,
                    "log_file_validation_enabled": True,
                    "kms_key_arn": KEY_ARN,
                    "is_organization_trail": False,
                },
                "is_logging": False,
                "trail_response_sha256": "c" * 64,
                "status_response_sha256": "d" * 64,
            },
            "bedrock": {
                "configured": False,
                "logging_config_sha256": evidence.canonical_sha256(None),
            },
        },
        "observed_at_epoch": observed_at,
    }


def test_cloudtrail_identity_requires_exact_trail_configuration() -> None:
    trail = {
        "Trail": {
            "Name": "teamagent-dev-trail",
            "S3BucketName": "teamagent-dev-cloudtrail-718959508629",
            "TrailARN": (
                "arn:aws:cloudtrail:ap-northeast-1:718959508629:"
                "trail/teamagent-dev-trail"
            ),
            "HomeRegion": evidence.REGION,
            "IsMultiRegionTrail": True,
            "IncludeGlobalServiceEvents": True,
            "LogFileValidationEnabled": True,
            "IsOrganizationTrail": False,
            "KmsKeyId": KEY_ARN,
        }
    }
    identity = evidence._cloudtrail_identity_contract(trail)
    assert identity["trail_arn"] == trail["Trail"]["TrailARN"]
    assert identity["kms_key_arn"] == KEY_ARN

    for field, value in (
        ("HomeRegion", "us-east-1"),
        ("IsMultiRegionTrail", False),
        ("IncludeGlobalServiceEvents", False),
        ("LogFileValidationEnabled", False),
        ("S3KeyPrefix", "unexpected"),
    ):
        mutated = copy.deepcopy(trail)
        mutated["Trail"][field] = value
        with pytest.raises(evidence.ContractError):
            evidence._cloudtrail_identity_contract(mutated)


@pytest.mark.parametrize(
    ("family", "expected_cluster"),
    [
        ("teamagent-dev-ingest", "teamagent-dev"),
        ("teamagent-dev-tiktok-acquire", "teamagent-dev-tiktok"),
        ("teamagent-dev-x-buzz-worker", "teamagent-dev"),
    ],
)
def test_writer_family_task_inventory_uses_exact_cluster_and_all_pages(
    family: str,
    expected_cluster: str,
) -> None:
    calls: list[tuple[str, str, tuple[str, ...], dict[str, str]]] = []

    class FakeAws:
        def pages(
            self,
            service: str,
            operation: str,
            arguments: tuple[str, ...],
            **pagination: str,
        ) -> list[tuple[dict[str, Any], Any]]:
            calls.append((service, operation, arguments, pagination))
            return [
                (
                    {"taskArns": []},
                    evidence.HttpEvidence(_aws_date(100), 100, "request-id"),
                )
            ]

    tasks, observed_at = evidence._list_family_tasks(
        FakeAws(),
        family,
        "RUNNING",
    )

    assert tasks == []
    assert observed_at == 100
    assert calls == [
        (
            "ecs",
            "list-tasks",
            (
                "--cluster",
                expected_cluster,
                "--family",
                family,
                "--desired-status",
                "RUNNING",
            ),
            {
                "token_field": "nextToken",
                "token_argument": "--next-token",
            },
        )
    ]


def test_unknown_writer_family_has_no_cluster_fallback() -> None:
    with pytest.raises(evidence.ContractError):
        evidence._list_family_tasks(object(), "teamagent-dev-unknown", "RUNNING")


def _versioning_workflow() -> dict[str, Any]:
    quiescence = _quiescence(130)
    action_pairs = [
        ("bedrock.DeleteModelInvocationLoggingConfiguration", "account"),
        ("cloudtrail.StopLogging", "teamagent-dev-trail"),
        ("ecs.UpdateService", "teamagent-dev-connect-web"),
        ("ecs.UpdateService", "teamagent-dev-mcp"),
        ("ecs.UpdateService", "teamagent-dev-openclaw"),
        ("eventbridge.DisableRule", "default/teamagent-rule"),
        (
            "lambda.UpdateEventSourceMapping",
            "12345678-1234-1234-1234-123456789abc",
        ),
        ("scheduler.UpdateSchedule", "default/teamagent-schedule"),
    ]
    actions = [
        {
            "kind": kind,
            "resource_id": resource_id,
            "response_sha256": "e" * 64,
            "request_id_sha256": "f" * 64,
            "aws_date_epoch": 120,
        }
        for kind, resource_id in action_pairs
    ]
    requirements = [
        {"kind": action["kind"], "resource_id": action["resource_id"]}
        for action in actions
    ]
    baseline = {"cloudtrail": "1" * 64, "bedrock": "2" * 64}
    enabled = {"status": "Enabled", "mfa_delete": "Disabled"}
    unversioned = {"status": "Unversioned", "mfa_delete": "Disabled"}

    def bucket_before(label: str) -> dict[str, Any]:
        name = (
            "teamagent-dev-cloudtrail-718959508629"
            if label == "cloudtrail"
            else "teamagent-dev-bedrock-logs-718959508629"
        )
        return {
            "identity": {
                "name": name,
                "arn": f"arn:aws:s3:::{name}",
                "owner_canonical_id": "owner-canonical-id",
                "creation_date": "2026-07-01T00:00:00Z",
            },
            "identity_observed_at_epoch": 90,
            "versioning": unversioned,
            "versioning_observed_at_epoch": 91,
            "post_quiescence_identity_observed_at_epoch": 131,
            "post_quiescence_versioning_observed_at_epoch": 132,
            "object_versions_observed_at_epoch": 133,
        }

    def enablement(label: str) -> dict[str, Any]:
        bucket = (
            "teamagent-dev-cloudtrail-718959508629"
            if label == "cloudtrail"
            else "teamagent-dev-bedrock-logs-718959508629"
        )
        return {
            "bucket": bucket,
            "action": "PutBucketVersioning",
            "requested_status": "Enabled",
            "response_sha256": "3" * 64,
            "request_id_sha256": "4" * 64,
            "response_date": _aws_date(134),
            "event_time_epoch": 134,
            "first_seen_enabled_epoch": 135,
            "timestamp_source": "aws-http-response-date",
            "error_code_present": False,
            "error_message_present": False,
            "addendum_present": False,
        }

    def observation(sequence: int, quiescence_at: int, observed_at: int) -> dict[str, Any]:
        observed_quiescence = _quiescence(quiescence_at)
        return {
            "sequence": sequence,
            "observed_at_epoch": observed_at,
            "quiescence": observed_quiescence,
            "quiescence_sha256": evidence.canonical_sha256(observed_quiescence),
            "object_versions_sha256": baseline,
            "versioning": {"cloudtrail": enabled, "bedrock": enabled},
        }

    bedrock_configuration = {
        "textDataDeliveryEnabled": True,
        "embeddingDataDeliveryEnabled": True,
        "imageDataDeliveryEnabled": False,
        "videoDataDeliveryEnabled": False,
        "s3Config": {
            "bucketName": "teamagent-dev-bedrock-logs-718959508629",
            "keyPrefix": "bedrock/",
        },
    }
    final_quiescence = _quiescence(1044)
    workflow = {
        "kind": "teamagent-first-time-versioning-cutover",
        "schema_version": 1,
        "account_id": evidence.ACCOUNT_ID,
        "region": evidence.REGION,
        "shared_lock_id": evidence.SHARED_LOCK_RECORD_ID,
        "shared_lock": {
            "record_id": evidence.SHARED_LOCK_RECORD_ID,
            "workflow_id": "12345678-1234-4abc-8abc-123456789abc",
            "acquired_at_epoch": 80,
            "lease_expires_at": 2000,
            "lock_receipt_sha256": "5" * 64,
            "initial_verification_epoch": 100,
            "pre_cutover_verification_epoch": 1045,
            "post_cutover_verification_epoch": 1047,
        },
        "aws_executable": {
            "path": "/usr/local/bin/aws",
            "device": 1,
            "inode": 2,
            "size": 3,
            "sha256": "6" * 64,
            "version": "aws-cli/2.27.0 Python/3.13",
        },
        "endpoints": evidence.ENDPOINTS,
        "caller_identity": {
            "UserId": "AROATEST:teamagent-terraform-worker",
            "Account": evidence.ACCOUNT_ID,
            "Arn": evidence.AUTOMATION_ARN,
        },
        "caller_identity_sha256": "",
        "caller_identity_observed_at_epoch": 100,
        "producer_disconnect": {
            "event_time_epoch": 120,
            "actions": actions,
            "action_set_sha256": evidence.canonical_sha256(actions),
            "action_requirements": requirements,
            "action_requirements_sha256": evidence.canonical_sha256(requirements),
            "quiescence": quiescence,
            "quiescence_sha256": evidence.canonical_sha256(quiescence),
        },
        "buckets_before": {
            "cloudtrail": bucket_before("cloudtrail"),
            "bedrock": bucket_before("bedrock"),
        },
        "versioning_enablements": {
            "cloudtrail": enablement("cloudtrail"),
            "bedrock": enablement("bedrock"),
        },
        "settle_seconds": 900,
        "not_before_epoch": 1035,
        "no_write_baseline_sha256": baseline,
        "post_settle_observations": [
            observation(1, 1040, 1041),
            observation(2, 1042, 1043),
        ],
        "final_recheck": {
            "observed_at_epoch": 1045,
            "quiescence": final_quiescence,
            "quiescence_sha256": evidence.canonical_sha256(final_quiescence),
            "object_versions_sha256": baseline,
            "versioning": {"cloudtrail": enabled, "bedrock": enabled},
        },
        "cutover": {
            "cloudtrail": {
                "action": "StartLogging",
                "response_sha256": "7" * 64,
                "request_id_sha256": "8" * 64,
                "response_date_epoch": 1046,
            },
            "bedrock": {
                "action": "PutModelInvocationLoggingConfiguration",
                "response_sha256": "9" * 64,
                "request_id_sha256": "a" * 64,
                "response_date_epoch": 1046,
                "configuration": bedrock_configuration,
                "configuration_sha256": evidence.canonical_sha256(
                    bedrock_configuration
                ),
            },
        },
    }
    workflow["caller_identity_sha256"] = evidence.canonical_sha256(
        workflow["caller_identity"]
    )
    workflow_claims = copy.deepcopy(workflow)
    recorded_at = 1048
    ledger_item = evidence._versioning_ledger_item(
        workflow_claims,
        recorded_at_epoch=recorded_at,
    )
    workflow["durable_ledger"] = {
        "record_id": (
            f"{evidence.VERSIONING_LEDGER_RECORD_PREFIX}"
            f"{workflow['shared_lock']['workflow_id']}"
        ),
        "record_type": "teamagent.first-time-versioning-cutover",
        "workflow_claims_sha256": evidence.canonical_sha256(workflow_claims),
        "item_sha256": evidence.canonical_sha256(ledger_item),
        "recorded_at_epoch": recorded_at,
        "audit_expires_at": recorded_at + 31536000,
        "put_response_sha256": "b" * 64,
        "put_request_id_sha256": "c" * 64,
        "put_aws_date_epoch": 1049,
        "confirmation_response_sha256": "d" * 64,
        "confirmation_request_id_sha256": "e" * 64,
        "confirmed_at_epoch": 1050,
        "final_observed_at_epoch": 1051,
        "final_observation_request_id_sha256": "f" * 64,
        "shared_lock_verified_at_epoch": 1052,
    }
    return _rehash_workflow(workflow)


def test_versioning_workflow_binds_first_time_quiescence_and_cutover() -> None:
    evidence.validate_versioning_workflow(_versioning_workflow())


@pytest.mark.parametrize(
    "mutation",
    [
        "later_enabled",
        "missing_disconnect",
        "active_task",
        "cloudtrail_on",
        "short_settle",
        "equal_observations",
        "final_write",
        "early_cutover",
        "wrong_bedrock",
        "missing_ledger",
        "ledger_hash",
        "ledger_time_inversion",
    ],
)
def test_versioning_workflow_rejects_mutation_and_time_inversion(
    mutation: str,
) -> None:
    workflow = _versioning_workflow()
    if mutation == "later_enabled":
        workflow["buckets_before"]["cloudtrail"]["versioning"]["status"] = "Enabled"
    elif mutation == "missing_disconnect":
        workflow["producer_disconnect"]["actions"].pop()
        workflow["producer_disconnect"]["action_requirements"].pop()
        workflow["producer_disconnect"]["action_set_sha256"] = evidence.canonical_sha256(
            workflow["producer_disconnect"]["actions"]
        )
        workflow["producer_disconnect"][
            "action_requirements_sha256"
        ] = evidence.canonical_sha256(
            workflow["producer_disconnect"]["action_requirements"]
        )
    elif mutation == "active_task":
        family = evidence.WRITER_FAMILIES[0]
        workflow["producer_disconnect"]["quiescence"]["ecs_families"][family][
            "running"
        ] = 1
        workflow["producer_disconnect"][
            "quiescence_sha256"
        ] = evidence.canonical_sha256(
            workflow["producer_disconnect"]["quiescence"]
        )
    elif mutation == "cloudtrail_on":
        workflow["producer_disconnect"]["quiescence"]["log_producers"][
            "cloudtrail"
        ]["is_logging"] = True
        workflow["producer_disconnect"][
            "quiescence_sha256"
        ] = evidence.canonical_sha256(
            workflow["producer_disconnect"]["quiescence"]
        )
    elif mutation == "short_settle":
        workflow["not_before_epoch"] -= 1
    elif mutation == "equal_observations":
        second = workflow["post_settle_observations"][1]
        second["observed_at_epoch"] = workflow["post_settle_observations"][0][
            "observed_at_epoch"
        ]
    elif mutation == "final_write":
        workflow["final_recheck"]["object_versions_sha256"] = {
            **workflow["final_recheck"]["object_versions_sha256"],
            "bedrock": "f" * 64,
        }
    elif mutation == "early_cutover":
        workflow["cutover"]["cloudtrail"]["response_date_epoch"] = 1044
    elif mutation == "wrong_bedrock":
        workflow["cutover"]["bedrock"]["configuration"]["s3Config"][
            "keyPrefix"
        ] = "other/"
        workflow["cutover"]["bedrock"][
            "configuration_sha256"
        ] = evidence.canonical_sha256(
            workflow["cutover"]["bedrock"]["configuration"]
        )
    elif mutation == "missing_ledger":
        del workflow["durable_ledger"]
    elif mutation == "ledger_hash":
        workflow["durable_ledger"]["item_sha256"] = "0" * 64
    elif mutation == "ledger_time_inversion":
        workflow["durable_ledger"]["confirmed_at_epoch"] = 1048
    _rehash_workflow(workflow)
    with pytest.raises(evidence.ContractError):
        evidence.validate_versioning_workflow(workflow)


def test_versioning_workflow_accepts_fresh_inventory_evidence_hashes() -> None:
    workflow = _versioning_workflow()
    for index, inventory_hash in enumerate(("1" * 64, "2" * 64), 1):
        observation = workflow["post_settle_observations"][index - 1]
        observation["quiescence"]["inventory_sha256"] = inventory_hash
        observation["quiescence_sha256"] = evidence.canonical_sha256(
            observation["quiescence"]
        )
    workflow["final_recheck"]["quiescence"]["inventory_sha256"] = "3" * 64
    workflow["final_recheck"]["quiescence_sha256"] = evidence.canonical_sha256(
        workflow["final_recheck"]["quiescence"]
    )
    workflow_claims = copy.deepcopy(workflow)
    del workflow_claims["durable_ledger"]
    del workflow_claims["workflow_sha256"]
    recorded_at = workflow["durable_ledger"]["recorded_at_epoch"]
    workflow["durable_ledger"]["workflow_claims_sha256"] = (
        evidence.canonical_sha256(workflow_claims)
    )
    workflow["durable_ledger"]["item_sha256"] = evidence.canonical_sha256(
        evidence._versioning_ledger_item(
            workflow_claims,
            recorded_at_epoch=recorded_at,
        )
    )
    _rehash_workflow(workflow)

    evidence.validate_versioning_workflow(workflow)


def _bound_file(path: Path, payload: bytes = b"exact-export") -> dict[str, Any]:
    path.write_bytes(payload)
    path.chmod(0o600)
    return evidence._local_file_binding(path)


def test_export_binding_rejects_byte_inode_rename_size_and_hardlink_mutation(
    tmp_path: Path,
) -> None:
    original = tmp_path / "export.bin"
    binding = _bound_file(original)
    evidence.verify_file_binding(binding)

    original.write_bytes(b"mutated-byte")
    with pytest.raises(evidence.ContractError):
        evidence.verify_file_binding(binding)

    original.unlink()
    _bound_file(original)
    with pytest.raises(evidence.ContractError):
        evidence.verify_file_binding(binding)

    binding = evidence._local_file_binding(original)
    moved = tmp_path / "moved.bin"
    original.rename(moved)
    _bound_file(original)
    with pytest.raises(evidence.ContractError):
        evidence.verify_file_binding(binding)

    original.unlink()
    moved.rename(original)
    binding = evidence._local_file_binding(original)
    hardlink = tmp_path / "hardlink.bin"
    os.link(original, hardlink)
    with pytest.raises(evidence.ContractError):
        evidence.verify_file_binding(binding)

    hardlink.unlink()
    binding = evidence._local_file_binding(original)
    original.write_bytes(b"longer-exact-export")
    with pytest.raises(evidence.ContractError):
        evidence.verify_file_binding(binding)


def test_fresh_export_is_read_write_exclusive_and_no_follow(tmp_path: Path) -> None:
    path = tmp_path / "fresh.bin"
    fd = evidence._fresh_output(path)
    try:
        os.write(fd, b"payload")
        os.lseek(fd, 0, os.SEEK_SET)
        assert os.read(fd, 7) == b"payload"
    finally:
        os.close(fd)
    with pytest.raises(FileExistsError):
        evidence._fresh_output(path)
    link = tmp_path / "link.bin"
    link.symlink_to(path)
    with pytest.raises(FileExistsError):
        evidence._fresh_output(link)


class _ExportAws:
    def __init__(self, *, get_version: str = "version-1") -> None:
        self.get_version = get_version

    def call(
        self,
        service: str,
        operation: str,
        arguments: Any = (),
        *,
        output_fd: int | None = None,
    ) -> tuple[dict[str, Any], Any]:
        assert "--expected-bucket-owner" in arguments or service == "sts"
        if operation == "head-object":
            return (
                {
                    "VersionId": "version-1",
                    "ContentLength": 7,
                    "LastModified": "1970-01-01T00:01:30Z",
                    "ETag": '"321c3cf486ed509164edec1e1981fec8"',
                    "ChecksumSHA256": "YXNkZg==",
                },
                evidence.HttpEvidence(_aws_date(100), 100, "request-head"),
            )
        if operation == "get-object":
            assert output_fd is not None
            os.write(output_fd, b"payload")
            return (
                {
                    "VersionId": self.get_version,
                    "ContentLength": 7,
                    "LastModified": "1970-01-01T00:01:30Z",
                    "ETag": '"321c3cf486ed509164edec1e1981fec8"',
                    "ChecksumSHA256": "YXNkZg==",
                },
                evidence.HttpEvidence(_aws_date(101), 101, "request-get"),
            )
        if service == "sts" and operation == "get-caller-identity":
            return (
                {
                    "UserId": "AROATEST:teamagent-terraform-worker",
                    "Account": evidence.ACCOUNT_ID,
                    "Arn": evidence.AUTOMATION_ARN,
                },
                evidence.HttpEvidence(_aws_date(102), 102, "request-observe"),
            )
        raise AssertionError((service, operation))


def test_exact_s3_export_binds_version_metadata_file_and_time(tmp_path: Path) -> None:
    binding = evidence.fetch_exact_s3_export(
        _ExportAws(),
        bucket="teamagent-dev-raw-files",
        key="cloudwatch-logs-export/log.json",
        version_id="version-1",
        output_path=tmp_path / "exact.bin",
        observation_epoch=99,
    )
    assert binding["file"]["content_sha256"] == hashlib.sha256(b"payload").hexdigest()
    assert binding["file"]["acquisition_identity_before"]["size"] == 0
    assert (
        binding["file"]["acquisition_identity_before"]["inode"]
        == binding["file"]["identity"]["inode"]
    )
    assert binding["s3"]["checksums"] == {"ChecksumSHA256": "YXNkZg=="}

    with pytest.raises(evidence.ContractError):
        evidence.fetch_exact_s3_export(
            _ExportAws(get_version="version-2"),
            bucket="teamagent-dev-raw-files",
            key="cloudwatch-logs-export/log.json",
            version_id="version-1",
            output_path=tmp_path / "wrong-version.bin",
            observation_epoch=99,
        )

    inverted = copy.deepcopy(binding)
    inverted["s3"]["last_modified_epoch"] = inverted["observed_at_epoch"] + 1
    verify_dir = tmp_path / "verify"
    verify_dir.mkdir(mode=0o700)
    missing_pre_download_identity = copy.deepcopy(binding)
    del missing_pre_download_identity["file"]["acquisition_identity_before"]
    with pytest.raises(evidence.ContractError):
        evidence.verify_exact_s3_export(
            _ExportAws(),
            binding=missing_pre_download_identity,
            fresh_directory=verify_dir,
        )

    mutated_pre_download_identity = copy.deepcopy(binding)
    mutated_pre_download_identity["file"]["acquisition_identity_before"][
        "inode"
    ] += 1
    with pytest.raises(evidence.ContractError):
        evidence.verify_exact_s3_export(
            _ExportAws(),
            binding=mutated_pre_download_identity,
            fresh_directory=verify_dir,
        )

    with pytest.raises(evidence.ContractError):
        evidence.verify_exact_s3_export(
            _ExportAws(),
            binding=inverted,
            fresh_directory=verify_dir,
        )


def _inventory_contract() -> dict[str, Any]:
    attributes = {
        "ConfirmationWasAuthenticated": "true",
        "Endpoint": evidence.APPROVED_EMAIL,
        "Owner": evidence.ACCOUNT_ID,
        "PendingConfirmation": "false",
        "Protocol": "email",
        "RawMessageDelivery": "false",
        "SubscriptionArn": SUBSCRIPTION_ARN,
        "TopicArn": evidence.CANONICAL_TOPIC,
    }
    return {
        "references": [],
        "publisher_references": [],
        "publishers": [],
        "destination": {
            "chatbot_configuration_arns": [],
            "subscription": {
                "endpoint": evidence.APPROVED_EMAIL,
                "filter_policy_present": False,
                "protocol": "email",
                "raw_message_delivery": False,
                "state": "confirmed",
            },
            "topic_arn": evidence.CANONICAL_TOPIC,
        },
        "subscription_metadata": {
            "endpoint": evidence.APPROVED_EMAIL,
            "owner": evidence.ACCOUNT_ID,
            "protocol": "email",
            "subscription_arn": SUBSCRIPTION_ARN,
            "topic_arn": evidence.CANONICAL_TOPIC,
            "attributes": attributes,
        },
        "topic_inventory": [evidence.CANONICAL_TOPIC],
        "alarm_subscription_count": 1,
        "publisher_coverage": sorted(evidence.KNOWN_SNS_PUBLISHER_TYPES),
        "source_pages": [
            {
                "source_type": "sns.topic",
                "source_id": "account",
                "page": 0,
                "response_sha256": "1" * 64,
                "aws_date_epoch": 199,
                "request_id_sha256": "2" * 64,
            }
        ],
    }


def _key_metadata() -> dict[str, Any]:
    return {
        "AWSAccountId": evidence.ACCOUNT_ID,
        "Arn": KEY_ARN,
        "KeyUsage": "SIGN_VERIFY",
        "KeySpec": "ECC_NIST_P256",
        "KeyState": "Enabled",
        "Enabled": True,
        "KeyManager": "CUSTOMER",
        "Origin": "AWS_KMS",
        "MultiRegion": False,
        "SigningAlgorithms": ["ECDSA_SHA_256"],
    }


def _challenge() -> dict[str, Any]:
    inventory = _inventory_contract()
    metadata = _key_metadata()
    challenge = {
        "kind": "teamagent-sns-delivery-challenge",
        "schema_version": 1,
        "challenge_id": "12345678-1234-4abc-8abc-123456789abc",
        "ledger_record_id": "sns-challenge#12345678-1234-4abc-8abc-123456789abc",
        "topic_arn": evidence.CANONICAL_TOPIC,
        "message_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "message_id_sha256": hashlib.sha256(
            b"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        ).hexdigest(),
        "challenge_nonce": "3" * 64,
        "challenge_nonce_sha256": hashlib.sha256(("3" * 64).encode()).hexdigest(),
        "published_at_epoch": 200,
        "expires_at_epoch": 1200,
        "publish_request_id_sha256": "4" * 64,
        "ledger_request_id_sha256": "5" * 64,
        "ledger_response_sha256": "6" * 64,
        "ledger_aws_date_epoch": 201,
        "observed_at_epoch": 202,
        "observation_request_id_sha256": "7" * 64,
        "aws_executable": {
            "path": "/usr/local/bin/aws",
            "device": 1,
            "inode": 2,
            "size": 3,
            "sha256": "9" * 64,
            "version": "aws-cli/2.27.0 Python/3.13",
        },
        "endpoints": evidence.ENDPOINTS,
        "caller_identity": {
            "UserId": "AROATEST:teamagent-terraform-worker",
            "Account": evidence.ACCOUNT_ID,
            "Arn": evidence.AUTOMATION_ARN,
        },
        "caller_identity_sha256": "",
        "caller_identity_observed_at_epoch": 198,
        "inventory_contract": inventory,
        "inventory_sha256": evidence.canonical_sha256(inventory),
        "destination_state_sha256": evidence.canonical_sha256(
            inventory["destination"]
        ),
        "subscription_metadata_sha256": evidence.canonical_sha256(
            inventory["subscription_metadata"]
        ),
        "raw_reference_set_sha256": evidence.canonical_sha256(
            inventory["references"]
        ),
        "ack_kms_key_arn": KEY_ARN,
        "ack_kms_key_metadata": metadata,
        "ack_kms_key_metadata_sha256": evidence.canonical_sha256(metadata),
        "ack_kms_key_request_id_sha256": "8" * 64,
    }
    challenge["caller_identity_sha256"] = evidence.canonical_sha256(
        challenge["caller_identity"]
    )
    challenge["challenge_sha256"] = evidence.canonical_sha256(challenge)
    return challenge


def _rehash_challenge(challenge: dict[str, Any]) -> None:
    challenge.pop("challenge_sha256", None)
    challenge["challenge_sha256"] = evidence.canonical_sha256(challenge)


def test_sns_challenge_rejects_arbitrary_hash_destination_and_time() -> None:
    evidence.validate_sns_challenge(_challenge())
    with pytest.raises(evidence.ContractError):
        evidence.validate_sns_challenge({"hash": "a" * 64})

    arbitrary = _challenge()
    arbitrary["inventory_sha256"] = "f" * 64
    _rehash_challenge(arbitrary)
    with pytest.raises(evidence.ContractError):
        evidence.validate_sns_challenge(arbitrary)

    normalized = _challenge()
    normalized["inventory_contract"]["destination"]["subscription"][
        "endpoint"
    ] = f" {evidence.APPROVED_EMAIL} "
    normalized["destination_state_sha256"] = evidence.canonical_sha256(
        normalized["inventory_contract"]["destination"]
    )
    normalized["inventory_sha256"] = evidence.canonical_sha256(
        normalized["inventory_contract"]
    )
    _rehash_challenge(normalized)
    with pytest.raises(evidence.ContractError):
        evidence.validate_sns_challenge(normalized)

    inverted = _challenge()
    inverted["inventory_contract"]["source_pages"][0]["aws_date_epoch"] = 201
    inverted["inventory_sha256"] = evidence.canonical_sha256(
        inverted["inventory_contract"]
    )
    _rehash_challenge(inverted)
    with pytest.raises(evidence.ContractError):
        evidence.validate_sns_challenge(inverted)


def test_recipient_ack_rejects_other_message_and_expiry_before_kms() -> None:
    challenge = _challenge()
    claims = {
        "kind": "teamagent-sns-recipient-ack",
        "schema_version": 1,
        "topic_arn": evidence.CANONICAL_TOPIC,
        "message_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "challenge_nonce": challenge["challenge_nonce"],
        "challenge_sha256": challenge["challenge_sha256"],
        "inventory_sha256": challenge["inventory_sha256"],
        "recipient_email": evidence.APPROVED_EMAIL,
        "received_at_epoch": 205,
        "expires_at_epoch": 300,
        "signer_principal_arn": (
            evidence.ACK_SIGNER_ARN_PREFIX + "recipient-session"
        ),
    }
    ack = {
        "kind": "teamagent-sns-recipient-signed-ack",
        "schema_version": 1,
        "claims": claims,
        "claims_sha256": evidence.canonical_sha256(claims),
        "kms_key_arn": KEY_ARN,
        "signature_base64": "c2lnbmF0dXJl",
        "sign_request_id_sha256": "9" * 64,
        "signed_at_epoch": 206,
    }
    with pytest.raises(evidence.ContractError):
        evidence.verify_recipient_ack(
            object(),
            challenge=challenge,
            ack=ack,
            now_epoch=207,
        )
    ack["claims"]["message_id"] = challenge["message_id"]
    ack["claims_sha256"] = evidence.canonical_sha256(ack["claims"])
    with pytest.raises(evidence.ContractError):
        evidence.verify_recipient_ack(
            object(),
            challenge=challenge,
            ack=ack,
            now_epoch=1200,
        )


def _publisher_state(*, dual: bool) -> dict[str, Any]:
    publisher_id = "cloudwatch.metric-alarm:alarm-a"
    topics = [evidence.CANONICAL_TOPIC]
    if dual:
        topics.append(evidence.LEGACY_TOPIC)
    return {
        publisher_id: {
            "source_type": "cloudwatch.metric-alarm",
            "source_id": "alarm-a",
            "topic_arns": sorted(topics),
        }
    }


def _checkpoint(
    phase: str,
    sequence: int,
    *,
    previous: dict[str, Any] | None,
    dual: bool,
    created_at: int,
) -> dict[str, Any]:
    publisher_id = (
        "cloudwatch.metric-alarm:alarm-a"
        if phase == "publisher_checkpoint"
        else ""
    )
    state = _publisher_state(dual=dual)
    ids = sorted(state)
    legacy_ids = ids if dual else []
    delivery = (
        {
            "verified": True,
            "inventory_sha256": HEX,
            "ledger_request_id_sha256": "b" * 64,
            "verified_at_epoch": created_at - 1,
        }
        if phase == "canonical_delivery_confirmed"
        else None
    )
    postcondition = {
        "phase": phase,
        "publisher_id": publisher_id,
        "inventory_sha256": HEX,
        "publisher_reference_set_sha256": "c" * 64,
        "publishers_sha256": "d" * 64,
        "publisher_ids": ids,
        "publisher_topic_state": state,
        "legacy_publisher_ids": legacy_ids,
        "canonical_publisher_ids": ids,
        "legacy_publisher_count": len(legacy_ids),
        "canonical_publisher_count": len(ids),
        "delivery_verification": delivery,
    }
    delivery_sha = "e" * 64 if delivery is not None else ""
    if phase == "dual_publish":
        rollback = {
            "mode": "hold-dual-until-legacy-delivery-verified",
            "automatic": False,
            "publisher_topic_state": state,
        }
    elif phase == "publisher_checkpoint":
        assert previous is not None
        rollback = {
            "mode": "restore-exact-publisher-checkpoint",
            "automatic": True,
            "publisher_topic_state": {
                publisher_id: previous["postcondition"]["publisher_topic_state"][
                    publisher_id
                ]
            },
        }
    elif phase in {"canonical_delivery_confirmed", "legacy_reference_zero"}:
        rollback = {
            "mode": "restore-all-durable-dual-publish",
            "automatic": True,
            "publisher_topic_state": _publisher_state(dual=True),
        }
    else:
        rollback = {
            "mode": "new-reviewed-migration-required",
            "automatic": False,
            "publisher_topic_state": {},
        }
    postcondition_sha = evidence.canonical_sha256(postcondition)
    checkpoint = {
        "kind": "teamagent-alarm-migration-checkpoint",
        "schema_version": 1,
        "migration_id": "2026-07-alarm-topic-consolidation-v1",
        "sequence": sequence,
        "phase": phase,
        "publisher_id": publisher_id,
        "idempotency_key": "",
        "delivery_receipt_sha256": delivery_sha,
        "inventory_sha256": HEX,
        "postcondition": postcondition,
        "postcondition_receipt_sha256": postcondition_sha,
        "rollback_plan": rollback,
        "previous_checkpoint_sha256": (
            evidence.canonical_sha256(previous) if previous is not None else ""
        ),
        "created_at_epoch": created_at,
    }
    checkpoint["idempotency_key"] = evidence.canonical_sha256(
        {
            "migration_id": checkpoint["migration_id"],
            "phase": phase,
            "publisher_id": publisher_id,
            "inventory_sha256": HEX,
            "postcondition_sha256": postcondition_sha,
            "delivery_receipt_sha256": delivery_sha,
        }
    )
    return checkpoint


def _checkpoint_chain() -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    for phase, dual, created_at in (
        ("dual_publish", True, 100),
        ("publisher_checkpoint", False, 101),
        ("canonical_delivery_confirmed", False, 102),
        ("legacy_reference_zero", False, 103),
        ("legacy_retired", False, 104),
    ):
        chain.append(
            _checkpoint(
                phase,
                len(chain) + 1,
                previous=chain[-1] if chain else None,
                dual=dual,
                created_at=created_at,
            )
        )
    return chain


def test_alarm_migration_checkpoint_chain_is_exact_and_resumable() -> None:
    previous = None
    for checkpoint in _checkpoint_chain():
        evidence.validate_alarm_migration_checkpoint(
            checkpoint,
            previous=previous,
        )
        previous = checkpoint


def test_alarm_migration_rejects_skip_rollback_hash_and_time_mutation() -> None:
    chain = _checkpoint_chain()
    skipped = _checkpoint(
        "canonical_delivery_confirmed",
        2,
        previous=chain[0],
        dual=False,
        created_at=101,
    )
    with pytest.raises(evidence.ContractError):
        evidence.validate_alarm_migration_checkpoint(skipped, previous=chain[0])

    wrong_rollback = copy.deepcopy(chain[1])
    wrong_rollback["rollback_plan"]["publisher_topic_state"] = {}
    with pytest.raises(evidence.ContractError):
        evidence.validate_alarm_migration_checkpoint(
            wrong_rollback,
            previous=chain[0],
        )

    arbitrary_delivery = copy.deepcopy(chain[2])
    arbitrary_delivery["postcondition"]["delivery_verification"] = {
        "verified": True
    }
    arbitrary_delivery["postcondition_receipt_sha256"] = evidence.canonical_sha256(
        arbitrary_delivery["postcondition"]
    )
    arbitrary_delivery["idempotency_key"] = evidence.canonical_sha256(
        {
            "migration_id": arbitrary_delivery["migration_id"],
            "phase": arbitrary_delivery["phase"],
            "publisher_id": arbitrary_delivery["publisher_id"],
            "inventory_sha256": arbitrary_delivery["inventory_sha256"],
            "postcondition_sha256": arbitrary_delivery[
                "postcondition_receipt_sha256"
            ],
            "delivery_receipt_sha256": arbitrary_delivery[
                "delivery_receipt_sha256"
            ],
        }
    )
    with pytest.raises(evidence.ContractError):
        evidence.validate_alarm_migration_checkpoint(
            arbitrary_delivery,
            previous=chain[1],
        )

    inverted = copy.deepcopy(chain[3])
    inverted["created_at_epoch"] = chain[2]["created_at_epoch"] - 1
    with pytest.raises(evidence.ContractError):
        evidence.validate_alarm_migration_checkpoint(
            inverted,
            previous=chain[2],
        )

    bad_idempotency = copy.deepcopy(chain[4])
    bad_idempotency["idempotency_key"] = "f" * 64
    with pytest.raises(evidence.ContractError):
        evidence.validate_alarm_migration_checkpoint(
            bad_idempotency,
            previous=chain[3],
        )


def test_runtime_evidence_authority_is_main_owned_after_one_time_bootstrap() -> None:
    terraform = (PROJECT_ROOT / "infra/terraform/runtime_evidence.tf").read_text()
    manifest = (
        PROJECT_ROOT / "infra/deploy/terraform_runtime_migrations.json"
    ).read_text()
    assert 'resource "aws_kms_key" "alarm_recipient_ack"' in terraform
    assert 'resource "aws_iam_role" "alarm_recipient_ack_signer"' in terraform
    assert 'resource "aws_iam_role" "runtime_automation"' in terraform
    assert 'resource "aws_iam_role_policy" "runtime_evidence_automation"' in terraform
    assert 'data "aws_kms_alias" "alarm_recipient_ack"' not in terraform
    assert 'data "aws_iam_role" "alarm_recipient_ack_signer"' not in terraform
    assert "runtime_evidence_automation_policy_contract" in terraform
    for address in (
        "aws_kms_key.alarm_recipient_ack",
        "aws_kms_alias.alarm_recipient_ack",
        "aws_iam_role.alarm_recipient_ack_signer",
        "aws_iam_role_policy.alarm_recipient_ack_signer",
        "aws_iam_role_policy.runtime_evidence_automation",
    ):
        assert address not in manifest
