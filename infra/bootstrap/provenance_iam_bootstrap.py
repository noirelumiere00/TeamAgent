#!/usr/bin/env python3
"""One-time, create-only provenance/IAM bootstrap.

The normal runtime guard intentionally requires the deployment-intent control
plane before it will produce a saved plan.  This program is the narrowly scoped
first-install path for that control plane:

* root may create and assume only a temporary CloudFormation-owned seed role
  with a stack-owned explicit-deny policy;
* the seed role is denied build, release, image, evidence-object, credential,
  and runtime mutation;
* one fixed Terraform target set is planned into the existing main backend;
* the saved plan must be create/no-op only and pass a structural allowlist;
* a durable conditional ledger row burns the bootstrap exactly once;
* the resulting objects are born in the main Terraform state (there is no
  second state that can claim them);
* the seed session is revoked and its stack is deleted after handoff.

No command in this module is run by tests against AWS.  Unit tests exercise the
pure contract, plan, and state validators with bounded fixtures.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXPECTED_ORIGIN = "git@github.com:noirelumiere00/TeamAgent.git"
EXPECTED_REMOTE_LOOKUP = "https://github.com/noirelumiere00/TeamAgent.git"
BOOTSTRAP_GIT_TOKEN_ENV = "TEAMAGENT_BOOTSTRAP_GIT_TOKEN"
BOOTSTRAP_AWS_CA_BUNDLE_ENV = "TEAMAGENT_BOOTSTRAP_AWS_CA_BUNDLE"
EXPECTED_BRANCH = "dev"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
ADDRESS_INDEX_RE = re.compile(r'\[(?:[0-9]+|"(?:[^"\\]|\\.)*")\]')
VERSION_ID_RE = re.compile(r"^[A-Za-z0-9._~+/=-]+$")
AWS_ENDPOINTS = {
    "cloudformation": "https://cloudformation.ap-northeast-1.amazonaws.com",
    "codebuild": "https://codebuild.ap-northeast-1.amazonaws.com",
    "codeconnections": "https://codeconnections.ap-northeast-1.amazonaws.com",
    "dynamodb": "https://dynamodb.ap-northeast-1.amazonaws.com",
    "ecr": "https://api.ecr.ap-northeast-1.amazonaws.com",
    "iam": "https://iam.amazonaws.com",
    "kms": "https://kms.ap-northeast-1.amazonaws.com",
    "logs": "https://logs.ap-northeast-1.amazonaws.com",
    "s3api": "https://s3.ap-northeast-1.amazonaws.com",
    "sts": "https://sts.ap-northeast-1.amazonaws.com",
}
ALLOWED_DATA_SOURCE_TYPES = {
    "aws_caller_identity",
    "aws_iam_policy_document",
    "aws_iam_user",
}
INLINE_POLICY_OWNERSHIP = {
    "aws_iam_role_policy.codebuild_launcher": (
        "role",
        "teamagent-dev-codebuild-launcher",
        "teamagent-dev-codebuild-launcher",
    ),
    "aws_iam_user_policy.aiia_dev_no_direct_start_build": (
        "user",
        "AIIAdev",
        "require-teamagent-codebuild-launcher-role",
    ),
    "aws_iam_user_policy.release_caller": (
        "user",
        "teamagent-release-caller",
        "teamagent-release-caller",
    ),
    "aws_iam_role_policy.release_launcher": (
        "role",
        "teamagent-dev-release-launcher",
        "teamagent-dev-release-launcher",
    ),
    "aws_iam_user_policy.release_control_update_caller": (
        "user",
        "teamagent-release-control-update-caller",
        "teamagent-release-control-update-caller",
    ),
    "aws_iam_role_policy.release_control_updater": (
        "role",
        "teamagent-dev-release-control-updater",
        "teamagent-dev-release-control-updater",
    ),
    "aws_iam_role_policy.tiktok_codebuild": (
        "role",
        "teamagent-dev-codebuild-tiktok-image",
        "teamagent-dev-codebuild-tiktok-image",
    ),
    "aws_iam_user_policy.tiktok_build_caller": (
        "user",
        "teamagent-tiktok-build-caller",
        "teamagent-tiktok-build-caller",
    ),
    "aws_iam_role_policy.tiktok_build_launcher": (
        "role",
        "teamagent-dev-tiktok-build-launcher",
        "teamagent-dev-tiktok-build-launcher",
    ),
    "aws_iam_role_policy.image_deployment_gate": (
        "role",
        "teamagent-dev-image-deployment-gate",
        "teamagent-dev-image-deployment-gate",
    ),
    "aws_iam_role_policy.mcp_source_publisher": (
        "role",
        "teamagent-dev-codebuild-mcp-source-publisher",
        "teamagent-dev-codebuild-mcp-source-publisher",
    ),
    "aws_iam_role_policy.image_attestor": (
        "role",
        "teamagent-dev-codebuild-image-attestor",
        "teamagent-dev-codebuild-image-attestor",
    ),
    "aws_iam_role_policy.image_promoter": (
        "role",
        "teamagent-dev-codebuild-image-promoter",
        "teamagent-dev-codebuild-image-promoter",
    ),
    "aws_iam_role_policy.openclaw_codebuild": (
        "role",
        "teamagent-dev-codebuild-openclaw",
        "teamagent-dev-codebuild-openclaw",
    ),
    "aws_iam_role_policy.openclaw_publisher": (
        "role",
        "teamagent-dev-openclaw-build-publisher",
        "teamagent-dev-openclaw-build-publisher",
    ),
    "aws_iam_role_policy.alarm_recipient_ack_signer": (
        "role",
        "teamagent-dev-alarm-recipient-ack-signer",
        "teamagent-dev-alarm-recipient-ack-signer",
    ),
    "aws_iam_role_policy.media_cutover_attestor": (
        "role",
        "teamagent-dev-media-cutover-attestor",
        "teamagent-dev-media-cutover-attestor",
    ),
    "aws_iam_role_policy.runtime_evidence_automation": (
        "role",
        "teamagent-dev-terraform-runtime-automation",
        "teamagent-dev-terraform-runtime-automation-evidence",
    ),
    "aws_iam_role_policy.runtime_automation_control_plane": (
        "role",
        "teamagent-dev-terraform-runtime-automation",
        "teamagent-dev-terraform-runtime-automation-control-plane",
    ),
}
ECR_LIFECYCLE_OWNERSHIP = {
    "aws_ecr_lifecycle_policy.openclaw_quarantine": "teamagent-openclaw-quarantine",
    "aws_ecr_lifecycle_policy.openclaw_verified_candidates": (
        "teamagent-openclaw-verified-candidates"
    ),
    "aws_ecr_lifecycle_policy.openclaw_media_quarantine": (
        "teamagent-openclaw-media-quarantine"
    ),
    "aws_ecr_lifecycle_policy.openclaw_media_verified_candidates": (
        "teamagent-openclaw-media-verified-candidates"
    ),
    "aws_ecr_lifecycle_policy.mcp_quarantine": "teamagent-mcp-quarantine",
    "aws_ecr_lifecycle_policy.mcp_verified_candidates": (
        "teamagent-mcp-verified-candidates"
    ),
    "aws_ecr_lifecycle_policy.mcp_media_quarantine": (
        "teamagent-media-worker-quarantine"
    ),
    "aws_ecr_lifecycle_policy.mcp_media_verified_candidates": (
        "teamagent-media-worker-verified-candidates"
    ),
    "aws_ecr_lifecycle_policy.tiktok_acquire_quarantine": (
        "teamagent-dev-tiktok-acquire-quarantine"
    ),
    "aws_ecr_lifecycle_policy.tiktok_acquire_verified_candidates": (
        "teamagent-dev-tiktok-acquire-verified-candidates"
    ),
}
S3_UPSERT_OWNERSHIP = {
    "aws_s3_bucket_public_access_block.image_release_evidence": (
        "teamagent-dev-image-release-evidence",
        "get-public-access-block",
    ),
    "aws_s3_bucket_versioning.image_release_evidence": (
        "teamagent-dev-image-release-evidence",
        "get-bucket-versioning",
    ),
    "aws_s3_bucket_server_side_encryption_configuration.image_release_evidence": (
        "teamagent-dev-image-release-evidence",
        "get-bucket-encryption",
    ),
    "aws_s3_bucket_object_lock_configuration.image_release_evidence": (
        "teamagent-dev-image-release-evidence",
        "get-object-lock-configuration",
    ),
    "aws_s3_bucket_lifecycle_configuration.image_release_evidence": (
        "teamagent-dev-image-release-evidence",
        "get-bucket-lifecycle-configuration",
    ),
    "aws_s3_bucket_policy.image_release_evidence": (
        "teamagent-dev-image-release-evidence",
        "get-bucket-policy",
    ),
    "aws_s3_bucket_public_access_block.openclaw_build_evidence": (
        "teamagent-dev-openclaw-build-evidence",
        "get-public-access-block",
    ),
    "aws_s3_bucket_versioning.openclaw_build_evidence": (
        "teamagent-dev-openclaw-build-evidence",
        "get-bucket-versioning",
    ),
    "aws_s3_bucket_server_side_encryption_configuration.openclaw_build_evidence": (
        "teamagent-dev-openclaw-build-evidence",
        "get-bucket-encryption",
    ),
    "aws_s3_bucket_object_lock_configuration.openclaw_build_evidence": (
        "teamagent-dev-openclaw-build-evidence",
        "get-object-lock-configuration",
    ),
    "aws_s3_bucket_lifecycle_configuration.openclaw_build_evidence": (
        "teamagent-dev-openclaw-build-evidence",
        "get-bucket-lifecycle-configuration",
    ),
    "aws_s3_bucket_policy.openclaw_build_evidence": (
        "teamagent-dev-openclaw-build-evidence",
        "get-bucket-policy",
    ),
}
UPSERT_RESOURCE_TYPES = {
    "aws_ecr_lifecycle_policy",
    "aws_iam_role_policy",
    "aws_iam_user_policy",
    "aws_s3_bucket_lifecycle_configuration",
    "aws_s3_bucket_object_lock_configuration",
    "aws_s3_bucket_policy",
    "aws_s3_bucket_public_access_block",
    "aws_s3_bucket_server_side_encryption_configuration",
    "aws_s3_bucket_versioning",
}


class BootstrapError(RuntimeError):
    """A fail-closed bootstrap contract violation."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"cannot read canonical {label}: {path}") from exc


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BootstrapError(f"cannot hash file: {path}") from exc
    return digest.hexdigest()


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise BootstrapError(f"{label} must be an object")
    return value


def _string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BootstrapError(f"{label} must be a non-empty string")
    return value


def _string_list(value: Any, *, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
    ):
        raise BootstrapError(f"{label} must be a unique non-empty string array")
    return list(value)


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise BootstrapError(
            f"{label} keys differ: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


@dataclass(frozen=True)
class BootstrapContract:
    raw: Mapping[str, Any]
    path: Path
    account_id: str
    region: str
    bootstrap_id: str
    seed: Mapping[str, Any]
    backend: Mapping[str, Any]
    release_contracts: tuple[str, ...]
    targets: tuple[str, ...]
    create_allowed: frozenset[str]
    existing_dependencies: frozenset[str]
    required_main_state: frozenset[str]
    allowed_outputs: frozenset[str]
    forbidden_type_prefixes: tuple[str, ...]
    forbidden_name_fragments: tuple[str, ...]


def load_contract(path: Path) -> BootstrapContract:
    raw = _mapping(load_json(path, label="bootstrap contract"), label="bootstrap contract")
    _exact_keys(
        raw,
        {
            "schema_version",
            "bootstrap_id",
            "account_id",
            "region",
            "seed",
            "backend",
            "release_contracts",
            "required_existing_aws_objects",
            "connection_names",
            "terraform_targets",
            "create_allowed_dependency_addresses",
            "existing_dependency_addresses",
            "required_main_state_addresses",
            "allowed_output_names",
            "forbidden_change_type_prefixes",
            "forbidden_main_state_name_fragments",
        },
        label="bootstrap contract",
    )
    if raw["schema_version"] != 1:
        raise BootstrapError("unsupported bootstrap contract schema")
    account_id = _string(raw["account_id"], label="account_id")
    if account_id != "718959508629":
        raise BootstrapError("bootstrap account is not the fixed TeamAgent account")
    region = _string(raw["region"], label="region")
    if region != "ap-northeast-1":
        raise BootstrapError("bootstrap region is not the fixed TeamAgent region")
    bootstrap_id = _string(raw["bootstrap_id"], label="bootstrap_id")
    seed = _mapping(raw["seed"], label="seed")
    _exact_keys(
        seed,
        {
            "stack_name",
            "role_name",
            "role_arn",
            "deny_policy_name",
            "deny_policy_arn",
            "inline_policy_name",
            "session_name",
            "session_arn",
            "source_identity",
            "max_session_seconds",
        },
        label="seed",
    )
    expected_role_arn = f"arn:aws:iam::{account_id}:role/{seed['role_name']}"
    expected_deny_policy_arn = f"arn:aws:iam::{account_id}:policy/{seed['deny_policy_name']}"
    expected_session_arn = (
        f"arn:aws:sts::{account_id}:assumed-role/{seed['role_name']}/{seed['session_name']}"
    )
    if (
        seed["role_arn"] != expected_role_arn
        or seed["deny_policy_arn"] != expected_deny_policy_arn
        or seed["session_arn"] != expected_session_arn
    ):
        raise BootstrapError("seed role/policy/session ARN does not match its fixed name")
    if seed["max_session_seconds"] != 3600:
        raise BootstrapError("seed session must be exactly one hour")
    if seed["inline_policy_name"] != "teamagent-production-provenance-bootstrap-boundary":
        raise BootstrapError("seed inline policy name is not the reviewed boundary")
    backend = _mapping(raw["backend"], label="backend")
    _exact_keys(
        backend,
        {"bucket", "key", "region", "dynamodb_table", "ledger_key"},
        label="backend",
    )
    if backend != {
        "bucket": "teamagent-tfstate-718959508629",
        "key": "teamagent/terraform.tfstate",
        "region": region,
        "dynamodb_table": "teamagent-tflock",
        "ledger_key": "bootstrap#teamagent-production-provenance-iam-v1",
    }:
        raise BootstrapError("bootstrap backend is not the exact main backend/ledger")

    release_contracts = tuple(_string_list(raw["release_contracts"], label="release_contracts"))
    targets = tuple(_string_list(raw["terraform_targets"], label="terraform_targets"))
    dependencies = frozenset(
        _string_list(
            raw["create_allowed_dependency_addresses"],
            label="create_allowed_dependency_addresses",
        )
    )
    existing = frozenset(
        _string_list(
            raw["existing_dependency_addresses"],
            label="existing_dependency_addresses",
        )
    )
    required = frozenset(
        _string_list(
            raw["required_main_state_addresses"],
            label="required_main_state_addresses",
        )
    )
    allowed_outputs = frozenset(
        _string_list(raw["allowed_output_names"], label="allowed_output_names")
    )
    forbidden_types = tuple(
        _string_list(
            raw["forbidden_change_type_prefixes"],
            label="forbidden_change_type_prefixes",
        )
    )
    forbidden_names = tuple(
        _string_list(
            raw["forbidden_main_state_name_fragments"],
            label="forbidden_main_state_name_fragments",
        )
    )
    create_allowed = frozenset(targets) | dependencies
    if required - create_allowed:
        raise BootstrapError("required main-state address is not create-allowlisted")
    if create_allowed & existing:
        raise BootstrapError("create and existing-only address allowlists overlap")
    for address in create_allowed:
        resource_type = address.split(".", 1)[0]
        if any(resource_type.startswith(prefix) for prefix in forbidden_types):
            raise BootstrapError(f"forbidden resource type is create-allowlisted: {address}")
    return BootstrapContract(
        raw=raw,
        path=path,
        account_id=account_id,
        region=region,
        bootstrap_id=bootstrap_id,
        seed=seed,
        backend=backend,
        release_contracts=release_contracts,
        targets=targets,
        create_allowed=create_allowed,
        existing_dependencies=existing,
        required_main_state=required,
        allowed_outputs=allowed_outputs,
        forbidden_type_prefixes=forbidden_types,
        forbidden_name_fragments=forbidden_names,
    )


def validate_release_contracts(repo_root: Path, contract: BootstrapContract) -> dict[str, str]:
    """Require every release contract to be explicitly blocked before AWS use."""

    result: dict[str, str] = {}
    canonical_root = repo_root.resolve(strict=True)
    for relative in contract.release_contracts:
        path = (canonical_root / relative).resolve(strict=True)
        try:
            path.relative_to(canonical_root)
        except ValueError as exc:
            raise BootstrapError("release contract escapes the repository") from exc
        value = _mapping(load_json(path, label=relative), label=relative)
        release = _mapping(value.get("release"), label=f"{relative}.release")
        _exact_keys(release, {"ready", "blocked_reason"}, label=f"{relative}.release")
        blocked_reason = release["blocked_reason"]
        if release["ready"] is not False:
            raise BootstrapError(f"bootstrap requires release.ready=false: {relative}")
        if not isinstance(blocked_reason, str) or not blocked_reason.strip():
            raise BootstrapError(f"blocked release contract needs a reason: {relative}")
        result[relative] = sha256_file(path)
    return result


def normalize_address(address: str) -> str:
    if not isinstance(address, str) or not address:
        raise BootstrapError("Terraform resource address is malformed")
    return ADDRESS_INDEX_RE.sub("", address)


def _state_instance_address(base: str, instance: Mapping[str, Any]) -> str:
    index = instance.get("index_key")
    if index is None:
        return base
    if isinstance(index, bool):
        raise BootstrapError(f"boolean Terraform state index is invalid: {base}")
    if isinstance(index, int):
        if index < 0:
            raise BootstrapError(f"negative Terraform state index is invalid: {base}")
        return f"{base}[{index}]"
    if isinstance(index, str):
        encoded = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
        return f"{base}[{encoded}]"
    raise BootstrapError(f"unsupported Terraform state index: {base}")


def state_addresses(state_value: Any) -> set[str]:
    state = _mapping(state_value, label="Terraform state")
    if state.get("version") != 4:
        raise BootstrapError("Terraform state version must be 4")
    lineage = state.get("lineage")
    try:
        if not isinstance(lineage, str) or str(uuid.UUID(lineage)) != lineage:
            raise ValueError
    except ValueError as exc:
        raise BootstrapError("Terraform state lineage is not a canonical UUID") from exc
    serial = state.get("serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 0:
        raise BootstrapError("Terraform state serial is invalid")
    resources = state.get("resources")
    if not isinstance(resources, list):
        raise BootstrapError("Terraform state resources must be an array")
    addresses: set[str] = set()
    for raw_resource in resources:
        resource = _mapping(raw_resource, label="Terraform state resource")
        mode = resource.get("mode", "managed")
        if mode == "data":
            continue
        if mode != "managed":
            raise BootstrapError("unsupported Terraform state resource mode")
        resource_type = _string(resource.get("type"), label="state resource type")
        name = _string(resource.get("name"), label="state resource name")
        module = resource.get("module")
        if module is not None and (not isinstance(module, str) or not module):
            raise BootstrapError("Terraform state module address is malformed")
        base = f"{resource_type}.{name}"
        if module:
            base = f"{module}.{base}"
        instances = resource.get("instances")
        if not isinstance(instances, list) or not instances:
            raise BootstrapError(f"managed state resource has no instances: {base}")
        for raw_instance in instances:
            instance = _mapping(raw_instance, label=f"state instance {base}")
            address = _state_instance_address(base, instance)
            if address in addresses:
                raise BootstrapError(f"duplicate Terraform state address: {address}")
            addresses.add(address)
    return addresses


def _state_serial_and_lineage(state_value: Any) -> tuple[int, str]:
    state = _mapping(state_value, label="Terraform state")
    state_addresses(state)
    return int(state["serial"]), str(state["lineage"])


def _contains_forbidden_fragment(value: Any, fragments: Sequence[str]) -> bool:
    if isinstance(value, str):
        return any(fragment in value for fragment in fragments)
    if isinstance(value, list):
        return any(_contains_forbidden_fragment(item, fragments) for item in value)
    if isinstance(value, dict):
        return any(
            _contains_forbidden_fragment(key, fragments)
            or _contains_forbidden_fragment(item, fragments)
            for key, item in value.items()
        )
    return False


def _contains_failed_check(value: Any) -> bool:
    if isinstance(value, list):
        return any(_contains_failed_check(item) for item in value)
    if isinstance(value, dict):
        if value.get("status") in {"error", "fail"}:
            return True
        return any(_contains_failed_check(item) for item in value.values())
    return False


@dataclass(frozen=True)
class PlanValidation:
    created_addresses: tuple[str, ...]
    no_op_addresses: tuple[str, ...]
    plan_sha256: str


def validate_plan(
    plan_value: Any,
    before_state_value: Any,
    contract: BootstrapContract,
    *,
    plan_sha256: str,
) -> PlanValidation:
    """Validate a bounded Terraform show -json document without AWS access."""

    if not SHA256_RE.fullmatch(plan_sha256):
        raise BootstrapError("saved plan SHA-256 is malformed")
    plan = _mapping(plan_value, label="Terraform plan")
    if plan.get("format_version") != "1.2" or not re.fullmatch(
        r"1\.12\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?",
        str(plan.get("terraform_version", "")),
    ):
        raise BootstrapError("Terraform plan format/tool version is not reviewed")
    if plan.get("errored") is not False:
        raise BootstrapError("Terraform bootstrap plan is errored or omits errored=false")
    if plan.get("complete") is not False:
        raise BootstrapError("Terraform bootstrap plan is not a fixed-target plan")
    if plan.get("applyable") is not True:
        raise BootstrapError("Terraform bootstrap plan is not applyable")
    if plan.get("resource_drift") not in (None, []):
        raise BootstrapError("Terraform bootstrap plan contains resource drift")
    if plan.get("deferred_changes") not in (None, []):
        raise BootstrapError("Terraform bootstrap plan contains deferred changes")
    if plan.get("action_invocations") not in (None, []):
        raise BootstrapError("Terraform bootstrap plan contains action invocations")
    diagnostics = plan.get("diagnostics", [])
    if not isinstance(diagnostics, list) or any(
        isinstance(item, dict) and item.get("severity") == "error" for item in diagnostics
    ):
        raise BootstrapError("Terraform bootstrap plan contains error diagnostics")
    checks = plan.get("checks", [])
    if not isinstance(checks, list) or _contains_failed_check(checks):
        raise BootstrapError("Terraform bootstrap plan contains a failed check")
    if "output_changes" not in plan:
        raise BootstrapError("Terraform plan omits required output_changes")
    output_changes = plan["output_changes"]
    if not isinstance(output_changes, dict):
        raise BootstrapError("Terraform plan output_changes must be an object")
    for name, raw_output_change in output_changes.items():
        if name not in contract.allowed_outputs:
            raise BootstrapError(f"output change is outside bootstrap allowlist: {name}")
        output_change = _mapping(
            raw_output_change,
            label=f"Terraform output change {name}",
        )
        actions = output_change.get("actions")
        if actions not in (["create"], ["update"], ["no-op"]):
            raise BootstrapError(f"output deletion/replacement is forbidden: {name}")

    prior = state_addresses(before_state_value)
    changes = plan.get("resource_changes")
    if not isinstance(changes, list):
        raise BootstrapError("Terraform plan resource_changes must be an array")
    created: set[str] = set()
    no_op: set[str] = set()
    seen_addresses: set[str] = set()
    for raw_change in changes:
        change_record = _mapping(raw_change, label="Terraform resource change")
        address = _string(change_record.get("address"), label="change address")
        if address in seen_addresses:
            raise BootstrapError(f"duplicate Terraform plan address: {address}")
        seen_addresses.add(address)
        mode = change_record.get("mode", "managed")
        resource_type = _string(change_record.get("type"), label=f"{address}.type")
        if change_record.get("provider_name") != "registry.terraform.io/hashicorp/aws":
            raise BootstrapError(f"Terraform provider is outside bootstrap allowlist: {address}")
        change = _mapping(change_record.get("change"), label=f"{address}.change")
        actions = change.get("actions")
        if not isinstance(actions, list) or not all(isinstance(item, str) for item in actions):
            raise BootstrapError(f"Terraform actions are malformed: {address}")
        if change_record.get("previous_address") is not None:
            raise BootstrapError(f"moved resource is forbidden during bootstrap: {address}")
        if change.get("importing") is not None:
            raise BootstrapError(f"import is forbidden during bootstrap: {address}")
        if mode == "data":
            if resource_type not in ALLOWED_DATA_SOURCE_TYPES:
                raise BootstrapError(f"data source is outside bootstrap allowlist: {address}")
            if actions not in (["read"], ["no-op"]):
                raise BootstrapError(f"data source has mutating actions: {address}")
            continue
        if mode != "managed":
            raise BootstrapError(f"unsupported plan resource mode: {address}")
        if any(resource_type.startswith(prefix) for prefix in contract.forbidden_type_prefixes):
            raise BootstrapError(f"runtime/guard resource reached bootstrap plan: {address}")
        base = normalize_address(address)
        if actions == ["create"]:
            if base not in contract.create_allowed:
                raise BootstrapError(f"create is outside bootstrap allowlist: {address}")
            if address in prior:
                raise BootstrapError(
                    f"plan creates an address already owned by main state: {address}"
                )
            created.add(address)
        elif actions == ["no-op"]:
            if base not in contract.create_allowed | contract.existing_dependencies:
                raise BootstrapError(f"no-op dependency is outside bootstrap allowlist: {address}")
            if address not in prior:
                raise BootstrapError(f"no-op address is not owned by main state: {address}")
            no_op.add(address)
        else:
            raise BootstrapError(f"bootstrap permits create/no-op only, got {actions!r}: {address}")

    if not created:
        raise BootstrapError("one-time bootstrap plan creates nothing")
    covered = prior | created
    missing = {
        required
        for required in contract.required_main_state
        if not any(normalize_address(address) == required for address in covered)
    }
    if missing:
        raise BootstrapError(
            f"bootstrap plan/state lacks required control-plane addresses: {sorted(missing)}"
        )
    return PlanValidation(
        created_addresses=tuple(sorted(created)),
        no_op_addresses=tuple(sorted(no_op)),
        plan_sha256=plan_sha256,
    )


@dataclass(frozen=True)
class HandoffValidation:
    before_serial: int
    after_serial: int
    lineage: str
    before_addresses_sha256: str
    after_addresses_sha256: str


def validate_handoff(
    before_state_value: Any,
    after_state_value: Any,
    plan_validation: PlanValidation,
    contract: BootstrapContract,
) -> HandoffValidation:
    """Prove direct main-state ownership and absence of a bootstrap-state copy."""

    before = state_addresses(before_state_value)
    after = state_addresses(after_state_value)
    before_serial, before_lineage = _state_serial_and_lineage(before_state_value)
    after_serial, after_lineage = _state_serial_and_lineage(after_state_value)
    if before_lineage != after_lineage:
        raise BootstrapError("main Terraform state lineage changed during bootstrap")
    if after_serial <= before_serial:
        raise BootstrapError("main Terraform state serial did not advance")
    removed = before - after
    if removed:
        raise BootstrapError(f"bootstrap removed main-state ownership: {sorted(removed)}")
    expected_additions = set(plan_validation.created_addresses)
    additions = after - before
    if additions != expected_additions:
        raise BootstrapError(
            "main-state additions differ from the reviewed create-only plan: "
            f"missing={sorted(expected_additions - additions)}, "
            f"extra={sorted(additions - expected_additions)}"
        )
    missing_required = {
        required
        for required in contract.required_main_state
        if not any(normalize_address(address) == required for address in after)
    }
    if missing_required:
        raise BootstrapError(
            f"handoff lacks required main-state ownership: {sorted(missing_required)}"
        )
    if _contains_forbidden_fragment(after_state_value, contract.forbidden_name_fragments):
        raise BootstrapError("temporary bootstrap object leaked into main Terraform state")
    before_outputs = _mapping(
        _mapping(before_state_value, label="before Terraform state").get("outputs", {}),
        label="before Terraform state outputs",
    )
    after_outputs = _mapping(
        _mapping(after_state_value, label="after Terraform state").get("outputs", {}),
        label="after Terraform state outputs",
    )
    removed_outputs = set(before_outputs) - set(after_outputs)
    if removed_outputs:
        raise BootstrapError(f"bootstrap removed Terraform outputs: {sorted(removed_outputs)}")
    changed_outputs = {
        name
        for name in set(before_outputs) & set(after_outputs)
        if before_outputs[name] != after_outputs[name]
    }
    added_outputs = set(after_outputs) - set(before_outputs)
    if (changed_outputs | added_outputs) - contract.allowed_outputs:
        raise BootstrapError("bootstrap changed an output outside the output allowlist")
    return HandoffValidation(
        before_serial=before_serial,
        after_serial=after_serial,
        lineage=before_lineage,
        before_addresses_sha256=sha256_bytes(canonical_bytes(sorted(before))),
        after_addresses_sha256=sha256_bytes(canonical_bytes(sorted(after))),
    )


def _secure_existing_file(path: Path, *, mode: int = 0o600) -> Path:
    try:
        before = path.lstat()
        canonical = path.resolve(strict=True)
        after = canonical.stat()
    except OSError as exc:
        raise BootstrapError(f"secure input does not exist: {path}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(after.st_mode):
        raise BootstrapError(f"secure input must be a regular non-symlink: {path}")
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise BootstrapError(f"secure input identity changed during resolution: {path}")
    if stat.S_IMODE(after.st_mode) != mode:
        raise BootstrapError(f"secure input mode must be {mode:o}: {path}")
    if after.st_uid != os.getuid():
        raise BootstrapError(f"secure input is not owned by the current user: {path}")
    parent = canonical.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
        raise BootstrapError(
            f"secure input parent must be owned and not group/world writable: {path}"
        )
    return canonical


def _secure_new_artifact_dir(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise BootstrapError("artifact directory must not already exist")
    parent = path.parent.resolve(strict=True)
    parent_stat = parent.stat()
    if parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o022:
        raise BootstrapError("artifact parent must be owned and not group/world writable")
    path.mkdir(mode=0o700)
    canonical = path.resolve(strict=True)
    if stat.S_IMODE(canonical.stat().st_mode) != 0o700:
        raise BootstrapError("artifact directory mode is not 0700")
    return canonical


def _secure_existing_artifact_dir(path: Path) -> Path:
    try:
        before = path.lstat()
        canonical = path.resolve(strict=True)
        after = canonical.stat()
    except OSError as exc:
        raise BootstrapError("artifact directory does not exist") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(after.st_mode)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or after.st_uid != os.getuid()
        or stat.S_IMODE(after.st_mode) != 0o700
    ):
        raise BootstrapError("artifact directory is not an owned canonical 0700 directory")
    return canonical


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_json(path: Path, value: Any) -> None:
    if path.exists() or path.is_symlink():
        raise BootstrapError(f"refusing to overwrite artifact: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except Exception:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        raise


def _write_private_bytes(path: Path, value: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise BootstrapError(f"refusing to overwrite artifact: {path}")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except Exception:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        raise


def _persist_or_verify_private_json(path: Path, value: Any) -> str:
    expected = canonical_bytes(value)
    if path.exists() or path.is_symlink():
        canonical = _secure_existing_file(path)
        if canonical.read_bytes() != expected:
            raise BootstrapError(f"durable artifact conflicts with reviewed claims: {path}")
        _fsync_directory(path.parent)
        return sha256_bytes(expected)

    temporary = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    _write_private_bytes(temporary, expected)
    try:
        try:
            # Publish only fully written, fsynced bytes. link(2) is an
            # atomic create-if-absent operation and never replaces a hostile
            # or concurrently published destination.
            os.link(temporary, path, follow_symlinks=False)
            _fsync_directory(path.parent)
        except FileExistsError:
            canonical = _secure_existing_file(path)
            if canonical.read_bytes() != expected:
                raise BootstrapError(
                    f"durable artifact conflicts with reviewed claims: {path}"
                ) from None
    finally:
        temporary.unlink(missing_ok=True)
        _fsync_directory(path.parent)
    canonical = _secure_existing_file(path)
    if canonical.read_bytes() != expected:
        raise BootstrapError(f"durable artifact conflicts with reviewed claims: {path}")
    _fsync_directory(path.parent)
    return sha256_bytes(expected)


def _persist_handoff_artifacts(
    artifact_dir: Path,
    *,
    claims: Mapping[str, Any],
    ownership: Mapping[str, Any],
) -> tuple[str, str]:
    claims_path = artifact_dir / "bootstrap-handoff-claims.json"
    ownership_path = artifact_dir / "bootstrap-handoff-ownership.json"
    claims_sha256 = _persist_or_verify_private_json(claims_path, claims)
    ownership_sha256 = _persist_or_verify_private_json(ownership_path, ownership)
    durable = {
        "kind": "teamagent-provenance-bootstrap-durable-handoff",
        "schema_version": 1,
        "claims_file": claims_path.name,
        "claims_sha256": claims_sha256,
        "ownership_file": ownership_path.name,
        "ownership_sha256": ownership_sha256,
    }
    _persist_or_verify_private_json(
        artifact_dir / "bootstrap-handoff-durable.json",
        durable,
    )
    _fsync_directory(artifact_dir)
    return claims_sha256, ownership_sha256


@dataclass(frozen=True)
class CommandResult:
    stdout: bytes
    stderr: bytes
    returncode: int


class CommandRunner:
    """Small injectable subprocess boundary with immutable tool identities."""

    def __init__(self) -> None:
        self._tools: dict[str, dict[str, Any]] = {}

    def _pin_or_verify_tool(
        self,
        name: str,
        *,
        env: Mapping[str, str],
    ) -> Path:
        record = self._tools.get(name)
        if record is None:
            discovered = shutil.which(name, path=env.get("PATH"))
            if not discovered:
                raise BootstrapError(f"required executable is absent: {name}")
            try:
                canonical = Path(discovered).resolve(strict=True)
                metadata = canonical.stat()
            except OSError as exc:
                raise BootstrapError(f"cannot resolve executable: {name}") from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise BootstrapError(f"executable is not a regular file: {name}")
            if metadata.st_uid not in {0, os.getuid()}:
                raise BootstrapError(f"executable has an untrusted owner: {name}")
            if metadata.st_mode & 0o022:
                raise BootstrapError(f"executable is group/world writable: {name}")
            record = {
                "path": str(canonical),
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
                "size": metadata.st_size,
                "sha256": sha256_file(canonical),
            }
            self._tools[name] = record
        canonical = Path(str(record["path"]))
        try:
            metadata = canonical.stat()
        except OSError as exc:
            raise BootstrapError(f"pinned executable disappeared: {name}") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, os.getuid()}
            or metadata.st_mode & 0o022
            or metadata.st_dev != record["device"]
            or metadata.st_ino != record["inode"]
            or metadata.st_size != record["size"]
            or sha256_file(canonical) != record["sha256"]
        ):
            raise BootstrapError(f"pinned executable changed during bootstrap: {name}")
        return canonical

    def tool_evidence(self) -> Mapping[str, Mapping[str, Any]]:
        return {
            name: {
                "path": value["path"],
                "sha256": value["sha256"],
                "size": value["size"],
            }
            for name, value in sorted(self._tools.items())
        }

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        check: bool = True,
    ) -> CommandResult:
        if not arguments:
            raise BootstrapError("refusing to execute an empty command")
        command = arguments[0]
        if command in {"aws", "git", "python3", "terraform"}:
            executable = self._pin_or_verify_tool(command, env=env)
            command_arguments = [str(executable), *arguments[1:]]
        else:
            command_arguments = list(arguments)
        completed = subprocess.run(
            command_arguments,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        result = CommandResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
        if check and result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[-2000:]
            raise BootstrapError(f"{command} failed ({result.returncode}): {stderr}")
        return result


def _reject_influential_environment(source: Mapping[str, str]) -> None:
    """Reject caller-controlled Git/Terraform selectors before any AWS call."""

    exact = {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CURL_VERBOSE",
        "GIT_DIR",
        "GIT_EXEC_PATH",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PROXY_COMMAND",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        "GIT_SSL_CIPHER_LIST",
        "GIT_SSL_NO_VERIFY",
        "GIT_WORK_TREE",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSAFEPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "TF_CLI_ARGS",
        "TF_CLI_CONFIG_FILE",
        "TF_DATA_DIR",
        "TF_IN_AUTOMATION",
        "TF_INPUT",
        "TF_PLUGIN_CACHE_DIR",
        "TF_REATTACH_PROVIDERS",
        "TF_REGISTRY_CLIENT_TIMEOUT",
        "TF_REGISTRY_DISCOVERY_RETRY",
        "TF_WORKSPACE",
    }
    prefixes = (
        "DYLD_",
        "GIT_CONFIG_",
        "GIT_TRACE",
        "TF_CLI_ARGS_",
        "TF_LOG",
        "TF_VAR_",
    )
    rejected = sorted(
        name
        for name in source
        if name in exact or any(name.startswith(prefix) for prefix in prefixes)
    )
    if rejected:
        raise BootstrapError(
            "bootstrap rejects inherited Git/Terraform control variables: " + ", ".join(rejected)
        )


def _clean_aws_environment(source: Mapping[str, str]) -> dict[str, str]:
    result = dict(source)
    for name in list(result):
        if name.startswith("AWS_ENDPOINT_URL") or name in {
            "AWS_CA_BUNDLE",
            "AWS_CONFIG_FILE",
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_DATA_PATH",
            "AWS_DEFAULT_PROFILE",
            "AWS_EC2_METADATA_SERVICE_ENDPOINT",
            "AWS_PROFILE",
            "AWS_ROLE_ARN",
            "AWS_SECURITY_TOKEN",
            "AWS_SHARED_CREDENTIALS_FILE",
            "AWS_WEB_IDENTITY_TOKEN_FILE",
            "BOTO_CONFIG",
            "CURL_CA_BUNDLE",
            "REQUESTS_CA_BUNDLE",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
        }:
            result.pop(name, None)
    result["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] = "true"
    result["AWS_PAGER"] = ""
    return result


def _bootstrap_ca_bundle(env: Mapping[str, str]) -> str | None:
    raw = env.get(BOOTSTRAP_AWS_CA_BUNDLE_ENV, "").strip()
    if not raw:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise BootstrapError("bootstrap AWS CA bundle path contains control characters")
    path = Path(raw)
    if not path.is_absolute():
        raise BootstrapError("bootstrap AWS CA bundle path must be absolute")
    if not path.is_file():
        raise BootstrapError("bootstrap AWS CA bundle path must be an existing regular file")
    return str(path)


def _temporary_root_environment(
    source: Mapping[str, str],
    *,
    region: str,
) -> dict[str, str]:
    """Require explicit temporary credentials before any root AWS mutation."""

    result = _clean_aws_environment(source)
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        value = result.get(name)
        if not isinstance(value, str) or not value or value.strip() != value:
            raise BootstrapError(
                "bootstrap root credentials must be an explicit temporary STS session"
            )
    result["AWS_REGION"] = region
    result["AWS_DEFAULT_REGION"] = region
    result["AWS_CONFIG_FILE"] = "/dev/null"
    result["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"
    result.pop("AWS_PROFILE", None)
    result.pop("AWS_DEFAULT_PROFILE", None)
    ca_bundle = _bootstrap_ca_bundle(source)
    if ca_bundle is not None:
        result["AWS_CA_BUNDLE"] = ca_bundle
    return result


def _session_environment(
    base: Mapping[str, str],
    credentials: Mapping[str, Any],
    *,
    region: str,
) -> dict[str, str]:
    result = _clean_aws_environment(base)
    mapping = {
        "AWS_ACCESS_KEY_ID": "AccessKeyId",
        "AWS_SECRET_ACCESS_KEY": "SecretAccessKey",
        "AWS_SESSION_TOKEN": "SessionToken",
    }
    for environment_name, credential_name in mapping.items():
        value = credentials.get(credential_name)
        if not isinstance(value, str) or not value:
            raise BootstrapError("STS returned incomplete seed credentials")
        result[environment_name] = value
    result["AWS_REGION"] = region
    result["AWS_DEFAULT_REGION"] = region
    result["AWS_CONFIG_FILE"] = "/dev/null"
    result["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"
    result.pop("AWS_PROFILE", None)
    result.pop("AWS_DEFAULT_PROFILE", None)
    ca_bundle = _bootstrap_ca_bundle(base)
    if ca_bundle is not None:
        result["AWS_CA_BUNDLE"] = ca_bundle
    return result


def _decode_json_result(result: CommandResult, *, label: str) -> Mapping[str, Any]:
    try:
        return _mapping(
            json.loads(result.stdout, object_pairs_hook=_reject_duplicate_keys),
            label=label,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"{label} command returned invalid JSON") from exc


def _aws(
    runner: CommandRunner,
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    region: str,
    check: bool = True,
) -> CommandResult:
    if not arguments or arguments[0] not in AWS_ENDPOINTS:
        raise BootstrapError("AWS service is outside the bootstrap endpoint allowlist")
    # IAM is a global service whose SigV4 credential scope is us-east-1 even
    # though every regional TeamAgent resource is pinned to ap-northeast-1.
    signing_region = "us-east-1" if arguments[0] == "iam" else region
    return runner.run(
        [
            "aws",
            "--region",
            signing_region,
            "--endpoint-url",
            AWS_ENDPOINTS[arguments[0]],
            "--no-cli-pager",
            *arguments,
        ],
        cwd=cwd,
        env=env,
        check=check,
    )


def _assert_identity(
    runner: CommandRunner,
    *,
    cwd: Path,
    env: Mapping[str, str],
    contract: BootstrapContract,
    expected_arn: str,
    label: str,
) -> Mapping[str, Any]:
    result = _aws(
        runner,
        ["sts", "get-caller-identity", "--output", "json"],
        cwd=cwd,
        env=env,
        region=contract.region,
    )
    identity = _decode_json_result(result, label=label)
    if identity.get("Account") != contract.account_id or identity.get("Arn") != expected_arn:
        raise BootstrapError(f"{label} is not the exact pinned identity")
    user_id = identity.get("UserId")
    if not isinstance(user_id, str) or not user_id:
        raise BootstrapError(f"{label} returned an invalid UserId")
    return identity


def _git_environment(env: Mapping[str, str]) -> dict[str, str]:
    # Git/SSH does not need AWS credentials. Never expose a root or seed
    # session to repository hooks, credential helpers, SSH configuration, or a
    # remote.
    result = dict(env)
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
    ):
        result.pop(name, None)
    result["GIT_CONFIG_GLOBAL"] = "/dev/null"
    result["GIT_CONFIG_NOSYSTEM"] = "1"
    result["GIT_NO_REPLACE_OBJECTS"] = "1"
    result["GIT_TERMINAL_PROMPT"] = "0"
    return result


def _http_auth_args(env: Mapping[str, str]) -> list[str]:
    """Explicit read-only token for credential-free HTTPS verification of a
    PRIVATE origin. Empty when no token -> public-repo behaviour unchanged.
    Token rides argv only (never persisted). GitHub git-over-HTTPS requires
    HTTP Basic (base64("x-access-token:"+token)); Bearer is rejected."""
    token = env.get(BOOTSTRAP_GIT_TOKEN_ENV, "").strip()
    if not token:
        return []
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in token):
        raise BootstrapError("bootstrap Git token contains control characters")
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraHeader=Authorization: Basic {basic}"]


def _assert_safe_local_git_transport(
    repo_root: Path,
    runner: CommandRunner,
    env: Mapping[str, str],
) -> None:
    result = runner.run(
        ["git", "config", "--local", "--name-only", "--null", "--list"],
        cwd=repo_root,
        env=env,
    )
    try:
        keys = [item.lower() for item in result.stdout.decode("utf-8").split("\0") if item]
    except UnicodeDecodeError as exc:
        raise BootstrapError("local Git configuration is not UTF-8") from exc
    exact_unsafe = {
        "core.fsmonitor",
        "core.gitproxy",
        "core.sshcommand",
        "core.worktree",
        "remote.origin.proxy",
        "remote.origin.uploadpack",
        "ssh.variant",
    }
    unsafe = [
        key
        for key in keys
        if key in exact_unsafe
        or key.startswith("http.")
        or key.startswith("protocol.")
        or key == "include.path"
        or (key.startswith("includeif.") and key.endswith(".path"))
        or (
            key.startswith("url.")
            and (key.endswith(".insteadof") or key.endswith(".pushinsteadof"))
        )
    ]
    if unsafe:
        raise BootstrapError(
            "local Git transport configuration can redirect provenance lookup: "
            + ", ".join(sorted(set(unsafe)))
        )


def _materialized_head_tree_sha256(
    repo_root: Path,
    runner: CommandRunner,
    env: Mapping[str, str],
) -> str:
    """Prove every tracked worktree byte and executable bit equals HEAD."""

    result = runner.run(
        ["git", "ls-tree", "-r", "-z", "--full-tree", "HEAD"],
        cwd=repo_root,
        env=env,
    )
    records = [record for record in result.stdout.split(b"\0") if record]
    if not records:
        raise BootstrapError("Git HEAD tree is empty")
    tree_digest = hashlib.sha256()
    for record in records:
        try:
            metadata, raw_path = record.split(b"\t", 1)
            raw_mode, object_type, expected_oid = metadata.split(b" ", 2)
            relative = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise BootstrapError("Git HEAD tree inventory is malformed") from exc
        if (
            object_type != b"blob"
            or raw_mode not in {b"100644", b"100755", b"120000"}
            or not re.fullmatch(rb"[0-9a-f]{40}", expected_oid)
        ):
            raise BootstrapError("Git HEAD contains an unsupported object")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise BootstrapError("Git HEAD path escapes the worktree")
        path = repo_root / relative_path
        try:
            metadata_before = path.lstat()
        except OSError as exc:
            raise BootstrapError(f"tracked worktree path is missing: {relative}") from exc
        if raw_mode == b"120000":
            if not stat.S_ISLNK(metadata_before.st_mode):
                raise BootstrapError(f"tracked symlink type changed: {relative}")
            try:
                content = os.readlink(os.fsencode(path))
                metadata_after = path.lstat()
            except OSError as exc:
                raise BootstrapError(f"cannot read tracked symlink: {relative}") from exc
            if not stat.S_ISLNK(metadata_after.st_mode) or (
                metadata_after.st_dev,
                metadata_after.st_ino,
                metadata_after.st_size,
                metadata_after.st_mtime_ns,
            ) != (
                metadata_before.st_dev,
                metadata_before.st_ino,
                metadata_before.st_size,
                metadata_before.st_mtime_ns,
            ):
                raise BootstrapError(f"tracked symlink changed while hashing: {relative}")
            content_size = len(content)
            digest = hashlib.sha1(usedforsecurity=False)
            digest.update(f"blob {content_size}\0".encode())
            digest.update(content)
        else:
            if not stat.S_ISREG(metadata_before.st_mode):
                raise BootstrapError(f"tracked file type changed: {relative}")
            expected_executable = raw_mode == b"100755"
            actual_executable = bool(metadata_before.st_mode & 0o111)
            if actual_executable != expected_executable:
                raise BootstrapError(f"tracked executable bit changed: {relative}")
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(path, flags)
            except OSError as exc:
                raise BootstrapError(f"cannot open tracked file: {relative}") from exc
            try:
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (metadata_before.st_dev, metadata_before.st_ino)
                    or opened.st_size != metadata_before.st_size
                ):
                    raise BootstrapError(f"tracked file identity changed: {relative}")
                digest = hashlib.sha1(usedforsecurity=False)
                digest.update(f"blob {opened.st_size}\0".encode())
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
                after = os.fstat(descriptor)
                if (after.st_dev, after.st_ino, after.st_size) != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                ):
                    raise BootstrapError(f"tracked file changed while hashing: {relative}")
            finally:
                os.close(descriptor)
        actual_oid = digest.hexdigest().encode()
        if actual_oid != expected_oid:
            raise BootstrapError(f"tracked worktree bytes differ from HEAD: {relative}")
        tree_digest.update(raw_mode)
        tree_digest.update(b"\0")
        tree_digest.update(raw_path)
        tree_digest.update(b"\0")
        tree_digest.update(actual_oid)
        tree_digest.update(b"\0")
    return tree_digest.hexdigest()


def _assert_tracked_terraform_source(
    repo_root: Path,
    runner: CommandRunner,
    env: Mapping[str, str],
) -> None:
    """Reject ignored overrides and index flags that can hide source changes."""

    git_env = _git_environment(env)
    tracked_result = runner.run(
        ["git", "ls-files", "-z", "--", "infra/terraform"],
        cwd=repo_root,
        env=git_env,
    )
    indexed_result = runner.run(
        ["git", "ls-files", "-v", "-z", "--", "infra/terraform"],
        cwd=repo_root,
        env=git_env,
    )
    try:
        tracked_paths = [item for item in tracked_result.stdout.decode("utf-8").split("\0") if item]
        indexed_records = [
            item for item in indexed_result.stdout.decode("utf-8").split("\0") if item
        ]
    except UnicodeDecodeError as exc:
        raise BootstrapError("Terraform source inventory is not UTF-8") from exc
    if not tracked_paths or len(indexed_records) != len(tracked_paths):
        raise BootstrapError("Terraform source inventory is incomplete")
    unsafe_index_records = [record for record in indexed_records if not record.startswith("H ")]
    if unsafe_index_records:
        raise BootstrapError("Terraform source uses skip-worktree/assume-unchanged/index state")

    terraform_relative = Path("infra/terraform")
    tracked_configuration = {
        Path(path).name
        for path in tracked_paths
        if Path(path).parent == terraform_relative
        and (path.endswith(".tf") or path.endswith(".tf.json"))
    }
    terraform_dir = repo_root / terraform_relative
    try:
        actual_configuration = {
            entry.name
            for entry in terraform_dir.iterdir()
            if entry.name.endswith(".tf") or entry.name.endswith(".tf.json")
        }
    except OSError as exc:
        raise BootstrapError("cannot inventory Terraform configuration") from exc
    if not tracked_configuration or actual_configuration != tracked_configuration:
        raise BootstrapError(
            "Terraform configuration includes an untracked/ignored override or is missing"
        )


def _validate_repository(
    repo_root: Path,
    runner: CommandRunner,
    env: Mapping[str, str],
) -> tuple[str, str]:
    git_env = _git_environment(env)
    _assert_safe_local_git_transport(repo_root, runner, git_env)
    status = runner.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
        cwd=repo_root,
        env=git_env,
    )
    if status.stdout:
        raise BootstrapError("bootstrap requires a clean Git worktree")
    _assert_tracked_terraform_source(repo_root, runner, git_env)
    source_tree_sha256 = _materialized_head_tree_sha256(repo_root, runner, git_env)
    symbolic = runner.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"],
        cwd=repo_root,
        env=git_env,
        check=False,
    )
    if symbolic.returncode == 0:
        raise BootstrapError("bootstrap must run from a detached origin/dev commit")
    if symbolic.returncode not in {1, 128}:
        raise BootstrapError("bootstrap could not prove detached HEAD")
    origin = (
        runner.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo_root,
            env=git_env,
        )
        .stdout.decode()
        .strip()
    )
    if origin != EXPECTED_ORIGIN:
        raise BootstrapError("bootstrap Git origin is not allowlisted")
    commit = (
        runner.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=repo_root,
            env=git_env,
        )
        .stdout.decode()
        .strip()
    )
    if not SHA1_RE.fullmatch(commit):
        raise BootstrapError("bootstrap Git commit is not a full SHA-1")
    tracking_commit = (
        runner.run(
            [
                "git",
                "rev-parse",
                "--verify",
                "refs/remotes/origin/dev^{commit}",
            ],
            cwd=repo_root,
            env=git_env,
        )
        .stdout.decode()
        .strip()
    )
    if tracking_commit != commit:
        raise BootstrapError("detached bootstrap commit is not exact local origin/dev")
    auth_args = _http_auth_args(git_env)
    remote = (
        runner.run(
            [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "http.followRedirects=false",
                "-c",
                "http.sslVerify=true",
                *auth_args,
                "ls-remote",
                "--exit-code",
                "--heads",
                EXPECTED_REMOTE_LOOKUP,
                "refs/heads/dev",
            ],
            cwd=repo_root,
            env=git_env,
        )
        .stdout.decode()
        .splitlines()
    )
    exact = [line.split() for line in remote if line.split()[1:] == ["refs/heads/dev"]]
    if len(exact) != 1 or exact[0][0] != commit:
        raise BootstrapError("local dev must equal the fresh protected remote head")
    return commit, source_tree_sha256


def _assert_repository_unchanged(
    repo_root: Path,
    runner: CommandRunner,
    env: Mapping[str, str],
    *,
    commit: str,
    source_tree_sha256: str,
) -> None:
    git_env = _git_environment(env)
    _assert_safe_local_git_transport(repo_root, runner, git_env)
    current = (
        runner.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=repo_root,
            env=git_env,
        )
        .stdout.decode()
        .strip()
    )
    if current != commit:
        raise BootstrapError("bootstrap Git commit changed after review")
    symbolic = runner.run(
        ["git", "symbolic-ref", "--quiet", "HEAD"],
        cwd=repo_root,
        env=git_env,
        check=False,
    )
    if symbolic.returncode == 0:
        raise BootstrapError("bootstrap detached HEAD changed after review")
    if symbolic.returncode not in {1, 128}:
        raise BootstrapError("bootstrap could not re-prove detached HEAD")
    origin = (
        runner.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=repo_root,
            env=git_env,
        )
        .stdout.decode()
        .strip()
    )
    if origin != EXPECTED_ORIGIN:
        raise BootstrapError("bootstrap Git origin changed after review")
    tracking_commit = (
        runner.run(
            [
                "git",
                "rev-parse",
                "--verify",
                "refs/remotes/origin/dev^{commit}",
            ],
            cwd=repo_root,
            env=git_env,
        )
        .stdout.decode()
        .strip()
    )
    if tracking_commit != commit:
        raise BootstrapError("bootstrap origin/dev changed after review")
    status = runner.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ],
        cwd=repo_root,
        env=git_env,
    )
    if status.stdout:
        raise BootstrapError("bootstrap worktree changed after review")
    _assert_tracked_terraform_source(repo_root, runner, git_env)
    if _materialized_head_tree_sha256(repo_root, runner, git_env) != source_tree_sha256:
        raise BootstrapError("materialized Git source tree changed after review")


def _assert_no_terraform_auto_inputs(terraform_dir: Path) -> None:
    forbidden: list[str] = []
    try:
        entries = list(terraform_dir.iterdir())
    except OSError as exc:
        raise BootstrapError("cannot inventory Terraform source directory") from exc
    for entry in entries:
        name = entry.name
        if (
            name in {"terraform.tfvars", "terraform.tfvars.json"}
            or name.endswith(".auto.tfvars")
            or name.endswith(".auto.tfvars.json")
        ):
            forbidden.append(name)
    if forbidden:
        raise BootstrapError(
            "automatic Terraform variable files are forbidden during bootstrap: "
            + ", ".join(sorted(forbidden))
        )


def _assert_file_hashes(
    expected: Mapping[Path, str],
    *,
    label: str,
) -> None:
    for path, expected_sha256 in expected.items():
        if sha256_file(path) != expected_sha256:
            raise BootstrapError(f"{label} changed after review: {path.name}")


def _preflight_existing_objects(
    runner: CommandRunner,
    *,
    cwd: Path,
    env: Mapping[str, str],
    contract: BootstrapContract,
) -> None:
    required = _mapping(
        contract.raw["required_existing_aws_objects"],
        label="required_existing_aws_objects",
    )
    _exact_keys(
        required,
        {"codebuild_projects", "s3_buckets", "dynamodb_tables", "iam_users"},
        label="required_existing_aws_objects",
    )
    projects = _string_list(required["codebuild_projects"], label="codebuild_projects")
    project_result = _decode_json_result(
        _aws(
            runner,
            ["codebuild", "batch-get-projects", "--names", *projects, "--output", "json"],
            cwd=cwd,
            env=env,
            region=contract.region,
        ),
        label="CodeBuild prerequisite inventory",
    )
    actual_projects = project_result.get("projects")
    missing_projects = project_result.get("projectsNotFound")
    if (
        not isinstance(actual_projects, list)
        or {item.get("name") for item in actual_projects if isinstance(item, dict)} != set(projects)
        or missing_projects not in (None, [])
    ):
        raise BootstrapError("existing quarantine builder prerequisite is absent/ambiguous")
    for item in actual_projects:
        project = _mapping(item, label="CodeBuild prerequisite project")
        name = _string(project.get("name"), label="CodeBuild prerequisite name")
        expected_arn = f"arn:aws:codebuild:{contract.region}:{contract.account_id}:project/{name}"
        if project.get("arn") != expected_arn:
            raise BootstrapError("existing quarantine builder ARN is not exact")
    for bucket in _string_list(required["s3_buckets"], label="s3_buckets"):
        _aws(
            runner,
            [
                "s3api",
                "head-bucket",
                "--bucket",
                bucket,
                "--expected-bucket-owner",
                contract.account_id,
            ],
            cwd=cwd,
            env=env,
            region=contract.region,
        )
    for table in _string_list(required["dynamodb_tables"], label="dynamodb_tables"):
        table_result = _decode_json_result(
            _aws(
                runner,
                ["dynamodb", "describe-table", "--table-name", table, "--output", "json"],
                cwd=cwd,
                env=env,
                region=contract.region,
            ),
            label=f"DynamoDB prerequisite {table}",
        )
        table_value = _mapping(table_result.get("Table"), label=f"{table}.Table")
        expected_arn = f"arn:aws:dynamodb:{contract.region}:{contract.account_id}:table/{table}"
        if (
            table_value.get("TableStatus") != "ACTIVE"
            or table_value.get("TableArn") != expected_arn
            or table_value.get("KeySchema") != [{"AttributeName": "LockID", "KeyType": "HASH"}]
            or table_value.get("AttributeDefinitions")
            != [{"AttributeName": "LockID", "AttributeType": "S"}]
        ):
            raise BootstrapError(f"DynamoDB prerequisite contract differs: {table}")
    for user in _string_list(required["iam_users"], label="iam_users"):
        user_result = _decode_json_result(
            _aws(
                runner,
                ["iam", "get-user", "--user-name", user, "--output", "json"],
                cwd=cwd,
                env=env,
                region=contract.region,
            ),
            label=f"IAM prerequisite {user}",
        )
        expected_arn = f"arn:aws:iam::{contract.account_id}:user/{user}"
        if _mapping(user_result.get("User"), label=f"{user}.User").get("Arn") != expected_arn:
            raise BootstrapError(f"IAM prerequisite identity mismatch: {user}")


def _created_resource_addresses(plan_value: Any) -> tuple[str, ...]:
    plan = _mapping(plan_value, label="Terraform plan")
    raw_changes = plan.get("resource_changes")
    if not isinstance(raw_changes, list):
        raise BootstrapError("Terraform plan resource_changes must be an array")
    created: list[str] = []
    for raw_change in raw_changes:
        change = _mapping(raw_change, label="Terraform resource change")
        address = _string(change.get("address"), label="Terraform resource address")
        detail = _mapping(change.get("change"), label=f"{address}.change")
        if detail.get("actions") == ["create"]:
            created.append(address)
    return tuple(sorted(created))


def _require_absence_error(
    result: CommandResult,
    *,
    label: str,
    markers: Sequence[str],
) -> None:
    if result.returncode == 0:
        raise BootstrapError(f"unowned upsert-style AWS object already exists: {label}")
    error = result.stderr.decode("utf-8", errors="replace")
    if not any(marker in error for marker in markers):
        raise BootstrapError(f"could not prove upsert-style AWS object absence: {label}")


def _assert_upsert_create_ownership(
    runner: CommandRunner,
    *,
    cwd: Path,
    env: Mapping[str, str],
    contract: BootstrapContract,
    plan_value: Any,
    before_addresses: set[str],
) -> None:
    """Prove every Put-style Terraform create cannot overwrite an AWS object."""

    for address in _created_resource_addresses(plan_value):
        base = normalize_address(address)
        resource_type = base.split(".", 1)[0]
        if resource_type not in UPSERT_RESOURCE_TYPES:
            continue
        if address in before_addresses or base in before_addresses:
            # A direct main-state owner makes an upsert safe, although the
            # create-only plan validator should normally classify it as no-op.
            continue

        inline = INLINE_POLICY_OWNERSHIP.get(base)
        if inline is not None:
            owner_type, owner_name, policy_name = inline
            command = (
                ["iam", "get-role-policy", "--role-name", owner_name]
                if owner_type == "role"
                else ["iam", "get-user-policy", "--user-name", owner_name]
            )
            result = _aws(
                runner,
                [*command, "--policy-name", policy_name, "--output", "json"],
                cwd=cwd,
                env=env,
                region=contract.region,
                check=False,
            )
            _require_absence_error(
                result,
                label=f"{owner_type}/{owner_name}/inline-policy/{policy_name}",
                markers=("NoSuchEntity", "NoSuchEntityException"),
            )
            continue

        repository = ECR_LIFECYCLE_OWNERSHIP.get(base)
        if repository is not None:
            result = _aws(
                runner,
                [
                    "ecr",
                    "get-lifecycle-policy",
                    "--repository-name",
                    repository,
                    "--output",
                    "json",
                ],
                cwd=cwd,
                env=env,
                region=contract.region,
                check=False,
            )
            _require_absence_error(
                result,
                label=f"ecr/{repository}/lifecycle-policy",
                markers=(
                    "LifecyclePolicyNotFoundException",
                    "RepositoryNotFoundException",
                ),
            )
            continue

        s3_probe = S3_UPSERT_OWNERSHIP.get(base)
        if s3_probe is not None:
            bucket, command = s3_probe
            result = _aws(
                runner,
                [
                    "s3api",
                    command,
                    "--bucket",
                    bucket,
                    "--expected-bucket-owner",
                    contract.account_id,
                    "--output",
                    "json",
                ],
                cwd=cwd,
                env=env,
                region=contract.region,
                check=False,
            )
            if command == "get-bucket-versioning" and result.returncode == 0:
                versioning = _decode_json_result(
                    result,
                    label=f"S3 versioning absence {bucket}",
                )
                if set(versioning) <= {"ResponseMetadata"}:
                    continue
            _require_absence_error(
                result,
                label=f"s3/{bucket}/{command}",
                markers=(
                    "NoSuchBucket",
                    "NoSuchBucketPolicy",
                    "NoSuchConfiguration",
                    "NoSuchLifecycleConfiguration",
                    "NoSuchPublicAccessBlockConfiguration",
                    "ObjectLockConfigurationNotFoundError",
                    "ServerSideEncryptionConfigurationNotFoundError",
                ),
            )
            continue

        raise BootstrapError(
            f"upsert-style create lacks an exact AWS ownership probe: {address}"
        )


def _terraform(
    runner: CommandRunner,
    arguments: Sequence[str],
    *,
    terraform_dir: Path,
    env: Mapping[str, str],
) -> CommandResult:
    return runner.run(
        ["terraform", f"-chdir={terraform_dir}", *arguments],
        cwd=terraform_dir.parent,
        env=env,
    )


def _validate_local_toolchain(
    runner: CommandRunner,
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> Mapping[str, str]:
    credential_free_env = _git_environment(env)
    aws_result = runner.run(
        ["aws", "--version"],
        cwd=cwd,
        env=credential_free_env,
    )
    aws_text = (aws_result.stdout + aws_result.stderr).decode("utf-8", errors="replace").strip()
    aws_match = re.fullmatch(r"aws-cli/(2\.[^\s]+)(?:\s.*)?", aws_text)
    if aws_match is None:
        raise BootstrapError("bootstrap requires AWS CLI v2")

    terraform_result = runner.run(
        ["terraform", "version", "-json"],
        cwd=cwd,
        env=credential_free_env,
    )
    terraform_value = _decode_json_result(
        terraform_result,
        label="Terraform version",
    )
    terraform_version = terraform_value.get("terraform_version")
    version_match = (
        re.fullmatch(r"1\.12\.([0-9]+)(?:[-+][0-9A-Za-z.-]+)?", terraform_version)
        if isinstance(terraform_version, str)
        else None
    )
    if version_match is None:
        raise BootstrapError("bootstrap requires reviewed Terraform 1.12.x")

    python_result = runner.run(
        ["python3", "--version"],
        cwd=cwd,
        env=credential_free_env,
    )
    python_text = (
        (python_result.stdout + python_result.stderr).decode("utf-8", errors="replace").strip()
    )
    python_match = re.fullmatch(r"Python (3)\.([0-9]+)\.([0-9]+)", python_text)
    if python_match is None or int(python_match.group(2)) < 11:
        raise BootstrapError("bootstrap requires Python 3.11 or newer")
    pinned_python = runner.tool_evidence().get("python3", {}).get("path")
    if not isinstance(pinned_python, str) or Path(sys.executable).resolve() != Path(pinned_python):
        raise BootstrapError("bootstrap interpreter differs from the pinned python3")

    git_result = runner.run(
        ["git", "--version"],
        cwd=cwd,
        env=credential_free_env,
    )
    git_text = git_result.stdout.decode("utf-8", errors="replace").strip()
    git_match = re.fullmatch(r"git version ([0-9]+(?:\.[0-9]+)+(?:[^\r\n]*)?)", git_text)
    if git_match is None:
        raise BootstrapError("Git version output is malformed")
    return {
        "aws_cli": aws_match.group(1),
        "terraform": str(terraform_version),
        "python": ".".join(python_match.groups()),
        "git": git_match.group(1),
    }


def _ledger_typed_item(
    contract: BootstrapContract,
    *,
    nonce: str,
    commit: str,
    plan_sha256: str,
    now: int,
) -> dict[str, dict[str, str]]:
    return {
        "LockID": {"S": str(contract.backend["ledger_key"])},
        "RecordType": {"S": contract.bootstrap_id},
        "State": {"S": "PREPARED"},
        "BootstrapNonce": {"S": nonce},
        "ControlCommit": {"S": commit},
        "PlanSha256": {"S": plan_sha256},
        "CreatedAtEpoch": {"N": str(now)},
    }


def _ledger_update(
    runner: CommandRunner,
    *,
    cwd: Path,
    env: Mapping[str, str],
    contract: BootstrapContract,
    nonce: str,
    expected_state: str,
    next_state: str,
    extra_values: Mapping[str, str] | None = None,
) -> Mapping[str, Any]:
    names = {"#state": "State"}
    values: dict[str, dict[str, str]] = {
        ":expected": {"S": expected_state},
        ":next": {"S": next_state},
        ":nonce": {"S": nonce},
    }
    assignments = ["#state = :next"]
    for index, (name, value) in enumerate(sorted((extra_values or {}).items())):
        name_token = f"#extra{index}"
        value_token = f":extra{index}"
        names[name_token] = name
        values[value_token] = {"S": value}
        assignments.append(f"{name_token} = {value_token}")
    key = {"LockID": {"S": str(contract.backend["ledger_key"])}}
    result = _aws(
        runner,
        [
            "dynamodb",
            "update-item",
            "--table-name",
            str(contract.backend["dynamodb_table"]),
            "--key",
            json.dumps(key, separators=(",", ":")),
            "--update-expression",
            f"SET {', '.join(assignments)}",
            "--condition-expression",
            "#state = :expected AND BootstrapNonce = :nonce",
            "--expression-attribute-names",
            json.dumps(names, separators=(",", ":")),
            "--expression-attribute-values",
            json.dumps(values, separators=(",", ":")),
            "--return-values",
            "ALL_NEW",
            "--output",
            "json",
        ],
        cwd=cwd,
        env=env,
        region=contract.region,
    )
    response = _decode_json_result(result, label=f"ledger transition {next_state}")
    attributes = _mapping(
        response.get("Attributes"),
        label=f"ledger transition {next_state} attributes",
    )
    state_value = _mapping(
        attributes.get("State"),
        label=f"ledger transition {next_state} state",
    )
    nonce_value = _mapping(
        attributes.get("BootstrapNonce"),
        label=f"ledger transition {next_state} nonce",
    )
    if state_value != {"S": next_state} or nonce_value != {"S": nonce}:
        raise BootstrapError(f"ledger transition {next_state} returned unsafe state")
    return response


def _read_bootstrap_ledger_item(
    runner: CommandRunner,
    *,
    cwd: Path,
    env: Mapping[str, str],
    contract: BootstrapContract,
    nonce: str,
) -> Mapping[str, Any] | None:
    key = {"LockID": {"S": str(contract.backend["ledger_key"])}}
    result = _aws(
        runner,
        [
            "dynamodb",
            "get-item",
            "--table-name",
            str(contract.backend["dynamodb_table"]),
            "--key",
            json.dumps(key, separators=(",", ":")),
            "--consistent-read",
            "--output",
            "json",
        ],
        cwd=cwd,
        env=env,
        region=contract.region,
    )
    response = _decode_json_result(result, label="bootstrap ledger read")
    raw_item = response.get("Item")
    if raw_item is None:
        return None
    item = _mapping(raw_item, label="bootstrap ledger item")
    if (
        item.get("LockID") != {"S": str(contract.backend["ledger_key"])}
        or item.get("RecordType") != {"S": contract.bootstrap_id}
        or item.get("BootstrapNonce") != {"S": nonce}
    ):
        raise BootstrapError("bootstrap ledger is owned by a different invocation")
    state = _mapping(item.get("State"), label="bootstrap ledger state")
    state_name = state.get("S")
    if state_name not in {
        "PREPARED",
        "APPLYING",
        "CONSUMED",
        "RECONCILE_REQUIRED",
    }:
        raise BootstrapError("bootstrap ledger returned an unknown state")
    return item


def _read_bootstrap_ledger_state(
    runner: CommandRunner,
    *,
    cwd: Path,
    env: Mapping[str, str],
    contract: BootstrapContract,
    nonce: str,
) -> str | None:
    item = _read_bootstrap_ledger_item(
        runner,
        cwd=cwd,
        env=env,
        contract=contract,
        nonce=nonce,
    )
    if item is None:
        return None
    return str(_mapping(item["State"], label="bootstrap ledger state")["S"])


def _assert_bootstrap_ledger_absent(
    runner: CommandRunner,
    *,
    cwd: Path,
    env: Mapping[str, str],
    contract: BootstrapContract,
) -> None:
    key = {"LockID": {"S": str(contract.backend["ledger_key"])}}
    result = _aws(
        runner,
        [
            "dynamodb",
            "get-item",
            "--table-name",
            str(contract.backend["dynamodb_table"]),
            "--key",
            json.dumps(key, separators=(",", ":")),
            "--consistent-read",
            "--output",
            "json",
        ],
        cwd=cwd,
        env=env,
        region=contract.region,
    )
    response = _decode_json_result(result, label="one-use bootstrap ledger preflight")
    if response.get("Item") is not None:
        _mapping(response["Item"], label="existing one-use bootstrap ledger item")
        raise BootstrapError("one-use bootstrap ledger already exists")


def _reconcile_ledger_after_failure(
    runner: CommandRunner,
    *,
    cwd: Path,
    env: Mapping[str, str],
    contract: BootstrapContract,
    nonce: str,
    failure_sha256: str,
) -> tuple[str | None, Mapping[str, Any] | None]:
    for expected_state in ("PREPARED", "APPLYING"):
        try:
            response = _ledger_update(
                runner,
                cwd=cwd,
                env=env,
                contract=contract,
                nonce=nonce,
                expected_state=expected_state,
                next_state="RECONCILE_REQUIRED",
                extra_values={"FailureSha256": failure_sha256},
            )
        except Exception:
            continue
        return "RECONCILE_REQUIRED", response
    try:
        observed = _read_bootstrap_ledger_state(
            runner,
            cwd=cwd,
            env=env,
            contract=contract,
            nonce=nonce,
        )
    except Exception:
        return "UNKNOWN_RECONCILIATION_REQUIRED", None
    if observed in {None, "CONSUMED", "RECONCILE_REQUIRED"}:
        return observed, None
    return "UNKNOWN_RECONCILIATION_REQUIRED", None


def _create_seed_stack(
    runner: CommandRunner,
    *,
    repo_root: Path,
    root_env: Mapping[str, str],
    contract: BootstrapContract,
    external_id: str,
    nonce: str,
    commit: str,
) -> str:
    template = contract.path.parent / "seed-stack.yaml"
    response = _decode_json_result(
        _aws(
            runner,
            [
                "cloudformation",
                "create-stack",
                "--stack-name",
                str(contract.seed["stack_name"]),
                "--template-body",
                f"file://{template}",
                "--parameters",
                f"ParameterKey=BootstrapExternalId,ParameterValue={external_id}",
                f"ParameterKey=BootstrapNonce,ParameterValue={nonce}",
                f"ParameterKey=BootstrapCommit,ParameterValue={commit}",
                "--client-request-token",
                f"teamagent-bootstrap-{nonce}",
                "--capabilities",
                "CAPABILITY_NAMED_IAM",
                "--on-failure",
                "DELETE",
                "--tags",
                f"Key=BootstrapId,Value={contract.bootstrap_id}",
                f"Key=BootstrapNonce,Value={nonce}",
                f"Key=ControlCommit,Value={commit}",
                "Key=ManagedBy,Value=TeamAgentBootstrap",
                "--output",
                "json",
            ],
            cwd=repo_root,
            env=root_env,
            region=contract.region,
        ),
        label="seed stack creation",
    )
    stack_id = response.get("StackId")
    if (
        not isinstance(stack_id, str)
        or re.fullmatch(
            rf"arn:aws:cloudformation:{re.escape(contract.region)}:"
            rf"{contract.account_id}:stack/{re.escape(str(contract.seed['stack_name']))}/"
            r"[0-9a-f-]{36}",
            stack_id,
        )
        is None
    ):
        raise BootstrapError("CloudFormation returned an unexpected seed stack ID")
    return stack_id


def _wait_for_seed_stack(
    runner: CommandRunner,
    *,
    repo_root: Path,
    root_env: Mapping[str, str],
    contract: BootstrapContract,
) -> None:
    _aws(
        runner,
        [
            "cloudformation",
            "wait",
            "stack-create-complete",
            "--stack-name",
            str(contract.seed["stack_name"]),
        ],
        cwd=repo_root,
        env=root_env,
        region=contract.region,
    )


def _assert_seed_absent(
    runner: CommandRunner,
    *,
    repo_root: Path,
    root_env: Mapping[str, str],
    contract: BootstrapContract,
) -> None:
    role = _aws(
        runner,
        ["iam", "get-role", "--role-name", str(contract.seed["role_name"])],
        cwd=repo_root,
        env=root_env,
        region=contract.region,
        check=False,
    )
    if role.returncode == 0:
        raise BootstrapError("temporary bootstrap role already exists")
    role_error = role.stderr.decode("utf-8", errors="replace")
    if "NoSuchEntity" not in role_error:
        raise BootstrapError("could not prove temporary bootstrap role absence")

    deny_policy = _aws(
        runner,
        [
            "iam",
            "get-policy",
            "--policy-arn",
            str(contract.seed["deny_policy_arn"]),
        ],
        cwd=repo_root,
        env=root_env,
        region=contract.region,
        check=False,
    )
    if deny_policy.returncode == 0:
        raise BootstrapError("temporary bootstrap deny policy already exists")
    deny_policy_error = deny_policy.stderr.decode("utf-8", errors="replace")
    if "NoSuchEntity" not in deny_policy_error:
        raise BootstrapError("could not prove temporary bootstrap deny policy absence")

    stack = _aws(
        runner,
        [
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            str(contract.seed["stack_name"]),
            "--output",
            "json",
        ],
        cwd=repo_root,
        env=root_env,
        region=contract.region,
        check=False,
    )
    if stack.returncode == 0:
        raise BootstrapError("temporary bootstrap stack already exists")
    stack_error = stack.stderr.decode("utf-8", errors="replace")
    if "does not exist" not in stack_error or str(contract.seed["stack_name"]) not in stack_error:
        raise BootstrapError("could not prove temporary bootstrap stack absence")


def _assume_seed(
    runner: CommandRunner,
    *,
    repo_root: Path,
    root_env: Mapping[str, str],
    contract: BootstrapContract,
    external_id: str,
) -> tuple[Mapping[str, Any], dict[str, str]]:
    assumed = _decode_json_result(
        _aws(
            runner,
            [
                "sts",
                "assume-role",
                "--role-arn",
                str(contract.seed["role_arn"]),
                "--role-session-name",
                str(contract.seed["session_name"]),
                "--source-identity",
                str(contract.seed["source_identity"]),
                "--external-id",
                external_id,
                "--duration-seconds",
                str(contract.seed["max_session_seconds"]),
                "--output",
                "json",
            ],
            cwd=repo_root,
            env=root_env,
            region=contract.region,
        ),
        label="seed AssumeRole",
    )
    credentials = _mapping(assumed.get("Credentials"), label="seed Credentials")
    assumed_user = _mapping(assumed.get("AssumedRoleUser"), label="seed AssumedRoleUser")
    if assumed_user.get("Arn") != contract.seed["session_arn"]:
        raise BootstrapError("STS returned an unexpected seed session ARN")
    expiration = credentials.get("Expiration")
    if not isinstance(expiration, str) or not expiration.endswith("Z"):
        raise BootstrapError("STS seed session expiration is malformed")
    session_env = _session_environment(root_env, credentials, region=contract.region)
    _assert_identity(
        runner,
        cwd=repo_root,
        env=session_env,
        contract=contract,
        expected_arn=str(contract.seed["session_arn"]),
        label="seed session",
    )
    return assumed, session_env


def _exact_tag_map(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, list):
        raise BootstrapError(f"{label} must be an array")
    result: dict[str, str] = {}
    for raw_tag in value:
        tag = _mapping(raw_tag, label=f"{label} tag")
        if set(tag) != {"Key", "Value"}:
            raise BootstrapError(f"{label} tag shape differs")
        key = _string(tag.get("Key"), label=f"{label} tag key")
        tag_value = _string(tag.get("Value"), label=f"{label} tag value")
        if key in result:
            raise BootstrapError(f"{label} contains duplicate tag: {key}")
        result[key] = tag_value
    return result


def _prove_seed_ownership(
    runner: CommandRunner,
    *,
    repo_root: Path,
    root_env: Mapping[str, str],
    contract: BootstrapContract,
    nonce: str,
    commit: str,
    expected_stack_id: str | None,
) -> str:
    response = _decode_json_result(
        _aws(
            runner,
            [
                "cloudformation",
                "describe-stacks",
                "--stack-name",
                str(contract.seed["stack_name"]),
                "--output",
                "json",
            ],
            cwd=repo_root,
            env=root_env,
            region=contract.region,
        ),
        label="seed ownership stack",
    )
    stacks = response.get("Stacks")
    if not isinstance(stacks, list) or len(stacks) != 1:
        raise BootstrapError("seed ownership stack inventory is ambiguous")
    stack = _mapping(stacks[0], label="seed ownership stack")
    stack_id = _string(stack.get("StackId"), label="seed ownership StackId")
    expected_prefix = (
        f"arn:aws:cloudformation:{contract.region}:{contract.account_id}:"
        f"stack/{contract.seed['stack_name']}/"
    )
    if (
        not stack_id.startswith(expected_prefix)
        or stack.get("StackName") != contract.seed["stack_name"]
        or (expected_stack_id is not None and stack_id != expected_stack_id)
    ):
        raise BootstrapError("seed stack identity is not owned by this invocation")
    stack_tags = _exact_tag_map(stack.get("Tags"), label="seed stack")
    expected_stack_tags = {
        "BootstrapId": contract.bootstrap_id,
        "BootstrapNonce": nonce,
        "ControlCommit": commit,
        "ManagedBy": "TeamAgentBootstrap",
    }
    if any(stack_tags.get(key) != value for key, value in expected_stack_tags.items()):
        raise BootstrapError("seed stack ownership tags do not match this invocation")

    parameters = stack.get("Parameters")
    if not isinstance(parameters, list):
        raise BootstrapError("seed stack parameters are missing")
    parameter_map: dict[str, str] = {}
    for raw_parameter in parameters:
        parameter = _mapping(raw_parameter, label="seed stack parameter")
        key = _string(parameter.get("ParameterKey"), label="seed parameter key")
        value = _string(parameter.get("ParameterValue"), label="seed parameter value")
        if key in parameter_map:
            raise BootstrapError(f"seed stack has duplicate parameter: {key}")
        parameter_map[key] = value
    if parameter_map.get("BootstrapNonce") != nonce or parameter_map.get(
        "BootstrapCommit"
    ) != commit:
        raise BootstrapError("seed stack parameters do not match this invocation")

    resource_response = _decode_json_result(
        _aws(
            runner,
            [
                "cloudformation",
                "describe-stack-resources",
                "--stack-name",
                stack_id,
                "--output",
                "json",
            ],
            cwd=repo_root,
            env=root_env,
            region=contract.region,
        ),
        label="seed ownership resources",
    )
    resources = resource_response.get("StackResources")
    if not isinstance(resources, list):
        raise BootstrapError("seed ownership resource inventory is missing")
    physical: dict[str, tuple[str, str]] = {}
    for raw_resource in resources:
        resource = _mapping(raw_resource, label="seed stack resource")
        logical_id = _string(
            resource.get("LogicalResourceId"),
            label="seed resource logical id",
        )
        if logical_id in physical:
            raise BootstrapError(f"seed stack resource is duplicated: {logical_id}")
        physical[logical_id] = (
            _string(resource.get("ResourceType"), label="seed resource type"),
            _string(
                resource.get("PhysicalResourceId"),
                label="seed resource physical id",
            ),
        )
    if physical != {
        "BootstrapDenyPolicy": (
            "AWS::IAM::ManagedPolicy",
            str(contract.seed["deny_policy_arn"]),
        ),
        "BootstrapExecutorRole": (
            "AWS::IAM::Role",
            str(contract.seed["role_name"]),
        ),
    }:
        raise BootstrapError("seed stack resources are not the exact reviewed pair")

    role_response = _decode_json_result(
        _aws(
            runner,
            [
                "iam",
                "get-role",
                "--role-name",
                str(contract.seed["role_name"]),
                "--output",
                "json",
            ],
            cwd=repo_root,
            env=root_env,
            region=contract.region,
        ),
        label="seed ownership role",
    )
    role = _mapping(role_response.get("Role"), label="seed ownership Role")
    if role.get("Arn") != contract.seed["role_arn"]:
        raise BootstrapError("seed role ARN is not exact")
    role_tags = _exact_tag_map(role.get("Tags"), label="seed role")
    expected_role_tags = {
        "BootstrapCommit": commit,
        "BootstrapId": contract.bootstrap_id,
        "BootstrapNonce": nonce,
        "ManagedBy": "CloudFormationTemporarySeed",
        "Project": "TeamAgent",
        "Purpose": "OneTimeProvenanceIamBootstrap",
    }
    if any(role_tags.get(key) != value for key, value in expected_role_tags.items()):
        raise BootstrapError("seed role ownership tags do not match this invocation")

    attached_response = _decode_json_result(
        _aws(
            runner,
            [
                "iam",
                "list-attached-role-policies",
                "--role-name",
                str(contract.seed["role_name"]),
                "--output",
                "json",
            ],
            cwd=repo_root,
            env=root_env,
            region=contract.region,
        ),
        label="seed ownership attached policies",
    )
    attached = attached_response.get("AttachedPolicies")
    if attached != [
        {
            "PolicyName": contract.seed["deny_policy_name"],
            "PolicyArn": contract.seed["deny_policy_arn"],
        }
    ]:
        raise BootstrapError("seed role has an unreviewed managed policy attachment")
    return stack_id


def _revoke_and_delete_seed(
    runner: CommandRunner,
    *,
    repo_root: Path,
    root_env: Mapping[str, str],
    session_env: Mapping[str, str] | None,
    contract: BootstrapContract,
    nonce: str,
    commit: str,
    expected_stack_id: str | None,
) -> dict[str, Any]:
    stack_probe = _aws(
        runner,
        [
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            str(contract.seed["stack_name"]),
            "--output",
            "json",
        ],
        cwd=repo_root,
        env=root_env,
        region=contract.region,
        check=False,
    )
    if stack_probe.returncode != 0:
        stack_error = stack_probe.stderr.decode("utf-8", errors="replace")
        if "does not exist" not in stack_error:
            raise BootstrapError("could not determine seed stack ownership before retirement")
        _assert_seed_absent(
            runner,
            repo_root=repo_root,
            root_env=root_env,
            contract=contract,
        )
        return {
            "already_absent": True,
            "ownership_proved": True,
            "stack_deleted": True,
            "role_deleted": True,
            "deny_policy_deleted": True,
            "session_probe_denied": None,
        }
    owned_stack_id = _prove_seed_ownership(
        runner,
        repo_root=repo_root,
        root_env=root_env,
        contract=contract,
        nonce=nonce,
        commit=commit,
        expected_stack_id=expected_stack_id,
    )
    retired_trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "RetiredBootstrapRole",
                "Effect": "Deny",
                "Principal": {"AWS": "*"},
                "Action": [
                    "sts:AssumeRole",
                    "sts:SetSourceIdentity",
                ],
            }
        ],
    }
    # Close the trust policy first. The token revocation below then covers all
    # sessions that could have been issued before this timestamp; no new
    # session can race stack deletion.
    _aws(
        runner,
        [
            "iam",
            "update-assume-role-policy",
            "--role-name",
            str(contract.seed["role_name"]),
            "--policy-document",
            json.dumps(retired_trust, separators=(",", ":")),
        ],
        cwd=repo_root,
        env=root_env,
        region=contract.region,
    )
    trust_closed_at = dt.datetime.now(dt.UTC).replace(microsecond=0)
    # Cover the already-issued session plus any session minted against a stale
    # trust-policy cache before stack deletion. The role cannot issue a session
    # longer than this window and the deny remains attached until role deletion.
    sessions_issued_through = trust_closed_at + dt.timedelta(
        seconds=int(contract.seed["max_session_seconds"])
    )
    revoke_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "RevokeBootstrapSessions",
                "Effect": "Deny",
                "Action": "*",
                "Resource": "*",
                "Condition": {
                    "DateLessThanEquals": {
                        "aws:TokenIssueTime": (
                            sessions_issued_through.isoformat().replace("+00:00", "Z")
                        )
                    }
                },
            }
        ],
    }
    _aws(
        runner,
        [
            "iam",
            "put-role-policy",
            "--role-name",
            str(contract.seed["role_name"]),
            "--policy-name",
            str(contract.seed["inline_policy_name"]),
            "--policy-document",
            json.dumps(revoke_policy, separators=(",", ":")),
        ],
        cwd=repo_root,
        env=root_env,
        region=contract.region,
    )
    session_revoked = None
    if session_env is not None:
        for attempt in range(12):
            probe = _aws(
                runner,
                [
                    "codeconnections",
                    "list-connections",
                    "--max-results",
                    "1",
                    "--output",
                    "json",
                ],
                cwd=repo_root,
                env=session_env,
                region=contract.region,
                check=False,
            )
            if probe.returncode == 0:
                if attempt == 11:
                    raise BootstrapError("bootstrap session remained authorized after revocation")
                time.sleep(5)
                continue
            probe_error = probe.stderr.decode("utf-8", errors="replace")
            if not any(
                marker in probe_error
                for marker in ("AccessDenied", "ExpiredToken", "explicit deny", "not authorized")
            ):
                raise BootstrapError("could not prove bootstrap session revocation")
            session_revoked = True
            break
    _aws(
        runner,
        [
            "cloudformation",
            "delete-stack",
            "--stack-name",
            owned_stack_id,
        ],
        cwd=repo_root,
        env=root_env,
        region=contract.region,
    )
    _aws(
        runner,
        [
            "cloudformation",
            "wait",
            "stack-delete-complete",
            "--stack-name",
            owned_stack_id,
        ],
        cwd=repo_root,
        env=root_env,
        region=contract.region,
    )
    role_probe = _aws(
        runner,
        ["iam", "get-role", "--role-name", str(contract.seed["role_name"])],
        cwd=repo_root,
        env=root_env,
        region=contract.region,
        check=False,
    )
    if role_probe.returncode == 0:
        raise BootstrapError("temporary bootstrap role still exists after stack deletion")
    role_error = role_probe.stderr.decode("utf-8", errors="replace")
    if "NoSuchEntity" not in role_error:
        raise BootstrapError("could not prove temporary bootstrap role deletion")
    deny_policy_probe = _aws(
        runner,
        [
            "iam",
            "get-policy",
            "--policy-arn",
            str(contract.seed["deny_policy_arn"]),
        ],
        cwd=repo_root,
        env=root_env,
        region=contract.region,
        check=False,
    )
    if deny_policy_probe.returncode == 0:
        raise BootstrapError("temporary bootstrap deny policy still exists after stack deletion")
    deny_policy_error = deny_policy_probe.stderr.decode("utf-8", errors="replace")
    if "NoSuchEntity" not in deny_policy_error:
        raise BootstrapError("could not prove temporary bootstrap deny policy deletion")
    return {
        "trust_closed_at": trust_closed_at.isoformat().replace("+00:00", "Z"),
        "sessions_issued_through_denied": (
            sessions_issued_through.isoformat().replace("+00:00", "Z")
        ),
        "trust_closed_before_revocation": True,
        "ownership_proved": True,
        "owned_stack_id": owned_stack_id,
        "session_probe_denied": session_revoked,
        "stack_deleted": True,
        "role_deleted": True,
        "deny_policy_deleted": True,
    }


def _list_github_connections(
    runner: CommandRunner,
    *,
    repo_root: Path,
    env: Mapping[str, str],
    contract: BootstrapContract,
) -> list[Mapping[str, Any]]:
    response = _decode_json_result(
        _aws(
            runner,
            [
                "codeconnections",
                "list-connections",
                "--max-results",
                "100",
                "--output",
                "json",
            ],
            cwd=repo_root,
            env=env,
            region=contract.region,
        ),
        label="CodeConnections inventory",
    )
    if response.get("NextToken") not in (None, ""):
        raise BootstrapError("CodeConnections inventory is truncated")
    connections = response.get("Connections")
    if not isinstance(connections, list):
        raise BootstrapError("CodeConnections inventory is malformed")
    if not all(isinstance(item, dict) for item in connections):
        raise BootstrapError("CodeConnections inventory contains malformed entries")
    return connections


def _validate_connection(
    item: Mapping[str, Any],
    *,
    name: str,
    contract: BootstrapContract,
) -> dict[str, str]:
    arn = item.get("ConnectionArn")
    status_value = item.get("ConnectionStatus")
    provider = item.get("ProviderType")
    owner_account_id = item.get("OwnerAccountId")
    if (
        item.get("ConnectionName") != name
        or not isinstance(arn, str)
        or re.fullmatch(
            rf"arn:aws:(?:codeconnections|codestar-connections):"
            rf"{re.escape(contract.region)}:{contract.account_id}:connection/[0-9a-f-]+",
            arn,
        )
        is None
        or provider != "GitHub"
        or owner_account_id != contract.account_id
        or status_value not in {"PENDING", "AVAILABLE"}
    ):
        raise BootstrapError(f"CodeConnection has an unsafe state: {name}")
    return {"name": name, "arn": arn, "status": str(status_value)}


def _assert_connection_ownership_before_apply(
    runner: CommandRunner,
    *,
    repo_root: Path,
    env: Mapping[str, str],
    contract: BootstrapContract,
    before_addresses: set[str],
    created_addresses: Sequence[str],
) -> None:
    connections = _list_github_connections(
        runner,
        repo_root=repo_root,
        env=env,
        contract=contract,
    )
    expected_names = _string_list(contract.raw["connection_names"], label="connection_names")
    address_by_name = {
        expected_names[0]: "aws_codestarconnections_connection.openclaw_codebuild",
        expected_names[1]: "aws_codestarconnections_connection.tiktok_codebuild",
    }
    normalized_before = {normalize_address(address) for address in before_addresses}
    normalized_created = {normalize_address(address) for address in created_addresses}
    for name, address in address_by_name.items():
        matches = [item for item in connections if item.get("ConnectionName") == name]
        if address in normalized_before:
            if len(matches) != 1:
                raise BootstrapError(f"main-state CodeConnection is missing or ambiguous: {name}")
            _validate_connection(matches[0], name=name, contract=contract)
        elif address in normalized_created:
            if matches:
                raise BootstrapError(
                    f"planned CodeConnection already exists outside main state: {name}"
                )
        elif matches:
            raise BootstrapError(f"disabled CodeConnection exists outside main state: {name}")


def _connection_inventory(
    runner: CommandRunner,
    *,
    repo_root: Path,
    env: Mapping[str, str],
    contract: BootstrapContract,
    after_addresses: set[str],
) -> list[dict[str, str]]:
    connections = _list_github_connections(
        runner,
        repo_root=repo_root,
        env=env,
        contract=contract,
    )
    expected_names = _string_list(contract.raw["connection_names"], label="connection_names")
    tiktok_owned = any(
        normalize_address(address) == "aws_codestarconnections_connection.tiktok_codebuild"
        for address in after_addresses
    )
    required_names = {expected_names[0]}
    if tiktok_owned:
        required_names.add(expected_names[1])
    selected: list[dict[str, str]] = []
    for name in expected_names:
        matches = [
            item
            for item in connections
            if isinstance(item, dict) and item.get("ConnectionName") == name
        ]
        if name not in required_names and not matches:
            continue
        if len(matches) != 1:
            raise BootstrapError(f"CodeConnection is missing or ambiguous: {name}")
        selected.append(_validate_connection(matches[0], name=name, contract=contract))
    return sorted(selected, key=lambda item: item["name"])


def _handoff_documents(
    *,
    contract: BootstrapContract,
    nonce: str,
    commit: str,
    source_tree_sha256: str,
    contract_sha256: str,
    seed_template_sha256: str,
    tfvars_sha256: str,
    release_hashes: Mapping[str, str],
    tool_versions: Mapping[str, str],
    tool_evidence: Mapping[str, Mapping[str, Any]],
    plan_sha256: str,
    plan_validation: PlanValidation,
    handoff: HandoffValidation,
    before_state: Any,
    after_state: Any,
    connections: Sequence[Mapping[str, Any]],
    seed_stack_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    before_addresses = state_addresses(before_state)
    after_addresses = state_addresses(after_state)
    claims = {
        "kind": "teamagent-provenance-bootstrap-handoff-claims",
        "schema_version": 1,
        "bootstrap_id": contract.bootstrap_id,
        "account_id": contract.account_id,
        "region": contract.region,
        "bootstrap_nonce": nonce,
        "control_commit": commit,
        "source_tree_sha256": source_tree_sha256,
        "contract_sha256": contract_sha256,
        "seed_template_sha256": seed_template_sha256,
        "tfvars_sha256": tfvars_sha256,
        "release_contract_sha256": dict(release_hashes),
        "toolchain": {
            "versions": dict(tool_versions),
            "executables": dict(tool_evidence),
        },
        "plan_sha256": plan_sha256,
        "created_addresses": list(plan_validation.created_addresses),
        "no_op_addresses": list(plan_validation.no_op_addresses),
        "required_main_state_addresses": sorted(contract.required_main_state),
        "owned_main_state_addresses": sorted(after_addresses),
        "main_state": {
            "lineage": handoff.lineage,
            "serial_before": handoff.before_serial,
            "serial_after": handoff.after_serial,
            "addresses_sha256_before": handoff.before_addresses_sha256,
            "addresses_sha256_after": handoff.after_addresses_sha256,
        },
        "connections": list(connections),
    }
    ownership = {
        "kind": "teamagent-provenance-bootstrap-main-state-ownership",
        "schema_version": 1,
        "bootstrap_id": contract.bootstrap_id,
        "bootstrap_nonce": nonce,
        "control_commit": commit,
        "seed_stack_id": seed_stack_id,
        "plan_sha256": plan_sha256,
        "lineage": handoff.lineage,
        "serial_before": handoff.before_serial,
        "serial_after": handoff.after_serial,
        "addresses_before": sorted(before_addresses),
        "addresses_after": sorted(after_addresses),
        "created_addresses": list(plan_validation.created_addresses),
        "no_op_addresses": list(plan_validation.no_op_addresses),
        "required_main_state_addresses": sorted(contract.required_main_state),
        "bootstrap_state_owns_main_objects": False,
    }
    return claims, ownership


def run_bootstrap(
    *,
    repo_root: Path,
    var_file: Path,
    artifact_dir: Path,
    contract_path: Path,
    runner: CommandRunner | None = None,
    process_env: Mapping[str, str] | None = None,
) -> Path:
    """Execute the real bootstrap workflow. Every AWS mutation is operator-triggered."""

    runner = runner or CommandRunner()
    source_env = os.environ if process_env is None else process_env
    _reject_influential_environment(source_env)
    base_env = _clean_aws_environment(source_env)
    repo_root = repo_root.resolve(strict=True)
    expected_contract_path = (
        repo_root / "infra" / "bootstrap" / "bootstrap_contract.json"
    ).resolve(strict=True)
    contract_path = contract_path.resolve(strict=True)
    if contract_path != expected_contract_path:
        raise BootstrapError("bootstrap contract path is not the fixed repository control")
    artifact_candidate = artifact_dir.resolve(strict=False)
    try:
        artifact_candidate.relative_to(repo_root)
    except ValueError:
        pass
    else:
        raise BootstrapError("bootstrap artifacts must be outside the Git worktree")
    contract = load_contract(contract_path)

    # Local fail-closed checks happen before even GetCallerIdentity.
    release_hashes = validate_release_contracts(repo_root, contract)
    var_file = _secure_existing_file(var_file)
    artifact_dir = _secure_new_artifact_dir(artifact_candidate)
    terraform_data_dir = artifact_dir / "terraform-data"
    terraform_data_dir.mkdir(mode=0o700)
    base_env["TF_CLI_CONFIG_FILE"] = "/dev/null"
    base_env["TF_DATA_DIR"] = str(terraform_data_dir)
    base_env["TF_INPUT"] = "0"
    base_env["TF_WORKSPACE"] = "default"
    base_env["CHECKPOINT_DISABLE"] = "1"
    commit, source_tree_sha256 = _validate_repository(repo_root, runner, base_env)
    tool_versions = _validate_local_toolchain(
        runner,
        cwd=repo_root,
        env=base_env,
    )
    terraform_dir = repo_root / "infra" / "terraform"
    _assert_no_terraform_auto_inputs(terraform_dir)
    immutable_inputs = {
        contract.path: sha256_file(contract.path),
        contract.path.parent / "seed-stack.yaml": sha256_file(
            contract.path.parent / "seed-stack.yaml"
        ),
        var_file: sha256_file(var_file),
        **{repo_root / relative: digest for relative, digest in release_hashes.items()},
    }
    base_env = _temporary_root_environment(base_env, region=contract.region)
    root_arn = f"arn:aws:iam::{contract.account_id}:root"
    root_identity = _assert_identity(
        runner,
        cwd=repo_root,
        env=base_env,
        contract=contract,
        expected_arn=root_arn,
        label="initial root caller",
    )

    external_id = secrets.token_hex(32)
    nonce = secrets.token_hex(32)
    invocation = {
        "kind": "teamagent-provenance-bootstrap-invocation",
        "schema_version": 1,
        "bootstrap_id": contract.bootstrap_id,
        "bootstrap_nonce": nonce,
        "bootstrap_external_id": external_id,
        "seed_client_request_token": f"teamagent-bootstrap-{nonce}",
        "control_commit": commit,
        "source_tree_sha256": source_tree_sha256,
        "contract_sha256": immutable_inputs[contract.path],
        "seed_template_sha256": immutable_inputs[contract.path.parent / "seed-stack.yaml"],
        "tfvars_sha256": immutable_inputs[var_file],
        "tfvars_path": str(var_file),
        "release_contract_sha256": release_hashes,
        "toolchain_versions": tool_versions,
    }
    _write_private_json(artifact_dir / "bootstrap-invocation.json", invocation)
    seed_created = False
    seed_absence_proved = False
    seed_stack_id: str | None = None
    session_env: dict[str, str] | None = None
    ledger_state: str | None = None
    ledger_may_exist = False
    ledger_absence_proved = False
    final_receipt_path = artifact_dir / "bootstrap-receipt.json"
    try:
        _assert_seed_absent(
            runner,
            repo_root=repo_root,
            root_env=base_env,
            contract=contract,
        )
        seed_absence_proved = True
        _assert_bootstrap_ledger_absent(
            runner,
            cwd=repo_root,
            env=base_env,
            contract=contract,
        )
        ledger_absence_proved = True
        _assert_repository_unchanged(
            repo_root,
            runner,
            base_env,
            commit=commit,
            source_tree_sha256=source_tree_sha256,
        )
        _assert_no_terraform_auto_inputs(terraform_dir)
        _assert_file_hashes(immutable_inputs, label="bootstrap input")
        # From this point an interrupted CreateStack response is treated as
        # potentially accepted. Cleanup must prove the seed absent.
        seed_absence_proved = False
        seed_created = True
        seed_stack_id = _create_seed_stack(
            runner,
            repo_root=repo_root,
            root_env=base_env,
            contract=contract,
            external_id=external_id,
            nonce=nonce,
            commit=commit,
        )
        _write_private_json(
            artifact_dir / "bootstrap-seed-created.json",
            {
                "kind": "teamagent-provenance-bootstrap-seed",
                "schema_version": 1,
                "bootstrap_id": contract.bootstrap_id,
                "bootstrap_nonce": nonce,
                "control_commit": commit,
                "stack_id": seed_stack_id,
            },
        )
        _wait_for_seed_stack(
            runner,
            repo_root=repo_root,
            root_env=base_env,
            contract=contract,
        )
        assumed, session_env = _assume_seed(
            runner,
            repo_root=repo_root,
            root_env=base_env,
            contract=contract,
            external_id=external_id,
        )
        _preflight_existing_objects(
            runner,
            cwd=repo_root,
            env=session_env,
            contract=contract,
        )

        _assert_repository_unchanged(
            repo_root,
            runner,
            session_env,
            commit=commit,
            source_tree_sha256=source_tree_sha256,
        )
        _assert_no_terraform_auto_inputs(terraform_dir)
        _assert_file_hashes(immutable_inputs, label="bootstrap input")
        _terraform(
            runner,
            ["init", "-input=false", "-lockfile=readonly"],
            terraform_dir=terraform_dir,
            env=session_env,
        )
        before_result = _terraform(
            runner,
            ["state", "pull"],
            terraform_dir=terraform_dir,
            env=session_env,
        )
        before_path = artifact_dir / "main-state-before.json"
        _write_private_bytes(before_path, before_result.stdout)
        before_state = load_json(before_path, label="main state before bootstrap")
        if _contains_forbidden_fragment(before_state, contract.forbidden_name_fragments):
            raise BootstrapError("temporary seed object is already present in main state")

        _assert_repository_unchanged(
            repo_root,
            runner,
            session_env,
            commit=commit,
            source_tree_sha256=source_tree_sha256,
        )
        _assert_no_terraform_auto_inputs(terraform_dir)
        _assert_file_hashes(immutable_inputs, label="bootstrap input")
        plan_path = artifact_dir / "provenance-bootstrap.tfplan"
        plan_arguments = [
            "plan",
            "-input=false",
            "-refresh=true",
            "-lock=true",
            "-lock-timeout=5m",
            f"-var-file={var_file}",
            f"-out={plan_path}",
            *[f"-target={target}" for target in contract.targets],
        ]
        _terraform(
            runner,
            plan_arguments,
            terraform_dir=terraform_dir,
            env=session_env,
        )
        os.chmod(plan_path, 0o600)
        plan_sha = sha256_file(plan_path)
        shown = _terraform(
            runner,
            ["show", "-json", str(plan_path)],
            terraform_dir=terraform_dir,
            env=session_env,
        )
        plan_json_path = artifact_dir / "provenance-bootstrap.plan.json"
        _write_private_bytes(plan_json_path, shown.stdout)
        plan_value = load_json(plan_json_path, label="bootstrap saved plan")
        plan_validation = validate_plan(
            plan_value,
            before_state,
            contract,
            plan_sha256=plan_sha,
        )
        if sha256_file(plan_path) != plan_sha:
            raise BootstrapError("saved plan changed during validation")
        _assert_repository_unchanged(
            repo_root,
            runner,
            session_env,
            commit=commit,
            source_tree_sha256=source_tree_sha256,
        )
        _assert_no_terraform_auto_inputs(terraform_dir)
        _assert_file_hashes(immutable_inputs, label="bootstrap input")
        _assert_connection_ownership_before_apply(
            runner,
            repo_root=repo_root,
            env=session_env,
            contract=contract,
            before_addresses=state_addresses(before_state),
            created_addresses=plan_validation.created_addresses,
        )
        _assert_upsert_create_ownership(
            runner,
            cwd=repo_root,
            env=session_env,
            contract=contract,
            plan_value=plan_value,
            before_addresses=state_addresses(before_state),
        )
        _write_private_json(
            artifact_dir / "bootstrap-reviewed-plan.json",
            {
                "kind": "teamagent-provenance-bootstrap-reviewed-plan",
                "schema_version": 1,
                "bootstrap_id": contract.bootstrap_id,
                "bootstrap_nonce": nonce,
                "control_commit": commit,
                "plan_file": plan_path.name,
                "plan_json_file": plan_json_path.name,
                "plan_sha256": plan_sha,
                "created_addresses": list(plan_validation.created_addresses),
                "no_op_addresses": list(plan_validation.no_op_addresses),
                "before_state_file": before_path.name,
                "before_state_sha256": sha256_file(before_path),
            },
        )

        now = int(dt.datetime.now(dt.UTC).timestamp())
        ledger_item = _ledger_typed_item(
            contract,
            nonce=nonce,
            commit=commit,
            plan_sha256=plan_sha,
            now=now,
        )
        ledger_path = artifact_dir / "bootstrap-ledger-prepared.json"
        _write_private_json(ledger_path, ledger_item)
        # The request may have been accepted even when the client receives an
        # error. Failure handling must reconcile by nonce before permitting a
        # reviewed retry.
        ledger_may_exist = True
        _aws(
            runner,
            [
                "dynamodb",
                "put-item",
                "--table-name",
                str(contract.backend["dynamodb_table"]),
                "--item",
                f"file://{ledger_path}",
                "--condition-expression",
                "attribute_not_exists(LockID)",
                "--return-consumed-capacity",
                "TOTAL",
                "--output",
                "json",
            ],
            cwd=repo_root,
            env=session_env,
            region=contract.region,
        )
        ledger_state = "PREPARED"
        applying = _ledger_update(
            runner,
            cwd=repo_root,
            env=session_env,
            contract=contract,
            nonce=nonce,
            expected_state="PREPARED",
            next_state="APPLYING",
        )
        _write_private_json(artifact_dir / "bootstrap-ledger-applying.json", applying)
        ledger_state = "APPLYING"

        if sha256_file(plan_path) != plan_sha:
            raise BootstrapError("saved plan changed immediately before apply")
        _assert_repository_unchanged(
            repo_root,
            runner,
            session_env,
            commit=commit,
            source_tree_sha256=source_tree_sha256,
        )
        _assert_no_terraform_auto_inputs(terraform_dir)
        _assert_file_hashes(immutable_inputs, label="bootstrap input")
        _assert_identity(
            runner,
            cwd=repo_root,
            env=session_env,
            contract=contract,
            expected_arn=str(contract.seed["session_arn"]),
            label="pre-apply seed session",
        )
        _terraform(
            runner,
            ["apply", "-input=false", "-auto-approve", str(plan_path)],
            terraform_dir=terraform_dir,
            env=session_env,
        )
        after_result = _terraform(
            runner,
            ["state", "pull"],
            terraform_dir=terraform_dir,
            env=session_env,
        )
        after_path = artifact_dir / "main-state-after.json"
        _write_private_bytes(after_path, after_result.stdout)
        after_state = load_json(after_path, label="main state after bootstrap")
        _assert_repository_unchanged(
            repo_root,
            runner,
            session_env,
            commit=commit,
            source_tree_sha256=source_tree_sha256,
        )
        _assert_file_hashes(immutable_inputs, label="bootstrap input")
        handoff = validate_handoff(before_state, after_state, plan_validation, contract)
        after_addresses = state_addresses(after_state)
        connections = _connection_inventory(
            runner,
            repo_root=repo_root,
            env=session_env,
            contract=contract,
            after_addresses=after_addresses,
        )
        claims, ownership = _handoff_documents(
            contract=contract,
            nonce=nonce,
            commit=commit,
            source_tree_sha256=source_tree_sha256,
            contract_sha256=immutable_inputs[contract.path],
            seed_template_sha256=immutable_inputs[
                contract.path.parent / "seed-stack.yaml"
            ],
            tfvars_sha256=immutable_inputs[var_file],
            release_hashes=release_hashes,
            tool_versions=tool_versions,
            tool_evidence=(
                runner.tool_evidence() if isinstance(runner, CommandRunner) else {}
            ),
            plan_sha256=plan_sha,
            plan_validation=plan_validation,
            handoff=handoff,
            before_state=before_state,
            after_state=after_state,
            connections=connections,
            seed_stack_id=seed_stack_id,
        )
        claims_sha, ownership_sha = _persist_handoff_artifacts(
            artifact_dir,
            claims=claims,
            ownership=ownership,
        )
        consumed = _ledger_update(
            runner,
            cwd=repo_root,
            env=session_env,
            contract=contract,
            nonce=nonce,
            expected_state="APPLYING",
            next_state="CONSUMED",
            extra_values={
                "HandoffClaimsSha256": claims_sha,
                "HandoffOwnershipSha256": ownership_sha,
            },
        )
        _write_private_json(artifact_dir / "bootstrap-ledger-consumed.json", consumed)
        ledger_state = "CONSUMED"

        retirement = _revoke_and_delete_seed(
            runner,
            repo_root=repo_root,
            root_env=base_env,
            session_env=session_env,
            contract=contract,
            nonce=nonce,
            commit=commit,
            expected_stack_id=seed_stack_id,
        )
        seed_created = False
        seed_absence_proved = True
        receipt = {
            "kind": "teamagent-provenance-iam-bootstrap-receipt",
            "schema_version": 1,
            "status": "SUCCEEDED",
            "claims": claims,
            "handoff_claims_sha256": claims_sha,
            "handoff_ownership_sha256": ownership_sha,
            "ledger": {
                "table": contract.backend["dynamodb_table"],
                "key": contract.backend["ledger_key"],
                "terminal_state": ledger_state,
            },
            "root": {
                "arn": root_identity["Arn"],
                "user_id_sha256": sha256_bytes(str(root_identity["UserId"]).encode()),
                "mutating_workflow_actions": [
                    "cloudformation:CreateStack",
                    "sts:AssumeRole",
                    "iam:UpdateAssumeRolePolicy(retire-only)",
                    "iam:PutRolePolicy(revoke-only)",
                    "cloudformation:DeleteStack",
                ],
            },
            "seed": {
                "stack_id": seed_stack_id,
                "session_arn": contract.seed["session_arn"],
                "assumed_role_id_sha256": sha256_bytes(
                    str(
                        _mapping(
                            assumed.get("AssumedRoleUser"),
                            label="receipt AssumedRoleUser",
                        )["AssumedRoleId"]
                    ).encode()
                ),
                "retirement": retirement,
            },
            "safety": {
                "release_contracts_ready": False,
                "build_or_release_invoked": False,
                "long_lived_access_key_created": False,
                "bootstrap_state_owns_main_objects": False,
                "connection_status_allows_build": all(
                    item["status"] == "AVAILABLE" for item in connections
                ),
            },
        }
        _write_private_json(final_receipt_path, receipt)
        return final_receipt_path
    except Exception as exc:
        if (
            session_env is not None
            and ledger_may_exist
            and ledger_state not in {"CONSUMED", "RECONCILE_REQUIRED"}
        ):
            failure_sha = sha256_bytes(f"{type(exc).__name__}:{exc}".encode())
            ledger_state, reconcile = _reconcile_ledger_after_failure(
                runner,
                cwd=repo_root,
                env=session_env,
                contract=contract,
                nonce=nonce,
                failure_sha256=failure_sha,
            )
            if reconcile is not None:
                try:
                    _write_private_json(
                        artifact_dir / "bootstrap-ledger-reconcile-required.json",
                        reconcile,
                    )
                except Exception:
                    ledger_state = "UNKNOWN_RECONCILIATION_REQUIRED"
        if seed_created:
            try:
                _revoke_and_delete_seed(
                    runner,
                    repo_root=repo_root,
                    root_env=base_env,
                    session_env=session_env,
                    contract=contract,
                    nonce=nonce,
                    commit=commit,
                    expected_stack_id=seed_stack_id,
                )
                seed_created = False
                seed_absence_proved = True
            except Exception:
                seed_absence_proved = False
        failure_path = artifact_dir / "bootstrap-failure.json"
        failure_status = (
            "FAILED_REVIEWED_RETRY_ALLOWED"
            if (
                ledger_state is None
                and ledger_absence_proved
                and seed_absence_proved
                and not seed_created
            )
            else "RECONCILE_REQUIRED"
        )
        try:
            _write_private_json(
                failure_path,
                {
                    "kind": "teamagent-provenance-iam-bootstrap-failure",
                    "schema_version": 1,
                    "status": failure_status,
                    "bootstrap_id": contract.bootstrap_id,
                    "control_commit": commit,
                    "source_tree_sha256": source_tree_sha256,
                    "seed_stack_id": seed_stack_id,
                    "ledger_state": ledger_state,
                    "ledger_absence_proved": ledger_absence_proved,
                    "seed_absence_proved": seed_absence_proved,
                    "seed_retired": seed_absence_proved and not seed_created,
                    "error_type": type(exc).__name__,
                    "error_sha256": sha256_bytes(str(exc).encode()),
                },
            )
        except Exception:
            pass
        raise


def _typed_string(item: Mapping[str, Any], name: str, *, label: str) -> str:
    value = _mapping(item.get(name), label=f"{label}.{name}")
    result = value.get("S")
    if not isinstance(result, str) or not result:
        raise BootstrapError(f"{label}.{name} is not a DynamoDB string")
    return result


def _seed_stack_id_from_artifacts(
    artifact_dir: Path,
    *,
    contract: BootstrapContract,
    nonce: str,
    commit: str,
) -> str | None:
    path = artifact_dir / "bootstrap-seed-created.json"
    if not path.exists():
        return None
    seed = _mapping(load_json(path, label="seed creation artifact"), label="seed artifact")
    if (
        seed.get("kind") != "teamagent-provenance-bootstrap-seed"
        or seed.get("schema_version") != 1
        or seed.get("bootstrap_id") != contract.bootstrap_id
        or seed.get("bootstrap_nonce") != nonce
        or seed.get("control_commit") != commit
    ):
        raise BootstrapError("seed creation artifact is not owned by this invocation")
    return _string(seed.get("stack_id"), label="seed artifact stack_id")


def _verify_consumed_handoff_artifacts(
    artifact_dir: Path,
    *,
    ledger_item: Mapping[str, Any],
) -> tuple[str, str]:
    claims_path = _secure_existing_file(artifact_dir / "bootstrap-handoff-claims.json")
    ownership_path = _secure_existing_file(
        artifact_dir / "bootstrap-handoff-ownership.json"
    )
    claims_sha = sha256_file(claims_path)
    ownership_sha = sha256_file(ownership_path)
    if (
        _typed_string(
            ledger_item,
            "HandoffClaimsSha256",
            label="consumed bootstrap ledger",
        )
        != claims_sha
        or _typed_string(
            ledger_item,
            "HandoffOwnershipSha256",
            label="consumed bootstrap ledger",
        )
        != ownership_sha
    ):
        raise BootstrapError("consumed ledger does not match durable handoff artifacts")
    return claims_sha, ownership_sha


def _persist_reconcile_receipt(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists() and not path.is_symlink():
        existing = _mapping(
            load_json(path, label="reconcile/retire receipt"),
            label="reconcile/retire receipt",
        )
        stable_keys = (
            "kind",
            "schema_version",
            "bootstrap_id",
            "bootstrap_nonce",
            "control_commit",
            "plan_reapplied",
        )
        if any(existing.get(key) != value.get(key) for key in stable_keys):
            raise BootstrapError("existing reconcile/retire receipt belongs to another run")
        return
    _persist_or_verify_private_json(path, value)


def reconcile_and_retire(
    *,
    repo_root: Path,
    artifact_dir: Path,
    contract_path: Path,
    runner: CommandRunner | None = None,
    process_env: Mapping[str, str] | None = None,
) -> Path:
    """Reconcile ownership and retire the seed without ever applying a plan."""

    runner = runner or CommandRunner()
    source_env = os.environ if process_env is None else process_env
    _reject_influential_environment(source_env)
    base_env = _clean_aws_environment(source_env)
    repo_root = repo_root.resolve(strict=True)
    expected_contract = (
        repo_root / "infra" / "bootstrap" / "bootstrap_contract.json"
    ).resolve(strict=True)
    contract_path = contract_path.resolve(strict=True)
    if contract_path != expected_contract:
        raise BootstrapError("reconcile contract path is not the fixed repository control")
    contract = load_contract(contract_path)
    validate_release_contracts(repo_root, contract)
    artifact_dir = _secure_existing_artifact_dir(artifact_dir)
    invocation_path = _secure_existing_file(
        artifact_dir / "bootstrap-invocation.json"
    )
    invocation = _mapping(
        load_json(invocation_path, label="bootstrap invocation"),
        label="bootstrap invocation",
    )
    nonce = _string(invocation.get("bootstrap_nonce"), label="bootstrap nonce")
    commit = _string(invocation.get("control_commit"), label="control commit")
    if (
        invocation.get("kind") != "teamagent-provenance-bootstrap-invocation"
        or invocation.get("schema_version") != 1
        or invocation.get("bootstrap_id") != contract.bootstrap_id
        or not SHA256_RE.fullmatch(nonce)
        or not SHA1_RE.fullmatch(commit)
        or invocation.get("contract_sha256") != sha256_file(contract.path)
        or invocation.get("seed_template_sha256")
        != sha256_file(contract.path.parent / "seed-stack.yaml")
    ):
        raise BootstrapError("bootstrap invocation artifact differs from reviewed controls")

    current_commit, source_tree_sha256 = _validate_repository(repo_root, runner, base_env)
    if (
        current_commit != commit
        or invocation.get("source_tree_sha256") != source_tree_sha256
    ):
        raise BootstrapError("reconcile repository differs from the bootstrap invocation")
    _validate_local_toolchain(runner, cwd=repo_root, env=base_env)
    base_env = _temporary_root_environment(base_env, region=contract.region)
    _assert_identity(
        runner,
        cwd=repo_root,
        env=base_env,
        contract=contract,
        expected_arn=f"arn:aws:iam::{contract.account_id}:root",
        label="reconcile root caller",
    )
    ledger_item = _read_bootstrap_ledger_item(
        runner,
        cwd=repo_root,
        env=base_env,
        contract=contract,
        nonce=nonce,
    )
    if ledger_item is None:
        raise BootstrapError("reconcile cannot find the one-use bootstrap ledger")
    state = _typed_string(ledger_item, "State", label="bootstrap ledger")
    seed_stack_id = _seed_stack_id_from_artifacts(
        artifact_dir,
        contract=contract,
        nonce=nonce,
        commit=commit,
    )
    receipt_path = artifact_dir / "bootstrap-reconcile-retire.json"

    if state == "CONSUMED":
        claims_sha, ownership_sha = _verify_consumed_handoff_artifacts(
            artifact_dir,
            ledger_item=ledger_item,
        )
        retirement = _revoke_and_delete_seed(
            runner,
            repo_root=repo_root,
            root_env=base_env,
            session_env=None,
            contract=contract,
            nonce=nonce,
            commit=commit,
            expected_stack_id=seed_stack_id,
        )
        _persist_reconcile_receipt(
            receipt_path,
            {
                "kind": "teamagent-provenance-bootstrap-reconcile-retire",
                "schema_version": 1,
                "status": "CONSUMED_RETIRED",
                "bootstrap_id": contract.bootstrap_id,
                "bootstrap_nonce": nonce,
                "control_commit": commit,
                "handoff_claims_sha256": claims_sha,
                "handoff_ownership_sha256": ownership_sha,
                "plan_reapplied": False,
                "retirement": retirement,
            },
        )
        return receipt_path

    if state == "PREPARED":
        _ledger_update(
            runner,
            cwd=repo_root,
            env=base_env,
            contract=contract,
            nonce=nonce,
            expected_state="PREPARED",
            next_state="RECONCILE_REQUIRED",
            extra_values={
                "FailureSha256": sha256_bytes(
                    b"reconcile-retire:apply-was-never-authorized"
                )
            },
        )
        retirement = _revoke_and_delete_seed(
            runner,
            repo_root=repo_root,
            root_env=base_env,
            session_env=None,
            contract=contract,
            nonce=nonce,
            commit=commit,
            expected_stack_id=seed_stack_id,
        )
        _persist_reconcile_receipt(
            receipt_path,
            {
                "kind": "teamagent-provenance-bootstrap-reconcile-retire",
                "schema_version": 1,
                "status": "RETIRED_WITHOUT_APPLY",
                "bootstrap_id": contract.bootstrap_id,
                "bootstrap_nonce": nonce,
                "control_commit": commit,
                "ledger_state": "RECONCILE_REQUIRED",
                "plan_reapplied": False,
                "retirement": retirement,
            },
        )
        return receipt_path

    if state not in {"APPLYING", "RECONCILE_REQUIRED"}:
        raise BootstrapError(f"bootstrap ledger has unsupported state: {state}")

    reviewed_path = _secure_existing_file(
        artifact_dir / "bootstrap-reviewed-plan.json"
    )
    reviewed = _mapping(
        load_json(reviewed_path, label="reviewed plan artifact"),
        label="reviewed plan artifact",
    )
    plan_sha = _string(reviewed.get("plan_sha256"), label="reviewed plan SHA-256")
    plan_path = _secure_existing_file(
        artifact_dir
        / _string(reviewed.get("plan_file"), label="reviewed plan filename")
    )
    plan_json_path = _secure_existing_file(
        artifact_dir
        / _string(reviewed.get("plan_json_file"), label="reviewed plan JSON filename")
    )
    before_path = _secure_existing_file(
        artifact_dir
        / _string(reviewed.get("before_state_file"), label="before state filename")
    )
    if (
        reviewed.get("kind") != "teamagent-provenance-bootstrap-reviewed-plan"
        or reviewed.get("schema_version") != 1
        or reviewed.get("bootstrap_id") != contract.bootstrap_id
        or reviewed.get("bootstrap_nonce") != nonce
        or reviewed.get("control_commit") != commit
        or sha256_file(plan_path) != plan_sha
        or sha256_file(before_path) != reviewed.get("before_state_sha256")
    ):
        raise BootstrapError("reviewed plan artifacts differ from the invocation")
    plan_value = load_json(plan_json_path, label="reviewed bootstrap plan")
    before_state = load_json(before_path, label="main state before bootstrap")
    plan_validation = validate_plan(
        plan_value,
        before_state,
        contract,
        plan_sha256=plan_sha,
    )
    if (
        list(plan_validation.created_addresses) != reviewed.get("created_addresses")
        or list(plan_validation.no_op_addresses) != reviewed.get("no_op_addresses")
    ):
        raise BootstrapError("reviewed plan address claims differ")

    terraform_data_dir = artifact_dir / "terraform-data"
    base_env["TF_CLI_CONFIG_FILE"] = "/dev/null"
    base_env["TF_DATA_DIR"] = str(terraform_data_dir)
    base_env["TF_INPUT"] = "0"
    base_env["TF_WORKSPACE"] = "default"
    base_env["CHECKPOINT_DISABLE"] = "1"
    terraform_dir = repo_root / "infra" / "terraform"
    _terraform(
        runner,
        ["init", "-input=false", "-lockfile=readonly"],
        terraform_dir=terraform_dir,
        env=base_env,
    )
    state_result = _terraform(
        runner,
        ["state", "pull"],
        terraform_dir=terraform_dir,
        env=base_env,
    )
    reconciled_state_path = artifact_dir / "main-state-reconciled.json"
    _persist_or_verify_private_json(
        reconciled_state_path,
        json.loads(state_result.stdout),
    )
    after_state = load_json(reconciled_state_path, label="reconciled main state")
    try:
        handoff = validate_handoff(before_state, after_state, plan_validation, contract)
    except BootstrapError:
        if state == "APPLYING":
            _ledger_update(
                runner,
                cwd=repo_root,
                env=base_env,
                contract=contract,
                nonce=nonce,
                expected_state="APPLYING",
                next_state="RECONCILE_REQUIRED",
                extra_values={
                    "FailureSha256": sha256_bytes(
                        b"reconcile-retire:main-state-handoff-incomplete"
                    )
                },
            )
        retirement = _revoke_and_delete_seed(
            runner,
            repo_root=repo_root,
            root_env=base_env,
            session_env=None,
            contract=contract,
            nonce=nonce,
            commit=commit,
            expected_stack_id=seed_stack_id,
        )
        _persist_reconcile_receipt(
            receipt_path,
            {
                "kind": "teamagent-provenance-bootstrap-reconcile-retire",
                "schema_version": 1,
                "status": "RECONCILE_REQUIRED_RETIRED",
                "bootstrap_id": contract.bootstrap_id,
                "bootstrap_nonce": nonce,
                "control_commit": commit,
                "ledger_state": "RECONCILE_REQUIRED",
                "plan_reapplied": False,
                "retirement": retirement,
            },
        )
        return receipt_path

    after_addresses = state_addresses(after_state)
    connections = _connection_inventory(
        runner,
        repo_root=repo_root,
        env=base_env,
        contract=contract,
        after_addresses=after_addresses,
    )
    release_hashes = _mapping(
        invocation.get("release_contract_sha256"),
        label="invocation release contract hashes",
    )
    tool_versions = _mapping(
        invocation.get("toolchain_versions"),
        label="invocation toolchain versions",
    )
    claims, ownership = _handoff_documents(
        contract=contract,
        nonce=nonce,
        commit=commit,
        source_tree_sha256=source_tree_sha256,
        contract_sha256=_string(
            invocation.get("contract_sha256"),
            label="invocation contract SHA-256",
        ),
        seed_template_sha256=_string(
            invocation.get("seed_template_sha256"),
            label="invocation seed template SHA-256",
        ),
        tfvars_sha256=_string(
            invocation.get("tfvars_sha256"),
            label="invocation tfvars SHA-256",
        ),
        release_hashes={
            _string(key, label="release contract path"): _string(
                value,
                label="release contract SHA-256",
            )
            for key, value in release_hashes.items()
        },
        tool_versions={
            _string(key, label="toolchain name"): _string(
                value,
                label="toolchain version",
            )
            for key, value in tool_versions.items()
        },
        tool_evidence={},
        plan_sha256=plan_sha,
        plan_validation=plan_validation,
        handoff=handoff,
        before_state=before_state,
        after_state=after_state,
        connections=connections,
        seed_stack_id=seed_stack_id,
    )
    claims_sha, ownership_sha = _persist_handoff_artifacts(
        artifact_dir,
        claims=claims,
        ownership=ownership,
    )
    try:
        consumed = _ledger_update(
            runner,
            cwd=repo_root,
            env=base_env,
            contract=contract,
            nonce=nonce,
            expected_state=state,
            next_state="CONSUMED",
            extra_values={
                "HandoffClaimsSha256": claims_sha,
                "HandoffOwnershipSha256": ownership_sha,
            },
        )
    except Exception:
        concurrent = _read_bootstrap_ledger_item(
            runner,
            cwd=repo_root,
            env=base_env,
            contract=contract,
            nonce=nonce,
        )
        if concurrent is None or _typed_string(
            concurrent,
            "State",
            label="concurrent bootstrap ledger",
        ) != "CONSUMED":
            raise
        _verify_consumed_handoff_artifacts(
            artifact_dir,
            ledger_item=concurrent,
        )
    else:
        _persist_or_verify_private_json(
            artifact_dir / "bootstrap-ledger-consumed.json",
            consumed,
        )
    retirement = _revoke_and_delete_seed(
        runner,
        repo_root=repo_root,
        root_env=base_env,
        session_env=None,
        contract=contract,
        nonce=nonce,
        commit=commit,
        expected_stack_id=seed_stack_id,
    )
    _persist_reconcile_receipt(
        receipt_path,
        {
            "kind": "teamagent-provenance-bootstrap-reconcile-retire",
            "schema_version": 1,
            "status": "HANDOFF_RECONCILED_AND_RETIRED",
            "bootstrap_id": contract.bootstrap_id,
            "bootstrap_nonce": nonce,
            "control_commit": commit,
            "handoff_claims_sha256": claims_sha,
            "handoff_ownership_sha256": ownership_sha,
            "plan_reapplied": False,
            "retirement": retirement,
        },
    )
    return receipt_path


def _default_contract_path() -> Path:
    return Path(__file__).resolve().with_name("bootstrap_contract.json")


def _repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def _command_validate_contract(args: argparse.Namespace) -> int:
    contract = load_contract(Path(args.contract).resolve(strict=True))
    hashes = validate_release_contracts(Path(args.repo_root).resolve(strict=True), contract)
    print(json.dumps({"valid": True, "release_contract_sha256": hashes}, sort_keys=True))
    return 0


def _command_validate_plan(args: argparse.Namespace) -> int:
    contract = load_contract(Path(args.contract).resolve(strict=True))
    plan_path = Path(args.plan_json).resolve(strict=True)
    before_path = Path(args.before_state).resolve(strict=True)
    validation = validate_plan(
        load_json(plan_path, label="plan JSON"),
        load_json(before_path, label="before state"),
        contract,
        plan_sha256=args.plan_sha256,
    )
    print(
        json.dumps(
            {
                "valid": True,
                "created_addresses": validation.created_addresses,
                "no_op_addresses": validation.no_op_addresses,
                "plan_sha256": validation.plan_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


def _command_validate_handoff(args: argparse.Namespace) -> int:
    contract = load_contract(Path(args.contract).resolve(strict=True))
    before = load_json(Path(args.before_state).resolve(strict=True), label="before state")
    after = load_json(Path(args.after_state).resolve(strict=True), label="after state")
    created = tuple(_string_list(args.created_address, label="created_address"))
    plan_validation = PlanValidation(
        created_addresses=created,
        no_op_addresses=(),
        plan_sha256=args.plan_sha256,
    )
    result = validate_handoff(before, after, plan_validation, contract)
    print(
        json.dumps(
            {
                "valid": True,
                "lineage": result.lineage,
                "serial_before": result.before_serial,
                "serial_after": result.after_serial,
                "addresses_sha256_before": result.before_addresses_sha256,
                "addresses_sha256_after": result.after_addresses_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


def _command_run(args: argparse.Namespace) -> int:
    receipt = run_bootstrap(
        repo_root=Path(args.repo_root).resolve(strict=True),
        var_file=Path(args.var_file),
        artifact_dir=Path(args.artifact_dir),
        contract_path=Path(args.contract).resolve(strict=True),
    )
    print(f"bootstrap receipt: {receipt}")
    return 0


def _command_reconcile_retire(args: argparse.Namespace) -> int:
    receipt = reconcile_and_retire(
        repo_root=Path(args.repo_root).resolve(strict=True),
        artifact_dir=Path(args.artifact_dir),
        contract_path=Path(args.contract).resolve(strict=True),
    )
    print(f"bootstrap reconcile/retire receipt: {receipt}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.set_defaults(command=None)
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    validate_contract_parser = subparsers.add_parser(
        "validate-contract",
        help="validate the blocked release contracts without AWS access",
    )
    validate_contract_parser.add_argument(
        "--repo-root",
        default=str(_repo_root_from_script()),
    )
    validate_contract_parser.add_argument(
        "--contract",
        default=str(_default_contract_path()),
    )
    validate_contract_parser.set_defaults(command=_command_validate_contract)

    validate_plan_parser = subparsers.add_parser(
        "validate-plan",
        help="validate a Terraform show -json bootstrap plan",
    )
    validate_plan_parser.add_argument("--plan-json", required=True)
    validate_plan_parser.add_argument("--before-state", required=True)
    validate_plan_parser.add_argument("--plan-sha256", required=True)
    validate_plan_parser.add_argument(
        "--contract",
        default=str(_default_contract_path()),
    )
    validate_plan_parser.set_defaults(command=_command_validate_plan)

    validate_handoff_parser = subparsers.add_parser(
        "validate-handoff",
        help="validate the direct main-state ownership transition",
    )
    validate_handoff_parser.add_argument("--before-state", required=True)
    validate_handoff_parser.add_argument("--after-state", required=True)
    validate_handoff_parser.add_argument("--created-address", action="append", required=True)
    validate_handoff_parser.add_argument("--plan-sha256", required=True)
    validate_handoff_parser.add_argument(
        "--contract",
        default=str(_default_contract_path()),
    )
    validate_handoff_parser.set_defaults(command=_command_validate_handoff)

    run_parser = subparsers.add_parser(
        "run",
        help="perform the operator-authorized one-time AWS bootstrap",
    )
    run_parser.add_argument("--var-file", required=True)
    run_parser.add_argument("--artifact-dir", required=True)
    run_parser.add_argument("--repo-root", default=str(_repo_root_from_script()))
    run_parser.add_argument("--contract", default=str(_default_contract_path()))
    run_parser.set_defaults(command=_command_run)

    reconcile_parser = subparsers.add_parser(
        "reconcile-retire",
        help="reconcile a prior handoff and idempotently retire its exact seed",
    )
    reconcile_parser.add_argument("--artifact-dir", required=True)
    reconcile_parser.add_argument("--repo-root", default=str(_repo_root_from_script()))
    reconcile_parser.add_argument("--contract", default=str(_default_contract_path()))
    reconcile_parser.set_defaults(command=_command_reconcile_retire)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command
    if command is None:
        parser.error("a command is required")
    try:
        return int(command(args))
    except BootstrapError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
