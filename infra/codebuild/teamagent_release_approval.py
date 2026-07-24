#!/usr/bin/env python3
"""Canonicalize and validate TeamAgent external release approvals.

This is the pure validation layer.  It deliberately has no AWS dependencies:
callers must validate S3 VersionIds, Object Lock metadata, and the KMS signature
before passing the payload here.

All payload timestamps use the existing evidence convention: RFC3339 UTC with a
``Z`` suffix and whole-second precision.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
from collections.abc import Mapping
from typing import Any

from teamagent_schema_versions import SCHEMA_VERSIONS

APPROVAL_SCHEMA_VERSION = SCHEMA_VERSIONS.external_approval
APPROVAL_KIND = "teamagent.core-media-release-approval"
INNER_CONTRACT_SCHEMA_VERSION = SCHEMA_VERSIONS.inner_runtime_contract
OUTER_CONTRACT_SCHEMA_VERSION = SCHEMA_VERSIONS.outer_core_media_contract
FORCED_ROLLBACK_GATE_VERSION = 1
FORCED_ROLLBACK_PASSED = "PASSED"
FORCED_ROLLBACK_PROVISIONAL = "PROVISIONAL_INITIAL_RELEASE"
FORCED_ROLLBACK_STATES = frozenset({FORCED_ROLLBACK_PASSED, FORCED_ROLLBACK_PROVISIONAL})
APPROVAL_PIPELINES = frozenset({"mcp", "x_buzz"})
APPROVAL_ENVIRONMENTS = frozenset({"dev"})
APPROVAL_OBSERVATION_KEYS = (
    "core.base.builder.arm64.digest",
    "core.binary.python.sha256",
    "media.binary.chromium.sha256",
    "media.binary.ffmpeg.sha256",
    "media.binary.node.sha256",
    "media.binary.python.sha256",
)

_TOP_LEVEL_KEYS = {
    "schema_version",
    "kind",
    "approval_id",
    "pipeline",
    "environment",
    "approved_at_utc",
    "expires_at_utc",
    "approved_by",
    "source_commit",
    "source_tree_oid",
    "contracts",
    "observations",
    "decision",
    "gates",
    "approval_authority",
}
_AUTHORITY_KEYS = {
    "publisher_project_arn",
    "publisher_build_id",
    "kms_key_arn",
}
_EXPECTED_KEYS = {
    "commit",
    "tree_oid",
    "inner_sha",
    "outer_sha",
    "observations",
    "pipeline",
    "environment",
    "approved_by",
    "authority",
    "forced_rollback_state",
}
_OPTIONAL_EXPECTED_KEYS = {
    "forced_rollback_evidence",
}

_GIT_OID_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
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
_IAM_ROLE_ARN_RE = re.compile(
    r"arn:(?:aws|aws-cn|aws-us-gov):iam::[0-9]{12}:role/"
    r"[A-Za-z0-9+=,.@_/-]{1,512}"
)
_CODEBUILD_PROJECT_ARN_RE = re.compile(
    r"arn:(?:aws|aws-cn|aws-us-gov):codebuild:[a-z0-9-]+:[0-9]{12}:"
    r"project/[A-Za-z0-9][A-Za-z0-9_-]{1,254}"
)
_KMS_KEY_ARN_RE = re.compile(
    r"arn:(?:aws|aws-cn|aws-us-gov):kms:[a-z0-9-]+:[0-9]{12}:"
    r"key/[A-Za-z0-9-]{1,256}"
)


class ProvenanceError(ValueError):
    """The approval payload is malformed, stale, or does not match its target."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProvenanceError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_nonfinite_json(value: str) -> None:
    raise ProvenanceError(f"non-finite JSON number is forbidden: {value}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return the exact UTF-8 JSON bytes used for hashing and signing."""

    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return (rendered + "\n").encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise ProvenanceError("approval payload is not canonical JSON data") from exc


def _require_builtin_json_types(
    value: Any,
    *,
    label: str,
    active_container_ids: set[int] | None = None,
    depth: int = 0,
) -> None:
    if depth > 64:
        raise ProvenanceError(f"{label} exceeds the JSON nesting limit")
    if active_container_ids is None:
        active_container_ids = set()
    if type(value) is dict:
        identity = id(value)
        if identity in active_container_ids:
            raise ProvenanceError(f"{label} contains a JSON container cycle")
        active_container_ids.add(identity)
        try:
            for key, item in value.items():
                if type(key) is not str:
                    raise ProvenanceError(f"{label} keys must be built-in strings")
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
            raise ProvenanceError(f"{label} contains a JSON container cycle")
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
    raise ProvenanceError(f"{label} contains a non-JSON or non-built-in value")


def _parse_payload(payload: bytes | dict[str, Any]) -> dict[str, Any]:
    if type(payload) is bytes:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProvenanceError("approval payload must be UTF-8") from exc
        try:
            value = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite_json,
            )
        except ProvenanceError:
            raise
        except (RecursionError, ValueError) as exc:
            raise ProvenanceError("approval payload is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ProvenanceError("approval payload must be a JSON object")
        if payload != canonical_json_bytes(value):
            raise ProvenanceError("approval payload bytes are not canonical")
        return value
    if type(payload) is dict:
        _require_builtin_json_types(payload, label="approval payload")
        value = json.loads(canonical_json_bytes(payload).decode("utf-8"))
        if type(value) is not dict:
            raise ProvenanceError("approval payload must be a JSON object")
        return value
    raise ProvenanceError("approval payload must be bytes or a dict")


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ProvenanceError(f"{label} must be an object")
    return value


def _string_keys(value: Mapping[Any, Any], *, label: str) -> set[str]:
    if any(type(key) is not str for key in value):
        raise ProvenanceError(f"{label} keys must be strings")
    return set(value)


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    keys = _string_keys(value, label=label)
    missing = sorted(expected - keys)
    unknown = sorted(keys - expected)
    if missing or unknown:
        raise ProvenanceError(f"{label} schema mismatch: missing={missing}, unknown={unknown}")


def _literal_integer(value: Any, expected: int, *, label: str) -> int:
    if type(value) is not int or value != expected:
        raise ProvenanceError(f"{label} must be integer {expected}")
    return value


def _text(value: Any, *, label: str, maximum: int = 16384) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ProvenanceError(f"{label} must be canonical non-empty text")
    return value


def _git_oid(value: Any, *, label: str) -> str:
    value = _text(value, label=label, maximum=40)
    if not _GIT_OID_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be a 40-character lowercase Git OID")
    return value


def _sha256(value: Any, *, label: str) -> str:
    value = _text(value, label=label, maximum=64)
    if not _SHA256_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be a lowercase SHA-256")
    return value


def _uuid4(value: Any, *, label: str) -> str:
    value = _text(value, label=label, maximum=36)
    if not _UUID4_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be a lowercase UUIDv4")
    return value


def _timestamp(value: Any, *, label: str) -> dt.datetime:
    value = _text(value, label=label, maximum=20)
    if not _RFC3339_UTC_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be an RFC3339 UTC timestamp at whole-second precision")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError as exc:
        raise ProvenanceError(f"{label} is not a valid UTC timestamp") from exc


def _iam_role_arn(value: Any, *, label: str) -> str:
    value = _text(value, label=label, maximum=600)
    if not _IAM_ROLE_ARN_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be an IAM role ARN")
    return value


def _codebuild_project_arn(value: Any, *, label: str) -> str:
    value = _text(value, label=label, maximum=512)
    if not _CODEBUILD_PROJECT_ARN_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be a CodeBuild project ARN")
    return value


def _kms_key_arn(value: Any, *, label: str) -> str:
    value = _text(value, label=label, maximum=512)
    if not _KMS_KEY_ARN_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be a KMS key ARN")
    return value


def _version_id(value: Any, *, label: str) -> str:
    value = _text(value, label=label, maximum=1024)
    if value in {"None", "null"} or not _S3_VERSION_ID_RE.fullmatch(value):
        raise ProvenanceError(f"{label} must be an exact S3 VersionId")
    return value


def _validate_now(now: dt.datetime) -> dt.datetime:
    if not isinstance(now, dt.datetime) or now.tzinfo is None:
        raise ProvenanceError("now must be a timezone-aware datetime")
    if now.utcoffset() is None:
        raise ProvenanceError("now must be a timezone-aware datetime")
    return now.astimezone(dt.UTC)


def _validate_contracts(value: Any, expected: Mapping[str, Any]) -> None:
    contracts = _mapping(value, label="approval.contracts")
    _exact_keys(contracts, {"inner", "outer"}, label="approval.contracts")
    for name, schema_version, expected_key in (
        ("inner", INNER_CONTRACT_SCHEMA_VERSION, "inner_sha"),
        ("outer", OUTER_CONTRACT_SCHEMA_VERSION, "outer_sha"),
    ):
        contract = _mapping(contracts[name], label=f"approval.contracts.{name}")
        _exact_keys(
            contract,
            {"schema_version", "raw_sha256"},
            label=f"approval.contracts.{name}",
        )
        _literal_integer(
            contract["schema_version"],
            schema_version,
            label=f"approval.contracts.{name}.schema_version",
        )
        actual_sha = _sha256(
            contract["raw_sha256"],
            label=f"approval.contracts.{name}.raw_sha256",
        )
        if actual_sha != expected[expected_key]:
            raise ProvenanceError(f"approval.contracts.{name}.raw_sha256 mismatch")


def _validate_observations(
    value: Any,
    *,
    expected_values: Mapping[str, str],
    approved_at: dt.datetime,
) -> None:
    if type(value) is not list:
        raise ProvenanceError("approval.observations must be an array")
    if len(value) != len(APPROVAL_OBSERVATION_KEYS):
        raise ProvenanceError("approval.observations must contain exactly six entries")

    actual_keys: list[str] = []
    for index, raw_observation in enumerate(value):
        label = f"approval.observations[{index}]"
        observation = _mapping(raw_observation, label=label)
        _exact_keys(
            observation,
            {"key", "value", "observed_at_utc", "source"},
            label=label,
        )
        key = _text(observation["key"], label=f"{label}.key", maximum=128)
        actual_keys.append(key)
        actual_value = _text(observation["value"], label=f"{label}.value")
        observed_at = _timestamp(
            observation["observed_at_utc"],
            label=f"{label}.observed_at_utc",
        )
        if observed_at > approved_at:
            raise ProvenanceError(f"{label}.observed_at_utc must not be later than approved_at_utc")
        _text(observation["source"], label=f"{label}.source")
        if key in expected_values and actual_value != expected_values[key]:
            raise ProvenanceError(f"{label}.value mismatch for {key}")

    if tuple(actual_keys) != APPROVAL_OBSERVATION_KEYS:
        raise ProvenanceError(
            "approval.observations keys must be exact, unique, and dictionary-sorted"
        )


def _validate_manifest_locator(value: Any) -> None:
    manifest = _mapping(value, label="approval forced gate drill_manifest")
    _exact_keys(
        manifest,
        {"payload", "signature", "kms_key_arn", "signing_algorithm"},
        label="approval forced gate drill_manifest",
    )
    payload = _mapping(
        manifest["payload"],
        label="approval forced gate drill_manifest.payload",
    )
    _exact_keys(
        payload,
        {"bucket", "key", "version_id", "sha256"},
        label="approval forced gate drill_manifest.payload",
    )
    bucket = _text(
        payload["bucket"],
        label="approval forced gate drill_manifest.payload.bucket",
        maximum=63,
    )
    if not _S3_BUCKET_RE.fullmatch(bucket):
        raise ProvenanceError(
            "approval forced gate drill_manifest.payload.bucket is not an S3 bucket"
        )
    if ".." in bucket or ".-" in bucket or "-." in bucket:
        raise ProvenanceError(
            "approval forced gate drill_manifest.payload.bucket is not an S3 bucket"
        )
    _text(
        payload["key"],
        label="approval forced gate drill_manifest.payload.key",
        maximum=1024,
    )
    _version_id(
        payload["version_id"],
        label="approval forced gate drill_manifest.payload.version_id",
    )
    _sha256(
        payload["sha256"],
        label="approval forced gate drill_manifest.payload.sha256",
    )

    signature = _mapping(
        manifest["signature"],
        label="approval forced gate drill_manifest.signature",
    )
    _exact_keys(
        signature,
        {"key", "version_id", "sha256"},
        label="approval forced gate drill_manifest.signature",
    )
    _text(
        signature["key"],
        label="approval forced gate drill_manifest.signature.key",
        maximum=1024,
    )
    _version_id(
        signature["version_id"],
        label="approval forced gate drill_manifest.signature.version_id",
    )
    _sha256(
        signature["sha256"],
        label="approval forced gate drill_manifest.signature.sha256",
    )
    _kms_key_arn(
        manifest["kms_key_arn"],
        label="approval forced gate drill_manifest.kms_key_arn",
    )
    if manifest["signing_algorithm"] != "RSASSA_PSS_SHA_256":
        raise ProvenanceError(
            "approval forced gate drill_manifest.signing_algorithm is unsupported"
        )


def _validate_provisional_campaign(value: Any, *, now: dt.datetime) -> None:
    campaign = _mapping(value, label="approval forced gate provisional_campaign")
    _exact_keys(
        campaign,
        {
            "campaign_id",
            "phase",
            "payload_version_id",
            "payload_sha256",
            "signature_version_id",
            "kms_key_arn",
            "expires_at_utc",
        },
        label="approval forced gate provisional_campaign",
    )
    _text(
        campaign["campaign_id"],
        label="approval forced gate provisional_campaign.campaign_id",
        maximum=256,
    )
    phase = _text(
        campaign["phase"],
        label="approval forced gate provisional_campaign.phase",
        maximum=2,
    )
    if phase not in {"R1", "R2"}:
        raise ProvenanceError("approval forced gate provisional_campaign.phase must be R1 or R2")
    _version_id(
        campaign["payload_version_id"],
        label="approval forced gate provisional_campaign.payload_version_id",
    )
    _sha256(
        campaign["payload_sha256"],
        label="approval forced gate provisional_campaign.payload_sha256",
    )
    _version_id(
        campaign["signature_version_id"],
        label="approval forced gate provisional_campaign.signature_version_id",
    )
    _kms_key_arn(
        campaign["kms_key_arn"],
        label="approval forced gate provisional_campaign.kms_key_arn",
    )
    expires_at = _timestamp(
        campaign["expires_at_utc"],
        label="approval forced gate provisional_campaign.expires_at_utc",
    )
    if now >= expires_at:
        raise ProvenanceError("approval forced gate provisional campaign is expired")


def _validate_forced_rollback_gate(value: Any, *, now: dt.datetime) -> dict[str, Any]:
    gate = _mapping(value, label="approval.gates.forced_rollback_evidence")
    gate_keys = _string_keys(gate, label="approval.gates.forced_rollback_evidence")
    common_keys = {"gate_version", "state"}
    missing = sorted(common_keys - gate_keys)
    unknown = sorted(
        gate_keys
        - {
            "gate_version",
            "state",
            "drill_manifest",
            "provisional_campaign",
        }
    )
    if missing or unknown:
        raise ProvenanceError(
            "approval.gates.forced_rollback_evidence schema mismatch: "
            f"missing={missing}, unknown={unknown}"
        )
    _literal_integer(
        gate["gate_version"],
        FORCED_ROLLBACK_GATE_VERSION,
        label="approval.gates.forced_rollback_evidence.gate_version",
    )
    state = _text(
        gate["state"],
        label="approval.gates.forced_rollback_evidence.state",
        maximum=31,
    )
    if state not in FORCED_ROLLBACK_STATES:
        raise ProvenanceError("approval.gates.forced_rollback_evidence.state is unsupported")
    if state == FORCED_ROLLBACK_PASSED:
        _exact_keys(
            gate,
            {"gate_version", "state", "drill_manifest"},
            label="approval.gates.forced_rollback_evidence PASSED variant",
        )
        _validate_manifest_locator(gate["drill_manifest"])
    else:
        _exact_keys(
            gate,
            {"gate_version", "state", "provisional_campaign"},
            label="approval.gates.forced_rollback_evidence PROVISIONAL variant",
        )
        _validate_provisional_campaign(gate["provisional_campaign"], now=now)
    return gate


def _validate_authority(value: Any, expected: Mapping[str, Any]) -> None:
    authority = _mapping(value, label="approval.approval_authority")
    _exact_keys(authority, _AUTHORITY_KEYS, label="approval.approval_authority")
    _codebuild_project_arn(
        authority["publisher_project_arn"],
        label="approval.approval_authority.publisher_project_arn",
    )
    _text(
        authority["publisher_build_id"],
        label="approval.approval_authority.publisher_build_id",
        maximum=512,
    )
    _kms_key_arn(
        authority["kms_key_arn"],
        label="approval.approval_authority.kms_key_arn",
    )
    if authority != expected:
        raise ProvenanceError("approval.approval_authority mismatch")


def _validate_expected(expected: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(expected, Mapping):
        raise ProvenanceError("expected approval bindings must be a mapping")
    expected_keys = _string_keys(expected, label="expected approval bindings")
    unknown = sorted(expected_keys - _EXPECTED_KEYS - _OPTIONAL_EXPECTED_KEYS)
    missing = sorted(_EXPECTED_KEYS - expected_keys)
    if missing or unknown:
        raise ProvenanceError(
            f"expected approval bindings mismatch: missing={missing}, unknown={unknown}"
        )
    normalized = dict(expected)
    normalized["commit"] = _git_oid(
        normalized["commit"],
        label="expected.commit",
    )
    normalized["tree_oid"] = _git_oid(
        normalized["tree_oid"],
        label="expected.tree_oid",
    )
    normalized["inner_sha"] = _sha256(
        normalized["inner_sha"],
        label="expected.inner_sha",
    )
    normalized["outer_sha"] = _sha256(
        normalized["outer_sha"],
        label="expected.outer_sha",
    )
    normalized["pipeline"] = _text(
        normalized["pipeline"],
        label="expected.pipeline",
        maximum=16,
    )
    if normalized["pipeline"] not in APPROVAL_PIPELINES:
        raise ProvenanceError("expected.pipeline is unsupported")
    normalized["environment"] = _text(
        normalized["environment"],
        label="expected.environment",
        maximum=32,
    )
    if normalized["environment"] not in APPROVAL_ENVIRONMENTS:
        raise ProvenanceError("expected.environment is unsupported")
    normalized["approved_by"] = _iam_role_arn(
        normalized["approved_by"],
        label="expected.approved_by",
    )

    observations = normalized["observations"]
    if not isinstance(observations, Mapping):
        raise ProvenanceError("expected.observations must be a mapping")
    observation_keys = _string_keys(observations, label="expected.observations")
    if tuple(sorted(observation_keys)) != APPROVAL_OBSERVATION_KEYS:
        raise ProvenanceError("expected.observations must contain the exact six keys")
    normalized["observations"] = {
        key: _text(observations[key], label=f"expected.observations[{key!r}]")
        for key in APPROVAL_OBSERVATION_KEYS
    }

    authority = _mapping(normalized["authority"], label="expected.authority")
    _exact_keys(authority, _AUTHORITY_KEYS, label="expected.authority")
    normalized["authority"] = dict(authority)
    _codebuild_project_arn(
        authority["publisher_project_arn"],
        label="expected.authority.publisher_project_arn",
    )
    _text(
        authority["publisher_build_id"],
        label="expected.authority.publisher_build_id",
        maximum=512,
    )
    _kms_key_arn(
        authority["kms_key_arn"],
        label="expected.authority.kms_key_arn",
    )

    state = _text(
        normalized["forced_rollback_state"],
        label="expected.forced_rollback_state",
        maximum=31,
    )
    if state not in FORCED_ROLLBACK_STATES:
        raise ProvenanceError("expected.forced_rollback_state is unsupported")
    normalized["forced_rollback_state"] = state
    expected_gate = normalized.get("forced_rollback_evidence")
    if expected_gate is not None:
        if type(expected_gate) is not dict:
            raise ProvenanceError("expected.forced_rollback_evidence must be an object")
        _require_builtin_json_types(
            expected_gate,
            label="expected.forced_rollback_evidence",
        )
        normalized["forced_rollback_evidence"] = copy.deepcopy(expected_gate)
        canonical_json_bytes(normalized["forced_rollback_evidence"])
    return normalized


def validate_approval_payload(
    payload: bytes | dict[str, Any],
    expected: Mapping[str, Any],
    *,
    now: dt.datetime,
) -> dict[str, Any]:
    """Validate an external approval and return a detached normalized object.

    ``expected`` must contain the fixed trust bindings ``commit``, ``tree_oid``,
    ``inner_sha``, ``outer_sha``, ``observations`` (key-to-value mapping),
    ``pipeline``, ``environment``, ``approved_by``, and ``authority``.  Callers
    must also select the operation-appropriate ``forced_rollback_state`` and may
    pin the complete ``forced_rollback_evidence`` object.
    """

    normalized_now = _validate_now(now)
    expected_bindings = _validate_expected(expected)
    approval = _parse_payload(payload)
    _exact_keys(approval, _TOP_LEVEL_KEYS, label="approval")
    _literal_integer(
        approval["schema_version"],
        APPROVAL_SCHEMA_VERSION,
        label="approval.schema_version",
    )
    if approval["kind"] != APPROVAL_KIND:
        raise ProvenanceError("approval.kind mismatch")
    _uuid4(approval["approval_id"], label="approval.approval_id")

    pipeline = _text(approval["pipeline"], label="approval.pipeline", maximum=16)
    if pipeline not in APPROVAL_PIPELINES:
        raise ProvenanceError("approval.pipeline is unsupported")
    if pipeline != expected_bindings["pipeline"]:
        raise ProvenanceError("approval.pipeline mismatch")
    environment = _text(
        approval["environment"],
        label="approval.environment",
        maximum=32,
    )
    if environment not in APPROVAL_ENVIRONMENTS:
        raise ProvenanceError("approval.environment is unsupported")
    if environment != expected_bindings["environment"]:
        raise ProvenanceError("approval.environment mismatch")

    approved_at = _timestamp(
        approval["approved_at_utc"],
        label="approval.approved_at_utc",
    )
    expires_at = _timestamp(
        approval["expires_at_utc"],
        label="approval.expires_at_utc",
    )
    if approved_at > normalized_now:
        raise ProvenanceError("approval.approved_at_utc must not be in the future")
    if expires_at <= approved_at:
        raise ProvenanceError("approval.expires_at_utc must be after approved_at_utc")
    if normalized_now >= expires_at:
        raise ProvenanceError("approval is expired")

    approved_by = _iam_role_arn(
        approval["approved_by"],
        label="approval.approved_by",
    )
    if approved_by != expected_bindings["approved_by"]:
        raise ProvenanceError("approval.approved_by mismatch")
    source_commit = _git_oid(
        approval["source_commit"],
        label="approval.source_commit",
    )
    if source_commit != expected_bindings["commit"]:
        raise ProvenanceError("approval.source_commit mismatch")
    source_tree_oid = _git_oid(
        approval["source_tree_oid"],
        label="approval.source_tree_oid",
    )
    if source_tree_oid != expected_bindings["tree_oid"]:
        raise ProvenanceError("approval.source_tree_oid mismatch")

    _validate_contracts(approval["contracts"], expected_bindings)
    _validate_observations(
        approval["observations"],
        expected_values=expected_bindings["observations"],
        approved_at=approved_at,
    )
    decision = _text(
        approval["decision"],
        label="approval.decision",
    )
    if not decision.startswith("APPROVED: "):
        raise ProvenanceError("approval.decision must begin with 'APPROVED: '")

    gates = _mapping(approval["gates"], label="approval.gates")
    _exact_keys(
        gates,
        {"forced_rollback_evidence"},
        label="approval.gates",
    )
    forced_gate = _validate_forced_rollback_gate(
        gates["forced_rollback_evidence"],
        now=normalized_now,
    )
    expected_state = expected_bindings["forced_rollback_state"]
    if forced_gate["state"] != expected_state:
        raise ProvenanceError("approval forced rollback state mismatch")
    expected_gate = expected_bindings.get("forced_rollback_evidence")
    if expected_gate is not None and canonical_json_bytes(forced_gate) != canonical_json_bytes(
        expected_gate
    ):
        raise ProvenanceError("approval forced rollback evidence mismatch")

    _validate_authority(
        approval["approval_authority"],
        expected_bindings["authority"],
    )
    return json.loads(canonical_json_bytes(approval).decode("utf-8"))
