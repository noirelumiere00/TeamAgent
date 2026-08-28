#!/usr/bin/env python3
"""Validate signed source declarations and image release receipts.

The fixed CodeBuild projects embed this file together with its trusted sibling
schema and approval validators.  It is also used by Terraform's plan-time
release gate.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_TRUSTED_HELPER_DIRECTORY = Path(__file__).resolve().parent
if str(_TRUSTED_HELPER_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_TRUSTED_HELPER_DIRECTORY))

from teamagent_release_approval import (  # noqa: E402
    APPROVAL_PIPELINES,
    FORCED_ROLLBACK_GATE_VERSION,
    FORCED_ROLLBACK_PASSED,
    FORCED_ROLLBACK_PROVISIONAL,
    validate_approval_payload,
)
from teamagent_release_approval import (  # noqa: E402
    ProvenanceError as ApprovalProvenanceError,
)
from teamagent_release_approval import (  # noqa: E402
    canonical_json_bytes as approval_canonical_json_bytes,
)
from teamagent_schema_versions import SCHEMA_VERSIONS  # noqa: E402

ACCOUNT_ID = "718959508629"
REGION = "ap-northeast-1"
REGISTRY = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"
EVIDENCE_BUCKET = "teamagent-dev-image-release-evidence"
SOURCE_BUCKET = "teamagent-dev-raw-files"
SOURCE_KEY = "codebuild/source.zip"
APP_HTML_KEY = "codebuild/connect-web-app.html"
SOURCE_REPOSITORY = "https://github.com/noirelumiere00/TeamAgent.git"
SOURCE_BRANCH = "dev"
SOURCE_DECLARATION_KIND = "teamagent.source-declaration"
RELEASE_RECEIPT_KIND = "teamagent.release-receipt"
DEPLOYMENT_INTENT_KIND = "teamagent.image-deployment-intent"
SOURCE_DECLARATION_SCHEMA = SCHEMA_VERSIONS.mcp_source_declaration
RELEASE_RECEIPT_SCHEMA = SCHEMA_VERSIONS.mcp_release_receipt
LEGACY_NON_MCP_RELEASE_RECEIPT_SCHEMA = 2
DEPLOYMENT_INTENT_SCHEMA = SCHEMA_VERSIONS.image_deployment_intent
APPROVAL_EVIDENCE_PREFIX = "approval-records"
APPROVAL_PUBLISHER_PROJECT_NAME = "teamagent-dev-approval-publisher"
APPROVAL_PUBLISHER_PROJECT_ARN = (
    f"arn:aws:codebuild:{REGION}:{ACCOUNT_ID}:project/{APPROVAL_PUBLISHER_PROJECT_NAME}"
)
APPROVAL_APPROVED_BY_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/teamagent-dev-approval-caller"
APPROVAL_OPERATION_STATES = {
    "build": FORCED_ROLLBACK_PASSED,
    "authorize": FORCED_ROLLBACK_PASSED,
    "terraform-plan": FORCED_ROLLBACK_PASSED,
    "terraform-apply": FORCED_ROLLBACK_PASSED,
    "drill": FORCED_ROLLBACK_PROVISIONAL,
}
# Time-boxed initial-release exemption -- NOT a permanent relaxation.
#
# Why it exists: build and authorize both demand FORCED_ROLLBACK_PASSED, but a
# PASSED gate can only come out of a drill whose own plan leg calls
# authorize_image_release.sh with --operation authorize.  Nothing in the tree
# ever passes --operation drill, so the first release can never be produced.
#
# What it does NOT claim: this cannot enforce "exactly once".  The validator is
# a pure function and one release fans out to at least four separate processes,
# so there is nowhere to keep a one-shot ledger.  What is enforced is narrower
# and honest: one byte-pinned gate object, two operations, and a deadline.
#
# Why it does not widen the attack surface: the PASSED branch of
# _validate_forced_rollback_gate only shape-checks drill_manifest -- no S3 read,
# no KMS verify -- so anyone who can issue an approval today can already declare
# a fabricated PASSED and get through unpinned.  The exemption below accepts a
# single fixed gate object, so it is strictly narrower and fully auditable.
# The sunset is therefore a forcing function to build the real PASSED path, not
# a security control.
INITIAL_RELEASE_EXEMPT_STATES: dict[str, frozenset[str]] = {
    "build": frozenset({FORCED_ROLLBACK_PROVISIONAL}),
    "authorize": frozenset({FORCED_ROLLBACK_PROVISIONAL}),
}
INITIAL_RELEASE_EXEMPTION_SUNSET_UTC = "2026-09-15T00:00:00Z"
INITIAL_RELEASE_EXEMPTION_CAMPAIGN_EXPIRES_AT_UTC = "2026-09-22T00:00:00Z"
INITIAL_RELEASE_EXEMPTION_CAMPAIGN_ID = "initial-release-exemption-no-charter-object"
# The campaign locators are deliberately unresolvable: no drill charter object
# exists yet, and inventing a plausible locator would be a fabricated citation.
_INITIAL_RELEASE_EXEMPTION_UNREFERENCED_VERSION_ID = "initial-release-exemption-no-charter-object"
_INITIAL_RELEASE_EXEMPTION_UNREFERENCED_SHA256 = "0" * 64
MAX_RELEASE_RECEIPT_LIFETIME_SECONDS = 3600
# Verified candidates are durable rollback inputs, not short-lived deployment
# authorizations. Active/rollback receipts remain limited to one hour.
MAX_CANDIDATE_RECEIPT_LIFETIME_SECONDS = 3650 * 24 * 60 * 60
MAX_DEPLOYMENT_INTENT_LIFETIME_SECONDS = 3600
DEPLOYMENT_INTENT_AUDIT_TTL_SECONDS = 90 * 24 * 60 * 60
MAX_RELEASE_GRAPH_DIGESTS = 256
DEPLOYMENT_INTENT_TABLE = "teamagent-dev-image-deployment-intents"
DEPLOYMENT_LOCK_RECORD_ID = "lock#teamagent/terraform.tfstate"
DEPLOYMENT_LOCK_LEASE_SECONDS = 300
AWS_EXECUTABLE = "aws"
ALLOWED_EXISTING_LOG_IMPORTS = {
    "aws_cloudwatch_log_group.codebuild_aiia_image_builder": (
        "/aws/codebuild/teamagent-dev-aiia-image-builder"
    ),
    "aws_cloudwatch_log_group.codebuild_image": ("/aws/codebuild/teamagent-dev-image-builder"),
    "aws_cloudwatch_log_group.ecs_containerinsights_teamagent": (
        "/aws/ecs/containerinsights/teamagent-dev/performance"
    ),
    "aws_cloudwatch_log_group.ecs_containerinsights_tiktok": (
        "/aws/ecs/containerinsights/teamagent-dev-tiktok/performance"
    ),
    "aws_cloudwatch_log_group.reminder_notify": ("/aws/lambda/teamagent-dev-reminders-notify"),
    "aws_cloudwatch_log_group.tiktok_dispatch": (
        "/aws/lambda/teamagent-dev-tiktok-acquire-dispatch"
    ),
    "aws_cloudwatch_log_group.x_dispatch": ("/aws/lambda/teamagent-dev-x-buzz-dispatch"),
}
SINGLE_ARM64_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}
REFERRER_ARTIFACT_TYPES = {
    "sbom": "application/spdx+json",
    "provenance": "application/vnd.in-toto+json",
}
SIGNATURE_ARTIFACT_TYPES = {
    # cosign 3.x emits the sigstore bundle envelope; the two older types stay
    # accepted so signatures produced by earlier cosign releases still verify.
    "application/vnd.dev.sigstore.bundle.v0.3+json",
    "application/vnd.dev.cosign.simplesigning.v1+json",
    "application/vnd.dsse.envelope.v1+json",
}
PIPELINES: dict[str, dict[str, Any]] = {
    "mcp": {
        "build_project": "teamagent-dev-image-builder",
        "contract_path": "infra/codebuild/teamagent_core_media_release_contract.json",
        "contract_label": "io.teamagent.build.release-contract-sha256",
        "subjects": {
            "core": (
                "teamagent-mcp-quarantine",
                "teamagent-mcp-verified-candidates",
                "teamagent-mcp",
            ),
            "media": (
                "teamagent-media-worker-quarantine",
                "teamagent-media-worker-verified-candidates",
                "teamagent-media-worker",
            ),
        },
    },
    "tiktok": {
        "build_project": "teamagent-dev-tiktok-image-builder",
        "contract_path": "infra/codebuild/tiktok_release_contract.json",
        "contract_label": "io.teamagent.build.contract-sha256",
        "subjects": {
            "tiktok": (
                "teamagent-dev-tiktok-acquire-quarantine",
                "teamagent-dev-tiktok-acquire-verified-candidates",
                "teamagent-dev-tiktok-acquire",
            ),
        },
    },
    "openclaw": {
        "build_project": "teamagent-dev-openclaw-provenance-builder",
        "contract_path": "infra/codebuild/openclaw_bundle_contract.json",
        "contract_label": "io.teamagent.build.contract-sha256",
        # OpenClaw keeps the explicit subject suffix even though its canonical
        # bundle is core-only, matching the immutable tags emitted by the
        # dedicated provenance build.
        "single_subject_tag_suffix": True,
        "subjects": {
            "core": (
                "teamagent-openclaw-quarantine",
                "teamagent-openclaw-verified-candidates",
                "teamagent-openclaw",
            ),
        },
    },
}
RELEASE_GATE_ADDRESS = "terraform_data.production_image_release_gate"
HMAC_RUNTIME_GATE_ADDRESSES = frozenset(
    {
        'terraform_data.hmac_live_task_gate["mcp"]',
        'terraform_data.hmac_live_task_gate["connect_web"]',
        'terraform_data.hmac_live_task_gate["morning_digest"]',
        "terraform_data.hmac_mcp_pre_update[0]",
        "terraform_data.hmac_mcp_post_update[0]",
        "terraform_data.hmac_connect_web_pre_update[0]",
        "terraform_data.hmac_connect_web_post_update[0]",
        "terraform_data.hmac_morning_digest_pre_update[0]",
        "terraform_data.hmac_morning_digest_post_update[0]",
    }
)
IMAGE_MANAGED_ECS_PIPELINES = {
    "aws_ecs_task_definition.mcp": "mcp",
    "aws_ecs_task_definition.canary": "canary",
    "aws_ecs_task_definition.connect_web": "connect_web",
    "aws_ecs_task_definition.ingest": "ingest",
    "aws_ecs_task_definition.morning_digest": "morning_digest",
    "aws_ecs_task_definition.x_buzz_worker": "x_buzz_worker",
    "aws_ecs_service.mcp": "mcp",
    "aws_ecs_service.connect_web": "connect_web",
    "aws_ecs_task_definition.openclaw": "openclaw",
    "aws_ecs_service.openclaw": "openclaw",
    "aws_ecs_task_definition.tiktok_acquire": "tiktok_acquire",
}

_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_S3_VERSION_RE = re.compile(r"[A-Za-z0-9._~+/=-]{1,1024}")
_BUILD_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:/._-]{0,511}")
_PATH_RE = re.compile(r"/[A-Za-z0-9][A-Za-z0-9_./+-]{0,511}")
_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,254}")
_INSTANCE_SELECTOR_RE = re.compile(r'\[(?:[0-9]+|"(?:[^"\\]|\\.)*")\]')
_KEY_ARN_RE = re.compile(rf"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/[0-9a-f-]{{36}}")
_DYNAMODB_TABLE_ARN_RE = re.compile(
    rf"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/"
    r"[A-Za-z0-9_.-]{3,255}"
)
_LEDGER_STAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_UUID4_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_UNTRUSTED_LABEL_VALUES = {
    "",
    "n/a",
    "none",
    "null",
    "placeholder",
    "tbd",
    "unknown",
}
_DYNAMODB_TRANSACTION_NAMESPACE = uuid.UUID("0bc3f23e-e672-4acf-9f2c-1d3ebc750979")


class EvidenceError(ValueError):
    """The signed evidence is malformed, stale, or outside the allowlist."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid {label}: {exc}") from exc


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing or unknown:
        raise EvidenceError(f"{label} schema mismatch: missing={missing}, unknown={unknown}")


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    return value


def _string(value: Any, *, label: str, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise EvidenceError(f"{label} must be a canonical non-empty string")
    return value


def _sha1(value: Any, *, label: str) -> str:
    value = _string(value, label=label)
    if not _SHA1_RE.fullmatch(value):
        raise EvidenceError(f"{label} must be a full lowercase Git SHA")
    return value


def _sha256(value: Any, *, label: str) -> str:
    value = _string(value, label=label)
    if not _SHA256_RE.fullmatch(value):
        raise EvidenceError(f"{label} must be a lowercase SHA-256")
    return value


def _uuid4(value: Any, *, label: str) -> str:
    value = _string(value, label=label, maximum=36)
    if not _UUID4_RE.fullmatch(value):
        raise EvidenceError(f"{label} must be a lowercase UUIDv4")
    return value


def _epoch_seconds(value: Any, *, label: str) -> str:
    value = _string(value, label=label, maximum=10)
    if not re.fullmatch(r"[1-9][0-9]{9}", value):
        raise EvidenceError(f"{label} must be canonical Unix epoch seconds")
    return value


def _dynamodb_transaction_token(apply_attempt_id: str, *, phase: str) -> str:
    attempt_id = _uuid4(apply_attempt_id, label="apply attempt ID")
    if phase not in {
        "begin-apply",
        "begin-media-apply",
        "consume-authorization",
    }:
        raise EvidenceError("DynamoDB transaction phase is not allowlisted")
    return str(
        uuid.uuid5(
            _DYNAMODB_TRANSACTION_NAMESPACE,
            f"{phase}:{attempt_id}",
        )
    )


def _digest(value: Any, *, label: str) -> str:
    value = _string(value, label=label)
    if not _DIGEST_RE.fullmatch(value):
        raise EvidenceError(f"{label} must be a sha256 digest")
    return value


def _version_id(value: Any, *, label: str) -> str:
    value = _string(value, label=label, maximum=1024)
    if value in {"None", "null"} or not _S3_VERSION_RE.fullmatch(value):
        raise EvidenceError(f"{label} must be a usable S3 VersionId")
    return value


def _timestamp(value: Any, *, label: str) -> dt.datetime:
    value = _string(value, label=label, maximum=64)
    if not value.endswith("Z"):
        raise EvidenceError(f"{label} must be UTC with a Z suffix")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EvidenceError(f"{label} is not an RFC3339 timestamp") from exc
    if parsed.microsecond:
        raise EvidenceError(f"{label} must use whole seconds")
    return parsed


def _metadata_timestamp(value: Any, *, label: str) -> dt.datetime:
    value = _string(value, label=label, maximum=64)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"{label} is not an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise EvidenceError(f"{label} must be UTC")
    return parsed


def _validate_shared_generation_ledger_binding(value: Any) -> dict[str, Any]:
    binding = _mapping(value, label="shared generation ledger binding")
    if not binding:
        return {}
    _exact_keys(
        binding,
        {
            "table_arn",
            "generation",
            "high_water_t0",
            "stage",
        },
        label="shared generation ledger binding",
    )
    table_arn = _string(
        binding["table_arn"],
        label="shared generation ledger table ARN",
        maximum=512,
    )
    if not _DYNAMODB_TABLE_ARN_RE.fullmatch(table_arn):
        raise EvidenceError("shared generation ledger table is outside the fixed account/region")
    generation = binding["generation"]
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise EvidenceError("shared generation ledger generation must be a nonnegative integer")
    high_water_t0 = (
        _timestamp(
            binding["high_water_t0"],
            label="shared generation ledger high-water T0",
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    stage = _string(
        binding["stage"],
        label="shared generation ledger stage",
        maximum=64,
    )
    if not _LEDGER_STAGE_RE.fullmatch(stage):
        raise EvidenceError("shared generation ledger stage is not canonical")
    return {
        "table_arn": table_arn,
        "generation": generation,
        "high_water_t0": high_water_t0,
        "stage": stage,
    }


def _bool(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise EvidenceError(f"{label} must be a boolean")
    return value


def _zero(value: Any, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value != 0:
        raise EvidenceError(f"{label} must be exactly zero")
    return value


def _reported_count(value: Any, *, label: str) -> int:
    """A recorded finding count: any non-negative integer.

    Used for the severities the release gate does not block on. Whether those
    findings are permitted at all is decided upstream by the contract-declared
    gate in actual_image_evidence, which refuses to emit a subject when the gate
    is exceeded. A pipeline whose contract declares no gate is still held to
    all-severities-zero there, so its receipts can never carry a non-zero count
    and this stays as strict as before for them. Here the value only has to be a
    truthful, well-formed count.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EvidenceError(f"{label} must be a non-negative integer")
    return value


def _json_object_argument(value: str, *, label: str) -> Mapping[str, Any]:
    try:
        parsed = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"{label} is not valid JSON") from exc
    return _mapping(parsed, label=label)


def release_receipt_schema_for_pipeline(pipeline: str) -> int:
    """Return the receipt schema without changing non-MCP release contracts."""

    pipeline = _string(pipeline, label="release receipt pipeline")
    if pipeline not in PIPELINES:
        raise EvidenceError("release receipt pipeline is not allowlisted")
    if pipeline == "mcp":
        return RELEASE_RECEIPT_SCHEMA
    return LEGACY_NON_MCP_RELEASE_RECEIPT_SCHEMA


def _validate_approval_locator_pair(
    value: Any,
    *,
    pipeline: str,
    expected_commit: str,
    label: str,
) -> dict[str, dict[str, str]]:
    locator = _mapping(value, label=label)
    _exact_keys(locator, {"payload", "signature"}, label=label)
    payload = _mapping(locator["payload"], label=f"{label}.payload")
    signature = _mapping(locator["signature"], label=f"{label}.signature")
    locator_keys = {"bucket", "key", "version_id", "sha256"}
    _exact_keys(payload, locator_keys, label=f"{label}.payload")
    _exact_keys(signature, locator_keys, label=f"{label}.signature")

    if pipeline not in APPROVAL_PIPELINES:
        raise EvidenceError("approval pipeline is not allowlisted")
    commit = _sha1(expected_commit, label="approval expected commit")
    payload_sha256 = _sha256(
        payload["sha256"],
        label=f"{label}.payload.sha256",
    )
    signature_sha256 = _sha256(
        signature["sha256"],
        label=f"{label}.signature.sha256",
    )
    expected_payload_key = f"{APPROVAL_EVIDENCE_PREFIX}/{pipeline}/{commit}/{payload_sha256}.json"
    if payload["bucket"] != EVIDENCE_BUCKET or signature["bucket"] != EVIDENCE_BUCKET:
        raise EvidenceError(f"{label} bucket is not fixed")
    payload_key = _string(payload["key"], label=f"{label}.payload.key")
    signature_key = _string(signature["key"], label=f"{label}.signature.key")
    if (
        payload_key != expected_payload_key
        or signature_key != f"{expected_payload_key}.sig"
        or ".." in payload_key
        or payload_key.startswith("/")
    ):
        raise EvidenceError(f"{label} key is not content-addressed")
    payload_version_id = _version_id(
        payload["version_id"],
        label=f"{label}.payload.version_id",
    )
    signature_version_id = _version_id(
        signature["version_id"],
        label=f"{label}.signature.version_id",
    )
    return {
        "payload": {
            "bucket": EVIDENCE_BUCKET,
            "key": payload_key,
            "version_id": payload_version_id,
            "sha256": payload_sha256,
        },
        "signature": {
            "bucket": EVIDENCE_BUCKET,
            "key": signature_key,
            "version_id": signature_version_id,
            "sha256": signature_sha256,
        },
    }


def validate_approval_evidence(
    value: Any,
    *,
    pipeline: str,
    expected_commit: str,
) -> dict[str, Any]:
    """Validate the signed approval locator and its two propagated hashes."""

    evidence = _mapping(value, label="approval evidence")
    _exact_keys(
        evidence,
        {
            "payload",
            "signature",
            "approval_payload_sha256",
            "forced_gate_sha256",
        },
        label="approval evidence",
    )
    locator = _validate_approval_locator_pair(
        {
            "payload": evidence["payload"],
            "signature": evidence["signature"],
        },
        pipeline=pipeline,
        expected_commit=expected_commit,
        label="approval evidence",
    )
    approval_payload_sha256 = _sha256(
        evidence["approval_payload_sha256"],
        label="approval evidence payload SHA-256",
    )
    if approval_payload_sha256 != locator["payload"]["sha256"]:
        raise EvidenceError("approval evidence payload SHA-256 is inconsistent")
    forced_gate_sha256 = _sha256(
        evidence["forced_gate_sha256"],
        label="approval evidence forced gate SHA-256",
    )
    return {
        **locator,
        "approval_payload_sha256": approval_payload_sha256,
        "forced_gate_sha256": forced_gate_sha256,
    }


def validate_source_declaration(
    value: Any,
    *,
    expected_commit: str | None = None,
    expected_source_version: str | None = None,
    expected_app_version: str | None = None,
    expected_app_sha256: str | None = None,
    expected_vault_manifest_sha256: str | None = None,
    expected_build_inputs_sha256: str | None = None,
    expected_contract_sha256: str | None = None,
    expected_build_context_sha256: str | None = None,
    expected_build_context_version: str | None = None,
    expected_remote_base_oid: str | None = None,
) -> dict[str, Any]:
    declaration = _mapping(value, label="source declaration")
    _exact_keys(
        declaration,
        {
            "schema_version",
            "kind",
            "publisher",
            "remote",
            "source",
            "build_context",
            "app_html",
            "application_provenance",
            "contract",
            "approval_evidence",
        },
        label="source declaration",
    )
    if declaration["schema_version"] != SOURCE_DECLARATION_SCHEMA:
        raise EvidenceError("unsupported source declaration schema")
    if declaration["kind"] != SOURCE_DECLARATION_KIND:
        raise EvidenceError("source declaration kind mismatch")

    publisher = _mapping(declaration["publisher"], label="source publisher")
    _exact_keys(
        publisher,
        {"project_arn", "build_id", "repository", "branch", "commit"},
        label="source publisher",
    )
    expected_project = (
        f"arn:aws:codebuild:{REGION}:{ACCOUNT_ID}:project/teamagent-dev-mcp-source-publisher"
    )
    if publisher["project_arn"] != expected_project:
        raise EvidenceError("source publisher project is not allowlisted")
    if not _BUILD_ID_RE.fullmatch(_string(publisher["build_id"], label="publisher build ID")):
        raise EvidenceError("publisher build ID is invalid")
    if publisher["repository"] != SOURCE_REPOSITORY or publisher["branch"] != SOURCE_BRANCH:
        raise EvidenceError("source repository or branch is not allowlisted")
    commit = _sha1(publisher["commit"], label="source commit")

    remote = _mapping(declaration["remote"], label="protected remote identity")
    _exact_keys(
        remote,
        {
            "repository",
            "head_ref",
            "head_oid",
            "base_ref",
            "base_oid",
            "merge_base_oid",
        },
        label="protected remote identity",
    )
    if (
        remote["repository"] != SOURCE_REPOSITORY
        or remote["head_ref"] != "refs/heads/dev"
        or remote["base_ref"] != "refs/heads/main"
    ):
        raise EvidenceError("protected remote repository or refs are not allowlisted")
    remote_head_oid = _sha1(remote["head_oid"], label="protected remote head OID")
    remote_base_oid = _sha1(remote["base_oid"], label="protected remote base OID")
    merge_base_oid = _sha1(remote["merge_base_oid"], label="reviewed merge-base OID")
    if remote_head_oid != commit:
        raise EvidenceError("protected remote head does not bind the source commit")
    if merge_base_oid != remote_base_oid:
        raise EvidenceError("protected base is not the reviewed merge-base")

    source = _mapping(declaration["source"], label="source archive")
    _exact_keys(
        source,
        {"bucket", "key", "version_id", "sha256", "manifest_sha256"},
        label="source archive",
    )
    if source["bucket"] != SOURCE_BUCKET or source["key"] != SOURCE_KEY:
        raise EvidenceError("source archive location is not allowlisted")
    source_version = _version_id(source["version_id"], label="source archive VersionId")
    _sha256(source["sha256"], label="source archive SHA-256")
    _sha256(source["manifest_sha256"], label="embedded source manifest SHA-256")

    build_context = _mapping(declaration["build_context"], label="build context")
    _exact_keys(
        build_context,
        {
            "bucket",
            "key",
            "version_id",
            "canonical_tar_sha256",
            "source_tree_oid",
            "normalization",
        },
        label="build context",
    )
    if build_context["bucket"] != EVIDENCE_BUCKET:
        raise EvidenceError("canonical build context bucket is not allowlisted")
    build_context_sha256 = _sha256(
        build_context["canonical_tar_sha256"],
        label="canonical build context SHA-256",
    )
    expected_context_key = (
        f"source-contexts/mcp/{commit}/{build_context_sha256}/{publisher['build_id']}.tar"
    )
    if build_context["key"] != expected_context_key:
        raise EvidenceError("canonical build context key does not bind the publisher")
    build_context_version = _version_id(
        build_context["version_id"],
        label="canonical build context VersionId",
    )
    _sha1(build_context["source_tree_oid"], label="source tree OID")
    if build_context["normalization"] != "teamagent-canonical-tar-v1":
        raise EvidenceError("build context normalization contract mismatch")

    app_html = _mapping(declaration["app_html"], label="app HTML")
    _exact_keys(
        app_html,
        {"bucket", "key", "version_id", "sha256"},
        label="app HTML",
    )
    if app_html["bucket"] != SOURCE_BUCKET or app_html["key"] != APP_HTML_KEY:
        raise EvidenceError("app HTML location is not allowlisted")
    app_version = _version_id(app_html["version_id"], label="app HTML VersionId")
    app_sha256 = _sha256(app_html["sha256"], label="app HTML SHA-256")

    application_provenance = _mapping(
        declaration["application_provenance"],
        label="application provenance",
    )
    _exact_keys(
        application_provenance,
        {"vault_manifest_sha256", "build_inputs_sha256"},
        label="application provenance",
    )
    vault_manifest_sha256 = _sha256(
        application_provenance["vault_manifest_sha256"],
        label="Vault manifest SHA-256",
    )
    build_inputs_sha256 = _sha256(
        application_provenance["build_inputs_sha256"],
        label="build_inputs SHA-256",
    )

    contract = _mapping(declaration["contract"], label="source contract")
    _exact_keys(contract, {"path", "sha256"}, label="source contract")
    if contract["path"] != PIPELINES["mcp"]["contract_path"]:
        raise EvidenceError("source contract path is not allowlisted")
    contract_sha256 = _sha256(contract["sha256"], label="source contract SHA-256")
    validate_approval_evidence(
        declaration["approval_evidence"],
        pipeline="mcp",
        expected_commit=commit,
    )

    expected_values = (
        (expected_commit, commit, "source commit"),
        (expected_source_version, source_version, "source archive VersionId"),
        (expected_app_version, app_version, "app HTML VersionId"),
        (expected_app_sha256, app_sha256, "app HTML SHA-256"),
        (
            expected_vault_manifest_sha256,
            vault_manifest_sha256,
            "Vault manifest SHA-256",
        ),
        (
            expected_build_inputs_sha256,
            build_inputs_sha256,
            "build_inputs SHA-256",
        ),
        (expected_contract_sha256, contract_sha256, "source contract SHA-256"),
        (
            expected_build_context_sha256,
            build_context_sha256,
            "canonical build context SHA-256",
        ),
        (
            expected_build_context_version,
            build_context_version,
            "canonical build context VersionId",
        ),
        (
            expected_remote_base_oid,
            remote_base_oid,
            "protected remote base OID",
        ),
    )
    for expected, actual, label in expected_values:
        if expected is not None and expected != actual:
            raise EvidenceError(f"{label} mismatch")
    return dict(declaration)


def source_declaration(
    *,
    project_arn: str,
    build_id: str,
    commit: str,
    source_version: str,
    source_sha256: str,
    manifest_sha256: str,
    build_context_key: str,
    build_context_version: str,
    build_context_sha256: str,
    source_tree_oid: str,
    remote_head_oid: str,
    remote_base_oid: str,
    merge_base_oid: str,
    app_version: str,
    app_sha256: str,
    vault_manifest_sha256: str,
    build_inputs_sha256: str,
    contract_sha256: str,
    approval_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_approval_evidence = validate_approval_evidence(
        approval_evidence,
        pipeline="mcp",
        expected_commit=commit,
    )
    value = {
        "schema_version": SOURCE_DECLARATION_SCHEMA,
        "kind": SOURCE_DECLARATION_KIND,
        "publisher": {
            "project_arn": project_arn,
            "build_id": build_id,
            "repository": SOURCE_REPOSITORY,
            "branch": SOURCE_BRANCH,
            "commit": commit,
        },
        "remote": {
            "repository": SOURCE_REPOSITORY,
            "head_ref": "refs/heads/dev",
            "head_oid": remote_head_oid,
            "base_ref": "refs/heads/main",
            "base_oid": remote_base_oid,
            "merge_base_oid": merge_base_oid,
        },
        "source": {
            "bucket": SOURCE_BUCKET,
            "key": SOURCE_KEY,
            "version_id": source_version,
            "sha256": source_sha256,
            "manifest_sha256": manifest_sha256,
        },
        "build_context": {
            "bucket": EVIDENCE_BUCKET,
            "key": build_context_key,
            "version_id": build_context_version,
            "canonical_tar_sha256": build_context_sha256,
            "source_tree_oid": source_tree_oid,
            "normalization": "teamagent-canonical-tar-v1",
        },
        "app_html": {
            "bucket": SOURCE_BUCKET,
            "key": APP_HTML_KEY,
            "version_id": app_version,
            "sha256": app_sha256,
        },
        "application_provenance": {
            "vault_manifest_sha256": vault_manifest_sha256,
            "build_inputs_sha256": build_inputs_sha256,
        },
        "contract": {
            "path": PIPELINES["mcp"]["contract_path"],
            "sha256": contract_sha256,
        },
        "approval_evidence": normalized_approval_evidence,
    }
    validate_source_declaration(value)
    return value


def verify_source_approval_binding(
    value: Any,
    *,
    expected_commit: str,
    expected_contract_sha256: str,
    expected_approval_evidence: Mapping[str, Any],
) -> str:
    """Validate a declaration and return its approval-bound source tree OID."""

    declaration = validate_source_declaration(
        value,
        expected_commit=expected_commit,
        expected_contract_sha256=expected_contract_sha256,
    )
    expected = validate_approval_evidence(
        expected_approval_evidence,
        pipeline="mcp",
        expected_commit=expected_commit,
    )
    actual = validate_approval_evidence(
        declaration["approval_evidence"],
        pipeline="mcp",
        expected_commit=expected_commit,
    )
    if actual != expected:
        raise EvidenceError("source declaration approval evidence mismatch")
    return _sha1(
        declaration["build_context"]["source_tree_oid"],
        label="source declaration tree OID",
    )


def subject_tag_suffix(pipeline: str, subject: str, subject_count: int) -> str:
    pipeline_contract = PIPELINES[pipeline]
    if subject_count > 1 or pipeline_contract.get("single_subject_tag_suffix") is True:
        return f"-{subject}"
    return ""


def _expected_tag(
    channel: str,
    pipeline: str,
    commit: str,
    subject: str,
    subject_count: int,
) -> str:
    suffix = subject_tag_suffix(pipeline, subject, subject_count)
    prefix = {
        "verified-candidate": "verified",
        "active": "active",
        "rollback": "rollback",
    }.get(channel)
    if prefix is None:
        raise EvidenceError("receipt channel is not allowlisted")
    return f"{prefix}-{commit}{suffix}"


def _validate_signature(
    value: Any,
    *,
    label: str,
    expected_subject_digest: str,
) -> dict[str, Any]:
    signature = _mapping(value, label=label)
    _exact_keys(
        signature,
        {
            "verified",
            "key_arn",
            "subject_digest",
            "referrer_digest",
            "bundle_sha256",
        },
        label=label,
    )
    if not _bool(signature["verified"], label=f"{label}.verified"):
        raise EvidenceError(f"{label} must be cryptographically verified")
    key_arn = _string(signature["key_arn"], label=f"{label}.key_arn")
    if not _KEY_ARN_RE.fullmatch(key_arn):
        raise EvidenceError(f"{label}.key_arn is not an allowlisted account key")
    if _digest(signature["subject_digest"], label=f"{label}.subject_digest") != (
        expected_subject_digest
    ):
        raise EvidenceError(f"{label} does not bind the expected subject digest")
    referrer_digest = _digest(
        signature["referrer_digest"],
        label=f"{label}.referrer_digest",
    )
    if referrer_digest == expected_subject_digest:
        raise EvidenceError(f"{label} referrer digest must differ from its subject")
    _sha256(signature["bundle_sha256"], label=f"{label}.bundle_sha256")
    return dict(signature)


def _validate_referrer(
    value: Any,
    *,
    label: str,
    kind: str,
    subject_digest: str,
) -> dict[str, Any]:
    referrer = _mapping(value, label=label)
    _exact_keys(
        referrer,
        {"digest", "artifact_type", "payload_sha256", "signature"},
        label=label,
    )
    digest = _digest(referrer["digest"], label=f"{label}.digest")
    if referrer["artifact_type"] != REFERRER_ARTIFACT_TYPES[kind]:
        raise EvidenceError(f"{label}.artifact_type mismatch")
    _sha256(referrer["payload_sha256"], label=f"{label}.payload_sha256")
    signature = _validate_signature(
        referrer["signature"],
        label=f"{label}.signature",
        expected_subject_digest=digest,
    )
    if signature["subject_digest"] == subject_digest:
        raise EvidenceError(f"{label} signature must bind the referrer, not only the image")
    return dict(referrer)


def validate_release_receipt(
    value: Any,
    *,
    expected_pipeline: str | None = None,
    expected_commit: str | None = None,
    expected_contract_sha256: str | None = None,
    allowed_channels: set[str] | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    receipt = _mapping(value, label="release receipt")
    pipeline = _string(receipt.get("pipeline"), label="pipeline")
    if pipeline not in PIPELINES:
        raise EvidenceError("release receipt pipeline is not allowlisted")
    expected_top_level_keys = {
        "schema_version",
        "kind",
        "pipeline",
        "channel",
        "issued_at",
        "expires_at",
        "build",
        "contract",
        "source_evidence",
        "subjects",
    }
    if pipeline == "mcp":
        expected_top_level_keys.add("approval_evidence")
    _exact_keys(
        receipt,
        expected_top_level_keys,
        label="release receipt",
    )
    if receipt["schema_version"] != release_receipt_schema_for_pipeline(pipeline):
        raise EvidenceError("unsupported release receipt schema")
    if receipt["kind"] != RELEASE_RECEIPT_KIND:
        raise EvidenceError("release receipt kind mismatch")
    if expected_pipeline is not None and pipeline != expected_pipeline:
        raise EvidenceError("release receipt pipeline mismatch")
    channel = _string(receipt["channel"], label="release channel")
    if channel not in {"verified-candidate", "active", "rollback"}:
        raise EvidenceError("release receipt channel is not allowlisted")
    if allowed_channels is not None and channel not in allowed_channels:
        raise EvidenceError("release receipt channel is not valid for this operation")

    issued_at = _timestamp(receipt["issued_at"], label="issued_at")
    expires_at = _timestamp(receipt["expires_at"], label="expires_at")
    lifetime = (expires_at - issued_at).total_seconds()
    maximum_lifetime = (
        MAX_CANDIDATE_RECEIPT_LIFETIME_SECONDS
        if channel == "verified-candidate"
        else MAX_RELEASE_RECEIPT_LIFETIME_SECONDS
    )
    if lifetime <= 0 or lifetime > maximum_lifetime:
        raise EvidenceError("release receipt validity window is invalid")
    current = now or dt.datetime.now(dt.UTC).replace(microsecond=0)
    if current < issued_at - dt.timedelta(minutes=5):
        raise EvidenceError("release receipt is not valid yet")
    if current >= expires_at:
        raise EvidenceError("release receipt is stale")

    pipeline_contract = PIPELINES[pipeline]
    build = _mapping(receipt["build"], label="release build")
    _exact_keys(
        build,
        {"project_arn", "build_id", "source_commit"},
        label="release build",
    )
    expected_project_arn = (
        f"arn:aws:codebuild:{REGION}:{ACCOUNT_ID}:project/{pipeline_contract['build_project']}"
    )
    if build["project_arn"] != expected_project_arn:
        raise EvidenceError("release build project is not allowlisted")
    if not _BUILD_ID_RE.fullmatch(_string(build["build_id"], label="release build ID")):
        raise EvidenceError("release build ID is invalid")
    commit = _sha1(build["source_commit"], label="release source commit")
    if expected_commit is not None and commit != expected_commit:
        raise EvidenceError("release source commit mismatch")
    normalized_approval_evidence: dict[str, Any] | None = None
    if pipeline == "mcp":
        normalized_approval_evidence = validate_approval_evidence(
            receipt["approval_evidence"],
            pipeline=pipeline,
            expected_commit=commit,
        )

    contract = _mapping(receipt["contract"], label="release contract")
    _exact_keys(contract, {"path", "sha256", "release_ready"}, label="release contract")
    if contract["path"] != pipeline_contract["contract_path"]:
        raise EvidenceError("release contract path is not allowlisted")
    contract_sha256 = _sha256(contract["sha256"], label="release contract SHA-256")
    if expected_contract_sha256 is not None and contract_sha256 != expected_contract_sha256:
        raise EvidenceError("release contract SHA-256 mismatch")
    if not _bool(contract["release_ready"], label="release contract ready"):
        raise EvidenceError("release contract is not ready")

    source_evidence = _mapping(receipt["source_evidence"], label="source evidence")
    _exact_keys(
        source_evidence,
        {
            "bucket",
            "key",
            "version_id",
            "sha256",
            "signature_key",
            "signature_version_id",
        },
        label="source evidence",
    )
    expected_source_bucket = (
        "teamagent-dev-openclaw-build-evidence" if pipeline == "openclaw" else EVIDENCE_BUCKET
    )
    if source_evidence["bucket"] != expected_source_bucket:
        raise EvidenceError("source evidence bucket is not allowlisted")
    key = _string(source_evidence["key"], label="source evidence key")
    allowed_prefix = {
        "mcp": f"source-declarations/mcp/{commit}/",
        "tiktok": f"source-manifests/tiktok/{commit}/",
        "openclaw": f"source-manifests/{commit}/",
    }[pipeline]
    if not key.startswith(allowed_prefix) or ".." in key or key.startswith("/"):
        raise EvidenceError("source evidence key is not allowlisted")
    _version_id(source_evidence["version_id"], label="source evidence VersionId")
    _sha256(source_evidence["sha256"], label="source evidence SHA-256")
    if source_evidence["signature_key"] != f"{key}.sig":
        raise EvidenceError("source evidence signature key mismatch")
    _version_id(
        source_evidence["signature_version_id"],
        label="source evidence signature VersionId",
    )

    subjects = receipt["subjects"]
    expected_subjects: Mapping[str, tuple[str, str, str]] = pipeline_contract["subjects"]
    if not isinstance(subjects, list) or len(subjects) != len(expected_subjects):
        raise EvidenceError("release receipt subject count mismatch")
    normalized_subjects: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    mcp_app_bindings: set[str] = set()
    mcp_context_bindings: set[str] = set()
    for index, raw_subject in enumerate(subjects):
        label = f"release subject[{index}]"
        subject = _mapping(raw_subject, label=label)
        _exact_keys(
            subject,
            {
                "name",
                "quarantine_repository",
                "candidate_repository",
                "release_repository",
                "candidate_tag",
                "release_tag",
                "digest",
                "media_type",
                "config_digest",
                "platform",
                "labels",
                "binaries",
                "scan",
                "sbom",
                "provenance",
                "image_signature",
            },
            label=label,
        )
        name = _string(subject["name"], label=f"{label}.name")
        if name in seen_names or name not in expected_subjects:
            raise EvidenceError(f"{label}.name is duplicate or not allowlisted")
        seen_names.add(name)
        (
            quarantine_repository,
            candidate_repository,
            release_repository,
        ) = expected_subjects[name]
        if (
            subject["quarantine_repository"] != quarantine_repository
            or subject["candidate_repository"] != candidate_repository
            or subject["release_repository"] != release_repository
        ):
            raise EvidenceError(f"{label} repositories do not match the allowlist")
        tag_suffix = subject_tag_suffix(pipeline, name, len(expected_subjects))
        candidate_tag = commit if pipeline == "tiktok" else f"candidate-{commit}{tag_suffix}"
        if subject["candidate_tag"] != candidate_tag:
            raise EvidenceError(f"{label}.candidate_tag must use the full source commit")
        expected_tag = _expected_tag(
            channel,
            pipeline,
            commit,
            name,
            len(expected_subjects),
        )
        if subject["release_tag"] != expected_tag:
            raise EvidenceError(f"{label}.release_tag is not canonical")
        digest = _digest(subject["digest"], label=f"{label}.digest")
        if subject["media_type"] not in SINGLE_ARM64_MEDIA_TYPES:
            raise EvidenceError(f"{label} must be a single scan-capable image manifest")
        _digest(subject["config_digest"], label=f"{label}.config_digest")
        platform = _mapping(subject["platform"], label=f"{label}.platform")
        _exact_keys(platform, {"os", "architecture"}, label=f"{label}.platform")
        if platform != {"os": "linux", "architecture": "arm64"}:
            raise EvidenceError(f"{label} must be linux/arm64")

        labels = _mapping(subject["labels"], label=f"{label}.labels")
        if not labels:
            raise EvidenceError(f"{label}.labels must not be empty")
        for label_name, label_value in labels.items():
            if not isinstance(label_name, str) or not _LABEL_RE.fullmatch(label_name):
                raise EvidenceError(f"{label}.labels contains an invalid OCI label")
            # 非空・プレースホルダ禁止は自前契約ラベルに限定する。digest固定の上流
            # ベース(chainguard python が dev.chainguard.package.main='' を同梱)の
            # 継承ラベルまで対象にすると原理的に通らない検査になる（実測）。
            if label_name.startswith("io.teamagent."):
                normalized_label_value = _string(
                    label_value,
                    label=f"{label}.labels[{label_name}]",
                    maximum=8192,
                )
                if normalized_label_value.strip().lower() in _UNTRUSTED_LABEL_VALUES:
                    raise EvidenceError(
                        f"{label}.labels[{label_name}] uses an untrusted placeholder"
                    )
            else:
                if not isinstance(label_value, str) or len(label_value) > 8192:
                    raise EvidenceError(
                        f"{label}.labels[{label_name}] must be a string within 8192 chars"
                    )
        if labels.get("org.opencontainers.image.revision") != commit:
            raise EvidenceError(f"{label} OCI revision does not match the full commit")
        contract_label = pipeline_contract["contract_label"]
        if labels.get(contract_label) != contract_sha256:
            raise EvidenceError(f"{label} OCI contract hash does not match the receipt")
        if pipeline == "mcp":
            assert normalized_approval_evidence is not None
            expected_runtime_kind = {
                "core": "core",
                "media": "media-worker",
            }[name]
            if (
                labels.get("org.opencontainers.image.ref.name") != "dev"
                or labels.get("io.teamagent.runtime.kind") != expected_runtime_kind
            ):
                raise EvidenceError(f"{label} MCP runtime identity labels mismatch")
            app_binding = _sha256(
                labels.get("io.teamagent.build.app-provenance-sha256"),
                label=f"{label} application provenance binding",
            )
            mcp_app_bindings.add(app_binding)
            context_binding = _sha256(
                labels.get("io.teamagent.build.context-sha256"),
                label=f"{label} canonical build context binding",
            )
            mcp_context_bindings.add(context_binding)
            if (
                labels.get("io.teamagent.build.release-approval-sha256")
                != normalized_approval_evidence["approval_payload_sha256"]
            ):
                raise EvidenceError(f"{label} release approval label does not match the receipt")
            if name == "core":
                _version_id(
                    labels.get("io.teamagent.contract.baked-app-html-version-id"),
                    label=f"{label} baked app HTML VersionId",
                )
                if labels.get("io.teamagent.contract.app-html-source") != "s3":
                    raise EvidenceError(f"{label} app HTML source must be s3")
                _version_id(
                    labels.get("io.teamagent.contract.app-html-version-id"),
                    label=f"{label} app HTML VersionId",
                )
                for label_name in (
                    "io.teamagent.contract.app-html-sha256",
                    "io.teamagent.contract.app-html-manifest-sha256",
                    "io.teamagent.contract.app-html-build-inputs-sha256",
                    "io.teamagent.contract.baked-app-html-sha256",
                ):
                    _sha256(
                        labels.get(label_name),
                        label=f"{label} {label_name}",
                    )

        binaries = subject["binaries"]
        if not isinstance(binaries, list) or not binaries:
            raise EvidenceError(f"{label}.binaries must contain actual-image probes")
        seen_paths: set[str] = set()
        for binary_index, raw_binary in enumerate(binaries):
            binary_label = f"{label}.binaries[{binary_index}]"
            binary = _mapping(raw_binary, label=binary_label)
            _exact_keys(binary, {"path", "sha256"}, label=binary_label)
            path = _string(binary["path"], label=f"{binary_label}.path", maximum=512)
            if not _PATH_RE.fullmatch(path) or ".." in path or path in seen_paths:
                raise EvidenceError(f"{binary_label}.path is unsafe or duplicate")
            seen_paths.add(path)
            _sha256(binary["sha256"], label=f"{binary_label}.sha256")

        scan = _mapping(subject["scan"], label=f"{label}.scan")
        _exact_keys(
            scan,
            {
                "scanner",
                "actual_image",
                "unknown",
                "low",
                "medium",
                "critical",
                "high",
                "secrets",
                "report_sha256",
            },
            label=f"{label}.scan",
        )
        if scan["scanner"] != "trivy":
            raise EvidenceError(f"{label}.scan must use Trivy")
        expected_image = f"{REGISTRY}/{quarantine_repository}@{digest}"
        if scan["actual_image"] != expected_image:
            raise EvidenceError(f"{label}.scan does not bind the quarantine digest")
        _reported_count(scan["unknown"], label=f"{label}.scan.unknown")
        _reported_count(scan["low"], label=f"{label}.scan.low")
        _reported_count(scan["medium"], label=f"{label}.scan.medium")
        _zero(scan["critical"], label=f"{label}.scan.critical")
        _zero(scan["high"], label=f"{label}.scan.high")
        _zero(scan["secrets"], label=f"{label}.scan.secrets")
        _sha256(scan["report_sha256"], label=f"{label}.scan.report_sha256")

        _validate_referrer(
            subject["sbom"],
            label=f"{label}.sbom",
            kind="sbom",
            subject_digest=digest,
        )
        _validate_referrer(
            subject["provenance"],
            label=f"{label}.provenance",
            kind="provenance",
            subject_digest=digest,
        )
        _validate_signature(
            subject["image_signature"],
            label=f"{label}.image_signature",
            expected_subject_digest=digest,
        )
        normalized_subjects.append(dict(subject))

    if pipeline == "mcp" and len(mcp_app_bindings) != 1:
        raise EvidenceError(
            "MCP core and media subjects must bind one application provenance digest"
        )
    if pipeline == "mcp" and len(mcp_context_bindings) != 1:
        raise EvidenceError(
            "MCP core and media subjects must bind one canonical build context digest"
        )

    if seen_names != set(expected_subjects):
        raise EvidenceError("release receipt is missing an allowlisted subject")
    if [subject["name"] for subject in normalized_subjects] != sorted(seen_names):
        raise EvidenceError("release receipt subjects must be sorted by name")
    return dict(receipt)


def release_receipt(
    *,
    pipeline: str,
    channel: str,
    issued_at: str,
    expires_at: str,
    build_project_arn: str,
    build_id: str,
    commit: str,
    contract_path: str,
    contract_sha256: str,
    source_bucket: str,
    source_key: str,
    source_version: str,
    source_sha256: str,
    source_signature_key: str,
    source_signature_version: str,
    subjects: list[Mapping[str, Any]],
    approval_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if pipeline == "mcp":
        if approval_evidence is None:
            raise EvidenceError("MCP release receipt approval evidence is required")
        normalized_approval_evidence = validate_approval_evidence(
            approval_evidence,
            pipeline=pipeline,
            expected_commit=commit,
        )
    else:
        if approval_evidence is not None:
            raise EvidenceError("legacy non-MCP release receipt forbids approval evidence")
        normalized_approval_evidence = None
    value = {
        "schema_version": release_receipt_schema_for_pipeline(pipeline),
        "kind": RELEASE_RECEIPT_KIND,
        "pipeline": pipeline,
        "channel": channel,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "build": {
            "project_arn": build_project_arn,
            "build_id": build_id,
            "source_commit": commit,
        },
        "contract": {
            "path": contract_path,
            "sha256": contract_sha256,
            "release_ready": True,
        },
        "source_evidence": {
            "bucket": source_bucket,
            "key": source_key,
            "version_id": source_version,
            "sha256": source_sha256,
            "signature_key": source_signature_key,
            "signature_version_id": source_signature_version,
        },
        "subjects": [dict(subject) for subject in subjects],
    }
    if normalized_approval_evidence is not None:
        value["approval_evidence"] = normalized_approval_evidence
    validate_release_receipt(
        value,
        expected_pipeline=pipeline,
        expected_commit=commit,
        expected_contract_sha256=contract_sha256,
        allowed_channels={channel},
        now=_timestamp(issued_at, label="issued_at"),
    )
    return value


def authorize_release_receipt(
    locator: Mapping[str, Any],
    *,
    channel: str,
    issued_at: str,
    expires_at: str,
) -> dict[str, Any]:
    """Create fresh active/rollback evidence from a signed verified candidate.

    The caller must cryptographically verify the locator before invoking this
    function. The locator must still be within its signed candidate window at
    the new authorization time; the new receipt receives a separate short
    active/rollback window.
    """

    if channel not in {"active", "rollback"}:
        raise EvidenceError("authorization channel must be active or rollback")
    authorization_time = _timestamp(issued_at, label="issued_at")
    validated = validate_release_receipt(
        locator,
        allowed_channels={"verified-candidate"},
        now=authorization_time,
    )
    value = copy.deepcopy(validated)
    value["channel"] = channel
    value["issued_at"] = issued_at
    value["expires_at"] = expires_at
    commit = value["build"]["source_commit"]
    subject_count = len(value["subjects"])
    for subject in value["subjects"]:
        subject["release_tag"] = _expected_tag(
            channel,
            value["pipeline"],
            commit,
            subject["name"],
            subject_count,
        )
    validate_release_receipt(
        value,
        expected_pipeline=value["pipeline"],
        expected_commit=commit,
        expected_contract_sha256=value["contract"]["sha256"],
        allowed_channels={channel},
        now=_timestamp(issued_at, label="issued_at"),
    )
    return value


def _release_coordinates_for_consumer(consumer_id: str) -> dict[str, str]:
    """Resolve deployment coordinates only through the code-owned registry."""

    try:
        registry_module = __import__("image_deployment_consumers")
        coordinates = registry_module.release_coordinates_for_consumer(consumer_id)
    except (AttributeError, ImportError, OSError, ValueError) as exc:
        raise EvidenceError("code-owned image consumer registry is invalid") from exc
    if (
        not isinstance(coordinates, dict)
        or set(coordinates) != {"pipeline", "subject", "repository"}
        or any(not isinstance(value, str) or not value for value in coordinates.values())
    ):
        raise EvidenceError("code-owned image consumer coordinates are invalid")
    return dict(coordinates)


def _validate_consumer_deploy_reference(
    validated_receipt: Mapping[str, Any],
    *,
    consumer_id: str,
    image: str,
) -> None:
    coordinates = _release_coordinates_for_consumer(consumer_id)
    expected_subject = coordinates["subject"]
    expected_repository = coordinates["repository"]
    image_match = re.fullmatch(
        rf"{re.escape(REGISTRY)}/{re.escape(expected_repository)}@"
        r"(sha256:[0-9a-f]{64})",
        _string(image, label=f"{consumer_id} deployment image"),
    )
    if image_match is None:
        raise EvidenceError(
            f"{consumer_id} image repository does not match the code-owned registry"
        )
    matches = [
        subject
        for subject in validated_receipt["subjects"]
        if (
            subject["name"] == expected_subject
            and subject["release_repository"] == expected_repository
            and subject["digest"] == image_match.group(1)
        )
    ]
    if len(matches) != 1:
        raise EvidenceError(
            f"{consumer_id} image does not match its registry-fixed receipt subject"
        )


def validate_deploy_reference(
    receipt: Mapping[str, Any],
    *,
    consumer_id: str,
    image: str,
    contract_sha256: str,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    coordinates = _release_coordinates_for_consumer(consumer_id)
    validated = validate_release_receipt(
        receipt,
        expected_pipeline=coordinates["pipeline"],
        expected_contract_sha256=contract_sha256,
        allowed_channels={"active", "rollback"},
        now=now,
    )
    _validate_consumer_deploy_reference(
        validated,
        consumer_id=consumer_id,
        image=image,
    )
    return validated


def _validate_mcp_deployment_application(
    receipt: Mapping[str, Any] | None,
    value: Any,
) -> str:
    application = _mapping(value, label="MCP deployment application provenance")
    _exact_keys(
        application,
        {
            "bucket",
            "key",
            "version_id",
            "sha256",
            "vault_manifest_sha256",
            "build_inputs_sha256",
            "baked_fallback_version_id",
            "baked_fallback_sha256",
        },
        label="MCP deployment application provenance",
    )
    if application["bucket"] != SOURCE_BUCKET or application["key"] != APP_HTML_KEY:
        raise EvidenceError("MCP deployment application S3 location is not fixed")
    version_id = _version_id(
        application["version_id"],
        label="MCP deployment app VersionId",
    )
    sha256 = _sha256(application["sha256"], label="MCP deployment app SHA-256")
    vault_manifest_sha256 = _sha256(
        application["vault_manifest_sha256"],
        label="MCP deployment Vault manifest SHA-256",
    )
    build_inputs_sha256 = _sha256(
        application["build_inputs_sha256"],
        label="MCP deployment build_inputs SHA-256",
    )
    fallback_version_id = _version_id(
        application["baked_fallback_version_id"],
        label="MCP baked fallback VersionId",
    )
    fallback_sha256 = _sha256(
        application["baked_fallback_sha256"],
        label="MCP baked fallback SHA-256",
    )
    payload = {
        "schema_version": 1,
        "app_html": {
            "bucket": SOURCE_BUCKET,
            "key": APP_HTML_KEY,
            "version_id": version_id,
            "sha256": sha256,
        },
        "application_provenance": {
            "vault_manifest_sha256": vault_manifest_sha256,
            "build_inputs_sha256": build_inputs_sha256,
        },
        "baked_fallback": {
            "version_id": fallback_version_id,
            "sha256": fallback_sha256,
        },
    }
    binding = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    if receipt is None:
        return binding
    subject_bindings = {
        subject["labels"].get("io.teamagent.build.app-provenance-sha256")
        for subject in receipt["subjects"]
    }
    if subject_bindings != {binding}:
        raise EvidenceError("MCP release subjects do not bind the requested application provenance")
    core_subjects = [subject for subject in receipt["subjects"] if subject.get("name") == "core"]
    expected_core_labels = {
        "io.teamagent.contract.app-html-source": "s3",
        "io.teamagent.contract.app-html-version-id": version_id,
        "io.teamagent.contract.app-html-sha256": sha256,
        "io.teamagent.contract.app-html-manifest-sha256": vault_manifest_sha256,
        "io.teamagent.contract.app-html-build-inputs-sha256": build_inputs_sha256,
        "io.teamagent.contract.baked-app-html-version-id": fallback_version_id,
        "io.teamagent.contract.baked-app-html-sha256": fallback_sha256,
    }
    if len(core_subjects) != 1 or any(
        core_subjects[0]["labels"].get(label_name) != expected_value
        for label_name, expected_value in expected_core_labels.items()
    ):
        raise EvidenceError("MCP core image does not bind the requested application contract")
    return binding


def validate_lifecycle_preview(
    value: Any,
    *,
    protected_digests: set[str],
) -> None:
    preview = _mapping(value, label="ECR lifecycle preview")
    if preview.get("nextToken") not in {None, ""}:
        raise EvidenceError("ECR lifecycle preview is truncated")
    results = preview.get("lifecyclePolicyPreviewResults")
    if not isinstance(results, list):
        raise EvidenceError("ECR lifecycle preview results are missing")
    normalized_protected = {_digest(item, label="protected digest") for item in protected_digests}
    expiring: set[str] = set()
    for index, raw_result in enumerate(results):
        result = _mapping(raw_result, label=f"lifecycle result[{index}]")
        action = _mapping(result.get("action"), label=f"lifecycle result[{index}].action")
        if action.get("type") != "EXPIRE":
            continue
        expiring.add(_digest(result.get("imageDigest"), label="preview image digest"))
    unsafe = sorted(expiring & normalized_protected)
    if unsafe:
        raise EvidenceError(f"lifecycle preview expires protected release graph: {unsafe}")


def _write_json(path: Path, value: Any) -> None:
    try:
        path.write_bytes(canonical_bytes(value))
    except OSError as exc:
        raise EvidenceError(f"cannot write evidence: {exc}") from exc


def _write_json_exclusive(path: Path, value: Any) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(value))
    except OSError as exc:
        raise EvidenceError(f"cannot exclusively write evidence: {exc}") from exc


def _aws_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
            "AWS_DEFAULT_REGION": REGION,
            "AWS_REGION": REGION,
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_PAGER": "",
        }
    )
    for key in tuple(environment):
        if key.startswith("AWS_ENDPOINT_URL") or key in {"AWS_PROFILE", "AWS_DEFAULT_PROFILE"}:
            environment.pop(key, None)
    return environment


def configure_aws_executable(path: Path) -> None:
    """Pin AWS calls to a caller-validated absolute executable."""

    global AWS_EXECUTABLE
    resolved = path.resolve(strict=True)
    if not resolved.is_absolute() or not resolved.is_file():
        raise EvidenceError("AWS executable must be an absolute regular file")
    AWS_EXECUTABLE = str(resolved)


def _aws(*args: str, output: Path | None = None) -> str:
    command = [AWS_EXECUTABLE, *args]
    if output is not None:
        command.append(str(output))
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            env=_aws_environment(),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EvidenceError("AWS evidence verification failed without exposing details") from exc
    return completed.stdout


def _aws_json(label: str, *args: str) -> Mapping[str, Any]:
    try:
        value = json.loads(_aws(*args), object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"invalid {label} response") from exc
    return _mapping(value, label=label)


def _approval_observation_values(
    runtime_contract_path: Path,
    contract_path: Path,
) -> dict[str, str]:
    try:
        from teamagent_bundle_provenance import (
            ProvenanceError as BundleProvenanceError,
        )
        from teamagent_bundle_provenance import approval_observation_values
    except ImportError as exc:
        raise EvidenceError("cannot derive approval observations from the contracts") from exc
    try:
        return approval_observation_values(runtime_contract_path, contract_path)
    except (OSError, BundleProvenanceError) as exc:
        raise EvidenceError("cannot derive approval observations from the contracts") from exc


def _approval_publisher_build_id(payload: bytes) -> str:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("approval payload is not valid UTF-8 JSON") from exc
    approval = _mapping(value, label="approval payload")
    authority = _mapping(
        approval.get("approval_authority"),
        label="approval payload authority",
    )
    build_id = _string(
        authority.get("publisher_build_id"),
        label="approval publisher build ID",
        maximum=512,
    )
    if not _BUILD_ID_RE.fullmatch(build_id) or not build_id.startswith(
        f"{APPROVAL_PUBLISHER_PROJECT_NAME}:"
    ):
        raise EvidenceError("approval publisher build ID is not from the fixed project")
    return build_id


def _reject_nonfinite_approval_json(value: str) -> None:
    raise EvidenceError(f"non-finite approval JSON number is forbidden: {value}")


def _canonical_approval_payload(payload: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_approval_json,
        )
    except EvidenceError:
        raise
    except (RecursionError, UnicodeDecodeError, ValueError) as exc:
        raise EvidenceError("approval payload is not valid JSON") from exc
    approval = _mapping(value, label="approval payload")
    try:
        canonical = approval_canonical_json_bytes(approval)
    except ApprovalProvenanceError as exc:
        raise EvidenceError("approval payload cannot be canonicalized") from exc
    if payload != canonical:
        raise EvidenceError("approval payload bytes are not canonical")
    return approval


_INITIAL_RELEASE_EXEMPTION_SUNSET = _timestamp(
    INITIAL_RELEASE_EXEMPTION_SUNSET_UTC,
    label="initial release exemption sunset",
)
_INITIAL_RELEASE_EXEMPTION_CAMPAIGN_EXPIRES_AT = _timestamp(
    INITIAL_RELEASE_EXEMPTION_CAMPAIGN_EXPIRES_AT_UTC,
    label="initial release exemption campaign expiry",
)
# The sunset must land strictly before the campaign expiry.  If they were equal
# the campaign expiry would fire first, and deleting the sunset guard would not
# turn any test red -- a guard no mutation can kill is not a guard.
if _INITIAL_RELEASE_EXEMPTION_SUNSET >= _INITIAL_RELEASE_EXEMPTION_CAMPAIGN_EXPIRES_AT:
    raise EvidenceError("initial release exemption sunset must precede the campaign expiry")


def _initial_release_exemption_gate(approval_signing_key_arn: str) -> dict[str, Any]:
    """The one forced-rollback gate object the exemption will accept."""

    return {
        "gate_version": FORCED_ROLLBACK_GATE_VERSION,
        "state": FORCED_ROLLBACK_PROVISIONAL,
        "provisional_campaign": {
            "campaign_id": INITIAL_RELEASE_EXEMPTION_CAMPAIGN_ID,
            "phase": "R1",
            "payload_version_id": _INITIAL_RELEASE_EXEMPTION_UNREFERENCED_VERSION_ID,
            "payload_sha256": _INITIAL_RELEASE_EXEMPTION_UNREFERENCED_SHA256,
            "signature_version_id": _INITIAL_RELEASE_EXEMPTION_UNREFERENCED_VERSION_ID,
            "kms_key_arn": approval_signing_key_arn,
            "expires_at_utc": INITIAL_RELEASE_EXEMPTION_CAMPAIGN_EXPIRES_AT_UTC,
        },
    }


def _observed_forced_rollback_state(payload: bytes) -> str:
    """Read the declared gate state.

    This only selects which expectation to build; validate_approval_payload
    re-checks the same field against that expectation afterwards, so a lie here
    cannot widen anything on its own.
    """

    approval = _canonical_approval_payload(payload)
    gates = _mapping(approval.get("gates"), label="approval payload gates")
    gate = _mapping(
        gates.get("forced_rollback_evidence"),
        label="approval payload forced rollback gate",
    )
    return _string(
        gate.get("state"),
        label="approval payload forced rollback state",
        maximum=31,
    )


def validate_approved_release_for_operation(
    payload: bytes,
    *,
    operation: str,
    approval_signing_key_arn: str,
    expected_commit: str,
    expected_tree_oid: str,
    expected_inner_sha256: str,
    expected_outer_sha256: str,
    expected_pipeline: str,
    expected_environment: str,
    expected_observations: Mapping[str, str],
    now: dt.datetime,
) -> dict[str, Any]:
    """Pure operation policy hook for the future Terraform release gate."""

    state = APPROVAL_OPERATION_STATES.get(operation)
    if state is None:
        raise EvidenceError("approval operation is not allowlisted")
    # The exemption is an overlay, never a replacement: when it does not apply we
    # fall through with the baseline scalar untouched, so the existing
    # "forced rollback state mismatch" rejection downstream still fires.
    exempt_gate: dict[str, Any] | None = None
    exempt_states = INITIAL_RELEASE_EXEMPT_STATES.get(operation, frozenset())
    if exempt_states and now < _INITIAL_RELEASE_EXEMPTION_SUNSET:
        observed = _observed_forced_rollback_state(payload)
        if observed in exempt_states:
            state = observed
            exempt_gate = _initial_release_exemption_gate(approval_signing_key_arn)
    publisher_build_id = _approval_publisher_build_id(payload)
    expected = {
        "commit": expected_commit,
        "tree_oid": expected_tree_oid,
        "inner_sha": expected_inner_sha256,
        "outer_sha": expected_outer_sha256,
        "observations": dict(expected_observations),
        "pipeline": expected_pipeline,
        "environment": expected_environment,
        "approved_by": APPROVAL_APPROVED_BY_ARN,
        "authority": {
            "publisher_project_arn": APPROVAL_PUBLISHER_PROJECT_ARN,
            "publisher_build_id": publisher_build_id,
            "kms_key_arn": approval_signing_key_arn,
        },
        "forced_rollback_state": state,
    }
    if exempt_gate is not None:
        # Pin the whole gate object by bytes.  validate_approval_payload compares
        # canonical JSON, so a single differing character is rejected -- the
        # operator has no freedom over what the exemption accepts.
        expected["forced_rollback_evidence"] = exempt_gate
    try:
        return validate_approval_payload(payload, expected, now=now)
    except ApprovalProvenanceError as exc:
        raise EvidenceError(f"approval payload validation failed: {exc}") from exc


def _download_approval_object(
    locator: Mapping[str, str],
    *,
    destination: Path,
    label: str,
    approval_encryption_key_arn: str,
) -> dt.datetime:
    head = _aws_json(
        f"{label} object metadata",
        "s3api",
        "head-object",
        "--region",
        REGION,
        "--bucket",
        locator["bucket"],
        "--key",
        locator["key"],
        "--version-id",
        locator["version_id"],
        "--expected-bucket-owner",
        ACCOUNT_ID,
        "--output",
        "json",
    )
    if (
        head.get("VersionId") != locator["version_id"]
        or head.get("ObjectLockMode") not in {"COMPLIANCE", "GOVERNANCE"}
        or head.get("ServerSideEncryption") != "aws:kms"
        or head.get("SSEKMSKeyId") != approval_encryption_key_arn
    ):
        raise EvidenceError(f"{label} object is not immutable exact approval evidence")
    retained_until = _metadata_timestamp(
        head.get("ObjectLockRetainUntilDate", ""),
        label=f"{label} retention",
    )
    response_version = _aws(
        "s3api",
        "get-object",
        "--region",
        REGION,
        "--bucket",
        locator["bucket"],
        "--key",
        locator["key"],
        "--version-id",
        locator["version_id"],
        "--expected-bucket-owner",
        ACCOUNT_ID,
        "--query",
        "VersionId",
        "--output",
        "text",
        output=destination,
    ).strip()
    if response_version != locator["version_id"]:
        raise EvidenceError(f"{label} download VersionId mismatch")
    try:
        raw_sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
    except OSError as exc:
        raise EvidenceError(f"cannot read downloaded {label}") from exc
    if raw_sha256 != locator["sha256"]:
        raise EvidenceError(f"{label} raw SHA-256 mismatch")
    return retained_until


def assert_approved_release(
    *,
    operation: str,
    approval_locators: Mapping[str, Any],
    approval_signing_key_arn: str,
    approval_encryption_key_arn: str,
    expected_commit: str,
    expected_tree_oid: str,
    expected_inner_sha256: str,
    expected_outer_sha256: str,
    expected_pipeline: str,
    expected_environment: str,
    runtime_contract_path: Path,
    contract_path: Path,
    now: dt.datetime | None = None,
    verified_record_out: Path | None = None,
) -> dict[str, Any]:
    """Fetch and fully verify one immutable external release approval."""

    current = now or dt.datetime.now(dt.UTC).replace(microsecond=0)
    if current.tzinfo is None or current.utcoffset() != dt.timedelta(0):
        raise EvidenceError("approval verification time must be UTC")
    if current.microsecond:
        current = current.replace(microsecond=0)
    signing_key_arn = _string(
        approval_signing_key_arn,
        label="approval signing key ARN",
    )
    encryption_key_arn = _string(
        approval_encryption_key_arn,
        label="approval encryption key ARN",
    )
    if not _KEY_ARN_RE.fullmatch(signing_key_arn) or not _KEY_ARN_RE.fullmatch(encryption_key_arn):
        raise EvidenceError("approval KMS key is outside the fixed account and region")
    if expected_pipeline not in APPROVAL_PIPELINES:
        raise EvidenceError("approval expected pipeline is not allowlisted")
    locators = _mapping(approval_locators, label="approval locators")
    _exact_keys(locators, {expected_pipeline}, label="approval locators")
    locator = _validate_approval_locator_pair(
        locators[expected_pipeline],
        pipeline=expected_pipeline,
        expected_commit=expected_commit,
        label=f"{expected_pipeline} approval locator",
    )

    try:
        inner_raw = runtime_contract_path.read_bytes()
        outer_raw = contract_path.read_bytes()
    except OSError as exc:
        raise EvidenceError("cannot read approval-bound contracts") from exc
    if hashlib.sha256(inner_raw).hexdigest() != _sha256(
        expected_inner_sha256,
        label="expected inner contract SHA-256",
    ):
        raise EvidenceError("actual inner contract raw SHA-256 mismatch")
    if hashlib.sha256(outer_raw).hexdigest() != _sha256(
        expected_outer_sha256,
        label="expected outer contract SHA-256",
    ):
        raise EvidenceError("actual outer contract raw SHA-256 mismatch")
    observations = _approval_observation_values(
        runtime_contract_path,
        contract_path,
    )

    with tempfile.TemporaryDirectory(prefix="teamagent-release-approval.") as temporary:
        root = Path(temporary)
        payload_path = root / "approval.json"
        signature_path = root / "approval.sig"
        payload_retention = _download_approval_object(
            locator["payload"],
            destination=payload_path,
            label="approval payload",
            approval_encryption_key_arn=encryption_key_arn,
        )
        signature_retention = _download_approval_object(
            locator["signature"],
            destination=signature_path,
            label="approval signature",
            approval_encryption_key_arn=encryption_key_arn,
        )
        if payload_retention != signature_retention:
            raise EvidenceError("approval payload and signature retention differ")
        try:
            payload = payload_path.read_bytes()
        except OSError as exc:
            raise EvidenceError("cannot read approval payload") from exc
        canonical_payload = _canonical_approval_payload(payload)
        approved_at = _timestamp(
            canonical_payload.get("approved_at_utc"),
            label="approval approved_at_utc",
        )
        minimum_retention = approved_at + dt.timedelta(days=3650)
        if payload_retention < minimum_retention:
            raise EvidenceError("approval retention is shorter than 3650 days")
        digest_path = root / "approval.sha256"
        digest_path.write_bytes(hashlib.sha256(payload).digest())
        verify_response = _aws_json(
            "approval KMS verification",
            "kms",
            "verify",
            "--region",
            REGION,
            "--key-id",
            signing_key_arn,
            "--message-type",
            "DIGEST",
            "--message",
            f"fileb://{digest_path}",
            "--signature",
            f"fileb://{signature_path}",
            "--signing-algorithm",
            "RSASSA_PSS_SHA_256",
            "--output",
            "json",
        )
        if verify_response.get("SignatureValid") is not True:
            raise EvidenceError("approval KMS signature is invalid")
        validated = validate_approved_release_for_operation(
            payload,
            operation=operation,
            approval_signing_key_arn=signing_key_arn,
            expected_commit=expected_commit,
            expected_tree_oid=expected_tree_oid,
            expected_inner_sha256=expected_inner_sha256,
            expected_outer_sha256=expected_outer_sha256,
            expected_pipeline=expected_pipeline,
            expected_environment=expected_environment,
            expected_observations=observations,
            now=current,
        )
    approval_expires_at = _timestamp(
        validated["expires_at_utc"],
        label="approval expires_at_utc",
    )
    if payload_retention <= approval_expires_at:
        raise EvidenceError("approval retention does not outlive the approval")
    approval_payload_sha256 = hashlib.sha256(payload).hexdigest()
    if approval_payload_sha256 != locator["payload"]["sha256"]:
        raise EvidenceError("approval payload SHA-256 does not match its locator")
    forced_gate_sha256 = hashlib.sha256(
        approval_canonical_json_bytes(
            validated["gates"]["forced_rollback_evidence"],
        )
    ).hexdigest()
    approval_evidence = validate_approval_evidence(
        {
            **locator,
            "approval_payload_sha256": approval_payload_sha256,
            "forced_gate_sha256": forced_gate_sha256,
        },
        pipeline=expected_pipeline,
        expected_commit=expected_commit,
    )
    if verified_record_out is not None:
        _write_json_exclusive(
            verified_record_out,
            {
                "approval_id": validated["approval_id"],
                "approved_at_utc": validated["approved_at_utc"],
                "approved_by": validated["approved_by"],
                "decision": validated["decision"],
                "expires_at_utc": validated["expires_at_utc"],
                "forced_gate_sha256": forced_gate_sha256,
                "payload": locator["payload"],
                "pipeline": validated["pipeline"],
                "signature": locator["signature"],
                "source_commit": validated["source_commit"],
            },
        )
    return approval_evidence


def _assert_no_release_lifecycle_policy(repository: str, *, label: str) -> None:
    """Fail closed unless ECR proves the release repository has no policy."""

    command = [
        "aws",
        "ecr",
        "get-lifecycle-policy",
        "--region",
        REGION,
        "--registry-id",
        ACCOUNT_ID,
        "--repository-name",
        repository,
        "--output",
        "json",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=_aws_environment(),
        )
    except OSError as exc:
        raise EvidenceError(f"{label} lifecycle-policy absence could not be verified") from exc
    if completed.returncode == 0:
        raise EvidenceError(f"{label} release repository must not have an ECR lifecycle policy")
    if "LifecyclePolicyNotFoundException" not in completed.stderr:
        raise EvidenceError(f"{label} lifecycle-policy absence could not be verified")


def _release_referrers(
    repository: str,
    subject_digest: str,
    *,
    label: str,
) -> list[Mapping[str, Any]]:
    response = _aws_json(
        f"{label} referrers",
        "ecr",
        "list-image-referrers",
        "--region",
        REGION,
        "--registry-id",
        ACCOUNT_ID,
        "--repository-name",
        repository,
        "--subject-id",
        f"imageDigest={subject_digest}",
        "--max-results",
        "50",
        "--output",
        "json",
    )
    if response.get("nextToken") not in {None, ""}:
        raise EvidenceError(f"{label} referrers are truncated")
    referrers = response.get("referrers")
    if not isinstance(referrers, list):
        raise EvidenceError(f"{label} referrers are missing")
    normalized: list[Mapping[str, Any]] = []
    for index, raw_referrer in enumerate(referrers):
        referrer = _mapping(raw_referrer, label=f"{label} referrer[{index}]")
        _digest(referrer.get("digest"), label=f"{label} referrer[{index}].digest")
        if referrer.get("artifactStatus") != "ACTIVE":
            raise EvidenceError(f"{label} referrer[{index}] is not ACTIVE")
        normalized.append(referrer)
    return normalized


def _release_referrer_graph(
    repository: str,
    subject_digest: str,
    *,
    label: str,
) -> dict[str, list[Mapping[str, Any]]]:
    """Discover every active referrer reachable from a release subject.

    Receipt-listed SBOM, provenance, and signature digests are necessary but
    not sufficient for retention: ECR can contain additional referrers that
    would otherwise remain unprotected. Pagination and oversized graphs fail
    closed.
    """

    root = _digest(subject_digest, label=f"{label} subject digest")
    pending = [root]
    discovered = {root}
    graph: dict[str, list[Mapping[str, Any]]] = {}
    while pending:
        current = pending.pop(0)
        referrers = _release_referrers(
            repository,
            current,
            label=f"{label} graph node {current}",
        )
        graph[current] = referrers
        for referrer in referrers:
            digest = _digest(referrer.get("digest"), label=f"{label} referrer digest")
            if digest in discovered:
                continue
            discovered.add(digest)
            if len(discovered) > MAX_RELEASE_GRAPH_DIGESTS:
                raise EvidenceError(
                    f"{label} release referrer graph exceeds {MAX_RELEASE_GRAPH_DIGESTS} digests"
                )
            pending.append(digest)
    return graph


def _validate_promoted_release(
    receipt: Mapping[str, Any],
) -> dict[str, set[str]]:
    """Prove promotion completed and return the full protected ECR graph."""

    protected: dict[str, set[str]] = {}
    for subject in receipt["subjects"]:
        repository = subject["release_repository"]
        digest = subject["digest"]
        label = f"{receipt['pipeline']}/{subject['name']} release"
        _assert_no_release_lifecycle_policy(repository, label=label)
        response = _aws_json(
            f"{label} image",
            "ecr",
            "batch-get-image",
            "--region",
            REGION,
            "--registry-id",
            ACCOUNT_ID,
            "--repository-name",
            repository,
            "--image-ids",
            f"imageDigest={digest}",
            "--accepted-media-types",
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "--output",
            "json",
        )
        images = response.get("images")
        failures = response.get("failures")
        if (
            not isinstance(images, list)
            or len(images) != 1
            or not isinstance(failures, list)
            or failures
        ):
            raise EvidenceError(f"{label} exact digest is not present")
        image = _mapping(images[0], label=f"{label} image")
        image_id = _mapping(image.get("imageId"), label=f"{label} image ID")
        if (
            image.get("registryId") != ACCOUNT_ID
            or image.get("repositoryName") != repository
            or image_id.get("imageDigest") != digest
            or image.get("imageManifestMediaType") != subject["media_type"]
            or image.get("imageManifestMediaType") not in SINGLE_ARM64_MEDIA_TYPES
        ):
            raise EvidenceError(f"{label} digest or single-manifest type mismatch")

        graph = _release_referrer_graph(repository, digest, label=label)
        referrers = graph[digest]
        protected.setdefault(repository, set()).update(graph)
        expected_artifacts = (
            ("SBOM", subject["sbom"]),
            ("provenance", subject["provenance"]),
        )
        for artifact_label, artifact in expected_artifacts:
            matches = [
                referrer
                for referrer in referrers
                if (
                    referrer.get("digest") == artifact["digest"]
                    and referrer.get("artifactType") == artifact["artifact_type"]
                    and isinstance(referrer.get("annotations"), dict)
                    and referrer["annotations"].get("io.teamagent.build.payload-sha256")
                    == artifact["payload_sha256"]
                )
            ]
            if len(matches) != 1:
                raise EvidenceError(
                    f"{label} exact {artifact_label} referrer is missing or ambiguous"
                )
            artifact_signatures = graph.get(artifact["digest"], [])
            exact_artifact_signatures = [
                referrer
                for referrer in artifact_signatures
                if (
                    referrer.get("digest") == artifact["signature"]["referrer_digest"]
                    and referrer.get("artifactType") in SIGNATURE_ARTIFACT_TYPES
                )
            ]
            if len(exact_artifact_signatures) != 1:
                raise EvidenceError(
                    f"{label} exact {artifact_label} signature is missing or ambiguous"
                )
        exact_image_signatures = [
            referrer
            for referrer in referrers
            if (
                referrer.get("digest") == subject["image_signature"]["referrer_digest"]
                and referrer.get("artifactType") in SIGNATURE_ARTIFACT_TYPES
            )
        ]
        if len(exact_image_signatures) != 1:
            raise EvidenceError(f"{label} exact image signature is missing or ambiguous")
    return protected


def _parse_terraform_gate_query(
    query: Mapping[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    str,
    str,
    str,
]:
    _exact_keys(
        query,
        {
            "consumer_manifest_json",
            "receipt_catalog_json",
            "consumer_receipt_bindings_json",
            "contracts_json",
            "contract_ready_json",
            "application_json",
            "shared_generation_ledger_json",
            "signing_key_arn",
            "encryption_key_arn",
            "deployment_intent_id",
        },
        label="Terraform gate query",
    )
    try:
        # These three carry one entry per consumer, so the 2048-byte default is far
        # too small: the exact-8 manifest measures ~13KB and a catalog holding a
        # distinct claim per consumer measures ~4KB. The bounds stay explicit and
        # finite -- they cap untrusted gate input, they are not a correctness knob.
        consumer_manifest = json.loads(
            _string(
                query["consumer_manifest_json"],
                label="consumer_manifest_json",
                maximum=65536,
            ),
            object_pairs_hook=_reject_duplicate_keys,
        )
        receipt_catalog = json.loads(
            _string(
                query["receipt_catalog_json"],
                label="receipt_catalog_json",
                maximum=16384,
            ),
            object_pairs_hook=_reject_duplicate_keys,
        )
        consumer_receipt_bindings = json.loads(
            _string(
                query["consumer_receipt_bindings_json"],
                label="consumer_receipt_bindings_json",
                maximum=4096,
            ),
            object_pairs_hook=_reject_duplicate_keys,
        )
        contracts = json.loads(
            _string(query["contracts_json"], label="contracts_json"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        ready = json.loads(
            _string(query["contract_ready_json"], label="contract_ready_json"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        application = json.loads(
            _string(query["application_json"], label="application_json"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        shared_generation_ledger = json.loads(
            _string(
                query["shared_generation_ledger_json"],
                label="shared_generation_ledger_json",
            ),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise EvidenceError("Terraform gate query contains invalid JSON") from exc
    if not all(
        isinstance(item, dict)
        for item in (
            consumer_manifest,
            receipt_catalog,
            consumer_receipt_bindings,
            contracts,
            ready,
            application,
            shared_generation_ledger,
        )
    ):
        raise EvidenceError("Terraform gate query maps are invalid")
    normalized_consumer_manifest = _validate_gate_consumer_manifest(consumer_manifest)
    normalized_shared_generation_ledger = _validate_shared_generation_ledger_binding(
        shared_generation_ledger
    )
    signing_key_arn = _string(query["signing_key_arn"], label="signing key ARN")
    encryption_key_arn = _string(query["encryption_key_arn"], label="encryption key ARN")
    intent_id = _uuid4(query["deployment_intent_id"], label="deployment intent ID")
    if not _KEY_ARN_RE.fullmatch(signing_key_arn) or not _KEY_ARN_RE.fullmatch(encryption_key_arn):
        raise EvidenceError("Terraform gate KMS key is outside the fixed account")
    return (
        normalized_consumer_manifest,
        receipt_catalog,
        consumer_receipt_bindings,
        contracts,
        ready,
        application,
        normalized_shared_generation_ledger,
        signing_key_arn,
        encryption_key_arn,
        intent_id,
    )


def _validate_gate_consumer_manifest(value: Any) -> dict[str, Any]:
    """Validate the full L/B/A manifest with the trusted Terraform helper."""

    helper_path = Path(__file__).resolve().parents[1] / "terraform" / ("image_release_context.py")
    try:
        spec = importlib.util.spec_from_file_location(
            "teamagent_image_release_context_manifest",
            helper_path,
        )
        if spec is None or spec.loader is None:
            raise OSError("could not load consumer manifest helper")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        validated = helper.validate_consumer_manifest(value)
    except (
        AttributeError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise EvidenceError("Terraform image consumer manifest is invalid") from exc
    if not isinstance(validated, dict):
        raise EvidenceError("Terraform image consumer manifest is invalid")
    return dict(validated)


def _consumer_manifest_rows(
    manifest: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    consumers = manifest.get("consumers")
    if not isinstance(consumers, list):
        raise EvidenceError("consumer manifest consumers must be a list")
    result: dict[str, Mapping[str, Any]] = {}
    for index, raw_consumer in enumerate(consumers):
        consumer = _mapping(
            raw_consumer,
            label=f"consumer manifest consumers[{index}]",
        )
        consumer_id = _string(
            consumer.get("consumer_id"),
            label=f"consumer manifest consumers[{index}].consumer_id",
        )
        if consumer_id in result:
            raise EvidenceError("consumer manifest contains a duplicate consumer")
        result[consumer_id] = consumer
    if len(result) != 8:
        raise EvidenceError("consumer manifest must contain exactly eight consumers")
    return result


# EventBridge rule が実行状態を持つ activator type。taskdef ポインタの在処
# （event target の ecs_target か、dispatch Lambda の environment か）は
# 実行状態の判定には影響しないため、両者をここで束ねる。
_EVENTBRIDGE_RULE_ACTIVATOR_TYPES = frozenset(
    {
        "eventbridge_rule_ecs_target",
        "eventbridge_rule_lambda_taskdef_arn_environment",
    }
)


def _consumer_execution_state(
    consumer: Mapping[str, Any],
    *,
    snapshot: str,
) -> int | str | bool | None:
    activator = _mapping(
        consumer.get("activator"),
        label=f"{consumer.get('consumer_id')} activator",
    )
    snapshot_value = _mapping(
        consumer.get(snapshot),
        label=f"{consumer.get('consumer_id')} {snapshot} snapshot",
    )
    if snapshot_value == {"absent": True}:
        return None
    state = _mapping(
        snapshot_value.get("activation"),
        label=f"{consumer.get('consumer_id')} {snapshot} activation",
    )
    activator_type = activator.get("type")
    if activator_type == "ecs_service":
        desired_count = state.get("desired_count")
        if (
            not isinstance(desired_count, int)
            or isinstance(desired_count, bool)
            or desired_count < 0
        ):
            raise EvidenceError("consumer ECS desired count is invalid")
        return desired_count
    if activator_type in _EVENTBRIDGE_RULE_ACTIVATOR_TYPES:
        rule_state = state.get("state")
        if rule_state not in {
            "ENABLED",
            "DISABLED",
            "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS",
        }:
            raise EvidenceError("consumer EventBridge rule state is invalid")
        return rule_state
    if activator_type == "lambda_taskdef_arn_environment":
        mapping_enabled = state.get("event_source_mapping_enabled")
        if not isinstance(mapping_enabled, bool):
            raise EvidenceError("consumer event source mapping state is invalid")
        return mapping_enabled
    raise EvidenceError("consumer activator type is not allowlisted")


def _consumer_is_executable(consumer: Mapping[str, Any], *, snapshot: str) -> bool:
    state = _consumer_execution_state(consumer, snapshot=snapshot)
    if state is None:
        return False
    activator = _mapping(
        consumer.get("activator"),
        label=f"{consumer.get('consumer_id')} activator",
    )
    if activator.get("type") == "ecs_service":
        return isinstance(state, int) and not isinstance(state, bool) and state > 0
    if activator.get("type") in _EVENTBRIDGE_RULE_ACTIVATOR_TYPES:
        return state != "DISABLED"
    return state is True


def _consumer_execution_increased(consumer: Mapping[str, Any]) -> bool:
    before = _consumer_execution_state(consumer, snapshot="before")
    after = _consumer_execution_state(consumer, snapshot="after")
    if after is None:
        return False
    if before is None:
        return True
    activator = _mapping(
        consumer.get("activator"),
        label=f"{consumer.get('consumer_id')} activator",
    )
    if activator.get("type") == "ecs_service":
        return (
            isinstance(before, int)
            and not isinstance(before, bool)
            and isinstance(after, int)
            and not isinstance(after, bool)
            and after > before
        )
    if activator.get("type") in _EVENTBRIDGE_RULE_ACTIVATOR_TYPES:
        return (before == "DISABLED" and after != "DISABLED") or (
            before == "ENABLED" and after == "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"
        )
    return not _consumer_is_executable(consumer, snapshot="before") and _consumer_is_executable(
        consumer, snapshot="after"
    )


def _consumer_target_digest(
    consumer_id: str,
    consumer: Mapping[str, Any],
) -> str:
    coordinates = _release_coordinates_for_consumer(consumer_id)
    after = _mapping(
        consumer.get("after"),
        label=f"{consumer_id} after snapshot",
    )
    image = _string(after.get("image"), label=f"{consumer_id} after image")
    match = re.fullmatch(
        rf"{re.escape(REGISTRY)}/{re.escape(coordinates['repository'])}@"
        r"(sha256:[0-9a-f]{64})",
        image,
    )
    if match is None:
        raise EvidenceError(f"{consumer_id} after image does not match the code-owned repository")
    return match.group(1)


def _receipt_required_consumers(
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Mapping[str, Any]], set[str]]:
    consumers = _consumer_manifest_rows(manifest)
    required: set[str] = set()
    for consumer_id, consumer in consumers.items():
        before = _mapping(
            consumer.get("before"),
            label=f"{consumer_id} before snapshot",
        )
        after = _mapping(
            consumer.get("after"),
            label=f"{consumer_id} after snapshot",
        )
        before_absent = before == {"absent": True}
        after_absent = after == {"absent": True}
        if after_absent:
            if not before_absent:
                raise EvidenceError(
                    f"{consumer_id} disabling is outside the image release workflow"
                )
            continue
        if before_absent:
            required.add(consumer_id)
            continue
        before_image = _string(before.get("image"), label=f"{consumer_id} before image")
        after_image = _string(after.get("image"), label=f"{consumer_id} after image")
        if (
            before_image != after_image
            or before.get("task_definition_arn") != after.get("task_definition_arn")
            or before.get("task_definition") != after.get("task_definition")
            or _consumer_execution_state(consumer, snapshot="before")
            != _consumer_execution_state(consumer, snapshot="after")
        ):
            required.add(consumer_id)
    mode = manifest.get("mode")
    if mode == "no-image-transition":
        if required:
            raise EvidenceError("no-image-transition manifest contains a receipt-requiring change")
    elif mode == "receipt-required":
        if not required:
            raise EvidenceError("receipt-required manifest contains no receipt-requiring change")
    else:
        raise EvidenceError("consumer manifest deployment mode is invalid")
    return consumers, required


def _canonical_receipt_claim_ids(
    claim_ids: Sequence[Any],
    *,
    allow_empty: bool = False,
) -> list[str]:
    normalized = [_sha256(claim_id, label="release receipt claim ID") for claim_id in claim_ids]
    canonical = sorted(set(normalized))
    if not canonical and not allow_empty:
        raise EvidenceError("deployment receipt claims are empty")
    return canonical


def _deployment_receipt_inputs(
    *,
    consumer_manifest: Mapping[str, Any],
    receipt_catalog: Mapping[str, Any],
    consumer_receipt_bindings: Mapping[str, Any],
) -> tuple[
    str,
    dict[str, Mapping[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, str],
    list[str],
]:
    manifest = _validate_gate_consumer_manifest(consumer_manifest)
    consumers, required = _receipt_required_consumers(manifest)
    mode = _string(manifest.get("mode"), label="deployment mode")

    normalized_bindings: dict[str, str] = {}
    for consumer_id, raw_claim_id in consumer_receipt_bindings.items():
        if not isinstance(consumer_id, str):
            raise EvidenceError("consumer receipt binding key is invalid")
        normalized_bindings[consumer_id] = _sha256(
            raw_claim_id,
            label=f"{consumer_id} receipt claim ID",
        )
    if set(normalized_bindings) != required:
        raise EvidenceError(
            "consumer receipt bindings do not exactly match receipt-requiring consumers"
        )

    claim_ids = _canonical_receipt_claim_ids(
        list(normalized_bindings.values()),
        allow_empty=mode == "no-image-transition",
    )
    if set(receipt_catalog) != set(claim_ids):
        raise EvidenceError(
            "receipt catalog must contain exactly the claims used by consumer bindings"
        )

    target_digests: dict[tuple[str, str], str] = {}
    for consumer_id in sorted(normalized_bindings):
        coordinates = _release_coordinates_for_consumer(consumer_id)
        subject_repository = (
            coordinates["subject"],
            coordinates["repository"],
        )
        target_digest = _consumer_target_digest(
            consumer_id,
            consumers[consumer_id],
        )
        existing_digest = target_digests.get(subject_repository)
        if existing_digest is not None and existing_digest != target_digest:
            raise EvidenceError(
                "one subject and release repository cannot target different digests"
            )
        target_digests[subject_repository] = target_digest

    references: dict[str, dict[str, Any]] = {}
    for claim_id in claim_ids:
        reference = _mapping(
            receipt_catalog.get(claim_id),
            label=f"release receipt catalog[{claim_id}]",
        )
        _exact_keys(
            reference,
            {
                "bucket",
                "key",
                "version_id",
                "signature_key",
                "signature_version_id",
            },
            label=f"release receipt catalog[{claim_id}]",
        )
        if reference["bucket"] != EVIDENCE_BUCKET:
            raise EvidenceError("deployment receipt claim bucket is not fixed")
        bound_consumers = sorted(
            consumer_id
            for consumer_id, bound_claim_id in normalized_bindings.items()
            if bound_claim_id == claim_id
        )
        expected_pipelines = {
            _release_coordinates_for_consumer(consumer_id)["pipeline"]
            for consumer_id in bound_consumers
        }
        if len(expected_pipelines) != 1:
            raise EvidenceError("one receipt claim cannot bind consumers from different pipelines")
        pipeline = next(iter(expected_pipelines))
        receipt_key = _string(
            reference["key"],
            label=f"release receipt catalog[{claim_id}].key",
        )
        receipt_key_match = re.fullmatch(
            rf"release-receipts/{re.escape(pipeline)}/"
            rf"[0-9a-f]{{40}}/({re.escape(claim_id)})\.json",
            receipt_key,
        )
        if receipt_key_match is None or reference["signature_key"] != f"{receipt_key}.sig":
            raise EvidenceError("deployment receipt claim key is not content-addressed")
        _version_id(
            reference["version_id"],
            label=f"release receipt catalog[{claim_id}] VersionId",
        )
        _version_id(
            reference["signature_version_id"],
            label=f"release receipt catalog[{claim_id}] signature VersionId",
        )
        references[claim_id] = dict(reference)

    if mode == "no-image-transition" and (receipt_catalog or consumer_receipt_bindings):
        raise EvidenceError("no-image-transition forbids receipts and consumer bindings")
    return mode, consumers, references, normalized_bindings, claim_ids


def _deployment_binding(
    *,
    consumer_manifest: Mapping[str, Any],
    receipt_catalog: Mapping[str, Any],
    consumer_receipt_bindings: Mapping[str, Any],
    contracts: Mapping[str, Any],
    application: Mapping[str, Any],
    shared_generation_ledger: Mapping[str, Any],
    release_channels: Mapping[str, Any],
    intent_id: str,
) -> tuple[str, list[str], str]:
    (
        mode,
        _consumers,
        references,
        normalized_bindings,
        claim_ids,
    ) = _deployment_receipt_inputs(
        consumer_manifest=consumer_manifest,
        receipt_catalog=receipt_catalog,
        consumer_receipt_bindings=consumer_receipt_bindings,
    )
    normalized_channels: dict[str, str] = {}
    if set(release_channels) != set(normalized_bindings):
        raise EvidenceError("deployment release channels do not match consumer receipt bindings")
    for consumer_id in sorted(normalized_bindings):
        channel = _string(
            release_channels.get(consumer_id),
            label=f"{consumer_id} deployment release channel",
        )
        if channel not in {"active", "rollback"}:
            raise EvidenceError("deployment release channel is not allowlisted")
        normalized_channels[consumer_id] = channel
    context = {
        "schema_version": DEPLOYMENT_INTENT_SCHEMA,
        "intent_id": intent_id,
        "mode": mode,
        "consumer_manifest": dict(consumer_manifest),
        "receipt_catalog": {
            claim_id: dict(references[claim_id]) for claim_id in sorted(references)
        },
        "consumer_receipt_bindings": {
            consumer_id: normalized_bindings[consumer_id]
            for consumer_id in sorted(normalized_bindings)
        },
        "contracts": {name: contracts[name] for name in sorted(contracts)},
        "release_channels": normalized_channels,
        "application": {name: application[name] for name in sorted(application)},
        "shared_generation_ledger": dict(shared_generation_ledger),
    }
    context_sha256 = hashlib.sha256(canonical_bytes(context)).hexdigest()
    claims_sha256 = hashlib.sha256(canonical_bytes(claim_ids)).hexdigest()
    return context_sha256, claim_ids, claims_sha256


def _terraform_gate(
    query: Mapping[str, Any],
    *,
    now: dt.datetime | None = None,
) -> dict[str, str]:
    (
        consumer_manifest,
        receipt_catalog,
        consumer_receipt_bindings,
        contracts,
        ready,
        application,
        shared_generation_ledger,
        signing_key_arn,
        encryption_key_arn,
        intent_id,
    ) = _parse_terraform_gate_query(query)
    (
        mode,
        consumers,
        references,
        normalized_bindings,
        claim_ids,
    ) = _deployment_receipt_inputs(
        consumer_manifest=consumer_manifest,
        receipt_catalog=receipt_catalog,
        consumer_receipt_bindings=consumer_receipt_bindings,
    )
    if mode == "receipt-required":
        relevant_consumers: set[str] = set()
        for consumer_id, consumer in consumers.items():
            before = _mapping(
                consumer.get("before"),
                label=f"{consumer_id} before snapshot",
            )
            after = _mapping(
                consumer.get("after"),
                label=f"{consumer_id} after snapshot",
            )
            if after == {"absent": True}:
                continue
            if (
                before == {"absent": True}
                or before.get("image") != after.get("image")
                or before.get("task_definition_arn") != after.get("task_definition_arn")
                or before.get("task_definition") != after.get("task_definition")
                or before.get("activation") != after.get("activation")
            ):
                relevant_consumers.add(consumer_id)
    else:
        relevant_consumers = {
            consumer_id
            for consumer_id, consumer in consumers.items()
            if _mapping(
                consumer.get("after"),
                label=f"{consumer_id} after snapshot",
            )
            != {"absent": True}
        }
    required_pipelines = {
        _release_coordinates_for_consumer(consumer_id)["pipeline"]
        for consumer_id in relevant_consumers
    }
    if set(contracts) != required_pipelines:
        raise EvidenceError("Terraform contracts do not exactly match required pipelines")
    if set(ready) != required_pipelines:
        raise EvidenceError(
            "Terraform contract readiness does not exactly match required pipelines"
        )
    for pipeline in sorted(required_pipelines):
        _sha256(
            contracts[pipeline],
            label=f"{pipeline} contract SHA-256",
        )
        if ready[pipeline] is not True:
            raise EvidenceError(f"{pipeline} release.ready is false")

    required_application_pipelines = {"mcp"} if "mcp" in required_pipelines else set()
    if set(application) != required_application_pipelines:
        raise EvidenceError(
            "Terraform application provenance does not exactly match required pipelines"
        )
    if "mcp" in required_application_pipelines:
        _validate_mcp_deployment_application(None, application["mcp"])

    current = _utc_now(now)
    verified_consumers: list[str] = []
    release_channels: dict[str, str] = {}
    receipt_expirations: list[dt.datetime] = []
    with tempfile.TemporaryDirectory(prefix="teamagent-release-gate.") as temporary:
        root = Path(temporary)
        for claim_id in claim_ids:
            bound_consumers = sorted(
                consumer_id
                for consumer_id, bound_claim_id in normalized_bindings.items()
                if bound_claim_id == claim_id
            )
            pipeline = _release_coordinates_for_consumer(bound_consumers[0])["pipeline"]
            contract_sha256 = _sha256(
                contracts[pipeline],
                label=f"{pipeline} contract SHA-256",
            )
            reference = references[claim_id]
            key = _string(reference["key"], label="release evidence key")
            signature_key = _string(reference["signature_key"], label="release signature key")
            expected_key_pattern = re.compile(
                rf"release-receipts/{re.escape(pipeline)}/"
                rf"([0-9a-f]{{40}})/({re.escape(claim_id)})\.json"
            )
            key_match = expected_key_pattern.fullmatch(key)
            if key_match is None or signature_key != f"{key}.sig" or ".." in key:
                raise EvidenceError("release evidence key is not content-addressed")
            version_id = _version_id(reference["version_id"], label="release VersionId")
            signature_version_id = _version_id(
                reference["signature_version_id"],
                label="release signature VersionId",
            )
            receipt_path = root / f"{claim_id}.json"
            signature_path = root / f"{claim_id}.sig"
            for object_key, object_version, destination, label in (
                (key, version_id, receipt_path, "receipt"),
                (
                    signature_key,
                    signature_version_id,
                    signature_path,
                    "signature",
                ),
            ):
                head_raw = _aws(
                    "s3api",
                    "head-object",
                    "--region",
                    REGION,
                    "--bucket",
                    EVIDENCE_BUCKET,
                    "--key",
                    object_key,
                    "--version-id",
                    object_version,
                    "--expected-bucket-owner",
                    ACCOUNT_ID,
                    "--output",
                    "json",
                )
                try:
                    head = json.loads(head_raw)
                except json.JSONDecodeError as exc:
                    raise EvidenceError(f"invalid {label} object metadata") from exc
                if (
                    head.get("VersionId") != object_version
                    or head.get("ObjectLockMode") not in {"COMPLIANCE", "GOVERNANCE"}
                    or head.get("ServerSideEncryption") != "aws:kms"
                    or head.get("SSEKMSKeyId") != encryption_key_arn
                ):
                    raise EvidenceError(f"{label} object is not immutable exact evidence")
                retained = _metadata_timestamp(
                    head.get("ObjectLockRetainUntilDate", ""),
                    label=f"{label} retention",
                )
                if retained <= current:
                    raise EvidenceError(f"{label} evidence retention has expired")
                response = _aws(
                    "s3api",
                    "get-object",
                    "--region",
                    REGION,
                    "--bucket",
                    EVIDENCE_BUCKET,
                    "--key",
                    object_key,
                    "--version-id",
                    object_version,
                    "--expected-bucket-owner",
                    ACCOUNT_ID,
                    "--query",
                    "VersionId",
                    "--output",
                    "text",
                    output=destination,
                ).strip()
                if response != object_version:
                    raise EvidenceError(f"{label} download VersionId mismatch")

            assert key_match is not None
            if hashlib.sha256(receipt_path.read_bytes()).hexdigest() != claim_id:
                raise EvidenceError("release receipt bytes do not match the content key")
            digest_path = root / f"{claim_id}.sha256"
            digest_path.write_bytes(hashlib.sha256(receipt_path.read_bytes()).digest())
            verify_raw = _aws(
                "kms",
                "verify",
                "--region",
                REGION,
                "--key-id",
                signing_key_arn,
                "--message-type",
                "DIGEST",
                "--message",
                f"fileb://{digest_path}",
                "--signature",
                f"fileb://{signature_path}",
                "--signing-algorithm",
                "RSASSA_PSS_SHA_256",
                "--output",
                "json",
            )
            try:
                signature_valid = json.loads(verify_raw).get("SignatureValid")
            except json.JSONDecodeError as exc:
                raise EvidenceError("invalid KMS verification response") from exc
            if signature_valid is not True:
                raise EvidenceError("release evidence KMS signature is invalid")
            receipt = load_json(receipt_path, label=f"{pipeline} release receipt")
            if (
                not isinstance(receipt, dict)
                or not isinstance(receipt.get("build"), dict)
                or receipt["build"].get("source_commit") != key_match.group(1)
            ):
                raise EvidenceError("release receipt commit does not match its content key")
            validated_receipt = validate_release_receipt(
                receipt,
                expected_pipeline=pipeline,
                expected_contract_sha256=contract_sha256,
                allowed_channels={"active", "rollback"},
                now=current,
            )
            verified_channel = _string(
                validated_receipt.get("channel"),
                label=f"{claim_id} verified release channel",
            )
            for consumer_id in bound_consumers:
                after = _mapping(
                    consumers[consumer_id].get("after"),
                    label=f"{consumer_id} after snapshot",
                )
                _validate_consumer_deploy_reference(
                    validated_receipt,
                    consumer_id=consumer_id,
                    image=_string(
                        after.get("image"),
                        label=f"{consumer_id} after image",
                    ),
                )
                if (
                    _consumer_execution_increased(consumers[consumer_id])
                    and verified_channel != "active"
                ):
                    raise EvidenceError(
                        f"{consumer_id} execution increase requires a fresh active receipt"
                    )
                release_channels[consumer_id] = verified_channel
                verified_consumers.append(consumer_id)
            receipt_expirations.append(
                _timestamp(
                    validated_receipt["expires_at"],
                    label=f"{claim_id} deployment receipt expires_at",
                )
            )
            if pipeline == "mcp":
                _validate_mcp_deployment_application(
                    validated_receipt,
                    application.get("mcp"),
                )
            _validate_promoted_release(validated_receipt)
    if mode == "no-image-transition":
        if release_channels or receipt_expirations or claim_ids:
            raise EvidenceError("no-image-transition contains release evidence")
        authorization_expires_at = int(
            (current + dt.timedelta(seconds=MAX_DEPLOYMENT_INTENT_LIFETIME_SECONDS)).timestamp()
        )
    else:
        if not receipt_expirations:
            raise EvidenceError("receipt-required deployment lacks fresh evidence")
        authorization_expires_at = int(min(receipt_expirations).timestamp())
    context_sha256, _, claims_sha256 = _deployment_binding(
        consumer_manifest=consumer_manifest,
        receipt_catalog=receipt_catalog,
        consumer_receipt_bindings=consumer_receipt_bindings,
        contracts=contracts,
        application=application,
        shared_generation_ledger=shared_generation_ledger,
        release_channels=release_channels,
        intent_id=intent_id,
    )
    return {
        "verified": "true",
        "verified_consumers": ",".join(sorted(verified_consumers)),
        "verified_pipelines": ",".join(sorted(required_pipelines)),
        "deployment_mode": mode,
        "deployment_context_sha256": context_sha256,
        "receipt_claims_sha256": claims_sha256,
        "receipt_authorization_expires_at": str(authorization_expires_at),
        "release_channels_json": json.dumps(
            release_channels,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _terraform_show_plan(plan_path: Path) -> Mapping[str, Any]:
    try:
        completed = subprocess.run(
            ["terraform", "show", "-json", str(plan_path)],
            check=True,
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ.get("PATH", ""),
                "TF_IN_AUTOMATION": "1",
            },
        )
        value = json.loads(
            completed.stdout,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise EvidenceError("saved Terraform plan cannot be inspected") from exc
    return _mapping(value, label="saved Terraform plan")


def _planned_resources(module: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    resources = module.get("resources", [])
    if not isinstance(resources, list):
        raise EvidenceError("saved Terraform plan resources are malformed")
    result = [_mapping(resource, label="saved Terraform plan resource") for resource in resources]
    child_modules = module.get("child_modules", [])
    if not isinstance(child_modules, list):
        raise EvidenceError("saved Terraform child modules are malformed")
    for child in child_modules:
        result.extend(
            _planned_resources(
                _mapping(child, label="saved Terraform child module"),
            )
        )
    return result


def _saved_plan_transition_classification(
    changes: Sequence[Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    deletes: list[dict[str, Any]] = []
    replacements: list[dict[str, Any]] = []
    replacement_details: list[dict[str, Any]] = []
    seen_addresses: set[str] = set()
    for index, raw_change in enumerate(changes):
        resource = _mapping(
            raw_change,
            label=f"saved Terraform resource change[{index}]",
        )
        if resource.get("mode", "managed") != "managed":
            continue
        address = _string(
            resource.get("address"),
            label="saved Terraform managed resource address",
        )
        if address in seen_addresses:
            raise EvidenceError("saved Terraform plan has duplicate managed addresses")
        seen_addresses.add(address)
        details = _mapping(
            resource.get("change"),
            label=f"saved Terraform resource change {address}",
        )
        actions = details.get("actions")
        if (
            not isinstance(actions, list)
            or not actions
            or any(
                action not in {"no-op", "create", "read", "update", "delete"} for action in actions
            )
        ):
            raise EvidenceError(f"saved Terraform actions are invalid for {address}")
        if address == RELEASE_GATE_ADDRESS:
            continue
        transition = {
            "address": address,
            "actions": actions,
        }
        if "delete" in actions and "create" not in actions:
            deletes.append(transition)
        elif "delete" in actions and "create" in actions:
            replacements.append(transition)
            replacement_details.append(
                {
                    "address": address,
                    "after": details.get("after"),
                }
            )
    deletes.sort(key=lambda item: item["address"])
    replacements.sort(key=lambda item: item["address"])
    value = {
        "delete": deletes,
        "replace": replacements,
    }
    return (
        {
            "delete_change_count": len(deletes),
            "replace_change_count": len(replacements),
            "transition_sha256": hashlib.sha256(canonical_bytes(value)).hexdigest(),
        },
        deletes,
        replacement_details,
    )


def _is_digest_preserving_task_replacement(
    transition: Mapping[str, Any],
    *,
    consumer_images: Mapping[str, Any],
) -> bool:
    address = _string(
        transition.get("address"),
        label="replacement Terraform address",
    )
    base_address = _INSTANCE_SELECTOR_RE.sub("", address)
    if not base_address.startswith("aws_ecs_task_definition."):
        return False
    pipeline = IMAGE_MANAGED_ECS_PIPELINES.get(base_address)
    image = consumer_images.get(pipeline) if pipeline else None
    after = transition.get("after")
    if not isinstance(image, str) or not image or not isinstance(after, dict):
        return False
    container_definitions = after.get("container_definitions")
    if not isinstance(container_definitions, str):
        return False
    try:
        containers = json.loads(
            container_definitions,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (EvidenceError, json.JSONDecodeError):
        return False
    if not isinstance(containers, list) or not containers:
        return False
    return any(
        isinstance(container, dict) and container.get("image") == image for container in containers
    )


def _is_exact_hmac_gate_replacement(transition: Mapping[str, Any]) -> bool:
    address = transition.get("address")
    return isinstance(address, str) and address in HMAC_RUNTIME_GATE_ADDRESSES


def _require_destructive_rollback_channels(
    *,
    deletes: Sequence[Mapping[str, Any]],
    replacements: Sequence[Mapping[str, Any]],
    consumer_images: Mapping[str, Any],
    release_channels: Mapping[str, Any],
) -> None:
    destructive = list(deletes)
    destructive.extend(
        transition
        for transition in replacements
        if not _is_digest_preserving_task_replacement(
            transition,
            consumer_images=consumer_images,
        )
        and not _is_exact_hmac_gate_replacement(transition)
    )
    for transition in destructive:
        address = _string(
            transition.get("address"),
            label="destructive Terraform address",
        )
        base_address = _INSTANCE_SELECTOR_RE.sub("", address)
        pipeline = IMAGE_MANAGED_ECS_PIPELINES.get(base_address)
        if pipeline is None:
            raise EvidenceError(
                "saved image release plan contains an unscoped destructive transition"
            )
        image = consumer_images.get(pipeline)
        if not isinstance(image, str) or not image:
            raise EvidenceError(f"{pipeline} image-empty destructive state is forbidden")
        if release_channels.get(pipeline) != "rollback":
            raise EvidenceError(
                f"{pipeline} destructive transition requires a fresh rollback receipt"
            )


def deployment_plan_metadata(
    plan_path: Path,
    *,
    plan_json: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    try:
        plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise EvidenceError("saved Terraform plan cannot be read") from exc
    expected_plan_sha256 = os.environ.get("TEAMAGENT_SAVED_PLAN_SHA256")
    if expected_plan_sha256 is not None and plan_sha256 != expected_plan_sha256:
        raise EvidenceError("saved Terraform plan differs from its staged digest")
    plan = plan_json or _terraform_show_plan(plan_path)
    if plan.get("complete") is not True:
        raise EvidenceError("saved Terraform plan is incomplete")
    if plan.get("errored") is not False:
        raise EvidenceError("saved Terraform plan is errored or untrusted")
    if plan.get("applyable") is not True:
        raise EvidenceError("saved Terraform plan is not applyable")
    changes_for_import_check = plan.get("resource_changes")
    if not isinstance(changes_for_import_check, list):
        raise EvidenceError("saved Terraform resource changes are missing")
    for raw_change in changes_for_import_check:
        change_for_import = _mapping(
            raw_change,
            label="saved Terraform resource change",
        )
        address = _string(
            change_for_import.get("address"),
            label="saved Terraform resource change address",
        )
        details_for_import = _mapping(
            change_for_import.get("change"),
            label=f"saved Terraform resource change {address}",
        )
        importing = details_for_import.get("importing")
        if importing is not None and importing is not False:
            import_contract = _mapping(
                importing,
                label=f"saved Terraform import operation {address}",
            )
            if (
                set(import_contract) != {"id"}
                or import_contract.get("id") != ALLOWED_EXISTING_LOG_IMPORTS.get(address)
                or details_for_import.get("actions") not in (["no-op"], ["update"])
            ):
                raise EvidenceError(
                    "image release saved plan import is outside the exact existing-log allowlist"
                )
    planned_values = _mapping(
        plan.get("planned_values"),
        label="saved Terraform planned values",
    )
    root_module = _mapping(
        planned_values.get("root_module"),
        label="saved Terraform root module",
    )
    gate_resources = [
        resource
        for resource in _planned_resources(root_module)
        if resource.get("address") == RELEASE_GATE_ADDRESS
    ]
    if len(gate_resources) != 1:
        raise EvidenceError("saved Terraform plan lacks one production release gate")
    gate_values = _mapping(
        gate_resources[0].get("values"),
        label="saved Terraform release gate values",
    )
    gate_input = _mapping(
        gate_values.get("input"),
        label="saved Terraform release gate input",
    )
    _exact_keys(
        gate_input,
        {
            "deployment_intent_id",
            "deployment_context_sha256",
            "receipt_claims_sha256",
            "consumer_manifest",
            "receipt_catalog",
            "consumer_receipt_bindings",
            "release_channels",
            "application_provenance",
            "shared_generation_ledger",
            "hmac_release_bindings",
            "deployment_gate_query",
            "receipt_authorization_expires_at",
            "deployment_mode",
        },
        label="saved Terraform release gate input",
    )
    consumer_manifest = _validate_gate_consumer_manifest(gate_input["consumer_manifest"])
    manifest_consumers, required_consumers = _receipt_required_consumers(consumer_manifest)
    receipt_catalog = _mapping(
        gate_input["receipt_catalog"],
        label="saved Terraform receipt catalog",
    )
    consumer_receipt_bindings = _mapping(
        gate_input["consumer_receipt_bindings"],
        label="saved Terraform consumer receipt bindings",
    )
    (
        bound_mode,
        _bound_consumers,
        _bound_references,
        normalized_consumer_receipt_bindings,
        _bound_claim_ids,
    ) = _deployment_receipt_inputs(
        consumer_manifest=consumer_manifest,
        receipt_catalog=receipt_catalog,
        consumer_receipt_bindings=consumer_receipt_bindings,
    )
    if normalized_consumer_receipt_bindings != dict(consumer_receipt_bindings):
        raise EvidenceError("saved Terraform consumer receipt bindings are noncanonical")
    release_channels = _mapping(
        gate_input["release_channels"],
        label="saved Terraform release channels",
    )
    if set(release_channels) != required_consumers or any(
        channel not in {"active", "rollback"} for channel in release_channels.values()
    ):
        raise EvidenceError(
            "saved Terraform release channels do not match receipt-requiring consumers"
        )
    deployment_mode = _string(
        gate_input["deployment_mode"],
        label="saved Terraform deployment mode",
    )
    if deployment_mode != consumer_manifest["mode"] or deployment_mode != bound_mode:
        raise EvidenceError("saved Terraform deployment mode is not manifest-derived")
    application_provenance = _mapping(
        gate_input["application_provenance"],
        label="saved Terraform application provenance",
    )
    required_pipelines = {
        _release_coordinates_for_consumer(consumer_id)["pipeline"]
        for consumer_id in required_consumers
    }
    if "mcp" in required_pipelines and "mcp" not in application_provenance:
        raise EvidenceError("saved Terraform plan lacks the MCP application binding")
    shared_generation_ledger = _validate_shared_generation_ledger_binding(
        gate_input["shared_generation_ledger"]
    )
    _mapping(
        gate_input["hmac_release_bindings"],
        label="saved Terraform HMAC release bindings",
    )
    deployment_gate_query = dict(
        _mapping(
            gate_input["deployment_gate_query"],
            label="saved Terraform deployment gate query",
        )
    )
    (
        query_consumer_manifest,
        query_receipt_catalog,
        query_consumer_receipt_bindings,
        _query_contracts,
        _query_ready,
        query_application,
        query_shared_generation_ledger,
        _query_signing_key_arn,
        _query_encryption_key_arn,
        query_intent_id,
    ) = _parse_terraform_gate_query(deployment_gate_query)
    if (
        query_consumer_manifest != consumer_manifest
        or query_receipt_catalog != dict(receipt_catalog)
        or query_consumer_receipt_bindings != dict(consumer_receipt_bindings)
        or query_application != dict(application_provenance)
        or query_shared_generation_ledger != shared_generation_ledger
    ):
        raise EvidenceError(
            "saved Terraform gate query does not match the planned deployment inputs"
        )
    receipt_authorization_expires_at = _epoch_seconds(
        gate_input["receipt_authorization_expires_at"],
        label="saved Terraform receipt authorization expiry",
    )

    (
        transitions,
        destructive_deletes,
        planned_replacements,
    ) = _saved_plan_transition_classification(changes_for_import_check)
    transition_images: dict[str, str] = {}
    for consumer_id, consumer in manifest_consumers.items():
        after = _mapping(
            consumer.get("after"),
            label=f"{consumer_id} after snapshot",
        )
        if after == {"absent": True}:
            continue
        transition_images[consumer_id] = _string(
            after.get("image"),
            label=f"{consumer_id} after image",
        )
    _require_destructive_rollback_channels(
        deletes=destructive_deletes,
        replacements=planned_replacements,
        consumer_images=transition_images,
        release_channels=release_channels,
    )
    changes = changes_for_import_check
    gate_changes = [
        _mapping(change, label="saved Terraform resource change")
        for change in changes
        if isinstance(change, dict) and change.get("address") == RELEASE_GATE_ADDRESS
    ]
    if len(gate_changes) != 1:
        raise EvidenceError("saved Terraform plan does not replace one release gate")
    change = _mapping(
        gate_changes[0].get("change"),
        label="saved Terraform release gate change",
    )
    actions = change.get("actions")
    if (
        not isinstance(actions, list)
        or "create" not in actions
        or any(action not in {"create", "delete"} for action in actions)
    ):
        raise EvidenceError("saved Terraform plan will not run the apply-time gate")

    intent_id = _uuid4(
        gate_input["deployment_intent_id"],
        label="saved Terraform deployment intent ID",
    )
    if query_intent_id != intent_id:
        raise EvidenceError("saved Terraform gate query belongs to another deployment intent")
    variables = plan.get("variables", {})
    if isinstance(variables, dict) and "image_deployment_intent_id" in variables:
        variable = _mapping(
            variables["image_deployment_intent_id"],
            label="saved Terraform intent variable",
        )
        if variable.get("value") != intent_id:
            raise EvidenceError("saved Terraform intent variable does not match the gate")
    return {
        "intent_id": intent_id,
        "plan_sha256": plan_sha256,
        "deployment_context_sha256": _sha256(
            gate_input["deployment_context_sha256"],
            label="saved Terraform deployment context SHA-256",
        ),
        "receipt_claims_sha256": _sha256(
            gate_input["receipt_claims_sha256"],
            label="saved Terraform receipt claims SHA-256",
        ),
        "shared_ledger_sha256": hashlib.sha256(
            canonical_bytes(shared_generation_ledger)
        ).hexdigest(),
        "plan_transition_sha256": transitions["transition_sha256"],
        "gate_query_sha256": hashlib.sha256(canonical_bytes(deployment_gate_query)).hexdigest(),
        "gate_query_json": json.dumps(
            deployment_gate_query,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "receipt_authorization_expires_at": receipt_authorization_expires_at,
        "deployment_mode": deployment_mode,
        "consumer_manifest_sha256": hashlib.sha256(canonical_bytes(consumer_manifest)).hexdigest(),
        "release_evidence_binding_sha256": hashlib.sha256(
            canonical_bytes(
                {
                    "receipt_catalog": dict(receipt_catalog),
                    "consumer_receipt_bindings": dict(consumer_receipt_bindings),
                    "release_channels": dict(release_channels),
                }
            )
        ).hexdigest(),
    }


def terraform_context_metadata(context_path: Path) -> dict[str, str | int]:
    """Validate and summarize the trusted backend/workspace/state/ownership context."""

    helper_path = Path(__file__).resolve().parents[1] / "terraform" / ("image_release_context.py")
    try:
        spec = importlib.util.spec_from_file_location(
            "teamagent_image_release_context",
            helper_path,
        )
        if spec is None or spec.loader is None:
            raise OSError("could not load context helper")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        value = json.loads(
            context_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise EvidenceError("Terraform runtime context is invalid") from exc
    try:
        validated = helper.validate_context(value)
        context_sha256 = helper.context_sha256(validated)
    except (
        AttributeError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        helper.ContextError,
        ValueError,
    ) as exc:
        raise EvidenceError("Terraform runtime context is invalid") from exc
    state = _mapping(validated["state"], label="Terraform runtime context state")
    plan = _mapping(validated["plan"], label="Terraform runtime context plan")
    consumer_manifest = _validate_gate_consumer_manifest(validated["consumer_manifest"])
    backend = _mapping(
        validated["backend"],
        label="Terraform runtime context backend",
    )
    backend_workspace_sha256 = hashlib.sha256(
        canonical_bytes(
            {
                "backend": dict(backend),
                "workspace": validated["workspace"],
            }
        )
    ).hexdigest()
    serial = state["serial"]
    if not isinstance(serial, int) or isinstance(serial, bool):
        raise EvidenceError("Terraform runtime context state serial is invalid")
    manifest_sha256 = hashlib.sha256(canonical_bytes(consumer_manifest)).hexdigest()
    if plan["consumer_count"] != 8 or plan["consumer_manifest_sha256"] != manifest_sha256:
        raise EvidenceError("Terraform context consumer manifest binding is invalid")
    return {
        "terraform_context_sha256": _sha256(
            context_sha256,
            label="Terraform runtime context SHA-256",
        ),
        "backend_workspace_sha256": backend_workspace_sha256,
        "state_lineage": _string(
            state["lineage"],
            label="Terraform state lineage",
        ),
        "state_serial": serial,
        "state_addresses_sha256": _sha256(
            state["managed_addresses_sha256"],
            label="Terraform state address ownership SHA-256",
        ),
        "plan_addresses_sha256": _sha256(
            plan["address_ownership_sha256"],
            label="Terraform plan address ownership SHA-256",
        ),
        "runtime_images_sha256": _sha256(
            plan["runtime_images_sha256"],
            label="Terraform runtime images SHA-256",
        ),
        "plan_transition_sha256": _sha256(
            plan["transition_sha256"],
            label="Terraform plan transition SHA-256",
        ),
        "consumer_manifest_sha256": manifest_sha256,
        "consumer_count": 8,
        "consumer_comparison_sha256": _sha256(
            plan["consumer_comparison_sha256"],
            label="Terraform consumer comparison SHA-256",
        ),
        "release_evidence_binding_sha256": _sha256(
            plan["release_evidence_binding_sha256"],
            label="Terraform release evidence binding SHA-256",
        ),
        "deployment_mode": _string(
            consumer_manifest["mode"],
            label="Terraform context deployment mode",
        ),
    }


def _utc_now(now: dt.datetime | None = None) -> dt.datetime:
    current = now or dt.datetime.now(dt.UTC)
    if current.tzinfo is None or current.utcoffset() != dt.timedelta(0):
        raise EvidenceError("deployment intent time must be UTC")
    return current.replace(microsecond=0)


def _ddb_attribute(value: str | int) -> dict[str, str]:
    if isinstance(value, bool):
        raise EvidenceError("DynamoDB boolean attributes are not supported")
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, str) and value:
        return {"S": value}
    raise EvidenceError("DynamoDB deployment intent attribute is invalid")


def _ddb_item(value: Mapping[str, str | int]) -> dict[str, dict[str, str]]:
    return {key: _ddb_attribute(item) for key, item in value.items()}


def _decode_ddb_item(value: Any) -> dict[str, str | int]:
    item = _mapping(value, label="DynamoDB deployment intent item")
    decoded: dict[str, str | int] = {}
    for key, raw_attribute in item.items():
        attribute = _mapping(raw_attribute, label=f"DynamoDB attribute {key}")
        if set(attribute) == {"S"}:
            decoded[key] = _string(attribute["S"], label=f"DynamoDB attribute {key}")
        elif set(attribute) == {"N"}:
            number = _string(attribute["N"], label=f"DynamoDB attribute {key}")
            if not re.fullmatch(r"0|[1-9][0-9]*", number):
                raise EvidenceError(f"DynamoDB attribute {key} is not an integer")
            decoded[key] = int(number)
        else:
            raise EvidenceError(f"DynamoDB attribute {key} has an unsupported type")
    return decoded


def _dynamodb_get(record_id: str) -> dict[str, str | int] | None:
    response = _aws_json(
        "deployment intent lookup",
        "dynamodb",
        "get-item",
        "--region",
        REGION,
        "--table-name",
        DEPLOYMENT_INTENT_TABLE,
        "--key",
        json.dumps({"record_id": {"S": record_id}}, separators=(",", ":")),
        "--consistent-read",
        "--output",
        "json",
    )
    item = response.get("Item")
    if item is None:
        return None
    return _decode_ddb_item(item)


def _dynamodb_put_prepared_intent(
    item: Mapping[str, str | int],
) -> None:
    try:
        _aws(
            "dynamodb",
            "put-item",
            "--region",
            REGION,
            "--table-name",
            DEPLOYMENT_INTENT_TABLE,
            "--item",
            json.dumps(_ddb_item(item), sort_keys=True, separators=(",", ":")),
            "--condition-expression",
            "attribute_not_exists(record_id)",
            "--return-consumed-capacity",
            "NONE",
            "--output",
            "json",
        )
    except EvidenceError as exc:
        if _dynamodb_get(str(item["record_id"])) is not None:
            raise EvidenceError("deployment intent already exists") from exc
        raise EvidenceError("deployment intent could not be created conditionally") from exc


def _deployment_lock_item(
    *,
    metadata: Mapping[str, str],
    terraform_context_sha256: str,
    apply_attempt_id: str,
    now: dt.datetime,
) -> dict[str, str | int]:
    return {
        "record_id": DEPLOYMENT_LOCK_RECORD_ID,
        "record_type": "teamagent.image-release-apply-lock",
        "schema_version": DEPLOYMENT_INTENT_SCHEMA,
        "state": "LOCKED",
        "intent_id": metadata["intent_id"],
        "plan_sha256": metadata["plan_sha256"],
        "deployment_context_sha256": metadata["deployment_context_sha256"],
        "receipt_claims_sha256": metadata["receipt_claims_sha256"],
        "gate_query_sha256": metadata["gate_query_sha256"],
        "terraform_context_sha256": terraform_context_sha256,
        "apply_attempt_id": apply_attempt_id,
        "acquired_at": now.isoformat().replace("+00:00", "Z"),
        "lease_expires_at": int(
            (now + dt.timedelta(seconds=DEPLOYMENT_LOCK_LEASE_SECONDS)).timestamp()
        ),
        "audit_expires_at": int(
            (now + dt.timedelta(seconds=DEPLOYMENT_INTENT_AUDIT_TTL_SECONDS)).timestamp()
        ),
    }


def _validate_deployment_lock(
    item: Mapping[str, str | int],
    *,
    metadata: Mapping[str, str],
    apply_attempt_id: str,
    now: dt.datetime,
    terraform_context_sha256: str | None = None,
) -> None:
    _exact_keys(
        item,
        {
            "record_id",
            "record_type",
            "schema_version",
            "state",
            "intent_id",
            "plan_sha256",
            "deployment_context_sha256",
            "receipt_claims_sha256",
            "gate_query_sha256",
            "terraform_context_sha256",
            "apply_attempt_id",
            "acquired_at",
            "lease_expires_at",
            "audit_expires_at",
        },
        label="image release apply lock",
    )
    if (
        item["record_id"] != DEPLOYMENT_LOCK_RECORD_ID
        or item["record_type"] != "teamagent.image-release-apply-lock"
        or item["schema_version"] != DEPLOYMENT_INTENT_SCHEMA
        or item["state"] != "LOCKED"
        or item["intent_id"] != metadata["intent_id"]
        or item["plan_sha256"] != metadata["plan_sha256"]
        or item["deployment_context_sha256"] != metadata["deployment_context_sha256"]
        or item["receipt_claims_sha256"] != metadata["receipt_claims_sha256"]
        or item["gate_query_sha256"] != metadata["gate_query_sha256"]
        or item["apply_attempt_id"] != apply_attempt_id
    ):
        raise EvidenceError("image release apply lock ownership mismatch")
    _timestamp(item["acquired_at"], label="apply lock acquisition")
    lease = item["lease_expires_at"]
    audit = item["audit_expires_at"]
    if (
        not isinstance(lease, int)
        or not isinstance(audit, int)
        or int(now.timestamp()) >= lease
        or audit <= lease
    ):
        raise EvidenceError("image release apply lock lease has expired")
    lock_context = _sha256(
        item["terraform_context_sha256"],
        label="apply lock Terraform context SHA-256",
    )
    if terraform_context_sha256 is not None and lock_context != terraform_context_sha256:
        raise EvidenceError("apply lock does not bind the live Terraform context")


def _dynamodb_transact_begin_apply(
    *,
    prepared: Mapping[str, str | int],
    metadata: Mapping[str, str],
    lock_item: Mapping[str, str | int],
    apply_attempt_id: str,
    control_commit: str,
    now: dt.datetime,
) -> None:
    now_text = now.isoformat().replace("+00:00", "Z")
    now_epoch = int(now.timestamp())
    expected_control_commit = _sha1(
        control_commit,
        label="apply control commit",
    )
    transaction = [
        {
            "Put": {
                "TableName": DEPLOYMENT_INTENT_TABLE,
                "Item": _ddb_item(lock_item),
                "ConditionExpression": (
                    "attribute_not_exists(record_id) OR lease_expires_at < :now"
                ),
                "ExpressionAttributeValues": _ddb_item({":now": now_epoch}),
            }
        },
        {
            "Update": {
                "TableName": DEPLOYMENT_INTENT_TABLE,
                "Key": _ddb_item({"record_id": str(prepared["record_id"])}),
                "UpdateExpression": (
                    "SET #state = :applying, apply_attempt_id = :attempt, "
                    "apply_started_at = :started"
                ),
                "ConditionExpression": (
                    "#state = :prepared "
                    "AND plan_sha256 = :plan "
                    "AND deployment_context_sha256 = :context "
                    "AND receipt_claims_sha256 = :claims "
                    "AND shared_ledger_sha256 = :shared_ledger "
                    "AND gate_query_sha256 = :gate_query "
                    "AND terraform_context_sha256 = :terraform_context "
                    "AND control_commit = :control_commit "
                    "AND authorization_expires_at > :now"
                ),
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": _ddb_item(
                    {
                        ":prepared": "PREPARED",
                        ":applying": "APPLYING",
                        ":attempt": apply_attempt_id,
                        ":started": now_text,
                        ":plan": metadata["plan_sha256"],
                        ":context": metadata["deployment_context_sha256"],
                        ":claims": metadata["receipt_claims_sha256"],
                        ":shared_ledger": metadata["shared_ledger_sha256"],
                        ":gate_query": metadata["gate_query_sha256"],
                        ":terraform_context": prepared["terraform_context_sha256"],
                        ":control_commit": expected_control_commit,
                        ":now": now_epoch,
                    }
                ),
            }
        },
    ]
    _aws(
        "dynamodb",
        "transact-write-items",
        "--region",
        REGION,
        "--transact-items",
        json.dumps(transaction, sort_keys=True, separators=(",", ":")),
        "--client-request-token",
        _dynamodb_transaction_token(
            apply_attempt_id,
            phase="begin-apply",
        ),
        "--return-consumed-capacity",
        "NONE",
        "--output",
        "json",
    )


def acquire_deployment_lock(
    plan_path: Path,
    *,
    apply_attempt_id: str,
    control_commit: str,
    now: dt.datetime | None = None,
    plan_json: Mapping[str, Any] | None = None,
) -> dict[str, str | int]:
    metadata = deployment_plan_metadata(plan_path, plan_json=plan_json)
    attempt_id = _uuid4(apply_attempt_id, label="apply attempt ID")
    if attempt_id == metadata["intent_id"]:
        raise EvidenceError("apply attempt ID must differ from deployment intent ID")
    current = _utc_now(now)
    prepared = _dynamodb_get(f"intent#{metadata['intent_id']}")
    if prepared is None:
        raise EvidenceError("prepared deployment intent does not exist")
    _validate_prepared_intent(
        prepared,
        metadata=metadata,
        claims_sha256=metadata["receipt_claims_sha256"],
        now=current,
        expected_control_commit=control_commit,
    )
    item = _deployment_lock_item(
        metadata=metadata,
        terraform_context_sha256=str(prepared["terraform_context_sha256"]),
        apply_attempt_id=attempt_id,
        now=current,
    )
    try:
        _dynamodb_transact_begin_apply(
            prepared=prepared,
            metadata=metadata,
            lock_item=item,
            apply_attempt_id=attempt_id,
            control_commit=control_commit,
            now=current,
        )
    except EvidenceError as exc:
        current_intent = _dynamodb_get(f"intent#{metadata['intent_id']}")
        existing = _dynamodb_get(DEPLOYMENT_LOCK_RECORD_ID)
        if current_intent is not None and existing is not None:
            try:
                _validate_applying_intent(
                    current_intent,
                    metadata=metadata,
                    claims_sha256=metadata["receipt_claims_sha256"],
                    apply_attempt_id=attempt_id,
                    now=current,
                    expected_control_commit=control_commit,
                )
                _validate_deployment_lock(
                    existing,
                    metadata=metadata,
                    apply_attempt_id=attempt_id,
                    now=current,
                )
                return existing
            except EvidenceError:
                pass
        if existing is not None and int(existing.get("lease_expires_at", 0)) > int(
            current.timestamp()
        ):
            raise EvidenceError(
                "another image/Terraform automation apply holds the shared lock"
            ) from exc
        if current_intent is not None and current_intent.get("state") != "PREPARED":
            raise EvidenceError(
                "deployment intent has already started a one-time apply attempt"
            ) from exc
        raise EvidenceError(
            "shared lock and one-time apply attempt could not be acquired atomically"
        ) from exc
    confirmed = _dynamodb_get(DEPLOYMENT_LOCK_RECORD_ID)
    if confirmed is None:
        raise EvidenceError("shared image release apply lock could not be confirmed")
    applying = _dynamodb_get(f"intent#{metadata['intent_id']}")
    if applying is None:
        raise EvidenceError("one-time deployment apply attempt could not be confirmed")
    _validate_applying_intent(
        applying,
        metadata=metadata,
        claims_sha256=metadata["receipt_claims_sha256"],
        apply_attempt_id=attempt_id,
        now=current,
        expected_control_commit=control_commit,
    )
    _validate_deployment_lock(
        confirmed,
        metadata=metadata,
        apply_attempt_id=attempt_id,
        now=current,
    )
    return confirmed


def _verified_receipt_claims_for_saved_plan(
    *,
    metadata: Mapping[str, str],
    query: Mapping[str, Any],
    now: dt.datetime,
) -> list[str]:
    verified = _terraform_gate(query, now=now)
    (
        consumer_manifest,
        receipt_catalog,
        consumer_receipt_bindings,
        contracts,
        _,
        application,
        shared_generation_ledger,
        _,
        _,
        intent_id,
    ) = _parse_terraform_gate_query(query)
    release_channels = json.loads(
        verified["release_channels_json"],
        object_pairs_hook=_reject_duplicate_keys,
    )
    context_sha256, receipt_claim_ids, claims_sha256 = _deployment_binding(
        consumer_manifest=consumer_manifest,
        receipt_catalog=receipt_catalog,
        consumer_receipt_bindings=consumer_receipt_bindings,
        contracts=contracts,
        application=application,
        shared_generation_ledger=shared_generation_ledger,
        release_channels=_mapping(
            release_channels,
            label="apply-time verified release channels",
        ),
        intent_id=intent_id,
    )
    query_sha256 = hashlib.sha256(canonical_bytes(query)).hexdigest()
    # _epoch_seconds validates the canonical form and hands back the string, so it
    # has to be widened before meeting the saved integer -- "1784269500" never
    # equals 1784269500, and that silently failed every receipt-required apply.
    receipt_expires_at = int(
        _epoch_seconds(
            verified["receipt_authorization_expires_at"],
            label="apply-time receipt authorization expiry",
        )
    )
    saved_receipt_expires_at = int(metadata["receipt_authorization_expires_at"])
    if verified["deployment_mode"] == "receipt-required":
        authorization_matches = receipt_expires_at == saved_receipt_expires_at
    else:
        now_epoch = int(now.timestamp())
        authorization_matches = (
            now_epoch
            < saved_receipt_expires_at
            <= now_epoch + MAX_DEPLOYMENT_INTENT_LIFETIME_SECONDS
        )
    if (
        intent_id != metadata["intent_id"]
        or query_sha256 != metadata["gate_query_sha256"]
        or not authorization_matches
        or verified["deployment_mode"] != metadata["deployment_mode"]
        or verified["deployment_context_sha256"] != context_sha256
        or verified["receipt_claims_sha256"] != claims_sha256
        or context_sha256 != metadata["deployment_context_sha256"]
        or claims_sha256 != metadata["receipt_claims_sha256"]
        or hashlib.sha256(canonical_bytes(shared_generation_ledger)).hexdigest()
        != metadata["shared_ledger_sha256"]
    ):
        raise EvidenceError("apply-time evidence does not match the saved deployment plan")
    return receipt_claim_ids


def validate_deployment_preflight(
    plan_path: Path,
    *,
    terraform_context_path: Path,
    apply_attempt_id: str,
    control_commit: str,
    now: dt.datetime | None = None,
    plan_json: Mapping[str, Any] | None = None,
) -> dict[str, str | int]:
    metadata = deployment_plan_metadata(plan_path, plan_json=plan_json)
    attempt_id = _uuid4(apply_attempt_id, label="apply attempt ID")
    current = _utc_now(now)
    context = terraform_context_metadata(terraform_context_path)
    applying = _dynamodb_get(f"intent#{metadata['intent_id']}")
    if applying is None:
        raise EvidenceError("deployment apply attempt does not exist")
    _validate_applying_intent(
        applying,
        metadata=metadata,
        claims_sha256=metadata["receipt_claims_sha256"],
        apply_attempt_id=attempt_id,
        now=current,
        expected_control_commit=control_commit,
    )
    for key, value in context.items():
        if applying.get(key) != value:
            raise EvidenceError(
                "live backend/workspace/state/address ownership differs from the plan"
            )
    lock = _dynamodb_get(DEPLOYMENT_LOCK_RECORD_ID)
    if lock is None:
        raise EvidenceError("shared image release apply lock does not exist")
    _validate_deployment_lock(
        lock,
        metadata=metadata,
        apply_attempt_id=attempt_id,
        now=current,
        terraform_context_sha256=str(context["terraform_context_sha256"]),
    )
    try:
        query_value = json.loads(
            metadata["gate_query_json"],
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise EvidenceError("saved Terraform deployment gate query is invalid") from exc
    receipt_claim_ids = _verified_receipt_claims_for_saved_plan(
        metadata=metadata,
        query=_mapping(
            query_value,
            label="saved Terraform deployment gate query",
        ),
        now=current,
    )
    # Re-sample production time after the remote KMS/S3/ECR verification. The
    # transaction below conditionally checks the same capped authorization,
    # lock, plan, context, query, attempt, and every one-use receipt claim.
    consume_time = current if now is not None else _utc_now()
    return _consume_applying_deployment_intent(
        metadata=metadata,
        receipt_claim_ids=receipt_claim_ids,
        apply_attempt_id=attempt_id,
        now=consume_time,
        expected_control_commit=control_commit,
        expected_terraform_context_sha256=str(context["terraform_context_sha256"]),
    )


def heartbeat_deployment_lock(
    plan_path: Path,
    *,
    apply_attempt_id: str,
    now: dt.datetime | None = None,
    plan_json: Mapping[str, Any] | None = None,
) -> dict[str, str | int]:
    metadata = deployment_plan_metadata(plan_path, plan_json=plan_json)
    attempt_id = _uuid4(apply_attempt_id, label="apply attempt ID")
    current = _utc_now(now)
    new_lease = int((current + dt.timedelta(seconds=DEPLOYMENT_LOCK_LEASE_SECONDS)).timestamp())
    try:
        _aws(
            "dynamodb",
            "update-item",
            "--region",
            REGION,
            "--table-name",
            DEPLOYMENT_INTENT_TABLE,
            "--key",
            json.dumps(
                _ddb_item({"record_id": DEPLOYMENT_LOCK_RECORD_ID}),
                separators=(",", ":"),
            ),
            "--update-expression",
            "SET lease_expires_at = :lease",
            "--condition-expression",
            (
                "#state = :locked AND intent_id = :intent "
                "AND plan_sha256 = :plan AND apply_attempt_id = :attempt "
                "AND lease_expires_at > :now"
            ),
            "--expression-attribute-names",
            json.dumps({"#state": "state"}, separators=(",", ":")),
            "--expression-attribute-values",
            json.dumps(
                _ddb_item(
                    {
                        ":locked": "LOCKED",
                        ":intent": metadata["intent_id"],
                        ":plan": metadata["plan_sha256"],
                        ":attempt": attempt_id,
                        ":now": int(current.timestamp()),
                        ":lease": new_lease,
                    }
                ),
                separators=(",", ":"),
            ),
            "--return-values",
            "NONE",
            "--output",
            "json",
        )
    except EvidenceError as exc:
        raise EvidenceError("shared image release apply lock heartbeat failed") from exc
    lock = _dynamodb_get(DEPLOYMENT_LOCK_RECORD_ID)
    if lock is None:
        raise EvidenceError("shared image release apply lock disappeared")
    _validate_deployment_lock(
        lock,
        metadata=metadata,
        apply_attempt_id=attempt_id,
        now=current,
    )
    return lock


def release_deployment_lock(
    plan_path: Path,
    *,
    apply_attempt_id: str,
    plan_json: Mapping[str, Any] | None = None,
) -> None:
    metadata = deployment_plan_metadata(plan_path, plan_json=plan_json)
    attempt_id = _uuid4(apply_attempt_id, label="apply attempt ID")
    try:
        _aws(
            "dynamodb",
            "delete-item",
            "--region",
            REGION,
            "--table-name",
            DEPLOYMENT_INTENT_TABLE,
            "--key",
            json.dumps(
                _ddb_item({"record_id": DEPLOYMENT_LOCK_RECORD_ID}),
                separators=(",", ":"),
            ),
            "--condition-expression",
            "intent_id = :intent AND plan_sha256 = :plan AND apply_attempt_id = :attempt",
            "--expression-attribute-values",
            json.dumps(
                _ddb_item(
                    {
                        ":intent": metadata["intent_id"],
                        ":plan": metadata["plan_sha256"],
                        ":attempt": attempt_id,
                    }
                ),
                separators=(",", ":"),
            ),
            "--return-consumed-capacity",
            "NONE",
            "--output",
            "json",
        )
    except EvidenceError as exc:
        raise EvidenceError("shared image release apply lock release failed") from exc


def prepare_deployment_intent(
    plan_path: Path,
    *,
    control_commit: str,
    terraform_context_path: Path,
    now: dt.datetime | None = None,
    plan_json: Mapping[str, Any] | None = None,
) -> dict[str, str | int]:
    metadata = deployment_plan_metadata(plan_path, plan_json=plan_json)
    terraform_context = terraform_context_metadata(terraform_context_path)
    if (
        metadata["plan_transition_sha256"] != terraform_context["plan_transition_sha256"]
        or metadata["consumer_manifest_sha256"] != terraform_context["consumer_manifest_sha256"]
        or metadata["release_evidence_binding_sha256"]
        != terraform_context["release_evidence_binding_sha256"]
        or metadata["deployment_mode"] != terraform_context["deployment_mode"]
    ):
        raise EvidenceError(
            "Terraform runtime context consumer manifest differs from the saved plan"
        )
    current = _utc_now(now)
    receipt_expires_at = int(metadata["receipt_authorization_expires_at"])
    if int(current.timestamp()) >= receipt_expires_at:
        raise EvidenceError("saved Terraform plan release receipt authorization is already stale")
    expires_at = min(
        int((current + dt.timedelta(seconds=MAX_DEPLOYMENT_INTENT_LIFETIME_SECONDS)).timestamp()),
        receipt_expires_at,
    )
    audit_expires = current + dt.timedelta(seconds=DEPLOYMENT_INTENT_AUDIT_TTL_SECONDS)
    item: dict[str, str | int] = {
        "record_id": f"intent#{metadata['intent_id']}",
        "record_type": DEPLOYMENT_INTENT_KIND,
        "schema_version": DEPLOYMENT_INTENT_SCHEMA,
        "intent_id": metadata["intent_id"],
        "state": "PREPARED",
        "plan_sha256": metadata["plan_sha256"],
        "deployment_context_sha256": metadata["deployment_context_sha256"],
        "receipt_claims_sha256": metadata["receipt_claims_sha256"],
        "shared_ledger_sha256": metadata["shared_ledger_sha256"],
        "gate_query_sha256": metadata["gate_query_sha256"],
        **terraform_context,
        "control_commit": _sha1(control_commit, label="deployment control commit"),
        "prepared_at": current.isoformat().replace("+00:00", "Z"),
        "authorization_expires_at": expires_at,
        "audit_expires_at": int(audit_expires.timestamp()),
    }
    _dynamodb_put_prepared_intent(item)
    return item


def _deployment_intent_base_keys() -> set[str]:
    return {
        "record_id",
        "record_type",
        "schema_version",
        "intent_id",
        "state",
        "plan_sha256",
        "deployment_context_sha256",
        "receipt_claims_sha256",
        "shared_ledger_sha256",
        "gate_query_sha256",
        "terraform_context_sha256",
        "backend_workspace_sha256",
        "state_lineage",
        "state_serial",
        "state_addresses_sha256",
        "plan_addresses_sha256",
        "runtime_images_sha256",
        "plan_transition_sha256",
        "consumer_manifest_sha256",
        "consumer_count",
        "consumer_comparison_sha256",
        "release_evidence_binding_sha256",
        "deployment_mode",
        "control_commit",
        "prepared_at",
        "authorization_expires_at",
        "audit_expires_at",
    }


def _validate_deployment_intent_binding(
    item: Mapping[str, str | int],
    *,
    metadata: Mapping[str, str],
    claims_sha256: str,
    now: dt.datetime,
    expected_state: str,
    expected_control_commit: str | None = None,
) -> None:
    if (
        item["record_id"] != f"intent#{metadata['intent_id']}"
        or item["record_type"] != DEPLOYMENT_INTENT_KIND
        or item["schema_version"] != DEPLOYMENT_INTENT_SCHEMA
        or item["intent_id"] != metadata["intent_id"]
    ):
        raise EvidenceError("deployment intent identity is invalid")
    if item["state"] != expected_state:
        raise EvidenceError(f"deployment intent is not in the required {expected_state} state")
    if (
        item["plan_sha256"] != metadata["plan_sha256"]
        or item["deployment_context_sha256"] != metadata["deployment_context_sha256"]
        or item["receipt_claims_sha256"] != claims_sha256
        or claims_sha256 != metadata["receipt_claims_sha256"]
        or item["shared_ledger_sha256"] != metadata["shared_ledger_sha256"]
        or item["gate_query_sha256"] != metadata["gate_query_sha256"]
        or item["plan_transition_sha256"] != metadata["plan_transition_sha256"]
        or item["consumer_manifest_sha256"] != metadata["consumer_manifest_sha256"]
        or item["release_evidence_binding_sha256"] != metadata["release_evidence_binding_sha256"]
        or item["deployment_mode"] != metadata["deployment_mode"]
    ):
        raise EvidenceError("deployment intent does not bind this saved plan")
    for context_hash_name in (
        "terraform_context_sha256",
        "backend_workspace_sha256",
        "state_addresses_sha256",
        "plan_addresses_sha256",
        "runtime_images_sha256",
        "plan_transition_sha256",
        "consumer_manifest_sha256",
        "consumer_comparison_sha256",
        "release_evidence_binding_sha256",
        "gate_query_sha256",
    ):
        _sha256(item[context_hash_name], label=f"deployment {context_hash_name}")
    if item["deployment_mode"] not in {
        "receipt-required",
        "no-image-transition",
    }:
        raise EvidenceError("deployment intent mode is invalid")
    if item["consumer_count"] != 8:
        raise EvidenceError("deployment intent consumer count is invalid")
    _string(item["state_lineage"], label="deployment Terraform state lineage")
    if (
        not isinstance(item["state_serial"], int)
        or isinstance(item["state_serial"], bool)
        or item["state_serial"] < 0
    ):
        raise EvidenceError("deployment Terraform state serial is invalid")
    control_commit = _sha1(
        item["control_commit"],
        label="deployment control commit",
    )
    if expected_control_commit is not None and control_commit != _sha1(
        expected_control_commit,
        label="apply control commit",
    ):
        raise EvidenceError("deployment intent control commit differs from the apply checkout")
    _timestamp(item["prepared_at"], label="prepared_at")
    authorization_expires_at = item["authorization_expires_at"]
    audit_expires_at = item["audit_expires_at"]
    if (
        not isinstance(authorization_expires_at, int)
        or not isinstance(audit_expires_at, int)
        or int(now.timestamp()) >= authorization_expires_at
        or authorization_expires_at > int(metadata["receipt_authorization_expires_at"])
        or audit_expires_at <= authorization_expires_at
    ):
        raise EvidenceError("prepared deployment intent is stale")


def _validate_prepared_intent(
    item: Mapping[str, str | int],
    *,
    metadata: Mapping[str, str],
    claims_sha256: str,
    now: dt.datetime,
    expected_control_commit: str | None = None,
) -> None:
    _exact_keys(
        item,
        _deployment_intent_base_keys(),
        label="prepared deployment intent",
    )
    _validate_deployment_intent_binding(
        item,
        metadata=metadata,
        claims_sha256=claims_sha256,
        now=now,
        expected_state="PREPARED",
        expected_control_commit=expected_control_commit,
    )


def _validate_applying_intent(
    item: Mapping[str, str | int],
    *,
    metadata: Mapping[str, str],
    claims_sha256: str,
    apply_attempt_id: str,
    now: dt.datetime,
    expected_control_commit: str | None = None,
) -> None:
    _exact_keys(
        item,
        _deployment_intent_base_keys()
        | {
            "apply_attempt_id",
            "apply_started_at",
        },
        label="applying deployment intent",
    )
    attempt_id = _uuid4(apply_attempt_id, label="apply attempt ID")
    if item["apply_attempt_id"] != attempt_id:
        raise EvidenceError("deployment intent belongs to another apply attempt")
    _timestamp(item["apply_started_at"], label="apply_started_at")
    _validate_deployment_intent_binding(
        item,
        metadata=metadata,
        claims_sha256=claims_sha256,
        now=now,
        expected_state="APPLYING",
        expected_control_commit=expected_control_commit,
    )


def _dynamodb_transact_consume(
    *,
    applying: Mapping[str, str | int],
    metadata: Mapping[str, str],
    receipt_claim_ids: list[str],
    apply_attempt_id: str,
    now: dt.datetime,
) -> None:
    now_text = now.isoformat().replace("+00:00", "Z")
    now_epoch = int(now.timestamp())
    audit_expires_at = int(applying["audit_expires_at"])
    transaction: list[dict[str, Any]] = [
        {
            "ConditionCheck": {
                "TableName": DEPLOYMENT_INTENT_TABLE,
                "Key": _ddb_item({"record_id": DEPLOYMENT_LOCK_RECORD_ID}),
                "ConditionExpression": (
                    "#state = :locked "
                    "AND intent_id = :intent "
                    "AND plan_sha256 = :plan "
                    "AND deployment_context_sha256 = :context "
                    "AND receipt_claims_sha256 = :claims "
                    "AND gate_query_sha256 = :gate_query "
                    "AND terraform_context_sha256 = :terraform_context "
                    "AND apply_attempt_id = :attempt "
                    "AND lease_expires_at > :now_epoch"
                ),
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": _ddb_item(
                    {
                        ":locked": "LOCKED",
                        ":intent": metadata["intent_id"],
                        ":plan": metadata["plan_sha256"],
                        ":context": metadata["deployment_context_sha256"],
                        ":claims": metadata["receipt_claims_sha256"],
                        ":gate_query": metadata["gate_query_sha256"],
                        ":terraform_context": applying["terraform_context_sha256"],
                        ":attempt": apply_attempt_id,
                        ":now_epoch": now_epoch,
                    }
                ),
            }
        },
        {
            "Update": {
                "TableName": DEPLOYMENT_INTENT_TABLE,
                "Key": _ddb_item({"record_id": str(applying["record_id"])}),
                "UpdateExpression": ("SET #state = :consumed, consumed_at = :consumed_at"),
                "ConditionExpression": (
                    "#state = :applying "
                    "AND apply_attempt_id = :attempt "
                    "AND plan_sha256 = :plan "
                    "AND deployment_context_sha256 = :context "
                    "AND receipt_claims_sha256 = :claims "
                    "AND shared_ledger_sha256 = :shared_ledger "
                    "AND gate_query_sha256 = :gate_query "
                    "AND terraform_context_sha256 = :terraform_context "
                    "AND control_commit = :control_commit "
                    "AND authorization_expires_at > :now_epoch"
                ),
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": _ddb_item(
                    {
                        ":applying": "APPLYING",
                        ":consumed": "CONSUMED",
                        ":consumed_at": now_text,
                        ":attempt": apply_attempt_id,
                        ":plan": metadata["plan_sha256"],
                        ":context": metadata["deployment_context_sha256"],
                        ":claims": metadata["receipt_claims_sha256"],
                        ":shared_ledger": metadata["shared_ledger_sha256"],
                        ":gate_query": metadata["gate_query_sha256"],
                        ":terraform_context": applying["terraform_context_sha256"],
                        ":control_commit": applying["control_commit"],
                        ":now_epoch": now_epoch,
                    }
                ),
            }
        },
    ]
    for claim_id in receipt_claim_ids:
        claim_item: dict[str, str | int] = {
            "record_id": f"receipt#{claim_id}",
            "record_type": "teamagent.release-receipt-claim",
            "schema_version": DEPLOYMENT_INTENT_SCHEMA,
            "receipt_claim_id": claim_id,
            "intent_id": metadata["intent_id"],
            "plan_sha256": metadata["plan_sha256"],
            "deployment_context_sha256": metadata["deployment_context_sha256"],
            "receipt_claims_sha256": metadata["receipt_claims_sha256"],
            "gate_query_sha256": metadata["gate_query_sha256"],
            "terraform_context_sha256": applying["terraform_context_sha256"],
            "apply_attempt_id": apply_attempt_id,
            "consumed_at": now_text,
            "audit_expires_at": audit_expires_at,
        }
        transaction.append(
            {
                "Put": {
                    "TableName": DEPLOYMENT_INTENT_TABLE,
                    "Item": _ddb_item(claim_item),
                    "ConditionExpression": "attribute_not_exists(record_id)",
                }
            }
        )
    _aws(
        "dynamodb",
        "transact-write-items",
        "--region",
        REGION,
        "--transact-items",
        json.dumps(transaction, sort_keys=True, separators=(",", ":")),
        "--client-request-token",
        _dynamodb_transaction_token(
            apply_attempt_id,
            phase="consume-authorization",
        ),
        "--return-consumed-capacity",
        "NONE",
        "--output",
        "json",
    )


def _confirmed_consumed_authorization(
    *,
    intent: Mapping[str, str | int],
    metadata: Mapping[str, str],
    receipt_claim_ids: list[str],
    apply_attempt_id: str,
) -> bool:
    if (
        intent.get("record_id") != f"intent#{metadata['intent_id']}"
        or intent.get("intent_id") != metadata["intent_id"]
        or intent.get("state") != "CONSUMED"
        or intent.get("apply_attempt_id") != apply_attempt_id
        or intent.get("plan_sha256") != metadata["plan_sha256"]
        or intent.get("deployment_context_sha256") != metadata["deployment_context_sha256"]
        or intent.get("receipt_claims_sha256") != metadata["receipt_claims_sha256"]
        or intent.get("shared_ledger_sha256") != metadata["shared_ledger_sha256"]
        or intent.get("gate_query_sha256") != metadata["gate_query_sha256"]
    ):
        return False
    consumed_at = intent.get("consumed_at")
    if not isinstance(consumed_at, str):
        return False
    try:
        _timestamp(consumed_at, label="consumed deployment intent")
    except EvidenceError:
        return False
    for claim_id in receipt_claim_ids:
        claim = _dynamodb_get(f"receipt#{claim_id}")
        if claim is None:
            return False
        expected = {
            "record_id": f"receipt#{claim_id}",
            "record_type": "teamagent.release-receipt-claim",
            "schema_version": DEPLOYMENT_INTENT_SCHEMA,
            "receipt_claim_id": claim_id,
            "intent_id": metadata["intent_id"],
            "plan_sha256": metadata["plan_sha256"],
            "deployment_context_sha256": metadata["deployment_context_sha256"],
            "receipt_claims_sha256": metadata["receipt_claims_sha256"],
            "gate_query_sha256": metadata["gate_query_sha256"],
            "terraform_context_sha256": intent.get("terraform_context_sha256"),
            "apply_attempt_id": apply_attempt_id,
            "consumed_at": consumed_at,
            "audit_expires_at": intent.get("audit_expires_at"),
        }
        if dict(claim) != expected:
            return False
    return True


def _consume_applying_deployment_intent(
    *,
    metadata: Mapping[str, str],
    receipt_claim_ids: list[str],
    apply_attempt_id: str,
    now: dt.datetime | None = None,
    expected_control_commit: str | None = None,
    expected_terraform_context_sha256: str | None = None,
) -> dict[str, str | int]:
    attempt_id = _uuid4(apply_attempt_id, label="apply attempt ID")
    if attempt_id == metadata["intent_id"]:
        raise EvidenceError("apply attempt ID must differ from deployment intent ID")
    deployment_mode = metadata.get("deployment_mode")
    if deployment_mode not in {"receipt-required", "no-image-transition"}:
        raise EvidenceError("deployment intent mode is invalid")
    normalized_claims = _canonical_receipt_claim_ids(
        receipt_claim_ids,
        allow_empty=deployment_mode == "no-image-transition",
    )
    if deployment_mode == "no-image-transition" and normalized_claims:
        raise EvidenceError("no-image-transition deployment cannot consume receipt claims")
    claims_sha256 = hashlib.sha256(canonical_bytes(normalized_claims)).hexdigest()
    current = _utc_now(now)
    record_id = f"intent#{metadata['intent_id']}"
    applying = _dynamodb_get(record_id)
    if applying is None:
        raise EvidenceError("deployment apply attempt does not exist")
    if applying.get("state") != "APPLYING":
        raise EvidenceError("deployment intent has already started or been consumed")
    _validate_applying_intent(
        applying,
        metadata=metadata,
        claims_sha256=claims_sha256,
        apply_attempt_id=attempt_id,
        now=current,
        expected_control_commit=expected_control_commit,
    )
    if expected_terraform_context_sha256 is not None and applying[
        "terraform_context_sha256"
    ] != _sha256(
        expected_terraform_context_sha256,
        label="live Terraform context SHA-256",
    ):
        raise EvidenceError("deployment apply attempt does not bind the live Terraform context")
    lock = _dynamodb_get(DEPLOYMENT_LOCK_RECORD_ID)
    if lock is None:
        raise EvidenceError("shared image release apply lock does not exist")
    _validate_deployment_lock(
        lock,
        metadata=metadata,
        apply_attempt_id=attempt_id,
        now=current,
        terraform_context_sha256=(
            expected_terraform_context_sha256 or str(applying["terraform_context_sha256"])
        ),
    )
    try:
        _dynamodb_transact_consume(
            applying=applying,
            metadata=metadata,
            receipt_claim_ids=normalized_claims,
            apply_attempt_id=attempt_id,
            now=current,
        )
    except EvidenceError as exc:
        current_intent = _dynamodb_get(record_id)
        if current_intent is not None and _confirmed_consumed_authorization(
            intent=current_intent,
            metadata=metadata,
            receipt_claim_ids=normalized_claims,
            apply_attempt_id=attempt_id,
        ):
            return current_intent
        if current_intent is not None and current_intent.get("state") != "APPLYING":
            raise EvidenceError(
                "deployment intent has already been consumed by another attempt"
            ) from exc
        if any(_dynamodb_get(f"receipt#{claim_id}") is not None for claim_id in normalized_claims):
            raise EvidenceError("release receipt has already authorized a deployment") from exc
        raise EvidenceError("atomic deployment authorization failed closed") from exc
    consumed = _dynamodb_get(record_id)
    if consumed is None or not _confirmed_consumed_authorization(
        intent=consumed,
        metadata=metadata,
        receipt_claim_ids=normalized_claims,
        apply_attempt_id=attempt_id,
    ):
        raise EvidenceError("consumed deployment intent could not be confirmed")
    return consumed


def consume_deployment_intent(
    plan_path: Path,
    *,
    query: Mapping[str, Any],
    apply_attempt_id: str,
    now: dt.datetime | None = None,
    plan_json: Mapping[str, Any] | None = None,
) -> dict[str, str | int]:
    metadata = deployment_plan_metadata(plan_path, plan_json=plan_json)
    current = _utc_now(now)
    receipt_claim_ids = _verified_receipt_claims_for_saved_plan(
        metadata=metadata,
        query=query,
        now=current,
    )
    return _consume_applying_deployment_intent(
        metadata=metadata,
        receipt_claim_ids=receipt_claim_ids,
        apply_attempt_id=apply_attempt_id,
        now=current,
    )


def _dynamodb_update_outcome(
    *,
    metadata: Mapping[str, str],
    apply_attempt_id: str,
    outcome: str,
    now: dt.datetime,
) -> None:
    source_condition = "#state = :consumed"
    values: dict[str, str] = {
        ":outcome": outcome,
        ":recorded": now.isoformat().replace("+00:00", "Z"),
        ":consumed": "CONSUMED",
        ":attempt": apply_attempt_id,
        ":plan": metadata["plan_sha256"],
    }
    if outcome == "RECONCILE_REQUIRED":
        source_condition = "(#state = :consumed OR #state = :applying)"
        values[":applying"] = "APPLYING"
    _aws(
        "dynamodb",
        "update-item",
        "--region",
        REGION,
        "--table-name",
        DEPLOYMENT_INTENT_TABLE,
        "--key",
        json.dumps(
            _ddb_item({"record_id": f"intent#{metadata['intent_id']}"}),
            separators=(",", ":"),
        ),
        "--update-expression",
        "SET #state = :outcome, outcome_recorded_at = :recorded",
        "--condition-expression",
        (f"{source_condition} AND apply_attempt_id = :attempt AND plan_sha256 = :plan"),
        "--expression-attribute-names",
        json.dumps({"#state": "state"}, separators=(",", ":")),
        "--expression-attribute-values",
        json.dumps(
            _ddb_item(values),
            separators=(",", ":"),
        ),
        "--return-values",
        "NONE",
        "--output",
        "json",
    )


def mark_deployment_intent_outcome(
    plan_path: Path,
    *,
    apply_attempt_id: str,
    outcome: str,
    now: dt.datetime | None = None,
    plan_json: Mapping[str, Any] | None = None,
) -> dict[str, str | int]:
    desired = {
        "applied": "APPLIED",
        "reconcile-required": "RECONCILE_REQUIRED",
    }.get(outcome)
    if desired is None:
        raise EvidenceError("deployment outcome is not allowlisted")
    attempt_id = _uuid4(apply_attempt_id, label="apply attempt ID")
    metadata = deployment_plan_metadata(plan_path, plan_json=plan_json)
    record_id = f"intent#{metadata['intent_id']}"
    current = _dynamodb_get(record_id)
    if current is None:
        raise EvidenceError("deployment intent does not exist")
    if (
        current.get("state") == desired
        and current.get("apply_attempt_id") == attempt_id
        and current.get("plan_sha256") == metadata["plan_sha256"]
    ):
        return current
    allowed_sources = {"CONSUMED", "APPLYING"} if desired == "RECONCILE_REQUIRED" else {"CONSUMED"}
    if (
        current.get("state") not in allowed_sources
        or current.get("apply_attempt_id") != attempt_id
        or current.get("plan_sha256") != metadata["plan_sha256"]
    ):
        raise EvidenceError("deployment outcome cannot change this intent state")
    _dynamodb_update_outcome(
        metadata=metadata,
        apply_attempt_id=attempt_id,
        outcome=desired,
        now=_utc_now(now),
    )
    updated = _dynamodb_get(record_id)
    if updated is None or updated.get("state") != desired:
        raise EvidenceError("deployment outcome could not be confirmed")
    return updated


def _environment_gate_query() -> Mapping[str, Any]:
    raw = os.environ.get("TEAMAGENT_DEPLOYMENT_GATE_QUERY", "")
    if not raw:
        raise EvidenceError("apply-time deployment gate query is missing")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise EvidenceError("apply-time deployment gate query is invalid") from exc
    return _mapping(value, label="apply-time deployment gate query")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    source = commands.add_parser("create-source-declaration")
    source.add_argument("--project-arn", required=True)
    source.add_argument("--build-id", required=True)
    source.add_argument("--commit", required=True)
    source.add_argument("--source-version", required=True)
    source.add_argument("--source-sha256", required=True)
    source.add_argument("--manifest-sha256", required=True)
    source.add_argument("--build-context-key", required=True)
    source.add_argument("--build-context-version", required=True)
    source.add_argument("--build-context-sha256", required=True)
    source.add_argument("--source-tree-oid", required=True)
    source.add_argument("--remote-head-oid", required=True)
    source.add_argument("--remote-base-oid", required=True)
    source.add_argument("--merge-base-oid", required=True)
    source.add_argument("--app-version", required=True)
    source.add_argument("--app-sha256", required=True)
    source.add_argument("--vault-manifest-sha256", required=True)
    source.add_argument("--build-inputs-sha256", required=True)
    source.add_argument("--contract-sha256", required=True)
    source.add_argument("--approval-evidence-json", required=True)
    source.add_argument("--output", type=Path, required=True)

    verify_source = commands.add_parser("verify-source-declaration")
    verify_source.add_argument("--declaration", type=Path, required=True)
    verify_source.add_argument("--expected-commit", required=True)
    verify_source.add_argument("--expected-source-version", required=True)
    verify_source.add_argument("--expected-app-version", required=True)
    verify_source.add_argument("--expected-app-sha256", required=True)
    verify_source.add_argument("--expected-vault-manifest-sha256", required=True)
    verify_source.add_argument("--expected-build-inputs-sha256", required=True)
    verify_source.add_argument("--expected-contract-sha256", required=True)
    verify_source.add_argument("--expected-build-context-sha256")
    verify_source.add_argument("--expected-build-context-version")
    verify_source.add_argument("--expected-remote-base-oid")

    verify_source_approval = commands.add_parser("verify-source-approval-binding")
    verify_source_approval.add_argument("--declaration", type=Path, required=True)
    verify_source_approval.add_argument("--expected-commit", required=True)
    verify_source_approval.add_argument("--expected-contract-sha256", required=True)
    verify_source_approval.add_argument(
        "--expected-approval-evidence-json",
        required=True,
    )

    receipt = commands.add_parser("verify-release-receipt")
    receipt.add_argument("--receipt", type=Path, required=True)
    receipt.add_argument("--expected-pipeline", choices=sorted(PIPELINES), required=True)
    receipt.add_argument("--expected-commit", required=True)
    receipt.add_argument("--expected-contract-sha256", required=True)
    receipt.add_argument(
        "--allowed-channel",
        action="append",
        choices=("verified-candidate", "active", "rollback"),
        required=True,
    )
    receipt.add_argument("--now")

    locator = commands.add_parser("verify-release-locator")
    locator.add_argument("--receipt", type=Path, required=True)
    locator.add_argument("--expected-pipeline", choices=sorted(PIPELINES), required=True)
    locator.add_argument("--expected-contract-sha256", required=True)

    create_receipt = commands.add_parser("create-release-receipt")
    create_receipt.add_argument("--pipeline", choices=sorted(PIPELINES), required=True)
    create_receipt.add_argument(
        "--channel",
        choices=("verified-candidate", "active", "rollback"),
        required=True,
    )
    create_receipt.add_argument("--issued-at", required=True)
    create_receipt.add_argument("--expires-at", required=True)
    create_receipt.add_argument("--build-project-arn", required=True)
    create_receipt.add_argument("--build-id", required=True)
    create_receipt.add_argument("--commit", required=True)
    create_receipt.add_argument("--contract-path", required=True)
    create_receipt.add_argument("--contract-sha256", required=True)
    create_receipt.add_argument("--source-bucket", required=True)
    create_receipt.add_argument("--source-key", required=True)
    create_receipt.add_argument("--source-version", required=True)
    create_receipt.add_argument("--source-sha256", required=True)
    create_receipt.add_argument("--source-signature-key", required=True)
    create_receipt.add_argument("--source-signature-version", required=True)
    create_receipt.add_argument("--subject", action="append", type=Path, required=True)
    create_receipt.add_argument("--approval-evidence-json")
    create_receipt.add_argument("--output", type=Path, required=True)

    authorize_receipt = commands.add_parser("authorize-release-receipt")
    authorize_receipt.add_argument("--locator", type=Path, required=True)
    authorize_receipt.add_argument(
        "--channel",
        choices=("active", "rollback"),
        required=True,
    )
    authorize_receipt.add_argument("--issued-at", required=True)
    authorize_receipt.add_argument("--expires-at", required=True)
    authorize_receipt.add_argument("--output", type=Path, required=True)

    lifecycle = commands.add_parser("verify-lifecycle-preview")
    lifecycle.add_argument("--preview", type=Path, required=True)
    lifecycle.add_argument("--protected-digest", action="append", required=True)

    prepare_intent = commands.add_parser("prepare-deployment-intent")
    prepare_intent.add_argument("--plan", type=Path, required=True)
    prepare_intent.add_argument("--control-commit", required=True)
    prepare_intent.add_argument("--terraform-context", type=Path, required=True)

    acquire_lock = commands.add_parser("acquire-deployment-lock")
    acquire_lock.add_argument("--plan", type=Path, required=True)
    acquire_lock.add_argument("--apply-attempt-id", required=True)
    acquire_lock.add_argument("--control-commit", required=True)

    preflight = commands.add_parser("validate-deployment-preflight")
    preflight.add_argument("--plan", type=Path, required=True)
    preflight.add_argument("--terraform-context", type=Path, required=True)
    preflight.add_argument("--apply-attempt-id", required=True)
    preflight.add_argument("--control-commit", required=True)

    heartbeat = commands.add_parser("heartbeat-deployment-lock")
    heartbeat.add_argument("--plan", type=Path, required=True)
    heartbeat.add_argument("--apply-attempt-id", required=True)

    release_lock = commands.add_parser("release-deployment-lock")
    release_lock.add_argument("--plan", type=Path, required=True)
    release_lock.add_argument("--apply-attempt-id", required=True)

    consume_intent = commands.add_parser("consume-deployment-intent")
    consume_intent.add_argument("--plan", type=Path, required=True)
    consume_intent.add_argument("--apply-attempt-id", required=True)

    outcome = commands.add_parser("mark-deployment-intent-outcome")
    outcome.add_argument("--plan", type=Path, required=True)
    outcome.add_argument("--apply-attempt-id", required=True)
    outcome.add_argument(
        "--outcome",
        choices=("applied", "reconcile-required"),
        required=True,
    )

    approval = commands.add_parser("assert-approved-release")
    approval.add_argument(
        "--operation",
        choices=sorted(APPROVAL_OPERATION_STATES),
        required=True,
    )
    approval.add_argument("--approval-locators-json", required=True)
    approval.add_argument("--approval-signing-key-arn", required=True)
    approval.add_argument("--approval-encryption-key-arn", required=True)
    approval.add_argument("--expected-commit", required=True)
    approval.add_argument("--expected-tree-oid", required=True)
    approval.add_argument("--expected-inner-sha256", required=True)
    approval.add_argument("--expected-outer-sha256", required=True)
    approval.add_argument(
        "--expected-pipeline",
        choices=sorted(APPROVAL_PIPELINES),
        required=True,
    )
    approval.add_argument("--expected-environment", required=True)
    approval.add_argument(
        "--runtime-contract",
        type=Path,
        default=Path("infra/codebuild/teamagent_runtime_contract.json"),
    )
    approval.add_argument(
        "--contract",
        type=Path,
        default=Path("infra/codebuild/teamagent_core_media_release_contract.json"),
    )
    approval.add_argument("--verified-record-out", type=Path)
    approval.add_argument("--now")

    commands.add_parser("terraform-gate")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create-source-declaration":
            value = source_declaration(
                project_arn=args.project_arn,
                build_id=args.build_id,
                commit=args.commit,
                source_version=args.source_version,
                source_sha256=args.source_sha256,
                manifest_sha256=args.manifest_sha256,
                build_context_key=args.build_context_key,
                build_context_version=args.build_context_version,
                build_context_sha256=args.build_context_sha256,
                source_tree_oid=args.source_tree_oid,
                remote_head_oid=args.remote_head_oid,
                remote_base_oid=args.remote_base_oid,
                merge_base_oid=args.merge_base_oid,
                app_version=args.app_version,
                app_sha256=args.app_sha256,
                vault_manifest_sha256=args.vault_manifest_sha256,
                build_inputs_sha256=args.build_inputs_sha256,
                contract_sha256=args.contract_sha256,
                approval_evidence=_json_object_argument(
                    args.approval_evidence_json,
                    label="approval evidence JSON",
                ),
            )
            _write_json(args.output, value)
        elif args.command == "verify-source-declaration":
            validate_source_declaration(
                load_json(args.declaration, label="source declaration"),
                expected_commit=args.expected_commit,
                expected_source_version=args.expected_source_version,
                expected_app_version=args.expected_app_version,
                expected_app_sha256=args.expected_app_sha256,
                expected_vault_manifest_sha256=args.expected_vault_manifest_sha256,
                expected_build_inputs_sha256=args.expected_build_inputs_sha256,
                expected_contract_sha256=args.expected_contract_sha256,
                expected_build_context_sha256=args.expected_build_context_sha256,
                expected_build_context_version=args.expected_build_context_version,
                expected_remote_base_oid=args.expected_remote_base_oid,
            )
        elif args.command == "verify-source-approval-binding":
            tree_oid = verify_source_approval_binding(
                load_json(args.declaration, label="source declaration"),
                expected_commit=args.expected_commit,
                expected_contract_sha256=args.expected_contract_sha256,
                expected_approval_evidence=_json_object_argument(
                    args.expected_approval_evidence_json,
                    label="expected approval evidence JSON",
                ),
            )
            print(tree_oid)
        elif args.command == "verify-release-receipt":
            now = _timestamp(args.now, label="now") if args.now else None
            validate_release_receipt(
                load_json(args.receipt, label="release receipt"),
                expected_pipeline=args.expected_pipeline,
                expected_commit=args.expected_commit,
                expected_contract_sha256=args.expected_contract_sha256,
                allowed_channels=set(args.allowed_channel),
                now=now,
            )
        elif args.command == "verify-release-locator":
            locator_receipt = load_json(args.receipt, label="release locator receipt")
            validate_release_receipt(
                locator_receipt,
                expected_pipeline=args.expected_pipeline,
                expected_contract_sha256=args.expected_contract_sha256,
                allowed_channels={"verified-candidate"},
            )
        elif args.command == "create-release-receipt":
            subjects = [
                _mapping(load_json(path, label="release subject"), label="release subject")
                for path in args.subject
            ]
            value = release_receipt(
                pipeline=args.pipeline,
                channel=args.channel,
                issued_at=args.issued_at,
                expires_at=args.expires_at,
                build_project_arn=args.build_project_arn,
                build_id=args.build_id,
                commit=args.commit,
                contract_path=args.contract_path,
                contract_sha256=args.contract_sha256,
                source_bucket=args.source_bucket,
                source_key=args.source_key,
                source_version=args.source_version,
                source_sha256=args.source_sha256,
                source_signature_key=args.source_signature_key,
                source_signature_version=args.source_signature_version,
                subjects=subjects,
                approval_evidence=(
                    _json_object_argument(
                        args.approval_evidence_json,
                        label="approval evidence JSON",
                    )
                    if args.approval_evidence_json is not None
                    else None
                ),
            )
            _write_json(args.output, value)
        elif args.command == "authorize-release-receipt":
            value = authorize_release_receipt(
                _mapping(
                    load_json(args.locator, label="verified-candidate locator"),
                    label="verified-candidate locator",
                ),
                channel=args.channel,
                issued_at=args.issued_at,
                expires_at=args.expires_at,
            )
            _write_json(args.output, value)
        elif args.command == "verify-lifecycle-preview":
            validate_lifecycle_preview(
                load_json(args.preview, label="lifecycle preview"),
                protected_digests=set(args.protected_digest),
            )
        elif args.command == "prepare-deployment-intent":
            prepared = prepare_deployment_intent(
                args.plan,
                control_commit=args.control_commit,
                terraform_context_path=args.terraform_context,
            )
            print(
                json.dumps(
                    {
                        "intent_id": prepared["intent_id"],
                        "plan_sha256": prepared["plan_sha256"],
                        "authorization_expires_at": prepared["authorization_expires_at"],
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "acquire-deployment-lock":
            lock = acquire_deployment_lock(
                args.plan,
                apply_attempt_id=args.apply_attempt_id,
                control_commit=args.control_commit,
            )
            print(json.dumps(lock, sort_keys=True))
        elif args.command == "validate-deployment-preflight":
            authorization = validate_deployment_preflight(
                args.plan,
                terraform_context_path=args.terraform_context,
                apply_attempt_id=args.apply_attempt_id,
                control_commit=args.control_commit,
            )
            print(json.dumps(authorization, sort_keys=True))
        elif args.command == "heartbeat-deployment-lock":
            lock = heartbeat_deployment_lock(
                args.plan,
                apply_attempt_id=args.apply_attempt_id,
            )
            print(json.dumps(lock, sort_keys=True))
        elif args.command == "release-deployment-lock":
            release_deployment_lock(
                args.plan,
                apply_attempt_id=args.apply_attempt_id,
            )
        elif args.command == "consume-deployment-intent":
            consumed = consume_deployment_intent(
                args.plan,
                query=_environment_gate_query(),
                apply_attempt_id=args.apply_attempt_id,
            )
            print(
                json.dumps(
                    {
                        "intent_id": consumed["intent_id"],
                        "state": consumed["state"],
                        "plan_sha256": consumed["plan_sha256"],
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "mark-deployment-intent-outcome":
            recorded = mark_deployment_intent_outcome(
                args.plan,
                apply_attempt_id=args.apply_attempt_id,
                outcome=args.outcome,
            )
            print(
                json.dumps(
                    {
                        "intent_id": recorded["intent_id"],
                        "state": recorded["state"],
                        "plan_sha256": recorded["plan_sha256"],
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "assert-approved-release":
            approval_now = _timestamp(args.now, label="now") if args.now else None
            approval_evidence = assert_approved_release(
                operation=args.operation,
                approval_locators=_json_object_argument(
                    args.approval_locators_json,
                    label="approval locators JSON",
                ),
                approval_signing_key_arn=args.approval_signing_key_arn,
                approval_encryption_key_arn=args.approval_encryption_key_arn,
                expected_commit=args.expected_commit,
                expected_tree_oid=args.expected_tree_oid,
                expected_inner_sha256=args.expected_inner_sha256,
                expected_outer_sha256=args.expected_outer_sha256,
                expected_pipeline=args.expected_pipeline,
                expected_environment=args.expected_environment,
                runtime_contract_path=args.runtime_contract,
                contract_path=args.contract,
                now=approval_now,
                verified_record_out=args.verified_record_out,
            )
            print(
                json.dumps(
                    approval_evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        elif args.command == "terraform-gate":
            query = json.load(sys.stdin, object_pairs_hook=_reject_duplicate_keys)
            print(json.dumps(_terraform_gate(query), sort_keys=True))
        else:
            raise EvidenceError("unsupported command")
    except (EvidenceError, json.JSONDecodeError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
