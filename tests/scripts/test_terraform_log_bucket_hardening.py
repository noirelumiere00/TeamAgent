"""CloudTrail/Bedrock S3 log bucket hardening contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TF_ROOT = PROJECT_ROOT / "infra" / "terraform"
SECURITY = TF_ROOT / "security.tf"
VARIABLES = TF_ROOT / "variables.tf"
README = TF_ROOT / "README.md"
GUARD = PROJECT_ROOT / "infra" / "deploy" / "terraform_runtime_guard.sh"
MIGRATIONS = PROJECT_ROOT / "infra" / "deploy" / "terraform_runtime_migrations.json"


def _block(path: Path, kind: str, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    marker = f'{kind} "{name}" {{'
    start = text.index(marker)
    brace = text.index("{", start)
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated Terraform block: {path}:{kind}.{name}")


@pytest.mark.parametrize(
    ("name", "enable_expression", "bucket_reference"),
    [
        (
            "cloudtrail",
            "var.enable_cloudtrail ? 1 : 0",
            "aws_s3_bucket.cloudtrail[0].id",
        ),
        (
            "bedrock_logs",
            "var.enable_bedrock_invocation_logging ? 1 : 0",
            "aws_s3_bucket.bedrock_logs[0].id",
        ),
    ],
)
def test_log_buckets_enable_versioning_without_mfa_delete(
    name: str,
    enable_expression: str,
    bucket_reference: str,
) -> None:
    block = _block(SECURITY, 'resource "aws_s3_bucket_versioning"', name)
    assert f"count  = {enable_expression}" in block
    assert f"bucket = {bucket_reference}" in block
    assert 'status = "Enabled"' in block
    assert "mfa_delete" not in block


@pytest.mark.parametrize(
    ("name", "service_sid", "bucket_reference"),
    [
        ("cloudtrail", "AWSCloudTrailWrite", "aws_s3_bucket.cloudtrail[0].arn"),
        ("bedrock_logs", "AllowBedrockPut", "aws_s3_bucket.bedrock_logs[0].arn"),
    ],
)
def test_log_bucket_policies_preserve_delivery_and_deny_plain_http(
    name: str,
    service_sid: str,
    bucket_reference: str,
) -> None:
    block = _block(SECURITY, 'resource "aws_s3_bucket_policy"', name)
    assert f'Sid       = "{service_sid}"' in block
    assert block.count('Sid       = "DenyInsecureTransport"') == 1
    assert 'Effect    = "Deny"' in block
    assert 'Principal = "*"' in block
    assert 'Action    = "s3:*"' in block
    assert bucket_reference in block
    assert f'"${{{bucket_reference}}}/*"' in block
    assert '"aws:SecureTransport" = "false"' in block

    if name == "cloudtrail":
        assert 'Sid       = "AWSCloudTrailAclCheck"' in block
        assert '"aws:SourceArn"' in block
        assert 'Service = "cloudtrail.amazonaws.com"' in block
    else:
        assert '"aws:SourceAccount"' in block
        assert '"aws:SourceArn"' in block
        assert 'Service = "bedrock.amazonaws.com"' in block
        assert (
            "/bedrock/AWSLogs/${data.aws_caller_identity.current.account_id}/"
            "BedrockModelInvocationLogs/*"
        ) in block


def test_bedrock_kms_delivery_grant_is_canonical_and_exact() -> None:
    kms = _block(SECURITY, 'resource "aws_kms_key"', "logs")
    statement = kms[kms.index('Sid    = "AllowBedrockLogs"') :]
    assert 'Action   = "kms:GenerateDataKey"' in statement
    for forbidden in (
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:GenerateDataKey*",
        "kms:DescribeKey",
    ):
        assert forbidden not in statement
    assert '"aws:SourceAccount"' in statement
    assert '"aws:SourceArn"' in statement
    assert "arn:aws:bedrock:${var.aws_region}:" in statement


def test_log_producers_are_staged_after_versioning() -> None:
    cloudtrail = _block(SECURITY, 'resource "aws_cloudtrail"', "main")
    assert (
        "count = var.enable_cloudtrail && var.enable_cloudtrail_log_delivery ? 1 : 0" in cloudtrail
    )
    assert "aws_s3_bucket_policy.cloudtrail" in cloudtrail
    assert "aws_s3_bucket_versioning.cloudtrail" in cloudtrail
    assert "15 minutes" in cloudtrail

    bedrock = _block(
        SECURITY,
        'resource "aws_bedrock_model_invocation_logging_configuration"',
        "main",
    )
    assert (
        "count = var.enable_bedrock_invocation_logging && "
        "var.enable_bedrock_invocation_log_delivery ? 1 : 0"
    ) in bedrock
    assert "aws_s3_bucket_policy.bedrock_logs" in bedrock
    assert "aws_s3_bucket_versioning.bedrock_logs" in bedrock


def test_bedrock_retention_is_exactly_60_days_only_under_bedrock_prefix() -> None:
    retention = _block(VARIABLES, "variable", "bedrock_logs_retention_days")
    security = SECURITY.read_text(encoding="utf-8")
    assert "default     = 60" in retention
    assert "var.bedrock_logs_retention_days == 60" in retention
    assert "object_lock" not in security.lower()
    assert "mfa_delete" not in security.lower()

    bucket = _block(SECURITY, 'resource "aws_s3_bucket"', "bedrock_logs")
    assert "var.bedrock_logs_retention_days == 60" in bucket
    lifecycle = _block(
        SECURITY,
        'resource "aws_s3_bucket_lifecycle_configuration"',
        "bedrock_logs",
    )
    assert lifecycle.count('prefix = "bedrock/"') == 2
    assert "days = var.bedrock_logs_retention_days" in lifecycle
    assert "noncurrent_days = var.bedrock_logs_retention_days" in lifecycle
    assert "expired_object_delete_marker = true" in lifecycle
    assert "cloudtrail" not in lifecycle
    assert 'resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail"' not in security


def test_first_enablement_wait_and_exact_guard_allowlist_are_documented() -> None:
    readme = README.read_text(encoding="utf-8")
    assert "enable_cloudtrail_log_delivery=false" in readme
    assert "enable_bedrock_invocation_log_delivery=false" in readme
    assert "15分" in readme
    assert "Object Lock" in readme
    assert "MFA Delete" in readme
    assert "bedrock_logs_retention_days = 60" in readme

    manifest = json.loads(MIGRATIONS.read_text(encoding="utf-8"))
    migration = manifest["migrations"]["2026-07-wolfi-runtime-v1"]
    assert manifest["log_versioning_stage"] == {
        "id": "2026-07-log-versioning-enable-v1",
        "enabled": False,
        "expires_at": "2026-08-31T00:00:00Z",
        "buckets": [
            "teamagent-dev-cloudtrail-718959508629",
            "teamagent-dev-bedrock-logs-718959508629",
        ],
        "allowed_write": "s3:PutBucketVersioning",
        "required_status_before": ["Unversioned", "Enabled"],
        "required_status_after": "Enabled",
        "mfa_delete": "Disabled",
        "producer_action": "no-op",
    }
    for address in (
        "aws_s3_bucket_versioning.cloudtrail[0]",
        "aws_s3_bucket_versioning.bedrock_logs[0]",
        "aws_s3_bucket_lifecycle_configuration.bedrock_logs[0]",
        "aws_s3_bucket_policy.cloudtrail[0]",
        "aws_s3_bucket_policy.bedrock_logs[0]",
        "aws_kms_key.logs",
    ):
        assert migration["allowed_changes"].count(address) == 1
    assert "2026-07-log-bucket-hardening-v1" not in manifest["migrations"]

    guard = GUARD.read_text(encoding="utf-8")
    assert "validate_log_bucket_hardening_plan" in guard
    assert "enable-log-versioning" in guard
    assert "teamagent-log-versioning-enable-receipt" in guard
    assert "validate_log_versioning_stage_manifest" in guard
    assert "verify_versioning_enable_receipt" in guard
    assert "put-bucket-versioning" in guard
    assert "--versioning-receipt" in guard
    assert "-target=" not in guard
    assert ".complete == true" in guard
    assert "producer no-op契約" in guard
    assert "verify_log_readiness_receipt" in guard
    assert ">= 900" in guard
    assert "versioning_receipt_sha256" in guard
    assert "enable-log-versioning" in readme
    assert "--versioning-receipt" in readme
    assert "非targeted" in readme


def _run_log_versioning_manifest_validator(
    tmp_path: Path,
    manifest: dict[str, object],
) -> subprocess.CompletedProcess[str]:
    body = GUARD.read_text(encoding="utf-8")
    function = re.search(
        r"validate_log_versioning_stage_manifest\(\) \{.*?"
        r"(?=\nmigration_to_file\(\))",
        body,
        flags=re.DOTALL,
    )
    assert function is not None
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    script = "\n".join(
        (
            "set -euo pipefail",
            f"MIGRATION_FILE={str(manifest_path)!r}",
            'die() { echo "★ $*" >&2; return 1; }',
            function.group(0),
            "validate_log_versioning_stage_manifest",
        )
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_log_versioning_manifest_gate_is_disabled_and_exact(
    tmp_path: Path,
) -> None:
    manifest = json.loads(MIGRATIONS.read_text(encoding="utf-8"))
    assert _run_log_versioning_manifest_validator(tmp_path, manifest).returncode != 0

    manifest["log_versioning_stage"]["enabled"] = True
    manifest["log_versioning_stage"]["expires_at"] = "2099-01-01T00:00:00Z"
    result = _run_log_versioning_manifest_validator(tmp_path, manifest)
    assert result.returncode == 0, result.stderr

    broadened = copy.deepcopy(manifest)
    broadened["log_versioning_stage"]["allowed_write"] = "s3:*"
    assert _run_log_versioning_manifest_validator(tmp_path, broadened).returncode != 0

    wrong_bucket = copy.deepcopy(manifest)
    wrong_bucket["log_versioning_stage"]["buckets"][1] = "wrong"
    assert _run_log_versioning_manifest_validator(tmp_path, wrong_bucket).returncode != 0


def _run_log_readiness_validator(
    tmp_path: Path,
    readiness: dict[str, object],
    versioning: dict[str, object],
) -> subprocess.CompletedProcess[str]:
    body = GUARD.read_text(encoding="utf-8")
    function = re.search(
        r"verify_log_readiness_receipt\(\) \{.*?"
        r"(?=\nDEPLOYMENT_LOCK_ID=)",
        body,
        flags=re.DOTALL,
    )
    assert function is not None
    readiness_path = tmp_path / "readiness.json"
    versioning_path = tmp_path / "versioning.json"
    snapshot_path = tmp_path / "snapshot.json"
    readiness_path.write_text(json.dumps(readiness), encoding="utf-8")
    versioning_path.write_text(json.dumps(versioning), encoding="utf-8")
    snapshot_path.write_text(
        json.dumps(
            {
                "log_buckets": {
                    "cloudtrail": {
                        "versioning_status": "Enabled",
                        "mfa_delete": "Disabled",
                    },
                    "bedrock": {
                        "versioning_status": "Enabled",
                        "mfa_delete": "Disabled",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    script = "\n".join(
        (
            "set -euo pipefail",
            'EXPECTED_ACCOUNT_ID="718959508629"',
            'REGION="ap-northeast-1"',
            'PROJECT="teamagent"',
            'ENVIRONMENT="dev"',
            "sha256_file() { openssl dgst -sha256 \"$1\" | awk '{print $NF}'; }",
            'die() { echo "★ $*" >&2; return 1; }',
            function.group(0),
            'verify_log_readiness_receipt "$1" "$2" "$3"',
        )
    )
    return subprocess.run(
        [
            "bash",
            "-c",
            script,
            "validator",
            str(readiness_path),
            str(versioning_path),
            str(snapshot_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_log_readiness_receipt_binds_versioning_sha_and_900_second_wait(
    tmp_path: Path,
) -> None:
    now = int(time.time())
    enabled_at = now - 901
    versioning: dict[str, object] = {"enabled_observed_at_epoch": enabled_at}
    versioning_bytes = json.dumps(versioning).encode()
    versioning_sha = hashlib.sha256(versioning_bytes).hexdigest()
    evidence = "a" * 64
    readiness: dict[str, object] = {
        "kind": "teamagent-log-rollout-readiness-receipt",
        "schema_version": 1,
        "account_id": "718959508629",
        "region": "ap-northeast-1",
        "versioning_receipt_sha256": versioning_sha,
        "created_at_epoch": now,
        "expires_at_epoch": now + 3600,
        "retention_export_evidence_sha256": evidence,
        "cloudtrail": {
            "bucket": "teamagent-dev-cloudtrail-718959508629",
            "versioning_status": "Enabled",
            "versioning_enabled_at_epoch": enabled_at,
            "verified_at_epoch": now,
            "latest_log_evidence_sha256": evidence,
            "latest_digest_evidence_sha256": evidence,
        },
        "bedrock": {
            "bucket": "teamagent-dev-bedrock-logs-718959508629",
            "versioning_status": "Enabled",
            "versioning_enabled_at_epoch": enabled_at,
            "verified_at_epoch": now,
            "latest_delivery_evidence_sha256": evidence,
        },
    }
    result = _run_log_readiness_validator(tmp_path, readiness, versioning)
    assert result.returncode == 0, result.stderr

    wrong_sha = copy.deepcopy(readiness)
    wrong_sha["versioning_receipt_sha256"] = "b" * 64
    assert _run_log_readiness_validator(tmp_path, wrong_sha, versioning).returncode != 0

    too_soon_versioning = {"enabled_observed_at_epoch": now - 899}
    too_soon = copy.deepcopy(readiness)
    too_soon_sha = hashlib.sha256(json.dumps(too_soon_versioning).encode()).hexdigest()
    too_soon["versioning_receipt_sha256"] = too_soon_sha
    too_soon["cloudtrail"]["versioning_enabled_at_epoch"] = now - 899
    too_soon["bedrock"]["versioning_enabled_at_epoch"] = now - 899
    assert (
        _run_log_readiness_validator(
            tmp_path,
            too_soon,
            too_soon_versioning,
        ).returncode
        != 0
    )


def _log_bucket_plan() -> dict[str, object]:
    account = "718959508629"
    region = "ap-northeast-1"
    cloudtrail_bucket = f"arn:aws:s3:::teamagent-dev-cloudtrail-{account}"
    bedrock_bucket = f"arn:aws:s3:::teamagent-dev-bedrock-logs-{account}"
    trail_arn = f"arn:aws:cloudtrail:{region}:{account}:trail/teamagent-dev-trail"

    cloudtrail_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AWSCloudTrailAclCheck",
                "Effect": "Allow",
                "Principal": {"Service": "cloudtrail.amazonaws.com"},
                "Action": "s3:GetBucketAcl",
                "Resource": cloudtrail_bucket,
                "Condition": {"StringEquals": {"aws:SourceArn": trail_arn}},
            },
            {
                "Sid": "AWSCloudTrailWrite",
                "Effect": "Allow",
                "Principal": {"Service": "cloudtrail.amazonaws.com"},
                "Action": "s3:PutObject",
                "Resource": f"{cloudtrail_bucket}/AWSLogs/{account}/*",
                "Condition": {
                    "StringEquals": {
                        "s3:x-amz-acl": "bucket-owner-full-control",
                        "aws:SourceArn": trail_arn,
                    }
                },
            },
            {
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [cloudtrail_bucket, f"{cloudtrail_bucket}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
        ],
    }
    bedrock_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowBedrockPut",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "s3:PutObject",
                "Resource": (
                    f"{bedrock_bucket}/bedrock/AWSLogs/{account}/BedrockModelInvocationLogs/*"
                ),
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{region}:{account}:*"},
                },
            },
            {
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [bedrock_bucket, f"{bedrock_bucket}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
        ],
    }
    cloudtrail_before_policy = copy.deepcopy(cloudtrail_policy)
    cloudtrail_before_policy["Statement"].pop()
    bedrock_before_policy = copy.deepcopy(bedrock_policy)
    bedrock_before_policy["Statement"].pop()
    kms_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowBedrockLogs",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "kms:GenerateDataKey",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{region}:{account}:*"},
                },
            }
        ],
    }

    def change(
        address: str,
        after: dict[str, object],
        *,
        actions: list[str] | None = None,
        before: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "address": address,
            "change": {
                "actions": actions or ["create"],
                "before": before,
                "after": after,
            },
        }

    return {
        "resource_changes": [
            change(
                "aws_s3_bucket_versioning.cloudtrail[0]",
                {
                    "bucket": f"teamagent-dev-cloudtrail-{account}",
                    "versioning_configuration": [{"status": "Enabled"}],
                },
            ),
            change(
                "aws_s3_bucket_versioning.bedrock_logs[0]",
                {
                    "bucket": f"teamagent-dev-bedrock-logs-{account}",
                    "versioning_configuration": [{"status": "Enabled"}],
                },
            ),
            change(
                "aws_s3_bucket_policy.cloudtrail[0]",
                {
                    "bucket": f"teamagent-dev-cloudtrail-{account}",
                    "policy": json.dumps(cloudtrail_policy),
                },
                actions=["update"],
                before={
                    "bucket": f"teamagent-dev-cloudtrail-{account}",
                    "policy": json.dumps(cloudtrail_before_policy),
                },
            ),
            change(
                "aws_s3_bucket_policy.bedrock_logs[0]",
                {
                    "bucket": f"teamagent-dev-bedrock-logs-{account}",
                    "policy": json.dumps(bedrock_policy),
                },
                actions=["update"],
                before={
                    "bucket": f"teamagent-dev-bedrock-logs-{account}",
                    "policy": json.dumps(bedrock_before_policy),
                },
            ),
            change(
                "aws_s3_bucket_lifecycle_configuration.bedrock_logs[0]",
                {
                    "bucket": f"teamagent-dev-bedrock-logs-{account}",
                    "rule": [
                        {
                            "id": "bedrock-current-and-noncurrent-60-days",
                            "status": "Enabled",
                            "filter": [{"prefix": "bedrock/"}],
                            "expiration": [
                                {
                                    "days": 60,
                                    "expired_object_delete_marker": False,
                                }
                            ],
                            "noncurrent_version_expiration": [
                                {
                                    "noncurrent_days": 60,
                                    "newer_noncurrent_versions": None,
                                }
                            ],
                        },
                        {
                            "id": "bedrock-expired-delete-markers",
                            "status": "Enabled",
                            "filter": [{"prefix": "bedrock/"}],
                            "expiration": [
                                {
                                    "days": None,
                                    "expired_object_delete_marker": True,
                                }
                            ],
                            "noncurrent_version_expiration": [],
                        },
                    ],
                },
            ),
            change(
                "aws_kms_key.logs",
                {"policy": json.dumps(kms_policy), "description": "logs"},
                actions=["update"],
                before={"policy": json.dumps(kms_policy), "description": "logs"},
            ),
            change(
                "aws_cloudtrail.main[0]",
                {
                    "name": "teamagent-dev-trail",
                    "s3_bucket_name": f"teamagent-dev-cloudtrail-{account}",
                    "is_multi_region_trail": True,
                    "include_global_service_events": True,
                    "enable_log_file_validation": True,
                    "kms_key_id": (
                        f"arn:aws:kms:{region}:{account}:key/11111111-2222-3333-4444-555555555555"
                    ),
                },
                actions=["no-op"],
                before={
                    "name": "teamagent-dev-trail",
                    "s3_bucket_name": f"teamagent-dev-cloudtrail-{account}",
                    "is_multi_region_trail": True,
                    "include_global_service_events": True,
                    "enable_log_file_validation": True,
                    "kms_key_id": (
                        f"arn:aws:kms:{region}:{account}:key/11111111-2222-3333-4444-555555555555"
                    ),
                },
            ),
            change(
                "aws_bedrock_model_invocation_logging_configuration.main[0]",
                {
                    "logging_config": [
                        {
                            "cloudwatch_config": [],
                            "embedding_data_delivery_enabled": True,
                            "image_data_delivery_enabled": False,
                            "s3_config": [
                                {
                                    "bucket_name": (f"teamagent-dev-bedrock-logs-{account}"),
                                    "key_prefix": "bedrock/",
                                }
                            ],
                            "text_data_delivery_enabled": True,
                            "video_data_delivery_enabled": False,
                        }
                    ]
                },
                actions=["no-op"],
                before={
                    "logging_config": [
                        {
                            "cloudwatch_config": [],
                            "embedding_data_delivery_enabled": True,
                            "image_data_delivery_enabled": False,
                            "s3_config": [
                                {
                                    "bucket_name": (f"teamagent-dev-bedrock-logs-{account}"),
                                    "key_prefix": "bedrock/",
                                }
                            ],
                            "text_data_delivery_enabled": True,
                            "video_data_delivery_enabled": False,
                        }
                    ]
                },
            ),
        ],
        "variables": {"bedrock_logs_retention_days": {"value": 60}},
    }


def _log_bucket_guard_filter() -> str:
    guard = GUARD.read_text(encoding="utf-8")
    match = re.search(
        r"validate_log_bucket_hardening_plan\(\) \{.*?"
        r'--arg environment "\$ENVIRONMENT" \'(?P<filter>.*?)'
        r'\n  \' "\$plan_json"',
        guard,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group("filter")


def _run_log_bucket_guard(plan: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "jq",
            "-e",
            "--arg",
            "account",
            "718959508629",
            "--arg",
            "region",
            "ap-northeast-1",
            "--arg",
            "project",
            "teamagent",
            "--arg",
            "environment",
            "dev",
            _log_bucket_guard_filter(),
        ],
        input=json.dumps(plan),
        capture_output=True,
        text=True,
        check=False,
    )


def test_log_bucket_plan_guard_accepts_only_exact_tls_and_delivery_policy() -> None:
    plan = _log_bucket_plan()
    assert _run_log_bucket_guard(plan).returncode == 0

    policy_change = plan["resource_changes"][2]  # type: ignore[index]
    policy = json.loads(policy_change["change"]["after"]["policy"])  # type: ignore[index]
    policy["Statement"][2]["Condition"]["Bool"]["aws:SecureTransport"] = "true"
    policy_change["change"]["after"]["policy"] = json.dumps(policy)  # type: ignore[index]
    assert _run_log_bucket_guard(plan).returncode != 0


@pytest.mark.parametrize(
    "mutation",
    [
        "versioning_destroy",
        "policy_destroy",
        "service_statement_broadened",
        "lifecycle_wrong_prefix",
        "kms_broadened",
        "producer_update",
    ],
)
def test_log_bucket_plan_guard_rejects_stale_or_broad_policy_changes(
    mutation: str,
) -> None:
    plan = _log_bucket_plan()
    changes = plan["resource_changes"]
    assert isinstance(changes, list)
    versioning = changes[0]["change"]  # type: ignore[index]
    policy = changes[2]["change"]  # type: ignore[index]

    if mutation == "versioning_destroy":
        versioning["actions"] = ["delete"]  # type: ignore[index]
        versioning["after"] = None  # type: ignore[index]
    elif mutation == "policy_destroy":
        policy["actions"] = ["delete"]  # type: ignore[index]
        policy["after"] = None  # type: ignore[index]
    elif mutation == "service_statement_broadened":
        bedrock = changes[3]["change"]  # type: ignore[index]
        document = json.loads(bedrock["after"]["policy"])  # type: ignore[index]
        document["Statement"][0]["Action"] = "s3:*"
        bedrock["after"]["policy"] = json.dumps(document)  # type: ignore[index]
    elif mutation == "lifecycle_wrong_prefix":
        lifecycle = changes[4]["change"]  # type: ignore[index]
        lifecycle["after"]["rule"][0]["filter"][0]["prefix"] = ""  # type: ignore[index]
    elif mutation == "kms_broadened":
        kms = changes[5]["change"]  # type: ignore[index]
        document = json.loads(kms["after"]["policy"])  # type: ignore[index]
        document["Statement"][0]["Action"] = "kms:*"
        kms["after"]["policy"] = json.dumps(document)  # type: ignore[index]
    elif mutation == "producer_update":
        producer = changes[6]["change"]  # type: ignore[index]
        producer["actions"] = ["update"]  # type: ignore[index]
    else:
        raise AssertionError(f"unknown mutation: {mutation}")

    assert _run_log_bucket_guard(plan).returncode != 0


def test_runtime_live_anchors_are_unchanged_by_log_hardening() -> None:
    migration = json.loads(MIGRATIONS.read_text(encoding="utf-8"))["migrations"][
        "2026-07-wolfi-runtime-v1"
    ]
    assert migration["from"]["task_definition_arns"]["connect_web"].endswith(":53")
    assert migration["from"]["task_definition_arns"]["canary"].endswith(":14")
    assert migration["from"]["connect_app_html"] == migration["to"]["connect_app_html"]
    assert migration["from"]["connect_app_html"]["version_id"] == (
        "FTXbcN70D0DCN90TI_hRK1IdQK_HhLee"
    )
    assert migration["from"]["connect_app_html"]["sha256"] == (
        "03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c"
    )
