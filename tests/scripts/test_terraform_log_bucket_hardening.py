"""CloudTrail/Bedrock S3 log bucket hardening contracts."""

from __future__ import annotations

import copy
import json
import re
import subprocess
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
        assert 'Service = "bedrock.amazonaws.com"' in block


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


def test_bedrock_retention_is_explicitly_indefinite_without_deletion_resources() -> None:
    retention = _block(VARIABLES, "variable", "bedrock_logs_retention_mode")
    security = SECURITY.read_text(encoding="utf-8")
    assert 'default     = "INDEFINITE"' in retention
    assert 'var.bedrock_logs_retention_mode == "INDEFINITE"' in retention
    assert "aws_s3_bucket_lifecycle_configuration" not in security
    assert "object_lock" not in security.lower()
    assert "mfa_delete" not in security.lower()

    bucket = _block(SECURITY, 'resource "aws_s3_bucket"', "bedrock_logs")
    assert 'var.bedrock_logs_retention_mode == "INDEFINITE"' in bucket


def test_first_enablement_wait_and_exact_guard_allowlist_are_documented() -> None:
    readme = README.read_text(encoding="utf-8")
    assert "enable_cloudtrail_log_delivery=false" in readme
    assert "enable_bedrock_invocation_log_delivery=false" in readme
    assert "15分" in readme
    assert "Object Lock" in readme
    assert "MFA Delete" in readme
    assert 'bedrock_logs_retention_mode = "INDEFINITE"' in readme

    migration = json.loads(MIGRATIONS.read_text(encoding="utf-8"))["migrations"][
        "2026-07-wolfi-runtime-v1"
    ]
    for address in (
        "aws_s3_bucket_versioning.cloudtrail[0]",
        "aws_s3_bucket_versioning.bedrock_logs[0]",
        "aws_s3_bucket_policy.cloudtrail[0]",
        "aws_s3_bucket_policy.bedrock_logs[0]",
    ):
        assert address in migration["allowed_changes"]

    guard = GUARD.read_text(encoding="utf-8")
    assert "validate_log_bucket_hardening_plan" in guard
    assert "versioning/TLS/service delivery契約" in guard


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
                "Resource": f"{bedrock_bucket}/AWSLogs/{account}/*",
                "Condition": {
                    "StringEquals": {
                        "s3:x-amz-acl": "bucket-owner-full-control",
                        "aws:SourceAccount": account,
                    }
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
        ]
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
        "versioning_not_create",
        "policy_not_update",
        "service_statement_changed",
        "policy_attribute_changed",
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

    if mutation == "versioning_not_create":
        versioning["actions"] = ["update"]  # type: ignore[index]
        versioning["before"] = copy.deepcopy(versioning["after"])  # type: ignore[index]
    elif mutation == "policy_not_update":
        policy["actions"] = ["create"]  # type: ignore[index]
        policy["before"] = None  # type: ignore[index]
    elif mutation == "service_statement_changed":
        before = json.loads(policy["before"]["policy"])  # type: ignore[index]
        before["Statement"][0]["Action"] = "s3:*"
        policy["before"]["policy"] = json.dumps(before)  # type: ignore[index]
    elif mutation == "policy_attribute_changed":
        policy["after"]["bucket"] = "wrong-bucket"  # type: ignore[index]
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
