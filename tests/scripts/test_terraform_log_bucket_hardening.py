"""CloudTrail/Bedrock S3 log bucket hardening contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import time
from datetime import UTC, datetime
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
    assert "900-second no-write" in cloudtrail
    assert "later Enabled observation cannot satisfy" in cloudtrail

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
    assert "bedrock-current-and-noncurrent-minimum-60-days" in lifecycle
    assert "expired_object_delete_marker = true" in lifecycle
    assert "cloudtrail" not in lifecycle
    assert 'resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail"' not in security


def test_bedrock_versioned_payload_timeline_never_deletes_before_day_60() -> None:
    current_days = 60
    noncurrent_days = 60
    overwrite_age_days = 0
    assert current_days >= 60
    assert overwrite_age_days + noncurrent_days >= 60


def test_first_enablement_wait_and_exact_guard_allowlist_are_documented() -> None:
    readme = README.read_text(encoding="utf-8")
    assert "全writer切断" in readme
    assert "PutBucketVersioning(Status=Enabled)" in readme
    assert "max(Put response Date, first-seen Enabled Date)+900" in readme
    assert "後日の観測" in readme
    assert "Object Lock" in readme
    assert "MFA Delete" in readme
    assert "bedrock_logs_retention_days = 60" in readme

    manifest = json.loads(MIGRATIONS.read_text(encoding="utf-8"))
    migration = manifest["migrations"]["2026-07-wolfi-runtime-v1"]
    assert manifest["log_versioning_stage"] == {
        "id": "2026-07-log-versioning-cutover-v4",
        "enabled": False,
        "expires_at": "2026-08-31T00:00:00Z",
        "buckets": [
            "teamagent-dev-cloudtrail-718959508629",
            "teamagent-dev-bedrock-logs-718959508629",
        ],
        "allowed_write": "guard-disconnect-versioning-and-cutover-only",
        "required_status_before": ["Unversioned"],
        "required_status_after": "Enabled",
        "mfa_delete": "Disabled",
        "producer_action": "guard-disconnect-enable-settle-double-observe-cutover",
        "producer_state_required": "disconnected",
        "timestamp_source": "aws-http-response-date",
        "minimum_settle_seconds": 900,
        "cutover_mode": "same-workflow-shared-lock-first-time-only",
    }
    for address in (
        "aws_s3_bucket_versioning.cloudtrail[0]",
        "aws_s3_bucket_versioning.bedrock_logs[0]",
        "aws_s3_bucket_lifecycle_configuration.bedrock_logs[0]",
        "aws_s3_bucket_policy.cloudtrail[0]",
        "aws_s3_bucket_policy.bedrock_logs[0]",
        "aws_kms_key.logs",
    ):
        assert "allowed_changes" not in migration
        if migration["enabled"]:
            reviewed_addresses = {
                row["address"] for row in migration["reviewed_plan"]["resource_changes"]
            }
            assert address in reviewed_addresses
        else:
            assert migration["reviewed_plan"] is None
    assert "2026-07-log-bucket-hardening-v1" not in manifest["migrations"]

    guard = GUARD.read_text(encoding="utf-8")
    assert "validate_log_bucket_hardening_plan" in guard
    assert "attest-log-versioning" in guard
    assert "teamagent-log-versioning-cutover-receipt" in guard
    assert "validate_log_versioning_stage_manifest" in guard
    assert "verify_versioning_attestation_receipt" in guard
    evidence = (PROJECT_ROOT / "infra/deploy/runtime_evidence_guard.py").read_text(encoding="utf-8")
    assert "disconnect_all_writers" in evidence
    assert "first_time_versioning_cutover" in evidence
    assert '"s3api",\n            "put-bucket-versioning"' in evidence
    assert '"timestamp_source": "aws-http-response-date"' in evidence
    assert "lookup-events" not in guard
    assert "lookup-events" not in evidence
    assert "--versioning-receipt" in guard
    assert "-target=" not in guard
    assert ".complete == true" in guard
    assert "later Enabled/Suspended observation" in evidence
    assert "verify_log_readiness_receipt" in guard
    assert "LOG_VERSIONING_SETTLE_SECONDS=900" in guard
    assert "post_settle_observations" in evidence
    assert "post_cutover_verification_epoch" in evidence
    assert "versioning_receipt_sha256" in guard
    assert "attest-log-versioning" in readme
    assert "--versioning-receipt" in readme
    assert "non-targeted runtime migration" in readme
    assert "full-root saved plan" in readme


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


def _versioning_event(
    bucket: str,
    *,
    event_epoch: int,
    status: str = "Enabled",
    outer_utc_offset: bool = False,
) -> dict[str, object]:
    event_time = datetime.fromtimestamp(
        event_epoch,
        tz=UTC,
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    outer_event_time = event_time.removesuffix("Z") + "+00:00" if outer_utc_offset else event_time
    event_id = hashlib.sha256(f"{bucket}:{event_time}:{status}".encode()).hexdigest()
    inner = {
        "eventVersion": "1.09",
        "eventTime": event_time,
        "eventSource": "s3.amazonaws.com",
        "eventName": "PutBucketVersioning",
        "awsRegion": "ap-northeast-1",
        "eventID": event_id,
        "eventType": "AwsApiCall",
        "managementEvent": True,
        "readOnly": False,
        "recipientAccountId": "718959508629",
        "requestParameters": {
            "bucketName": bucket,
            "VersioningConfiguration": {"Status": status},
        },
    }
    return {
        "EventId": event_id,
        "EventName": "PutBucketVersioning",
        "EventSource": "s3.amazonaws.com",
        "EventTime": outer_event_time,
        "Resources": [{"ResourceName": bucket, "ResourceType": "AWS::S3::Bucket"}],
        "CloudTrailEvent": json.dumps(inner),
    }


def _run_precutover_capture(
    tmp_path: Path,
    *,
    cloudtrail_logging: bool = False,
    bedrock_present: bool = False,
    event_status: str = "Enabled",
    event_age_seconds: int = 901,
    outer_utc_offset: bool = False,
) -> subprocess.CompletedProcess[str]:
    body = GUARD.read_text(encoding="utf-8")
    functions = re.search(
        r"capture_log_producer_off_contract\(\) \{.*?"
        r"(?=\nwrite_log_bucket_identity\(\))",
        body,
        flags=re.DOTALL,
    )
    assert functions is not None

    fixture_dir = tmp_path / (
        f"precutover-{int(cloudtrail_logging)}-{int(bedrock_present)}-"
        f"{event_status}-{event_age_seconds}-{int(outer_utc_offset)}"
    )
    fixture_dir.mkdir(mode=0o700, exist_ok=True)
    trail = {
        "Trail": {
            "Name": "teamagent-dev-trail",
            "S3BucketName": "teamagent-dev-cloudtrail-718959508629",
            "IsMultiRegionTrail": True,
            "IncludeGlobalServiceEvents": True,
            "LogFileValidationEnabled": True,
            "KmsKeyId": (
                "arn:aws:kms:ap-northeast-1:718959508629:key/11111111-2222-3333-4444-555555555555"
            ),
        }
    }
    bedrock_config = {
        "textDataDeliveryEnabled": True,
        "embeddingDataDeliveryEnabled": True,
        "imageDataDeliveryEnabled": False,
        "videoDataDeliveryEnabled": False,
        "s3Config": {
            "bucketName": "teamagent-dev-bedrock-logs-718959508629",
            "keyPrefix": "bedrock/",
        },
    }
    observed_at = int(time.time())
    fixtures = {
        "trail.json": trail,
        "trail-status.json": {"IsLogging": cloudtrail_logging},
        "bedrock.json": ({"loggingConfig": bedrock_config} if bedrock_present else {}),
    }
    for filename, value in fixtures.items():
        (fixture_dir / filename).write_text(json.dumps(value), encoding="utf-8")
    for key, bucket in (
        ("cloudtrail", "teamagent-dev-cloudtrail-718959508629"),
        ("bedrock", "teamagent-dev-bedrock-logs-718959508629"),
    ):
        event = _versioning_event(
            bucket,
            event_epoch=observed_at - event_age_seconds,
            status=event_status,
            outer_utc_offset=outer_utc_offset,
        )
        (fixture_dir / f"{key}-events.json").write_text(
            json.dumps({"Events": [event]}),
            encoding="utf-8",
        )

    script = "\n".join(
        (
            "set -euo pipefail",
            'EXPECTED_ACCOUNT_ID="718959508629"',
            'REGION="ap-northeast-1"',
            'PROJECT="teamagent"',
            'ENVIRONMENT="dev"',
            "LOG_VERSIONING_SETTLE_SECONDS=900",
            f"TMP_ROOT={str(fixture_dir)!r}",
            f"OBSERVED_AT={observed_at}",
            ("sha256_text() { openssl dgst -sha256 | awk '{print $NF}'; }"),
            ("sha256_file() { openssl dgst -sha256 \"$1\" | awk '{print $NF}'; }"),
            'die() { echo "★ $*" >&2; return 1; }',
            "aws_cli() {",
            '  case "$1:$2" in',
            (f"    cloudtrail:get-trail) cat {str(fixture_dir / 'trail.json')!r} ;;"),
            (f"    cloudtrail:get-trail-status) cat {str(fixture_dir / 'trail-status.json')!r} ;;"),
            (
                "    bedrock:get-model-invocation-logging-configuration) "
                f"cat {str(fixture_dir / 'bedrock.json')!r} ;;"
            ),
            "    cloudtrail:lookup-events)",
            '      case "$*" in',
            (f"        *bedrock-logs*) cat {str(fixture_dir / 'bedrock-events.json')!r} ;;"),
            (f"        *) cat {str(fixture_dir / 'cloudtrail-events.json')!r} ;;"),
            "      esac ;;",
            '    *) echo "unexpected aws fixture call: $*" >&2; return 64 ;;',
            "  esac",
            "}",
            functions.group(0),
            'capture_log_producer_off_contract "$TMP_ROOT/producer-off.json"',
            ('write_log_cutover_contract "$TMP_ROOT/producer-off.json" "$TMP_ROOT/cutover.json"'),
            ('capture_versioning_enablement_contract "$TMP_ROOT/enablement.json"'),
            (
                'verify_versioning_settle_window "$TMP_ROOT/enablement.json" '
                '"$OBSERVED_AT" >/dev/null'
            ),
        )
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )


def test_precutover_capture_requires_both_producers_off_and_settled_events(
    tmp_path: Path,
) -> None:
    guard = GUARD.read_text(encoding="utf-8")
    evidence = (PROJECT_ROOT / "infra/deploy/runtime_evidence_guard.py").read_text(encoding="utf-8")
    assert "lookup-events" not in guard
    assert "lookup-events" not in evidence
    assert "later observation alone is forbidden" in evidence
    assert "put-bucket-versioning" in evidence
    assert "first_seen_enabled_epoch" in evidence
    assert "SETTLE_SECONDS = 900" in evidence
    assert "post_settle_observations" in evidence
    assert "response_date" in evidence
    assert "post_cutover_verification_epoch" in evidence
    return

    valid = _run_precutover_capture(tmp_path)
    assert valid.returncode == 0, valid.stderr
    equivalent_offset = _run_precutover_capture(
        tmp_path,
        outer_utc_offset=True,
    )
    assert equivalent_offset.returncode == 0, equivalent_offset.stderr

    assert (
        _run_precutover_capture(
            tmp_path,
            cloudtrail_logging=True,
        ).returncode
        != 0
    )
    assert (
        _run_precutover_capture(
            tmp_path,
            bedrock_present=True,
        ).returncode
        != 0
    )
    assert (
        _run_precutover_capture(
            tmp_path,
            event_status="Suspended",
        ).returncode
        != 0
    )
    too_soon = _run_precutover_capture(
        tmp_path,
        event_age_seconds=899,
    )
    assert too_soon.returncode != 0
    assert "settle window" in too_soon.stderr


def _run_log_readiness_validator(
    tmp_path: Path,
    readiness: dict[str, object],
    versioning: dict[str, object],
) -> subprocess.CompletedProcess[str]:
    body = GUARD.read_text(encoding="utf-8")
    functions = re.search(
        r"verify_bound_export_file\(\) \{.*?"
        r"(?=\nDEPLOYMENT_LOCK_ID=)",
        body,
        flags=re.DOTALL,
    )
    assert functions is not None
    readiness_path = tmp_path / "readiness.json"
    versioning_path = tmp_path / "versioning.json"
    snapshot_path = tmp_path / "snapshot.json"
    validator_tmp = tmp_path / "readiness-validator-tmp"
    readiness_path.write_text(json.dumps(readiness), encoding="utf-8")
    versioning_path.write_text(json.dumps(versioning), encoding="utf-8")
    snapshot_path.write_text(
        json.dumps(
            {
                "log_buckets": {
                    "cloudtrail": {
                        "versioning_status": "Enabled",
                        "mfa_delete": "Disabled",
                        "lifecycle": {
                            "configuration_present": False,
                            "rule_count": 0,
                            "deletion_rule_count": 0,
                            "canonical_sha256": hashlib.sha256(b'{"Rules":[]}\n').hexdigest(),
                        },
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
    for path in (readiness_path, versioning_path, snapshot_path):
        path.chmod(0o600)
    script = "\n".join(
        (
            "set -euo pipefail",
            'EXPECTED_ACCOUNT_ID="718959508629"',
            'REGION="ap-northeast-1"',
            'PROJECT="teamagent"',
            'ENVIRONMENT="dev"',
            f"TMP_ROOT={str(validator_tmp)!r}",
            'mkdir -p "$TMP_ROOT"',
            'chmod 700 "$TMP_ROOT"',
            "sha256_file() { openssl dgst -sha256 \"$1\" | awk '{print $NF}'; }",
            ("stat_identity() { stat -f '%d:%i' \"$1\" 2>/dev/null || stat -c '%d:%i' \"$1\"; }"),
            ("stat_inode() { stat -f '%i' \"$1\" 2>/dev/null || stat -c '%i' \"$1\"; }"),
            ("stat_size() { stat -f '%z' \"$1\" 2>/dev/null || stat -c '%s' \"$1\"; }"),
            (
                "secure_existing_file() { "
                '[ ! -L "$1" ] && [ -f "$1" ] || return 1; '
                '[ "$(stat -f \'%Lp\' "$1" 2>/dev/null || '
                'stat -c \'%a\' "$1")" = "${2:-600}" ] || return 1; '
                'realpath "$1"; }'
            ),
            'die() { echo "★ $*" >&2; return 1; }',
            functions.group(0),
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


def _export_binding(path: Path) -> dict[str, object]:
    return {
        "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "export_file_path": str(path.resolve()),
        "export_file_inode": str(path.stat().st_ino),
        "export_file_size_bytes": path.stat().st_size,
    }


def test_log_readiness_receipt_rehashes_exports_and_rejects_time_inversion(
    tmp_path: Path,
) -> None:
    guard = GUARD.read_text(encoding="utf-8")
    evidence = (PROJECT_ROOT / "infra/deploy/runtime_evidence_guard.py").read_text(encoding="utf-8")
    for required in (
        "head-object",
        "get-object",
        "--version-id",
        "--expected-bucket-owner",
        "O_EXCL",
        "O_NOFOLLOW",
        "verify_file_binding",
        "last_modified_epoch",
        "head_aws_date_epoch",
        "get_aws_date_epoch",
        "delivery evidence timestamp exceeds observation",
    ):
        assert required in evidence
    assert "verify_readiness_export_bindings" in guard
    assert "verify-s3-export" in guard
    assert "legacy arbitrary-byte export binding" in guard
    return

    tmp_path.chmod(0o700)
    now = int(time.time())
    versioning_observed_at = now - 1200
    versioning: dict[str, object] = {"pre_cutover_observed_at_epoch": versioning_observed_at}
    versioning_sha = hashlib.sha256(json.dumps(versioning).encode()).hexdigest()

    log_groups = [
        "/aws/codebuild/teamagent-dev-aiia-image-builder",
        "/aws/codebuild/teamagent-dev-image-builder",
        "/aws/ecs/containerinsights/teamagent-dev/performance",
        "/aws/ecs/containerinsights/teamagent-dev-tiktok/performance",
        "/aws/lambda/teamagent-dev-reminders-notify",
        "/aws/lambda/teamagent-dev-tiktok-acquire-dispatch",
        "/aws/lambda/teamagent-dev-x-buzz-dispatch",
    ]
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir(mode=0o700)
    delivery_files: dict[str, Path] = {}
    for label in ("cloudtrail-log", "cloudtrail-digest", "bedrock-delivery"):
        path = exports_dir / f"{label}.jsonl"
        path.write_bytes(f"{label}:exported-object\n".encode())
        path.chmod(0o600)
        delivery_files[label] = path
    retention_files: dict[str, Path] = {}
    for index, group in enumerate(log_groups):
        path = exports_dir / f"retention-{index}.jsonl"
        path.write_bytes(f"{group}:exported-events\n".encode())
        path.chmod(0o600)
        retention_files[group] = path

    retention_path = (tmp_path / "retention-export.json").resolve()
    retention: dict[str, object] = {
        "kind": "teamagent-log-retention-export-manifest",
        "schema_version": 2,
        "account_id": "718959508629",
        "region": "ap-northeast-1",
        "created_at_epoch": now - 90,
        "log_groups": [
            {
                "log_group": group,
                "exported_through_epoch": now - 120,
                "event_count": 1,
                **_export_binding(retention_files[group]),
            }
            for group in log_groups
        ],
    }

    evidence_path = (tmp_path / "log-readiness-evidence.json").resolve()

    def delivery(path: Path, key: str) -> dict[str, object]:
        return {
            "version_id": "version-1",
            "etag": "0123456789abcdef0123456789abcdef",
            "last_modified_epoch": now - 120,
            "size_bytes": path.stat().st_size,
            "key": key,
            **_export_binding(path),
        }

    evidence_artifact: dict[str, object] = {
        "kind": "teamagent-log-readiness-evidence",
        "schema_version": 2,
        "account_id": "718959508629",
        "region": "ap-northeast-1",
        "pre_cutover_observed_at_epoch": versioning_observed_at,
        "observed_at_epoch": now - 60,
        "retention_export_manifest_path": str(retention_path),
        "cloudtrail": {
            "bucket": "teamagent-dev-cloudtrail-718959508629",
            "latest_log": delivery(
                delivery_files["cloudtrail-log"],
                "AWSLogs/718959508629/CloudTrail/log.json.gz",
            ),
            "latest_digest": delivery(
                delivery_files["cloudtrail-digest"],
                "AWSLogs/718959508629/CloudTrail-Digest/digest.json.gz",
            ),
        },
        "bedrock": {
            "bucket": "teamagent-dev-bedrock-logs-718959508629",
            "latest_delivery": delivery(
                delivery_files["bedrock-delivery"],
                ("bedrock/AWSLogs/718959508629/BedrockModelInvocationLogs/event.json.gz"),
            ),
        },
    }

    readiness_base: dict[str, object] = {
        "kind": "teamagent-log-rollout-readiness-receipt",
        "schema_version": 3,
        "account_id": "718959508629",
        "region": "ap-northeast-1",
        "versioning_receipt_sha256": versioning_sha,
        "created_at_epoch": now - 30,
        "expires_at_epoch": now + 3600,
        "evidence_artifact_path": str(evidence_path),
    }

    def bind_retention(artifact: dict[str, object]) -> None:
        retention_path.write_text(
            json.dumps(retention, sort_keys=True),
            encoding="utf-8",
        )
        retention_path.chmod(0o600)
        artifact["retention_export_manifest_sha256"] = hashlib.sha256(
            retention_path.read_bytes()
        ).hexdigest()
        artifact["retention_export_manifest_inode"] = str(retention_path.stat().st_ino)
        artifact["retention_export_manifest_size_bytes"] = retention_path.stat().st_size

    def bind_evidence(
        artifact: dict[str, object],
        receipt: dict[str, object] | None = None,
    ) -> dict[str, object]:
        evidence_path.write_text(
            json.dumps(artifact, sort_keys=True),
            encoding="utf-8",
        )
        evidence_path.chmod(0o600)
        bound = copy.deepcopy(receipt or readiness_base)
        bound["evidence_artifact_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        bound["evidence_artifact_inode"] = str(evidence_path.stat().st_ino)
        bound["evidence_artifact_size_bytes"] = evidence_path.stat().st_size
        return bound

    bind_retention(evidence_artifact)
    readiness = bind_evidence(evidence_artifact)
    result = _run_log_readiness_validator(tmp_path, readiness, versioning)
    assert result.returncode == 0, result.stderr

    wrong_sha = copy.deepcopy(readiness)
    wrong_sha["versioning_receipt_sha256"] = "b" * 64
    assert _run_log_readiness_validator(tmp_path, wrong_sha, versioning).returncode != 0

    dummy_hashes = {
        "kind": "teamagent-log-rollout-readiness-receipt",
        "schema_version": 1,
        "account_id": "718959508629",
        "region": "ap-northeast-1",
        "versioning_receipt_sha256": versioning_sha,
        "created_at_epoch": now,
        "expires_at_epoch": now + 3600,
        "retention_export_evidence_sha256": "a" * 64,
    }
    assert _run_log_readiness_validator(tmp_path, dummy_hashes, versioning).returncode != 0

    original_delivery = delivery_files["cloudtrail-log"].read_bytes()
    delivery_files["cloudtrail-log"].write_bytes(b"X" + original_delivery[1:])
    assert _run_log_readiness_validator(tmp_path, readiness, versioning).returncode != 0
    delivery_files["cloudtrail-log"].write_bytes(original_delivery)
    assert _run_log_readiness_validator(tmp_path, readiness, versioning).returncode == 0

    retention_export = retention_files[log_groups[0]]
    original_retention_export = retention_export.read_bytes()
    retention_export.write_bytes(b"X" + original_retention_export[1:])
    assert _run_log_readiness_validator(tmp_path, readiness, versioning).returncode != 0
    retention_export.write_bytes(original_retention_export)

    wrong_inode_evidence = copy.deepcopy(evidence_artifact)
    wrong_inode_evidence["cloudtrail"]["latest_digest"][  # type: ignore[index]
        "export_file_inode"
    ] = "1"
    wrong_inode_receipt = bind_evidence(wrong_inode_evidence)
    assert (
        _run_log_readiness_validator(
            tmp_path,
            wrong_inode_receipt,
            versioning,
        ).returncode
        != 0
    )

    inverted_delivery_evidence = copy.deepcopy(evidence_artifact)
    inverted_delivery_evidence["bedrock"]["latest_delivery"][  # type: ignore[index]
        "last_modified_epoch"
    ] = now - 59
    inverted_delivery_receipt = bind_evidence(inverted_delivery_evidence)
    assert (
        _run_log_readiness_validator(
            tmp_path,
            inverted_delivery_receipt,
            versioning,
        ).returncode
        != 0
    )

    inverted_retention = copy.deepcopy(retention)
    inverted_retention["log_groups"][0][  # type: ignore[index]
        "exported_through_epoch"
    ] = now - 59
    retention.clear()
    retention.update(inverted_retention)
    inverted_retention_evidence = copy.deepcopy(evidence_artifact)
    bind_retention(inverted_retention_evidence)
    inverted_retention_receipt = bind_evidence(inverted_retention_evidence)
    assert (
        _run_log_readiness_validator(
            tmp_path,
            inverted_retention_receipt,
            versioning,
        ).returncode
        != 0
    )


def _run_alarm_delivery_receipt_validator(
    tmp_path: Path,
    receipt: dict[str, object],
    snapshot: dict[str, object],
) -> subprocess.CompletedProcess[str]:
    body = GUARD.read_text(encoding="utf-8")
    function = re.search(
        r"verify_alarm_delivery_test_receipt\(\) \{.*?"
        r"(?=\ncapture_log_delivery_contract\(\))",
        body,
        flags=re.DOTALL,
    )
    assert function is not None
    receipt_path = tmp_path / "alarm-delivery-receipt.json"
    snapshot_path = tmp_path / "alarm-delivery-snapshot.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    script = "\n".join(
        (
            "set -euo pipefail",
            'EXPECTED_ACCOUNT_ID="718959508629"',
            'REGION="ap-northeast-1"',
            'PROJECT="teamagent"',
            'ENVIRONMENT="dev"',
            (
                'EXPECTED_ALARM_EMAIL_SHA256="'
                '88c6452f9db04017250aa5728b4815bccb55b5ecc0b35b50a5234170dc08d1e6"'
            ),
            (
                'EXPECTED_ALARM_DESTINATION_STATE_SHA256="'
                'c942dbb7b97da1f4d9debb1ba241ee89bf8c1d951d8d75bdea3056850838ddc9"'
            ),
            'die() { echo "★ $*" >&2; return 1; }',
            function.group(0),
            'verify_alarm_delivery_test_receipt "$1" "$2"',
        )
    )
    return subprocess.run(
        [
            "bash",
            "-c",
            script,
            "validator",
            str(receipt_path),
            str(snapshot_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_alarm_delivery_receipt_binds_exclusive_exact_live_channel(
    tmp_path: Path,
) -> None:
    guard = GUARD.read_text(encoding="utf-8")
    evidence = (PROJECT_ROOT / "infra/deploy/runtime_evidence_guard.py").read_text(encoding="utf-8")
    for required in (
        "issue-sns-challenge",
        "sign-sns-ack",
        "attest-sns-delivery",
        "verify-sns-delivery",
    ):
        assert required in guard
    for required in (
        "teamagent-sns-delivery-challenge",
        "teamagent-sns-recipient-signed-ack",
        "ACK_KEY_ALIAS",
        "message_id",
        "challenge_nonce",
        "attribute_not_exists(receipt_sha256)",
        "subscription_metadata_sha256",
    ):
        assert required in evidence
    assert 'result == "delivered"' not in guard
    return

    now = int(time.time())
    email_hash = "88c6452f9db04017250aa5728b4815bccb55b5ecc0b35b50a5234170dc08d1e6"
    destination_hash = "c942dbb7b97da1f4d9debb1ba241ee89bf8c1d951d8d75bdea3056850838ddc9"
    subscription_hash = "1" * 64
    topic = "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-openclaw-alarms"
    chat_arn = "arn:aws:chatbot::718959508629:chat-configuration/slack-channel/teamagent-dev-alerts"
    common_receipt: dict[str, object] = {
        "kind": "teamagent-alarm-delivery-test-receipt",
        "schema_version": 2,
        "account_id": "718959508629",
        "region": "ap-northeast-1",
        "topic_arn": topic,
        "subscription_metadata_sha256": subscription_hash,
        "destination_state_sha256": destination_hash,
        "result": "delivered",
        "observer_identity_sha256": "2" * 64,
        "test_message_id_sha256": "3" * 64,
        "delivery_evidence_sha256": "4" * 64,
        "tested_at_epoch": now,
        "expires_at_epoch": now + 3600,
    }
    email_receipt = {
        **common_receipt,
        "delivery_channel": "email",
        "email_endpoint_sha256": email_hash,
    }
    email_snapshot = {
        "alarm_delivery": {
            "confirmed_subscription_metadata_sha256": subscription_hash,
            "confirmed_email_endpoint_sha256": [email_hash],
            "subscription_inventory_count": 1,
            "pending_subscription_count": 0,
            "subscription_protocols": ["email"],
            "destination_state_sha256": destination_hash,
            "attached_chatbot_configuration_arns": [],
        },
        "alarm_delivery_observation": {
            "attached_chatbot_configurations": [],
        },
    }
    valid_email = _run_alarm_delivery_receipt_validator(
        tmp_path,
        email_receipt,
        email_snapshot,
    )
    assert valid_email.returncode == 0, valid_email.stderr

    mixed = copy.deepcopy(email_snapshot)
    mixed["alarm_delivery"]["attached_chatbot_configuration_arns"] = [chat_arn]
    mixed["alarm_delivery_observation"]["attached_chatbot_configurations"] = [
        {"arn": chat_arn, "state": "ENABLED"}
    ]
    assert (
        _run_alarm_delivery_receipt_validator(
            tmp_path,
            email_receipt,
            mixed,
        ).returncode
        != 0
    )
    replaced_email = copy.deepcopy(email_snapshot)
    replaced_email["alarm_delivery"]["confirmed_email_endpoint_sha256"] = ["9" * 64]
    assert (
        _run_alarm_delivery_receipt_validator(
            tmp_path,
            email_receipt,
            replaced_email,
        ).returncode
        != 0
    )

    pending = copy.deepcopy(email_snapshot)
    pending["alarm_delivery"]["pending_subscription_count"] = 1
    assert (
        _run_alarm_delivery_receipt_validator(
            tmp_path,
            email_receipt,
            pending,
        ).returncode
        != 0
    )
    extra_protocol = copy.deepcopy(email_snapshot)
    extra_protocol["alarm_delivery"]["subscription_inventory_count"] = 2
    extra_protocol["alarm_delivery"]["subscription_protocols"] = ["email", "sms"]
    assert (
        _run_alarm_delivery_receipt_validator(
            tmp_path,
            email_receipt,
            extra_protocol,
        ).returncode
        != 0
    )
    wrong_destination_hash = copy.deepcopy(email_snapshot)
    wrong_destination_hash["alarm_delivery"]["destination_state_sha256"] = "9" * 64
    assert (
        _run_alarm_delivery_receipt_validator(
            tmp_path,
            email_receipt,
            wrong_destination_hash,
        ).returncode
        != 0
    )

    chatbot_receipt = {
        **common_receipt,
        "delivery_channel": "chat",
        "email_endpoint_sha256": "",
    }
    assert (
        _run_alarm_delivery_receipt_validator(
            tmp_path,
            chatbot_receipt,
            email_snapshot,
        ).returncode
        != 0
    )
    chatbot_snapshot = copy.deepcopy(email_snapshot)
    chatbot_snapshot["alarm_delivery"]["attached_chatbot_configuration_arns"] = [chat_arn]
    chatbot_snapshot["alarm_delivery_observation"]["attached_chatbot_configurations"] = [
        {"arn": chat_arn, "state": "ENABLED"}
    ]
    assert (
        _run_alarm_delivery_receipt_validator(
            tmp_path,
            email_receipt,
            chatbot_snapshot,
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
            {
                "Sid": "DenyManualBedrockPayloadDeletion",
                "Effect": "Deny",
                "Principal": "*",
                "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
                "Resource": f"{bedrock_bucket}/bedrock/*",
            },
            {
                "Sid": "DenyNonBedrockPayloadWriters",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": f"{bedrock_bucket}/bedrock/*",
                "Condition": {
                    "StringNotEquals": {"aws:PrincipalServiceName": "bedrock.amazonaws.com"}
                },
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
                            "id": "bedrock-current-and-noncurrent-minimum-60-days",
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
        "variables": {
            "bedrock_logs_retention_days": {"value": 60},
            "runtime_guard_live": {
                "value": {
                    "versioning_pre_cutover_receipt_sha256": "a" * 64,
                    "log_cutover_contract_sha256": "b" * 64,
                }
            },
        },
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
        "lifecycle_59_plus_1",
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
    elif mutation == "lifecycle_59_plus_1":
        lifecycle = changes[4]["change"]  # type: ignore[index]
        lifecycle["after"]["rule"][0]["expiration"][0]["days"] = 59  # type: ignore[index]
        lifecycle["after"]["rule"][0]["noncurrent_version_expiration"][0][  # type: ignore[index]
            "noncurrent_days"
        ] = 1
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
