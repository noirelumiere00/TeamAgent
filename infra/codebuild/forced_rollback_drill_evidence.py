#!/usr/bin/env python3
"""Pure schema and invariant validation for forced-rollback drill evidence.

This module deliberately performs no network or file I/O.  Callers collect the
AWS observations and pass one complete aggregate here; this layer only decides
whether that built-in ``dict`` is a canonical, internally bound drill record.

All timestamps are RFC3339 UTC with a ``Z`` suffix and whole-second precision.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import re
from pathlib import PurePosixPath
from typing import Any

from teamagent_release_approval import ProvenanceError, canonical_json_bytes

DRILL_SCHEMA_VERSION = 1
DRILL_KIND = "teamagent.forced-rollback-drill"
ACCOUNT_ID = "718959508629"
REGION = "ap-northeast-1"
ENVIRONMENT_NAME = "dev"
MAX_START_DELAY_SECONDS = 1800
MAX_OLD_DWELL_SECONDS = 1200
SIGNING_ALGORITHM = "RSASSA_PSS_SHA_256"
TRUSTED_AUTOMATION_ARN = (
    f"arn:aws:sts::{ACCOUNT_ID}:assumed-role/"
    "teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker"
)
APPROVAL_APPROVED_BY_ARN = (
    f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-approval-caller"
)

DRILL_STATUSES = frozenset({"PASSED", "FAILED", "RECONCILE_REQUIRED"})
LEG_RESULTS = frozenset({"PASSED", "FAILED"})
SAFE_TERMINAL_CLASSIFICATIONS = frozenset(
    {"INITIAL_NEW", "PREVIOUS_OLD", "UNKNOWN"}
)
RECOVERY_RESULTS = frozenset(
    {"PASSED", "FAILED", "NOT_ATTEMPTED", "NOT_REQUIRED"}
)

_ROOT_KEYS = {
    "schema_version",
    "kind",
    "drill_id",
    "status",
    "environment",
    "control",
    "actors",
    "scope",
    "baseline",
    "legs",
    "safe_terminal_state",
    "artifact_manifest",
    "integrity",
}
_ENVIRONMENT_KEYS = {"account_id", "region", "name"}
_CONTROL_KEYS = {
    "git_commit",
    "drill_contract_sha256",
    "initial_release_apply",
    "initial_release_verified_at_utc",
    "started_at_utc",
    "completed_at_utc",
    "max_start_delay_seconds",
    "max_old_dwell_seconds",
}
_ACTORS_KEYS = {
    "initiating_principal",
    "automation_principals",
    "approvals",
}
_PRINCIPAL_KEYS = {"arn", "user_id", "source_identity"}
_AUTOMATION_PRINCIPAL_KEYS = {"account_id", "arn", "user_id"}
_ACTOR_APPROVAL_KEYS = {"approval_id", "approved_by_arn"}
_SCOPE_KEYS = {"pipelines", "subjects", "resources"}
_SCOPE_SUBJECT_KEYS = {
    "pipeline",
    "name",
    "release_repository",
    "previous_digest",
    "initial_new_digest",
}
_SCOPE_RESOURCE_KEYS = {
    "consumer_id",
    "terraform_address",
    "pipeline",
    "subject",
    "previous_task_definition_arn",
    "previous_task_revision",
    "initial_new_task_definition_arn",
    "initial_new_task_revision",
}
_SNAPSHOT_KEYS = {"subjects", "resources"}
_SNAPSHOT_SUBJECT_KEYS = {"pipeline", "name", "release_repository", "digest"}
_SNAPSHOT_RESOURCE_KEYS = {
    "consumer_id",
    "terraform_address",
    "pipeline",
    "subject",
    "task_definition_arn",
    "task_revision",
    "digest",
}
_SNAPSHOT_EVIDENCE_KEYS = {"snapshot", "locator"}
_BASELINE_KEYS = {
    "terraform_lineage",
    "terraform_serial",
    "live_snapshot",
    "initial_new_verified",
}
_LEG_KEYS = {
    "ordinal",
    "name",
    "channel",
    "from",
    "to",
    "release_authorizations",
    "plan",
    "approval",
    "apply",
    "ecs",
    "run_task_health",
    "dm_qa",
    "started_at_utc",
    "completed_at_utc",
    "result",
    "recovery",
}
_RELEASE_AUTHORIZATION_KEYS = {
    "authorization_id",
    "deployment_intent_id",
    "drill_id",
    "pipeline",
    "channel",
    "subjects",
    "issued_at_utc",
    "release_approval_id",
    "release_approved_by_arn",
    "receipt_sha256",
    "locator",
}
_PLAN_KEYS = {
    "sha256",
    "receipt_sha256",
    "created_at_utc",
    "terraform_lineage",
    "terraform_serial",
    "from",
    "to",
    "changed_resources",
    "locator",
}
_APPROVAL_KEYS = {
    "confirmation_id",
    "drill_id",
    "action",
    "plan_sha256",
    "approval_text_sha256",
    "consumed_at_utc",
    "release_approval",
    "receipt_sha256",
    "locator",
}
_RELEASE_APPROVAL_KEYS = {
    "approval_id",
    "approved_at_utc",
    "approved_by",
    "decision",
    "expires_at_utc",
    "forced_gate_sha256",
    "payload",
    "pipeline",
    "signature",
    "source_commit",
}
_EXTERNAL_LOCATOR_KEYS = {"bucket", "key", "version_id", "sha256"}
_APPLY_KEYS = {
    "apply_attempt_id",
    "plan_sha256",
    "receipt_sha256",
    "started_at_utc",
    "completed_at_utc",
    "result",
    "terraform_lineage",
    "terraform_serial_before",
    "terraform_serial_after",
    "state",
    "automation_principal",
    "automation_identity_sha256",
    "automation_identity_locator",
    "locator",
}
_ECS_KEYS = {
    "result",
    "steady",
    "verified_at_utc",
    "live_snapshot",
    "locator",
}
_RUN_TASK_HEALTH_KEYS = {
    "result",
    "verified_at_utc",
    "apply_attempt_id",
    "task_definition_arn",
    "image",
    "log_stream_name",
    "task",
    "checks",
    "locator",
}
_RUN_TASK_KEYS = {
    "task_arn",
    "task_definition_arn",
    "image",
    "image_digest",
    "exit_code",
    "stopped_reason_code",
    "log_stream_name",
}
_RUN_TASK_CHECK_KEYS = {
    "connect_build_inputs_sha256",
    "connect_contract_ok",
    "connect_http_200",
    "connect_manifest_sha256",
    "connect_sha256",
    "connect_version_id",
    "mcp_http_200",
}
_DM_QA_KEYS = {
    "result",
    "verified_at_utc",
    "apply_attempt_id",
    "mcp_task_definition_arn",
    "openclaw_task_definition_arn",
    "locator",
}
_RECOVERY_KEYS = {
    "attempted",
    "result",
    "completed_at_utc",
    "last_exact_confirmed_digests",
    "locator",
}
_SAFE_TERMINAL_KEYS = {
    "classification",
    "steady",
    "verified_at_utc",
    "live_snapshot",
}
_LOCATOR_KEYS = {
    "bucket",
    "key",
    "version_id",
    "sha256",
    "size",
    "content_type",
    "object_lock_mode",
    "retain_until",
    "encryption_kms_key_arn",
    "signature",
    "signer",
    "exact_version_redownload",
}
_SIGNATURE_KEYS = {"key", "version_id", "sha256", "verified"}
_SIGNER_KEYS = {"kms_key_arn", "algorithm"}
_REDOWNLOAD_KEYS = {
    "requested_version_id",
    "returned_version_id",
    "sha256",
    "size",
    "bytes_match",
}
_INTEGRITY_KEYS = {
    "canonical_sha256",
    "kms_key_arn",
    "signing_algorithm",
    "signature",
    "immutable_object",
}
_EXPECTED_BINDING_KEYS = {
    "git_commit",
    "drill_contract_sha256",
    "initial_release_apply",
    "initial_release_verified_at_utc",
    "scope",
}
_IMMUTABLE_OBJECT_KEYS = {
    "bucket",
    "key",
    "version_id",
    "sha256",
    "size",
    "content_type",
    "object_lock_mode",
    "retain_until",
    "encryption_kms_key_arn",
    "exact_version_redownload",
}

_GIT_OID_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
_UUID4_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_RFC3339_UTC_RE = re.compile(
    r"[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z"
)
_S3_VERSION_ID_RE = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}")
_S3_BUCKET_RE = re.compile(
    r"(?=.{3,63}\Z)(?![0-9]+(?:\.[0-9]+){3}\Z)"
    r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?"
)
_KMS_KEY_ARN_RE = re.compile(
    rf"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/[A-Za-z0-9-]{{1,256}}"
)
_PRINCIPAL_ARN_RE = re.compile(
    rf"arn:aws:(?:iam|sts)::{ACCOUNT_ID}:"
    r"(?:role|user|assumed-role)/[A-Za-z0-9+=,.@_/-]{1,512}"
)
_IAM_ROLE_ARN_RE = re.compile(
    rf"arn:aws:iam::{ACCOUNT_ID}:role/[A-Za-z0-9+=,.@_/-]{{1,512}}"
)
_PIPELINE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}")
_REPOSITORY_RE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,255}")
_TERRAFORM_ADDRESS_RE = re.compile(
    r"aws_ecs_task_definition\.[a-z][a-z0-9_]*(?:\[[0-9]+\])?"
)
_TASK_DEFINITION_ARN_RE = re.compile(
    rf"arn:aws:ecs:{REGION}:{ACCOUNT_ID}:task-definition/"
    r"([A-Za-z0-9_-]{1,255}):([1-9][0-9]*)"
)
_TASK_ARN_RE = re.compile(
    rf"arn:aws:ecs:{REGION}:{ACCOUNT_ID}:task/"
    r"[A-Za-z0-9_-]{1,255}/[0-9a-f]{32}"
)
_ECR_IMAGE_RE = re.compile(
    rf"{ACCOUNT_ID}\.dkr\.ecr\.{REGION}\.amazonaws\.com/"
    r"[a-z0-9][a-z0-9._/-]{0,255}@sha256:[0-9a-f]{64}"
)
_CONTENT_TYPE_RE = re.compile(
    r"[a-z0-9][a-z0-9.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9.+_-]{0,126}"
)


class DrillEvidenceError(ValueError):
    """The drill aggregate is malformed or violates a fail-closed invariant."""


def _require_builtin_json_types(
    value: Any,
    *,
    label: str,
    active_container_ids: set[int] | None = None,
    depth: int = 0,
) -> None:
    if depth > 64:
        raise DrillEvidenceError(f"{label} exceeds the JSON nesting limit")
    if active_container_ids is None:
        active_container_ids = set()
    if type(value) is dict:
        identity = id(value)
        if identity in active_container_ids:
            raise DrillEvidenceError(f"{label} contains a JSON container cycle")
        active_container_ids.add(identity)
        try:
            for key, item in value.items():
                if type(key) is not str:
                    raise DrillEvidenceError(
                        f"{label} keys must be built-in strings"
                    )
                _require_builtin_json_types(
                    item,
                    label=f"{label}.{key}",
                    active_container_ids=active_container_ids,
                    depth=depth + 1,
                )
        finally:
            active_container_ids.remove(identity)
        return
    if type(value) is list:
        identity = id(value)
        if identity in active_container_ids:
            raise DrillEvidenceError(f"{label} contains a JSON container cycle")
        active_container_ids.add(identity)
        try:
            for index, item in enumerate(value):
                _require_builtin_json_types(
                    item,
                    label=f"{label}[{index}]",
                    active_container_ids=active_container_ids,
                    depth=depth + 1,
                )
        finally:
            active_container_ids.remove(identity)
        return
    if value is None or type(value) in {bool, int, float, str}:
        return
    raise DrillEvidenceError(f"{label} contains a non-JSON or non-built-in value")


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise DrillEvidenceError(f"{label} must be a built-in object")
    if any(type(key) is not str for key in value):
        raise DrillEvidenceError(f"{label} keys must be built-in strings")
    return value


def _exact_object(
    value: Any,
    expected: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    item = _object(value, label=label)
    actual = set(item)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise DrillEvidenceError(
            f"{label} schema mismatch: missing={missing}, unknown={unknown}"
        )
    return item


def _array(value: Any, *, label: str) -> list[Any]:
    if type(value) is not list:
        raise DrillEvidenceError(f"{label} must be a built-in array")
    return value


def _text(value: Any, *, label: str, maximum: int = 2048) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise DrillEvidenceError(f"{label} must be canonical non-empty text")
    return value


def _optional_text(value: Any, *, label: str, maximum: int = 2048) -> str | None:
    if value is None:
        return None
    return _text(value, label=label, maximum=maximum)


def _integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise DrillEvidenceError(f"{label} must be an integer >= {minimum}")
    return value


def _literal_integer(value: Any, expected: int, *, label: str) -> int:
    if type(value) is not int or value != expected:
        raise DrillEvidenceError(f"{label} must be integer {expected}")
    return value


def _boolean(value: Any, *, label: str) -> bool:
    if type(value) is not bool:
        raise DrillEvidenceError(f"{label} must be a boolean")
    return value


def _true(value: Any, *, label: str) -> None:
    if type(value) is not bool or value is not True:
        raise DrillEvidenceError(f"{label} must be true")


def _choice(value: Any, choices: frozenset[str], *, label: str) -> str:
    value = _text(value, label=label, maximum=64)
    if value not in choices:
        raise DrillEvidenceError(f"{label} is unsupported")
    return value


def _pattern(
    value: Any,
    pattern: re.Pattern[str],
    *,
    label: str,
    description: str,
    maximum: int = 2048,
) -> str:
    value = _text(value, label=label, maximum=maximum)
    if not pattern.fullmatch(value):
        raise DrillEvidenceError(f"{label} must be {description}")
    return value


def _sha256(value: Any, *, label: str) -> str:
    return _pattern(
        value,
        _SHA256_RE,
        label=label,
        description="a lowercase SHA-256",
        maximum=64,
    )


def _digest(value: Any, *, label: str) -> str:
    return _pattern(
        value,
        _DIGEST_RE,
        label=label,
        description="a lowercase sha256 OCI digest",
        maximum=71,
    )


def _uuid(value: Any, *, label: str) -> str:
    return _pattern(
        value,
        _UUID_RE,
        label=label,
        description="a canonical lowercase UUID",
        maximum=36,
    )


def _uuid4(value: Any, *, label: str) -> str:
    return _pattern(
        value,
        _UUID4_RE,
        label=label,
        description="a canonical lowercase UUIDv4",
        maximum=36,
    )


def _timestamp(value: Any, *, label: str) -> dt.datetime:
    value = _pattern(
        value,
        _RFC3339_UTC_RE,
        label=label,
        description="an RFC3339 UTC timestamp at whole-second precision",
        maximum=20,
    )
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.UTC
        )
    except ValueError as exc:
        raise DrillEvidenceError(f"{label} is not a valid UTC timestamp") from exc


def _version_id(value: Any, *, label: str) -> str:
    value = _text(value, label=label, maximum=1024)
    if value in {"None", "null"} or not _S3_VERSION_ID_RE.fullmatch(value):
        raise DrillEvidenceError(f"{label} must be an exact S3 VersionId")
    return value


def _kms_key_arn(value: Any, *, label: str) -> str:
    return _pattern(
        value,
        _KMS_KEY_ARN_RE,
        label=label,
        description=f"a {ACCOUNT_ID}/{REGION} KMS key ARN",
        maximum=512,
    )


def _task_definition_arn(value: Any, *, label: str) -> tuple[str, int]:
    value = _text(value, label=label, maximum=512)
    match = _TASK_DEFINITION_ARN_RE.fullmatch(value)
    if match is None:
        raise DrillEvidenceError(
            f"{label} must be an exact {ACCOUNT_ID}/{REGION} task definition ARN"
        )
    return match.group(1), int(match.group(2))


def canonical_drill_body_bytes(value: dict[str, Any]) -> bytes:
    """Return the canonical bytes bound by ``integrity.canonical_sha256``.

    The preimage is the aggregate with ``integrity.signature`` and
    ``integrity.immutable_object`` removed.  ``integrity.canonical_sha256`` is
    necessarily removed as well: retaining the digest inside its own preimage
    would make the definition circular.  The KMS ARN and signing algorithm stay
    in the preimage.  Rendering itself is delegated to the release-approval
    canonical JSON implementation (UTF-8, sorted compact JSON, one trailing LF).
    """

    if type(value) is not dict:
        raise DrillEvidenceError("drill evidence must be a built-in object")
    _require_builtin_json_types(value, label="drill")
    try:
        projected = copy.deepcopy(value)
    except (RecursionError, TypeError) as exc:
        raise DrillEvidenceError("drill evidence cannot be copied") from exc
    integrity = _object(
        projected.get("integrity"),
        label="drill.integrity",
    )
    for key in ("canonical_sha256", "signature", "immutable_object"):
        if key not in integrity:
            raise DrillEvidenceError(
                f"drill.integrity.{key} is required for canonical projection"
            )
        del integrity[key]
    try:
        return canonical_json_bytes(projected)
    except ProvenanceError as exc:
        raise DrillEvidenceError("drill evidence is not canonical JSON data") from exc


def canonical_drill_sha256(value: dict[str, Any]) -> str:
    """Hash the canonical drill body projection."""

    return hashlib.sha256(canonical_drill_body_bytes(value)).hexdigest()


def _validate_signature(
    value: Any,
    *,
    payload_key: str,
    label: str,
) -> dict[str, Any]:
    signature = _exact_object(value, _SIGNATURE_KEYS, label=label)
    key = _text(signature["key"], label=f"{label}.key", maximum=1024)
    if key != f"{payload_key}.sig":
        raise DrillEvidenceError(f"{label}.key must be the payload key plus '.sig'")
    _version_id(signature["version_id"], label=f"{label}.version_id")
    _sha256(signature["sha256"], label=f"{label}.sha256")
    _true(signature["verified"], label=f"{label}.verified")
    return signature


def _validate_redownload(
    value: Any,
    *,
    version_id: str,
    sha256: str,
    size: int,
    label: str,
) -> None:
    redownload = _exact_object(value, _REDOWNLOAD_KEYS, label=label)
    requested = _version_id(
        redownload["requested_version_id"],
        label=f"{label}.requested_version_id",
    )
    returned = _version_id(
        redownload["returned_version_id"],
        label=f"{label}.returned_version_id",
    )
    redownload_sha = _sha256(redownload["sha256"], label=f"{label}.sha256")
    redownload_size = _integer(
        redownload["size"],
        label=f"{label}.size",
        minimum=1,
    )
    _true(redownload["bytes_match"], label=f"{label}.bytes_match")
    if requested != version_id or returned != version_id:
        raise DrillEvidenceError(
            f"{label} must request and return the locator's exact VersionId"
        )
    if redownload_sha != sha256 or redownload_size != size:
        raise DrillEvidenceError(
            f"{label} SHA-256 and size must exactly match the locator"
        )


def _validate_locator(
    value: Any,
    *,
    label: str,
    aggregate_completed_at: dt.datetime,
) -> dict[str, Any]:
    locator = _exact_object(value, _LOCATOR_KEYS, label=label)
    bucket = _text(locator["bucket"], label=f"{label}.bucket", maximum=63)
    if (
        not _S3_BUCKET_RE.fullmatch(bucket)
        or ".." in bucket
        or ".-" in bucket
        or "-." in bucket
    ):
        raise DrillEvidenceError(f"{label}.bucket must be a canonical S3 bucket")

    key = _text(locator["key"], label=f"{label}.key", maximum=1024)
    parsed_key = PurePosixPath(key)
    raw_key_parts = key.split("/")
    if (
        parsed_key.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_key_parts)
        or "\\" in key
    ):
        raise DrillEvidenceError(f"{label}.key must be a safe exact S3 key")

    version_id = _version_id(locator["version_id"], label=f"{label}.version_id")
    sha256 = _sha256(locator["sha256"], label=f"{label}.sha256")
    size = _integer(locator["size"], label=f"{label}.size", minimum=1)
    _pattern(
        locator["content_type"],
        _CONTENT_TYPE_RE,
        label=f"{label}.content_type",
        description="a canonical media type",
        maximum=255,
    )
    if locator["object_lock_mode"] != "COMPLIANCE":
        raise DrillEvidenceError(f"{label}.object_lock_mode must be COMPLIANCE")
    retain_until = _timestamp(
        locator["retain_until"],
        label=f"{label}.retain_until",
    )
    if retain_until <= aggregate_completed_at:
        raise DrillEvidenceError(
            f"{label}.retain_until must be after drill completion"
        )
    _kms_key_arn(
        locator["encryption_kms_key_arn"],
        label=f"{label}.encryption_kms_key_arn",
    )
    _validate_signature(
        locator["signature"],
        payload_key=key,
        label=f"{label}.signature",
    )
    signer = _exact_object(
        locator["signer"],
        _SIGNER_KEYS,
        label=f"{label}.signer",
    )
    _kms_key_arn(signer["kms_key_arn"], label=f"{label}.signer.kms_key_arn")
    if signer["algorithm"] != SIGNING_ALGORITHM:
        raise DrillEvidenceError(
            f"{label}.signer.algorithm must be {SIGNING_ALGORITHM}"
        )
    _validate_redownload(
        locator["exact_version_redownload"],
        version_id=version_id,
        sha256=sha256,
        size=size,
        label=f"{label}.exact_version_redownload",
    )
    return locator


def _locator_identity(locator: dict[str, Any]) -> tuple[str, str, str]:
    return locator["bucket"], locator["key"], locator["version_id"]


def _validate_principal(value: Any, *, label: str) -> dict[str, Any]:
    principal = _exact_object(value, _PRINCIPAL_KEYS, label=label)
    _pattern(
        principal["arn"],
        _PRINCIPAL_ARN_RE,
        label=f"{label}.arn",
        description=f"an IAM or STS principal ARN in account {ACCOUNT_ID}",
        maximum=600,
    )
    _text(principal["user_id"], label=f"{label}.user_id", maximum=512)
    _optional_text(
        principal["source_identity"],
        label=f"{label}.source_identity",
        maximum=256,
    )
    return principal


def _validate_automation_principal(
    value: Any,
    *,
    label: str,
) -> dict[str, Any]:
    principal = _exact_object(
        value,
        _AUTOMATION_PRINCIPAL_KEYS,
        label=label,
    )
    if principal["account_id"] != ACCOUNT_ID:
        raise DrillEvidenceError(
            f"{label}.account_id is not the fixed AWS account"
        )
    _pattern(
        principal["arn"],
        _PRINCIPAL_ARN_RE,
        label=f"{label}.arn",
        description=f"an IAM or STS principal ARN in account {ACCOUNT_ID}",
        maximum=600,
    )
    if principal["arn"] != TRUSTED_AUTOMATION_ARN:
        raise DrillEvidenceError(
            f"{label}.arn is not the exact trusted automation caller"
        )
    _text(principal["user_id"], label=f"{label}.user_id", maximum=512)
    return principal


def _automation_principal_identity(
    principal: dict[str, Any],
) -> tuple[str, str, str]:
    return (
        principal["account_id"],
        principal["arn"],
        principal["user_id"],
    )


def _principal_arn(value: Any, *, label: str) -> str:
    return _pattern(
        value,
        _PRINCIPAL_ARN_RE,
        label=label,
        description=f"an IAM or STS principal ARN in account {ACCOUNT_ID}",
        maximum=600,
    )


def _validate_external_locator(value: Any, *, label: str) -> dict[str, Any]:
    locator = _exact_object(value, _EXTERNAL_LOCATOR_KEYS, label=label)
    bucket = _text(locator["bucket"], label=f"{label}.bucket", maximum=63)
    if (
        not _S3_BUCKET_RE.fullmatch(bucket)
        or ".." in bucket
        or ".-" in bucket
        or "-." in bucket
    ):
        raise DrillEvidenceError(f"{label}.bucket must be a canonical S3 bucket")
    key = _text(locator["key"], label=f"{label}.key", maximum=1024)
    parsed_key = PurePosixPath(key)
    if (
        parsed_key.is_absolute()
        or any(part in {"", ".", ".."} for part in key.split("/"))
        or "\\" in key
    ):
        raise DrillEvidenceError(f"{label}.key must be a safe exact S3 key")
    _version_id(locator["version_id"], label=f"{label}.version_id")
    _sha256(locator["sha256"], label=f"{label}.sha256")
    return locator


def _validate_environment(value: Any) -> None:
    environment = _exact_object(
        value,
        _ENVIRONMENT_KEYS,
        label="drill.environment",
    )
    expected = {
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "name": ENVIRONMENT_NAME,
    }
    if environment != expected:
        raise DrillEvidenceError("drill.environment must exactly match dev")


def _validate_control(
    value: Any,
    *,
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    control = _exact_object(value, _CONTROL_KEYS, label="drill.control")
    _pattern(
        control["git_commit"],
        _GIT_OID_RE,
        label="drill.control.git_commit",
        description="a 40-character lowercase Git OID",
        maximum=40,
    )
    _sha256(
        control["drill_contract_sha256"],
        label="drill.control.drill_contract_sha256",
    )
    verified_at = _timestamp(
        control["initial_release_verified_at_utc"],
        label="drill.control.initial_release_verified_at_utc",
    )
    started_at = _timestamp(
        control["started_at_utc"],
        label="drill.control.started_at_utc",
    )
    completed_at = _timestamp(
        control["completed_at_utc"],
        label="drill.control.completed_at_utc",
    )
    if not verified_at <= started_at <= completed_at:
        raise DrillEvidenceError(
            "drill.control timestamps must be monotonically nondecreasing"
        )
    _literal_integer(
        control["max_start_delay_seconds"],
        MAX_START_DELAY_SECONDS,
        label="drill.control.max_start_delay_seconds",
    )
    _literal_integer(
        control["max_old_dwell_seconds"],
        MAX_OLD_DWELL_SECONDS,
        label="drill.control.max_old_dwell_seconds",
    )
    initial_apply = _validate_locator(
        control["initial_release_apply"],
        label="drill.control.initial_release_apply",
        aggregate_completed_at=completed_at,
    )
    locators.append(initial_apply)
    return {
        "verified_at": verified_at,
        "started_at": started_at,
        "completed_at": completed_at,
    }


def _validate_actors(value: Any) -> dict[str, Any]:
    actors = _exact_object(value, _ACTORS_KEYS, label="drill.actors")
    initiating = _validate_principal(
        actors["initiating_principal"],
        label="drill.actors.initiating_principal",
    )

    automation_values = _array(
        actors["automation_principals"],
        label="drill.actors.automation_principals",
    )
    if not automation_values:
        raise DrillEvidenceError(
            "drill.actors.automation_principals must not be empty"
        )
    automation: list[dict[str, Any]] = []
    for index, item in enumerate(automation_values):
        automation.append(
            _validate_automation_principal(
                item,
                label=f"drill.actors.automation_principals[{index}]",
            )
        )
    automation_identities = [
        _automation_principal_identity(item) for item in automation
    ]
    if len(automation_identities) != len(set(automation_identities)):
        raise DrillEvidenceError(
            "drill.actors.automation_principals must be unique"
        )
    if automation_identities != sorted(automation_identities):
        raise DrillEvidenceError(
            "drill.actors.automation_principals must be canonically sorted"
        )
    if any(
        initiating["arn"] == item["arn"]
        and initiating["user_id"] == item["user_id"]
        for item in automation
    ):
        raise DrillEvidenceError(
            "drill initiating and automation principals must be distinct"
        )

    approval_values = _array(
        actors["approvals"],
        label="drill.actors.approvals",
    )
    if len(approval_values) != 2:
        raise DrillEvidenceError(
            "drill.actors.approvals must contain exactly two approvals"
        )
    approvals: list[dict[str, Any]] = []
    approval_ids: list[str] = []
    for index, raw_approval in enumerate(approval_values):
        label = f"drill.actors.approvals[{index}]"
        actor_approval = _exact_object(
            raw_approval,
            _ACTOR_APPROVAL_KEYS,
            label=label,
        )
        approval_ids.append(
            _uuid4(actor_approval["approval_id"], label=f"{label}.approval_id")
        )
        _principal_arn(
            actor_approval["approved_by_arn"],
            label=f"{label}.approved_by_arn",
        )
        if actor_approval["approved_by_arn"] != APPROVAL_APPROVED_BY_ARN:
            raise DrillEvidenceError(
                f"{label}.approved_by_arn is not the fixed approval caller"
            )
        approvals.append(actor_approval)
    if len(set(approval_ids)) != 2:
        raise DrillEvidenceError("drill approval IDs must be distinct")
    return {
        "initiating": initiating,
        "automation": automation,
        "approvals": approvals,
    }


def _validate_scope(value: Any) -> dict[str, Any]:
    scope = _exact_object(value, _SCOPE_KEYS, label="drill.scope")
    raw_pipelines = _array(scope["pipelines"], label="drill.scope.pipelines")
    if not raw_pipelines:
        raise DrillEvidenceError("drill.scope.pipelines must not be empty")
    pipelines = [
        _pattern(
            pipeline,
            _PIPELINE_RE,
            label=f"drill.scope.pipelines[{index}]",
            description="a canonical pipeline name",
            maximum=64,
        )
        for index, pipeline in enumerate(raw_pipelines)
    ]
    if pipelines != sorted(set(pipelines)):
        raise DrillEvidenceError(
            "drill.scope.pipelines must be unique and canonically sorted"
        )

    raw_subjects = _array(scope["subjects"], label="drill.scope.subjects")
    if not raw_subjects:
        raise DrillEvidenceError("drill.scope.subjects must not be empty")
    subjects: list[dict[str, Any]] = []
    subject_identities: list[tuple[str, str]] = []
    for index, raw_subject in enumerate(raw_subjects):
        label = f"drill.scope.subjects[{index}]"
        subject = _exact_object(raw_subject, _SCOPE_SUBJECT_KEYS, label=label)
        pipeline = _pattern(
            subject["pipeline"],
            _PIPELINE_RE,
            label=f"{label}.pipeline",
            description="a canonical pipeline name",
            maximum=64,
        )
        name = _pattern(
            subject["name"],
            _IDENTIFIER_RE,
            label=f"{label}.name",
            description="a canonical subject name",
            maximum=128,
        )
        _pattern(
            subject["release_repository"],
            _REPOSITORY_RE,
            label=f"{label}.release_repository",
            description="a canonical release repository",
            maximum=256,
        )
        previous = _digest(
            subject["previous_digest"],
            label=f"{label}.previous_digest",
        )
        initial_new = _digest(
            subject["initial_new_digest"],
            label=f"{label}.initial_new_digest",
        )
        if pipeline not in pipelines:
            raise DrillEvidenceError(f"{label}.pipeline is outside drill.scope")
        if previous == initial_new:
            raise DrillEvidenceError(
                f"{label} previous and initial-new digests must differ"
            )
        subjects.append(subject)
        subject_identities.append((pipeline, name))
    if subject_identities != sorted(set(subject_identities)):
        raise DrillEvidenceError(
            "drill.scope.subjects must be unique and canonically sorted"
        )
    if set(pipelines) != {pipeline for pipeline, _ in subject_identities}:
        raise DrillEvidenceError(
            "drill.scope.pipelines must exactly cover subject pipelines"
        )

    subjects_by_identity = {
        identity: subject
        for identity, subject in zip(subject_identities, subjects, strict=True)
    }
    raw_resources = _array(scope["resources"], label="drill.scope.resources")
    if not raw_resources:
        raise DrillEvidenceError("drill.scope.resources must not be empty")
    resources: list[dict[str, Any]] = []
    consumer_ids: list[str] = []
    terraform_addresses: list[str] = []
    referenced_subjects: set[tuple[str, str]] = set()
    task_families: dict[str, str] = {}
    for index, raw_resource in enumerate(raw_resources):
        label = f"drill.scope.resources[{index}]"
        resource = _exact_object(raw_resource, _SCOPE_RESOURCE_KEYS, label=label)
        consumer_id = _pattern(
            resource["consumer_id"],
            _IDENTIFIER_RE,
            label=f"{label}.consumer_id",
            description="a canonical consumer ID",
            maximum=128,
        )
        terraform_address = _pattern(
            resource["terraform_address"],
            _TERRAFORM_ADDRESS_RE,
            label=f"{label}.terraform_address",
            description="an exact ECS task definition Terraform address",
            maximum=256,
        )
        pipeline = _pattern(
            resource["pipeline"],
            _PIPELINE_RE,
            label=f"{label}.pipeline",
            description="a canonical pipeline name",
            maximum=64,
        )
        subject_name = _pattern(
            resource["subject"],
            _IDENTIFIER_RE,
            label=f"{label}.subject",
            description="a canonical subject name",
            maximum=128,
        )
        subject_identity = (pipeline, subject_name)
        if subject_identity not in subjects_by_identity:
            raise DrillEvidenceError(f"{label} references an unknown subject")
        previous_family, previous_revision = _task_definition_arn(
            resource["previous_task_definition_arn"],
            label=f"{label}.previous_task_definition_arn",
        )
        initial_family, initial_revision = _task_definition_arn(
            resource["initial_new_task_definition_arn"],
            label=f"{label}.initial_new_task_definition_arn",
        )
        declared_previous_revision = _integer(
            resource["previous_task_revision"],
            label=f"{label}.previous_task_revision",
            minimum=1,
        )
        declared_initial_revision = _integer(
            resource["initial_new_task_revision"],
            label=f"{label}.initial_new_task_revision",
            minimum=1,
        )
        if (
            previous_family != initial_family
            or previous_revision != declared_previous_revision
            or initial_revision != declared_initial_revision
            or initial_revision <= previous_revision
        ):
            raise DrillEvidenceError(
                f"{label} old/new task definition family or revision is inconsistent"
            )
        resources.append(resource)
        consumer_ids.append(consumer_id)
        terraform_addresses.append(terraform_address)
        referenced_subjects.add(subject_identity)
        task_families[consumer_id] = previous_family
    if consumer_ids != sorted(set(consumer_ids)):
        raise DrillEvidenceError(
            "drill.scope.resources must be unique and sorted by consumer_id"
        )
    if len(terraform_addresses) != len(set(terraform_addresses)):
        raise DrillEvidenceError(
            "drill.scope.resources terraform addresses must be unique"
        )
    if referenced_subjects != set(subject_identities):
        raise DrillEvidenceError(
            "drill.scope.resources must cover every scoped subject"
        )

    initial_new_subjects = [
        {
            "pipeline": subject["pipeline"],
            "name": subject["name"],
            "release_repository": subject["release_repository"],
            "digest": subject["initial_new_digest"],
        }
        for subject in subjects
    ]
    previous_subjects = [
        {
            "pipeline": subject["pipeline"],
            "name": subject["name"],
            "release_repository": subject["release_repository"],
            "digest": subject["previous_digest"],
        }
        for subject in subjects
    ]
    initial_new_digests = {
        (subject["pipeline"], subject["name"]): subject["initial_new_digest"]
        for subject in subjects
    }
    previous_digests = {
        (subject["pipeline"], subject["name"]): subject["previous_digest"]
        for subject in subjects
    }
    initial_new_resources = [
        {
            "consumer_id": resource["consumer_id"],
            "terraform_address": resource["terraform_address"],
            "pipeline": resource["pipeline"],
            "subject": resource["subject"],
            "task_definition_arn": resource["initial_new_task_definition_arn"],
            "task_revision": resource["initial_new_task_revision"],
            "digest": initial_new_digests[
                (resource["pipeline"], resource["subject"])
            ],
        }
        for resource in resources
    ]
    previous_resources = [
        {
            "consumer_id": resource["consumer_id"],
            "terraform_address": resource["terraform_address"],
            "pipeline": resource["pipeline"],
            "subject": resource["subject"],
            "task_definition_arn": resource["previous_task_definition_arn"],
            "task_revision": resource["previous_task_revision"],
            "digest": previous_digests[
                (resource["pipeline"], resource["subject"])
            ],
        }
        for resource in resources
    ]
    return {
        "pipelines": pipelines,
        "subjects": subjects,
        "subjects_by_identity": subjects_by_identity,
        "resources": resources,
        "task_families": task_families,
        "initial_new": {
            "subjects": initial_new_subjects,
            "resources": initial_new_resources,
        },
        "previous_old": {
            "subjects": previous_subjects,
            "resources": previous_resources,
        },
    }


def _validate_subject_list(
    value: Any,
    *,
    scope: dict[str, Any],
    label: str,
    pipeline: str | None = None,
) -> list[dict[str, Any]]:
    entries = _array(value, label=label)
    expected_subjects = [
        subject
        for subject in scope["subjects"]
        if pipeline is None or subject["pipeline"] == pipeline
    ]
    if len(entries) != len(expected_subjects):
        raise DrillEvidenceError(
            f"{label} must contain every expected subject exactly once"
        )
    for index, (raw_entry, scoped) in enumerate(
        zip(entries, expected_subjects, strict=True)
    ):
        entry_label = f"{label}[{index}]"
        entry = _exact_object(
            raw_entry,
            _SNAPSHOT_SUBJECT_KEYS,
            label=entry_label,
        )
        for key in ("pipeline", "name", "release_repository"):
            if entry[key] != scoped[key]:
                raise DrillEvidenceError(
                    f"{entry_label}.{key} does not match drill.scope"
                )
        _digest(entry["digest"], label=f"{entry_label}.digest")
    return entries


def _validate_snapshot(
    value: Any,
    *,
    scope: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    snapshot = _exact_object(value, _SNAPSHOT_KEYS, label=label)
    subjects = _validate_subject_list(
        snapshot["subjects"],
        scope=scope,
        label=f"{label}.subjects",
    )
    actual_digests = {
        (subject["pipeline"], subject["name"]): subject["digest"]
        for subject in subjects
    }
    resources = _array(snapshot["resources"], label=f"{label}.resources")
    if len(resources) != len(scope["resources"]):
        raise DrillEvidenceError(
            f"{label}.resources must contain every scoped resource exactly once"
        )
    for index, (raw_resource, scoped) in enumerate(
        zip(resources, scope["resources"], strict=True)
    ):
        resource_label = f"{label}.resources[{index}]"
        resource = _exact_object(
            raw_resource,
            _SNAPSHOT_RESOURCE_KEYS,
            label=resource_label,
        )
        for key in (
            "consumer_id",
            "terraform_address",
            "pipeline",
            "subject",
        ):
            if resource[key] != scoped[key]:
                raise DrillEvidenceError(
                    f"{resource_label}.{key} does not match drill.scope"
                )
        family, revision = _task_definition_arn(
            resource["task_definition_arn"],
            label=f"{resource_label}.task_definition_arn",
        )
        declared_revision = _integer(
            resource["task_revision"],
            label=f"{resource_label}.task_revision",
            minimum=1,
        )
        if (
            family != scope["task_families"][resource["consumer_id"]]
            or revision != declared_revision
        ):
            raise DrillEvidenceError(
                f"{resource_label} task definition family or revision is inconsistent"
            )
        digest = _digest(resource["digest"], label=f"{resource_label}.digest")
        subject_digest = actual_digests[
            (resource["pipeline"], resource["subject"])
        ]
        if digest != subject_digest:
            raise DrillEvidenceError(
                f"{resource_label}.digest differs from its subject digest"
            )
    return snapshot


def _validate_snapshot_evidence(
    value: Any,
    *,
    scope: dict[str, Any],
    label: str,
    aggregate_completed_at: dt.datetime,
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence = _exact_object(value, _SNAPSHOT_EVIDENCE_KEYS, label=label)
    _validate_snapshot(
        evidence["snapshot"],
        scope=scope,
        label=f"{label}.snapshot",
    )
    locator = _validate_locator(
        evidence["locator"],
        label=f"{label}.locator",
        aggregate_completed_at=aggregate_completed_at,
    )
    locators.append(locator)
    return evidence


def _validate_baseline(
    value: Any,
    *,
    scope: dict[str, Any],
    aggregate_completed_at: dt.datetime,
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    baseline = _exact_object(value, _BASELINE_KEYS, label="drill.baseline")
    _uuid(
        baseline["terraform_lineage"],
        label="drill.baseline.terraform_lineage",
    )
    _integer(
        baseline["terraform_serial"],
        label="drill.baseline.terraform_serial",
    )
    live_snapshot = _validate_snapshot_evidence(
        baseline["live_snapshot"],
        scope=scope,
        label="drill.baseline.live_snapshot",
        aggregate_completed_at=aggregate_completed_at,
        locators=locators,
    )
    _true(
        baseline["initial_new_verified"],
        label="drill.baseline.initial_new_verified",
    )
    if live_snapshot["snapshot"] != scope["initial_new"]:
        raise DrillEvidenceError(
            "drill.baseline.live_snapshot must be the exact initial-new state"
        )
    return baseline


def _validate_release_authorizations(
    value: Any,
    *,
    drill_id: str,
    channel: str,
    target: dict[str, Any],
    scope: dict[str, Any],
    label: str,
    aggregate_completed_at: dt.datetime,
    locators: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    authorizations = _array(value, label=label)
    if len(authorizations) != len(scope["pipelines"]):
        raise DrillEvidenceError(
            f"{label} must contain exactly one authorization per pipeline"
        )
    for index, (raw_authorization, pipeline) in enumerate(
        zip(authorizations, scope["pipelines"], strict=True)
    ):
        authorization_label = f"{label}[{index}]"
        authorization = _exact_object(
            raw_authorization,
            _RELEASE_AUTHORIZATION_KEYS,
            label=authorization_label,
        )
        _uuid4(
            authorization["authorization_id"],
            label=f"{authorization_label}.authorization_id",
        )
        _uuid4(
            authorization["deployment_intent_id"],
            label=f"{authorization_label}.deployment_intent_id",
        )
        if authorization["drill_id"] != drill_id:
            raise DrillEvidenceError(
                f"{authorization_label}.drill_id does not bind this drill"
            )
        if authorization["pipeline"] != pipeline:
            raise DrillEvidenceError(
                f"{authorization_label}.pipeline is missing, duplicate, or out of order"
            )
        if authorization["channel"] != channel:
            raise DrillEvidenceError(
                f"{authorization_label}.channel does not bind its leg"
            )
        subjects = _validate_subject_list(
            authorization["subjects"],
            scope=scope,
            label=f"{authorization_label}.subjects",
            pipeline=pipeline,
        )
        expected_subjects = [
            subject
            for subject in target["subjects"]
            if subject["pipeline"] == pipeline
        ]
        if subjects != expected_subjects:
            raise DrillEvidenceError(
                f"{authorization_label}.subjects do not bind the leg target"
            )
        _timestamp(
            authorization["issued_at_utc"],
            label=f"{authorization_label}.issued_at_utc",
        )
        _uuid4(
            authorization["release_approval_id"],
            label=f"{authorization_label}.release_approval_id",
        )
        _pattern(
            authorization["release_approved_by_arn"],
            _IAM_ROLE_ARN_RE,
            label=f"{authorization_label}.release_approved_by_arn",
            description="an IAM role ARN in the fixed account",
            maximum=600,
        )
        if (
            authorization["release_approved_by_arn"]
            != APPROVAL_APPROVED_BY_ARN
        ):
            raise DrillEvidenceError(
                f"{authorization_label}.release_approved_by_arn is not "
                "the fixed approval caller"
            )
        receipt_sha256 = _sha256(
            authorization["receipt_sha256"],
            label=f"{authorization_label}.receipt_sha256",
        )
        locator = _validate_locator(
            authorization["locator"],
            label=f"{authorization_label}.locator",
            aggregate_completed_at=aggregate_completed_at,
        )
        if locator["sha256"] != receipt_sha256:
            raise DrillEvidenceError(
                f"{authorization_label}.receipt_sha256 must match its locator"
            )
        locators.append(locator)
    return authorizations


def _validate_plan(
    value: Any,
    *,
    source: dict[str, Any],
    target: dict[str, Any],
    scope: dict[str, Any],
    label: str,
    aggregate_completed_at: dt.datetime,
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    plan = _exact_object(value, _PLAN_KEYS, label=label)
    plan_sha = _sha256(plan["sha256"], label=f"{label}.sha256")
    receipt_sha = _sha256(
        plan["receipt_sha256"],
        label=f"{label}.receipt_sha256",
    )
    _timestamp(plan["created_at_utc"], label=f"{label}.created_at_utc")
    _uuid(plan["terraform_lineage"], label=f"{label}.terraform_lineage")
    _integer(plan["terraform_serial"], label=f"{label}.terraform_serial")
    plan_from = _validate_snapshot(
        plan["from"],
        scope=scope,
        label=f"{label}.from",
    )
    plan_to = _validate_snapshot(
        plan["to"],
        scope=scope,
        label=f"{label}.to",
    )
    if plan_from != source or plan_to != target:
        raise DrillEvidenceError(f"{label} must exactly bind the leg endpoints")
    changed_resources = _array(
        plan["changed_resources"],
        label=f"{label}.changed_resources",
    )
    expected_resources = [
        resource["terraform_address"] for resource in scope["resources"]
    ]
    if changed_resources != expected_resources:
        raise DrillEvidenceError(
            f"{label}.changed_resources must be the exact scoped resource list"
        )
    locator = _validate_locator(
        plan["locator"],
        label=f"{label}.locator",
        aggregate_completed_at=aggregate_completed_at,
    )
    if locator["sha256"] != receipt_sha:
        raise DrillEvidenceError(
            f"{label}.receipt_sha256 must match its exact receipt locator"
        )
    locators.append(locator)
    return plan


def _validate_approval(
    value: Any,
    *,
    drill_id: str,
    plan_sha256: str,
    label: str,
    aggregate_completed_at: dt.datetime,
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    approval = _exact_object(value, _APPROVAL_KEYS, label=label)
    confirmation_id = _text(
        approval["confirmation_id"],
        label=f"{label}.confirmation_id",
        maximum=4,
    )
    if confirmation_id not in {"OK-1", "OK-2"}:
        raise DrillEvidenceError(f"{label}.confirmation_id is unsupported")
    if approval["drill_id"] != drill_id:
        raise DrillEvidenceError(f"{label}.drill_id does not bind this drill")
    if approval["action"] not in {"rollback", "restore"}:
        raise DrillEvidenceError(f"{label}.action is unsupported")
    actual_plan_sha = _sha256(
        approval["plan_sha256"],
        label=f"{label}.plan_sha256",
    )
    if actual_plan_sha != plan_sha256:
        raise DrillEvidenceError(f"{label}.plan_sha256 does not bind its plan")
    _sha256(
        approval["approval_text_sha256"],
        label=f"{label}.approval_text_sha256",
    )
    expected_confirmation = (
        f"APPROVE {drill_id} {approval['action']} {plan_sha256}\n".encode()
    )
    if (
        hashlib.sha256(expected_confirmation).hexdigest()
        != approval["approval_text_sha256"]
    ):
        raise DrillEvidenceError(
            f"{label}.approval_text_sha256 does not bind the exact confirmation"
        )
    _timestamp(
        approval["consumed_at_utc"],
        label=f"{label}.consumed_at_utc",
    )
    release = _exact_object(
        approval["release_approval"],
        _RELEASE_APPROVAL_KEYS,
        label=f"{label}.release_approval",
    )
    _uuid4(
        release["approval_id"],
        label=f"{label}.release_approval.approval_id",
    )
    approved_at = _timestamp(
        release["approved_at_utc"],
        label=f"{label}.release_approval.approved_at_utc",
    )
    expires_at = _timestamp(
        release["expires_at_utc"],
        label=f"{label}.release_approval.expires_at_utc",
    )
    if approved_at >= expires_at:
        raise DrillEvidenceError(
            f"{label}.release_approval expiration must follow approval"
        )
    _pattern(
        release["approved_by"],
        _IAM_ROLE_ARN_RE,
        label=f"{label}.release_approval.approved_by",
        description="an IAM role ARN in the fixed account",
        maximum=600,
    )
    if release["approved_by"] != APPROVAL_APPROVED_BY_ARN:
        raise DrillEvidenceError(
            f"{label}.release_approval.approved_by is not the fixed "
            "approval caller"
        )
    decision = _text(
        release["decision"],
        label=f"{label}.release_approval.decision",
        maximum=2048,
    )
    if not decision.startswith("APPROVED: "):
        raise DrillEvidenceError(
            f"{label}.release_approval.decision is not an approval"
        )
    _sha256(
        release["forced_gate_sha256"],
        label=f"{label}.release_approval.forced_gate_sha256",
    )
    if release["pipeline"] != "mcp":
        raise DrillEvidenceError(
            f"{label}.release_approval.pipeline must be mcp"
        )
    _pattern(
        release["source_commit"],
        _GIT_OID_RE,
        label=f"{label}.release_approval.source_commit",
        description="a 40-character lowercase Git OID",
        maximum=40,
    )
    payload = _validate_external_locator(
        release["payload"],
        label=f"{label}.release_approval.payload",
    )
    signature = _validate_external_locator(
        release["signature"],
        label=f"{label}.release_approval.signature",
    )
    expected_payload_key = (
        "approval-records/mcp/"
        f"{release['source_commit']}/{payload['sha256']}.json"
    )
    if (
        payload["bucket"] != "teamagent-dev-image-release-evidence"
        or payload["key"] != expected_payload_key
        or signature["bucket"] != payload["bucket"]
        or signature["key"] != f"{payload['key']}.sig"
    ):
        raise DrillEvidenceError(
            f"{label}.release_approval locators do not bind the approved MCP source"
        )
    receipt_sha = _sha256(
        approval["receipt_sha256"],
        label=f"{label}.receipt_sha256",
    )
    locator = _validate_locator(
        approval["locator"],
        label=f"{label}.locator",
        aggregate_completed_at=aggregate_completed_at,
    )
    if locator["sha256"] != receipt_sha:
        raise DrillEvidenceError(
            f"{label}.receipt_sha256 must match its exact locator"
        )
    locators.append(locator)
    return approval


def _validate_apply(
    value: Any,
    *,
    plan_sha256: str,
    target: dict[str, Any],
    scope: dict[str, Any],
    label: str,
    aggregate_completed_at: dt.datetime,
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    apply = _exact_object(value, _APPLY_KEYS, label=label)
    _uuid4(apply["apply_attempt_id"], label=f"{label}.apply_attempt_id")
    actual_plan_sha = _sha256(
        apply["plan_sha256"],
        label=f"{label}.plan_sha256",
    )
    if actual_plan_sha != plan_sha256:
        raise DrillEvidenceError(f"{label}.plan_sha256 does not bind its plan")
    receipt_sha = _sha256(
        apply["receipt_sha256"],
        label=f"{label}.receipt_sha256",
    )
    started_at = _timestamp(apply["started_at_utc"], label=f"{label}.started_at_utc")
    completed_at = _timestamp(
        apply["completed_at_utc"],
        label=f"{label}.completed_at_utc",
    )
    if started_at > completed_at:
        raise DrillEvidenceError(f"{label} timestamps must be monotonic")
    result = _choice(apply["result"], LEG_RESULTS, label=f"{label}.result")
    _uuid(apply["terraform_lineage"], label=f"{label}.terraform_lineage")
    serial_before = _integer(
        apply["terraform_serial_before"],
        label=f"{label}.terraform_serial_before",
    )
    serial_after = _integer(
        apply["terraform_serial_after"],
        label=f"{label}.terraform_serial_after",
    )
    if serial_after < serial_before:
        raise DrillEvidenceError(f"{label} Terraform serial must not decrease")
    state = _validate_snapshot(
        apply["state"],
        scope=scope,
        label=f"{label}.state",
    )
    automation_principal = _validate_automation_principal(
        apply["automation_principal"],
        label=f"{label}.automation_principal",
    )
    automation_identity_sha = _sha256(
        apply["automation_identity_sha256"],
        label=f"{label}.automation_identity_sha256",
    )
    automation_identity_locator = _validate_locator(
        apply["automation_identity_locator"],
        label=f"{label}.automation_identity_locator",
        aggregate_completed_at=aggregate_completed_at,
    )
    if automation_identity_locator["sha256"] != automation_identity_sha:
        raise DrillEvidenceError(
            f"{label}.automation_identity_sha256 must match its locator"
        )
    locators.append(automation_identity_locator)
    if result == "PASSED":
        if serial_after <= serial_before:
            raise DrillEvidenceError(
                f"{label} successful apply must advance the Terraform serial"
            )
        if state != target:
            raise DrillEvidenceError(
                f"{label} successful apply state must exactly match the leg target"
            )
    locator = _validate_locator(
        apply["locator"],
        label=f"{label}.locator",
        aggregate_completed_at=aggregate_completed_at,
    )
    if locator["sha256"] != receipt_sha:
        raise DrillEvidenceError(
            f"{label}.receipt_sha256 must match its exact locator"
        )
    locators.append(locator)
    return apply


def _validate_ecs(
    value: Any,
    *,
    target: dict[str, Any],
    scope: dict[str, Any],
    label: str,
    aggregate_completed_at: dt.datetime,
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    ecs = _exact_object(value, _ECS_KEYS, label=label)
    result = _choice(ecs["result"], LEG_RESULTS, label=f"{label}.result")
    steady = _boolean(ecs["steady"], label=f"{label}.steady")
    _timestamp(ecs["verified_at_utc"], label=f"{label}.verified_at_utc")
    live_snapshot = _validate_snapshot(
        ecs["live_snapshot"],
        scope=scope,
        label=f"{label}.live_snapshot",
    )
    if result == "PASSED" and (not steady or live_snapshot != target):
        raise DrillEvidenceError(
            f"{label} PASSED requires steady ECS state at the exact leg target"
        )
    locator = _validate_locator(
        ecs["locator"],
        label=f"{label}.locator",
        aggregate_completed_at=aggregate_completed_at,
    )
    locators.append(locator)
    return ecs


def _validate_run_task_health(
    value: Any,
    *,
    target: dict[str, Any],
    apply_attempt_id: str,
    label: str,
    aggregate_completed_at: dt.datetime,
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    health = _exact_object(value, _RUN_TASK_HEALTH_KEYS, label=label)
    result = _choice(health["result"], LEG_RESULTS, label=f"{label}.result")
    _timestamp(health["verified_at_utc"], label=f"{label}.verified_at_utc")
    if health["apply_attempt_id"] != apply_attempt_id:
        raise DrillEvidenceError(
            f"{label}.apply_attempt_id does not bind its apply"
        )
    task_definition_arn = _text(
        health["task_definition_arn"],
        label=f"{label}.task_definition_arn",
        maximum=600,
    )
    _task_definition_arn(
        task_definition_arn,
        label=f"{label}.task_definition_arn",
    )
    matching_resources = [
        resource
        for resource in target["resources"]
        if resource["task_definition_arn"] == task_definition_arn
    ]
    if (
        len(matching_resources) != 1
        or matching_resources[0]["consumer_id"] != "canary"
    ):
        raise DrillEvidenceError(
            f"{label}.task_definition_arn is not the leg target canary"
        )
    image = _pattern(
        health["image"],
        _ECR_IMAGE_RE,
        label=f"{label}.image",
        description="an exact account ECR digest image",
        maximum=600,
    )
    matching_subjects = [
        subject
        for subject in target["subjects"]
        if subject["pipeline"] == matching_resources[0]["pipeline"]
        and subject["name"] == matching_resources[0]["subject"]
    ]
    if (
        len(matching_subjects) != 1
        or image
        != (
            f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/"
            f"{matching_subjects[0]['release_repository']}@"
            f"{matching_subjects[0]['digest']}"
        )
    ):
        raise DrillEvidenceError(
            f"{label}.image does not bind its target subject digest"
        )
    log_stream_name = _text(
        health["log_stream_name"],
        label=f"{label}.log_stream_name",
        maximum=512,
    )
    task = _exact_object(
        health["task"],
        _RUN_TASK_KEYS,
        label=f"{label}.task",
    )
    _pattern(
        task["task_arn"],
        _TASK_ARN_RE,
        label=f"{label}.task.task_arn",
        description="a canonical ECS task ARN",
        maximum=600,
    )
    if (
        task["task_definition_arn"] != task_definition_arn
        or task["image"] != image
        or task["image_digest"] != image.rsplit("@", 1)[1]
        or task["log_stream_name"] != log_stream_name
    ):
        raise DrillEvidenceError(
            f"{label}.task must bind the exact probe task definition, image, and log"
        )
    _literal_integer(task["exit_code"], 0, label=f"{label}.task.exit_code")
    if task["stopped_reason_code"] != "EssentialContainerExited":
        raise DrillEvidenceError(
            f"{label}.task.stopped_reason_code is not successful"
        )
    checks = _exact_object(
        health["checks"],
        _RUN_TASK_CHECK_KEYS,
        label=f"{label}.checks",
    )
    if result == "PASSED" and not all(value is True for value in checks.values()):
        raise DrillEvidenceError(f"{label} PASSED requires all seven exact checks")
    locator = _validate_locator(
        health["locator"],
        label=f"{label}.locator",
        aggregate_completed_at=aggregate_completed_at,
    )
    locators.append(locator)
    return health


def _validate_dm_qa(
    value: Any,
    *,
    target: dict[str, Any],
    apply_attempt_id: str,
    label: str,
    aggregate_completed_at: dt.datetime,
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    dm_qa = _exact_object(value, _DM_QA_KEYS, label=label)
    result = _choice(dm_qa["result"], LEG_RESULTS, label=f"{label}.result")
    _timestamp(dm_qa["verified_at_utc"], label=f"{label}.verified_at_utc")
    if dm_qa["apply_attempt_id"] != apply_attempt_id:
        raise DrillEvidenceError(
            f"{label}.apply_attempt_id does not bind its apply"
        )
    mcp_task_definition = _text(
        dm_qa["mcp_task_definition_arn"],
        label=f"{label}.mcp_task_definition_arn",
        maximum=600,
    )
    _task_definition_arn(
        mcp_task_definition,
        label=f"{label}.mcp_task_definition_arn",
    )
    openclaw_task_definition = _text(
        dm_qa["openclaw_task_definition_arn"],
        label=f"{label}.openclaw_task_definition_arn",
        maximum=600,
    )
    openclaw_family, _ = _task_definition_arn(
        openclaw_task_definition,
        label=f"{label}.openclaw_task_definition_arn",
    )
    expected_mcp = [
        resource["task_definition_arn"]
        for resource in target["resources"]
        if resource["consumer_id"] == "mcp"
    ]
    if (
        result == "PASSED"
        and (
            expected_mcp != [mcp_task_definition]
            or openclaw_family != "teamagent-dev-openclaw"
        )
    ):
        raise DrillEvidenceError(
            f"{label} PASSED must bind the exact target MCP and observed OpenClaw revisions"
        )
    locator = _validate_locator(
        dm_qa["locator"],
        label=f"{label}.locator",
        aggregate_completed_at=aggregate_completed_at,
    )
    expected_key = (
        f"forced-rollback-drills/{apply_attempt_id}/dm-qa/result.json"
    )
    if (
        locator["bucket"] != "teamagent-dev-openclaw-rollout-evidence"
        or locator["key"] != expected_key
    ):
        raise DrillEvidenceError(
            f"{label}.locator does not bind the exact apply DM QA evidence"
        )
    locators.append(locator)
    return dm_qa


def _validate_recovery(
    value: Any,
    *,
    scope: dict[str, Any],
    label: str,
    aggregate_completed_at: dt.datetime,
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    recovery = _exact_object(value, _RECOVERY_KEYS, label=label)
    attempted = _boolean(recovery["attempted"], label=f"{label}.attempted")
    result = _choice(
        recovery["result"],
        RECOVERY_RESULTS,
        label=f"{label}.result",
    )
    _validate_subject_list(
        recovery["last_exact_confirmed_digests"],
        scope=scope,
        label=f"{label}.last_exact_confirmed_digests",
    )
    if attempted:
        if result not in {"PASSED", "FAILED"}:
            raise DrillEvidenceError(
                f"{label}.result must record an attempted recovery outcome"
            )
        _timestamp(
            recovery["completed_at_utc"],
            label=f"{label}.completed_at_utc",
        )
        locator = _validate_locator(
            recovery["locator"],
            label=f"{label}.locator",
            aggregate_completed_at=aggregate_completed_at,
        )
        locators.append(locator)
    else:
        if result not in {"NOT_ATTEMPTED", "NOT_REQUIRED"}:
            raise DrillEvidenceError(
                f"{label}.result is inconsistent with attempted=false"
            )
        if recovery["completed_at_utc"] is not None or recovery["locator"] is not None:
            raise DrillEvidenceError(
                f"{label} unattempted recovery must use null completion and locator"
            )
    return recovery


def _validate_leg(
    value: Any,
    *,
    index: int,
    drill_id: str,
    required_source: dict[str, Any],
    required_target: dict[str, Any],
    require_exact_transition: bool,
    scope: dict[str, Any],
    aggregate_completed_at: dt.datetime,
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    label = f"drill.legs[{index}]"
    leg = _exact_object(value, _LEG_KEYS, label=label)
    expected = (
        (1, "rollback_to_previous", "rollback"),
        (2, "restore_active", "active"),
    )[index]
    _literal_integer(
        leg["ordinal"],
        expected[0],
        label=f"{label}.ordinal",
    )
    if (leg["ordinal"], leg["name"], leg["channel"]) != expected:
        raise DrillEvidenceError(
            f"{label} must be ordinal/name/channel {expected!r}"
        )
    leg_from = _validate_snapshot(
        leg["from"],
        scope=scope,
        label=f"{label}.from",
    )
    leg_to = _validate_snapshot(
        leg["to"],
        scope=scope,
        label=f"{label}.to",
    )
    if require_exact_transition and (
        leg_from != required_source or leg_to != required_target
    ):
        raise DrillEvidenceError(
            f"{label} endpoints are not the exact required transition"
        )

    started_at = _timestamp(
        leg["started_at_utc"],
        label=f"{label}.started_at_utc",
    )
    completed_at = _timestamp(
        leg["completed_at_utc"],
        label=f"{label}.completed_at_utc",
    )
    if started_at > completed_at:
        raise DrillEvidenceError(f"{label} timestamps must be monotonic")

    authorizations = _validate_release_authorizations(
        leg["release_authorizations"],
        drill_id=drill_id,
        channel=expected[2],
        target=leg_to,
        scope=scope,
        label=f"{label}.release_authorizations",
        aggregate_completed_at=aggregate_completed_at,
        locators=locators,
    )
    plan = _validate_plan(
        leg["plan"],
        source=leg_from,
        target=leg_to,
        scope=scope,
        label=f"{label}.plan",
        aggregate_completed_at=aggregate_completed_at,
        locators=locators,
    )
    approval = _validate_approval(
        leg["approval"],
        drill_id=drill_id,
        plan_sha256=plan["sha256"],
        label=f"{label}.approval",
        aggregate_completed_at=aggregate_completed_at,
        locators=locators,
    )
    expected_confirmation = (("OK-1", "rollback"), ("OK-2", "restore"))[index]
    if (
        approval["confirmation_id"],
        approval["action"],
    ) != expected_confirmation:
        raise DrillEvidenceError(
            f"{label}.approval must use confirmation/action "
            f"{expected_confirmation!r}"
        )
    release_approval = approval["release_approval"]
    for authorization_index, authorization in enumerate(authorizations):
        if (
            authorization["release_approval_id"]
            != release_approval["approval_id"]
            or authorization["release_approved_by_arn"]
            != release_approval["approved_by"]
        ):
            raise DrillEvidenceError(
                f"{label}.release_authorizations[{authorization_index}] "
                "does not bind the verified release approval"
            )
        issued_at = _timestamp(
            authorization["issued_at_utc"],
            label=(
                f"{label}.release_authorizations[{authorization_index}]"
                ".issued_at_utc"
            ),
        )
        release_approved_at = _timestamp(
            release_approval["approved_at_utc"],
            label=f"{label}.approval.release_approval.approved_at_utc",
        )
        release_expires_at = _timestamp(
            release_approval["expires_at_utc"],
            label=f"{label}.approval.release_approval.expires_at_utc",
        )
        if not release_approved_at <= issued_at < release_expires_at:
            raise DrillEvidenceError(
                f"{label} release authorization is outside approval validity"
            )
    apply = _validate_apply(
        leg["apply"],
        plan_sha256=plan["sha256"],
        target=leg_to,
        scope=scope,
        label=f"{label}.apply",
        aggregate_completed_at=aggregate_completed_at,
        locators=locators,
    )
    ecs = _validate_ecs(
        leg["ecs"],
        target=leg_to,
        scope=scope,
        label=f"{label}.ecs",
        aggregate_completed_at=aggregate_completed_at,
        locators=locators,
    )
    run_task_health = _validate_run_task_health(
        leg["run_task_health"],
        target=leg_to,
        apply_attempt_id=apply["apply_attempt_id"],
        label=f"{label}.run_task_health",
        aggregate_completed_at=aggregate_completed_at,
        locators=locators,
    )
    dm_qa = _validate_dm_qa(
        leg["dm_qa"],
        target=leg_to,
        apply_attempt_id=apply["apply_attempt_id"],
        label=f"{label}.dm_qa",
        aggregate_completed_at=aggregate_completed_at,
        locators=locators,
    )
    if (
        ecs["locator"] != apply["locator"]
        or run_task_health["locator"] != apply["locator"]
    ):
        raise DrillEvidenceError(
            f"{label} apply, ECS, and run-task proof must share the exact "
            "runtime-guard receipt locator"
        )
    recovery = _validate_recovery(
        leg["recovery"],
        scope=scope,
        label=f"{label}.recovery",
        aggregate_completed_at=aggregate_completed_at,
        locators=locators,
    )
    result = _choice(leg["result"], LEG_RESULTS, label=f"{label}.result")

    authorization_times = [
        _timestamp(
            authorization["issued_at_utc"],
            label=f"{label}.release_authorizations[{auth_index}].issued_at_utc",
        )
        for auth_index, authorization in enumerate(authorizations)
    ]
    plan_created_at = _timestamp(
        plan["created_at_utc"],
        label=f"{label}.plan.created_at_utc",
    )
    confirmed_at = _timestamp(
        approval["consumed_at_utc"],
        label=f"{label}.approval.consumed_at_utc",
    )
    apply_started_at = _timestamp(
        apply["started_at_utc"],
        label=f"{label}.apply.started_at_utc",
    )
    apply_completed_at = _timestamp(
        apply["completed_at_utc"],
        label=f"{label}.apply.completed_at_utc",
    )
    proof_times = [
        _timestamp(
            ecs["verified_at_utc"],
            label=f"{label}.ecs.verified_at_utc",
        ),
        _timestamp(
            run_task_health["verified_at_utc"],
            label=f"{label}.run_task_health.verified_at_utc",
        ),
        _timestamp(
            dm_qa["verified_at_utc"],
            label=f"{label}.dm_qa.verified_at_utc",
        ),
    ]
    if not (
        all(started_at <= issued_at <= plan_created_at for issued_at in authorization_times)
        and started_at
        <= plan_created_at
        <= confirmed_at
        <= apply_started_at
        <= apply_completed_at
        and all(apply_started_at <= proof_at <= apply_completed_at for proof_at in proof_times)
        and apply_completed_at <= completed_at
    ):
        raise DrillEvidenceError(
            f"{label} authorization/plan/approval/apply/proof timestamps "
            "must be monotonically nondecreasing"
        )
    recovery_completed_at: dt.datetime | None = None
    if recovery["attempted"]:
        recovery_completed_at = _timestamp(
            recovery["completed_at_utc"],
            label=f"{label}.recovery.completed_at_utc",
        )
        if not apply_completed_at <= recovery_completed_at <= completed_at:
            raise DrillEvidenceError(
                f"{label}.recovery must complete after apply and before leg completion"
            )

    proof_passed = (
        apply["result"] == "PASSED"
        and ecs["result"] == "PASSED"
        and ecs["steady"] is True
        and run_task_health["result"] == "PASSED"
        and dm_qa["result"] == "PASSED"
    )
    if result == "PASSED" and not proof_passed:
        raise DrillEvidenceError(
            f"{label}.result cannot be PASSED when a required proof failed"
        )
    if result == "FAILED" and proof_passed:
        raise DrillEvidenceError(
            f"{label}.result cannot be FAILED when every required proof passed"
        )
    if result == "FAILED" and recovery["result"] == "NOT_REQUIRED":
        raise DrillEvidenceError(
            f"{label}.recovery must record attempted or NOT_ATTEMPTED after failure"
        )

    return {
        "value": leg,
        "started_at": started_at,
        "completed_at": completed_at,
        "plan_created_at": plan_created_at,
        "apply_started_at": apply_started_at,
        "apply_completed_at": apply_completed_at,
        "recovery_completed_at": recovery_completed_at,
        "authorizations": authorizations,
        "plan": plan,
        "approval": approval,
        "apply": apply,
        "ecs": ecs,
        "run_task_health": run_task_health,
        "dm_qa": dm_qa,
        "recovery": recovery,
        "result": result,
    }


def _validate_safe_terminal_state(
    value: Any,
    *,
    scope: dict[str, Any],
    aggregate_completed_at: dt.datetime,
    locators: list[dict[str, Any]],
) -> dict[str, Any]:
    terminal = _exact_object(
        value,
        _SAFE_TERMINAL_KEYS,
        label="drill.safe_terminal_state",
    )
    classification = _choice(
        terminal["classification"],
        SAFE_TERMINAL_CLASSIFICATIONS,
        label="drill.safe_terminal_state.classification",
    )
    steady = _boolean(
        terminal["steady"],
        label="drill.safe_terminal_state.steady",
    )
    _timestamp(
        terminal["verified_at_utc"],
        label="drill.safe_terminal_state.verified_at_utc",
    )
    live_snapshot = _validate_snapshot_evidence(
        terminal["live_snapshot"],
        scope=scope,
        label="drill.safe_terminal_state.live_snapshot",
        aggregate_completed_at=aggregate_completed_at,
        locators=locators,
    )
    expected_snapshot = {
        "INITIAL_NEW": scope["initial_new"],
        "PREVIOUS_OLD": scope["previous_old"],
    }.get(classification)
    if expected_snapshot is not None and live_snapshot["snapshot"] != expected_snapshot:
        raise DrillEvidenceError(
            "drill.safe_terminal_state classification and snapshot disagree"
        )
    if classification == "UNKNOWN" and steady:
        raise DrillEvidenceError(
            "drill.safe_terminal_state UNKNOWN cannot be marked steady"
        )
    return terminal


def _validate_artifact_manifest(
    value: Any,
    *,
    referenced_locators: list[dict[str, Any]],
    aggregate_completed_at: dt.datetime,
) -> None:
    manifest = _array(value, label="drill.artifact_manifest")
    validated_manifest: list[dict[str, Any]] = []
    for index, raw_locator in enumerate(manifest):
        validated_manifest.append(
            _validate_locator(
                raw_locator,
                label=f"drill.artifact_manifest[{index}]",
                aggregate_completed_at=aggregate_completed_at,
            )
        )

    referenced_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    for locator in referenced_locators:
        identity = _locator_identity(locator)
        previous = referenced_by_identity.get(identity)
        if previous is not None and previous != locator:
            raise DrillEvidenceError(
                "reused drill artifact identity has conflicting locator metadata"
            )
        referenced_by_identity[identity] = locator
    manifest_identities = [_locator_identity(locator) for locator in validated_manifest]
    if manifest_identities != sorted(manifest_identities):
        raise DrillEvidenceError(
            "drill.artifact_manifest must be sorted by bucket/key/VersionId"
        )
    if len(manifest_identities) != len(set(manifest_identities)):
        raise DrillEvidenceError(
            "drill.artifact_manifest must not contain duplicate locators"
        )
    expected_manifest = sorted(
        referenced_by_identity.values(),
        key=_locator_identity,
    )
    if validated_manifest != expected_manifest:
        raise DrillEvidenceError(
            "drill.artifact_manifest must exactly cover every referenced artifact"
        )


def _validate_integrity(
    value: Any,
    *,
    drill: dict[str, Any],
    aggregate_completed_at: dt.datetime,
) -> None:
    integrity = _exact_object(value, _INTEGRITY_KEYS, label="drill.integrity")
    expected_sha = _sha256(
        integrity["canonical_sha256"],
        label="drill.integrity.canonical_sha256",
    )
    kms_key_arn = _kms_key_arn(
        integrity["kms_key_arn"],
        label="drill.integrity.kms_key_arn",
    )
    if integrity["signing_algorithm"] != SIGNING_ALGORITHM:
        raise DrillEvidenceError(
            f"drill.integrity.signing_algorithm must be {SIGNING_ALGORITHM}"
        )
    immutable = _exact_object(
        integrity["immutable_object"],
        _IMMUTABLE_OBJECT_KEYS,
        label="drill.integrity.immutable_object",
    )
    signature = _exact_object(
        integrity["signature"],
        _SIGNATURE_KEYS,
        label="drill.integrity.signature",
    )
    synthetic_locator = {
        **immutable,
        "signature": signature,
        "signer": {
            "kms_key_arn": kms_key_arn,
            "algorithm": integrity["signing_algorithm"],
        },
    }
    _validate_locator(
        synthetic_locator,
        label="drill.integrity aggregate locator",
        aggregate_completed_at=aggregate_completed_at,
    )
    if immutable["content_type"] != "application/json":
        raise DrillEvidenceError(
            "drill.integrity.immutable_object.content_type must be application/json"
        )
    canonical_body = canonical_drill_body_bytes(drill)
    if hashlib.sha256(canonical_body).hexdigest() != expected_sha:
        raise DrillEvidenceError(
            "drill.integrity.canonical_sha256 does not bind the canonical body"
        )
    if immutable["sha256"] != expected_sha or immutable["size"] != len(
        canonical_body
    ):
        raise DrillEvidenceError(
            "drill.integrity.immutable_object must be the exact canonical body"
        )


def _validate_expected_bindings(
    value: Any,
    *,
    aggregate_completed_at: dt.datetime,
) -> dict[str, Any]:
    """Validate caller-supplied trust anchors without loading registry files.

    The caller must derive ``scope`` from the code-owned consumer registry and
    the exact initial-release receipt.  Requiring it as a separate argument
    prevents an aggregate from authorizing a third digest/revision/resource by
    merely editing its own signed ``scope``.
    """

    _require_builtin_json_types(value, label="expected drill bindings")
    expected = _exact_object(
        value,
        _EXPECTED_BINDING_KEYS,
        label="expected drill bindings",
    )
    _pattern(
        expected["git_commit"],
        _GIT_OID_RE,
        label="expected drill bindings.git_commit",
        description="a 40-character lowercase Git OID",
        maximum=40,
    )
    _sha256(
        expected["drill_contract_sha256"],
        label="expected drill bindings.drill_contract_sha256",
    )
    _validate_locator(
        expected["initial_release_apply"],
        label="expected drill bindings.initial_release_apply",
        aggregate_completed_at=aggregate_completed_at,
    )
    _timestamp(
        expected["initial_release_verified_at_utc"],
        label="expected drill bindings.initial_release_verified_at_utc",
    )
    _validate_scope(expected["scope"])
    return expected


def _validate_cross_invariants(
    *,
    status: str,
    drill_id: str,
    control: dict[str, Any],
    actors: dict[str, Any],
    baseline: dict[str, Any],
    legs: list[dict[str, Any]],
    terminal: dict[str, Any],
) -> None:
    leg1 = legs[0]
    leg2 = legs[1]
    initial_new = baseline["live_snapshot"]["snapshot"]

    lineage = baseline["terraform_lineage"]
    baseline_serial = baseline["terraform_serial"]
    if (
        leg1["plan"]["terraform_lineage"] != lineage
        or leg2["plan"]["terraform_lineage"] != lineage
        or leg1["apply"]["terraform_lineage"] != lineage
        or leg2["apply"]["terraform_lineage"] != lineage
    ):
        raise DrillEvidenceError(
            "both plans and applies must bind the baseline Terraform lineage"
        )
    if (
        leg1["plan"]["terraform_serial"] != baseline_serial
        or leg1["apply"]["terraform_serial_before"] != baseline_serial
        or leg2["plan"]["terraform_serial"]
        != leg1["apply"]["terraform_serial_after"]
        or leg2["apply"]["terraform_serial_before"]
        != leg1["apply"]["terraform_serial_after"]
    ):
        raise DrillEvidenceError(
            "restore plan must be based on the rollback-complete Terraform serial"
        )
    if leg2["plan_created_at"] < leg1["completed_at"]:
        raise DrillEvidenceError(
            "restore plan must be created after rollback leg completion"
        )
    if leg1["plan"]["sha256"] == leg2["plan"]["sha256"]:
        raise DrillEvidenceError("each leg must use a fresh, distinct plan")
    if (
        leg1["apply"]["apply_attempt_id"]
        == leg2["apply"]["apply_attempt_id"]
    ):
        raise DrillEvidenceError("each leg must use a fresh apply attempt ID")

    authorization_ids = [
        authorization["authorization_id"]
        for leg in legs
        for authorization in leg["authorizations"]
    ]
    intent_ids = [
        authorization["deployment_intent_id"]
        for leg in legs
        for authorization in leg["authorizations"]
    ]
    if len(authorization_ids) != len(set(authorization_ids)):
        raise DrillEvidenceError("release authorization IDs must be globally distinct")
    if len(intent_ids) != len(set(intent_ids)):
        raise DrillEvidenceError("deployment intent IDs must be globally distinct")
    for leg in legs:
        for authorization in leg["authorizations"]:
            if authorization["drill_id"] != drill_id:
                raise DrillEvidenceError(
                    "every release authorization must bind the drill ID"
                )

    actor_approvals = actors["approvals"]
    leg_approvals = [leg1["approval"], leg2["approval"]]
    if [item["approval_id"] for item in actor_approvals] != [
        item["release_approval"]["approval_id"] for item in leg_approvals
    ]:
        raise DrillEvidenceError(
            "drill.actors.approvals must exactly index the two release approvals"
        )
    for index, (actor_approval, leg_approval) in enumerate(
        zip(actor_approvals, leg_approvals, strict=True)
    ):
        if (
            actor_approval["approved_by_arn"]
            != leg_approval["release_approval"]["approved_by"]
        ):
            raise DrillEvidenceError(
                f"leg {index + 1} release approver differs from actors.approvals"
            )
        if leg_approval["drill_id"] != drill_id:
            raise DrillEvidenceError(
                f"leg {index + 1} approval does not bind the drill ID"
            )
        if leg_approval["plan_sha256"] != legs[index]["plan"]["sha256"]:
            raise DrillEvidenceError(
                f"leg {index + 1} approval does not bind its exact plan SHA"
            )
    actual_automation = sorted(
        {
            _automation_principal_identity(
                leg["apply"]["automation_principal"]
            ):
            leg["apply"]["automation_principal"]
            for leg in legs
        }.values(),
        key=_automation_principal_identity,
    )
    if actors["automation"] != actual_automation:
        raise DrillEvidenceError(
            "drill.actors.automation_principals must exactly index apply callers"
        )

    safe_verified_at = _timestamp(
        terminal["verified_at_utc"],
        label="drill.safe_terminal_state.verified_at_utc",
    )
    if not (
        control["started_at"]
        <= leg1["started_at"]
        <= leg1["completed_at"]
        <= leg2["started_at"]
        <= leg2["completed_at"]
        <= safe_verified_at
        <= control["completed_at"]
    ):
        raise DrillEvidenceError(
            "drill, leg, and safe-terminal timestamps must be monotonically "
            "nondecreasing"
        )

    if status == "PASSED":
        if leg1["value"]["from"] != initial_new:
            raise DrillEvidenceError(
                "leg1.from must equal the baseline initial-new state"
            )
        if leg1["value"]["to"] != leg2["value"]["from"]:
            raise DrillEvidenceError("leg1.to must exactly equal leg2.from")
        if leg2["value"]["to"] != initial_new:
            raise DrillEvidenceError(
                "leg2.to must equal the baseline initial-new state"
            )
        if any(leg["result"] != "PASSED" for leg in legs):
            raise DrillEvidenceError(
                "a drill containing any FAILED leg can never be PASSED"
            )
        if (
            leg1["apply_started_at"] - control["verified_at"]
        ).total_seconds() > MAX_START_DELAY_SECONDS:
            raise DrillEvidenceError(
                "rollback apply exceeded max_start_delay_seconds"
            )
        if (
            control["started_at"] - control["verified_at"]
        ).total_seconds() > MAX_START_DELAY_SECONDS:
            raise DrillEvidenceError(
                "drill start exceeded max_start_delay_seconds"
            )
        if (
            leg2["started_at"] - leg1["completed_at"]
        ).total_seconds() > MAX_OLD_DWELL_SECONDS:
            raise DrillEvidenceError(
                "previous-old dwell exceeded max_old_dwell_seconds"
            )
        for index, leg in enumerate(legs):
            recovery = leg["recovery"]
            if (
                recovery["attempted"]
                or recovery["result"] != "NOT_REQUIRED"
                or recovery["last_exact_confirmed_digests"]
                != leg["value"]["to"]["subjects"]
            ):
                raise DrillEvidenceError(
                    f"PASSED leg {index + 1} cannot contain recovery activity"
                )
        if (
            leg2["apply"]["state"] != initial_new
            or leg2["ecs"]["live_snapshot"] != initial_new
        ):
            raise DrillEvidenceError(
                "final Terraform and live ECS state must exactly match initial new"
            )
        if (
            terminal["classification"] != "INITIAL_NEW"
            or terminal["steady"] is not True
            or terminal["live_snapshot"]["snapshot"] != initial_new
        ):
            raise DrillEvidenceError(
                "PASSED requires a steady exact INITIAL_NEW terminal state"
            )
    else:
        if not any(
            leg["recovery"]["result"] != "NOT_REQUIRED" for leg in legs
        ):
            raise DrillEvidenceError(
                "FAILED/RECONCILE_REQUIRED must record whether restoration was attempted"
            )


def validate_drill_evidence(
    value: dict[str, Any],
    expected: dict[str, Any],
) -> dict[str, Any]:
    """Validate and return a detached forced-rollback drill aggregate.

    ``expected`` is the caller's trusted binding assembled from the code-owned
    consumer registry, the drill contract, and the exact initial-release
    receipt.  Both arguments are pure in-memory objects.
    """

    _require_builtin_json_types(value, label="drill")
    drill = _exact_object(value, _ROOT_KEYS, label="drill")
    _literal_integer(
        drill["schema_version"],
        DRILL_SCHEMA_VERSION,
        label="drill.schema_version",
    )
    if drill["kind"] != DRILL_KIND:
        raise DrillEvidenceError(f"drill.kind must be {DRILL_KIND}")
    drill_id = _uuid4(drill["drill_id"], label="drill.drill_id")
    status = _choice(drill["status"], DRILL_STATUSES, label="drill.status")
    _validate_environment(drill["environment"])

    referenced_locators: list[dict[str, Any]] = []
    control = _validate_control(
        drill["control"],
        locators=referenced_locators,
    )
    expected_bindings = _validate_expected_bindings(
        expected,
        aggregate_completed_at=control["completed_at"],
    )
    for key in (
        "git_commit",
        "drill_contract_sha256",
        "initial_release_apply",
        "initial_release_verified_at_utc",
    ):
        if drill["control"][key] != expected_bindings[key]:
            raise DrillEvidenceError(
                f"drill.control.{key} differs from the trusted binding"
            )
    actors = _validate_actors(drill["actors"])
    scope = _validate_scope(drill["scope"])
    if drill["scope"] != expected_bindings["scope"]:
        raise DrillEvidenceError(
            "drill.scope differs from the trusted registry/receipt closure"
        )
    baseline = _validate_baseline(
        drill["baseline"],
        scope=scope,
        aggregate_completed_at=control["completed_at"],
        locators=referenced_locators,
    )

    raw_legs = _array(drill["legs"], label="drill.legs")
    if len(raw_legs) != 2:
        raise DrillEvidenceError("drill.legs must contain exactly two legs")
    legs = [
        _validate_leg(
            raw_legs[0],
            index=0,
            drill_id=drill_id,
            required_source=scope["initial_new"],
            required_target=scope["previous_old"],
            require_exact_transition=status == "PASSED",
            scope=scope,
            aggregate_completed_at=control["completed_at"],
            locators=referenced_locators,
        ),
        _validate_leg(
            raw_legs[1],
            index=1,
            drill_id=drill_id,
            required_source=scope["previous_old"],
            required_target=scope["initial_new"],
            require_exact_transition=status == "PASSED",
            scope=scope,
            aggregate_completed_at=control["completed_at"],
            locators=referenced_locators,
        ),
    ]
    terminal = _validate_safe_terminal_state(
        drill["safe_terminal_state"],
        scope=scope,
        aggregate_completed_at=control["completed_at"],
        locators=referenced_locators,
    )
    _validate_cross_invariants(
        status=status,
        drill_id=drill_id,
        control=control,
        actors=actors,
        baseline=baseline,
        legs=legs,
        terminal=terminal,
    )
    _validate_artifact_manifest(
        drill["artifact_manifest"],
        referenced_locators=referenced_locators,
        aggregate_completed_at=control["completed_at"],
    )
    _validate_integrity(
        drill["integrity"],
        drill=drill,
        aggregate_completed_at=control["completed_at"],
    )
    try:
        return copy.deepcopy(drill)
    except (RecursionError, TypeError) as exc:
        raise DrillEvidenceError("validated drill evidence cannot be copied") from exc


def validate_forced_rollback_drill_evidence(
    value: dict[str, Any],
    expected: dict[str, Any],
) -> dict[str, Any]:
    """Descriptive alias for :func:`validate_drill_evidence`."""

    return validate_drill_evidence(value, expected)
