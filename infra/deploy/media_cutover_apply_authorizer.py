#!/usr/bin/env python3
"""Atomically authorize one media migration Terraform apply.

This helper runs only under the independent MFA media-cutover attestor.  It
verifies the signed READY evidence and, in one DynamoDB transaction:

* acquires the shared deployment lock;
* moves the exact image intent from PREPARED to APPLYING; and
* irreversibly consumes the exact media evidence row.

The Terraform automation role can continue/heartbeat the authorized attempt,
but it cannot mint or consume the authoritative media row.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra" / "codebuild"))
sys.path.insert(0, str(ROOT / "infra" / "deploy"))

import release_evidence as release  # noqa: E402
import runtime_evidence_guard as runtime  # noqa: E402


class AuthorizationError(RuntimeError):
    """The exact one-use media apply could not be authorized."""


AUTHORIZATION_LEDGER_PREFIX = f"{runtime.MEDIA_CUTOVER_LEDGER_PREFIX}authorization#"
AUTHORIZATION_KIND = "teamagent-media-apply-authorization"
AUTHORIZATION_RECORD_TYPE = "teamagent.media-apply-authorization"
AUTHORIZATION_SCHEMA_VERSION = 1
_AUTHORIZATION_KEYS = {
    "apply_attempt_id",
    "authorization_sha256",
    "authorized_at_epoch",
    "claims_sha256",
    "control_commit",
    "image_deployment_intent_id",
    "kind",
    "kms_key_arn",
    "lock_lease_expires_at",
    "migration_contract_sha256",
    "plan_sha256",
    "record_id",
    "reviewed_plan_sha256",
    "schema_version",
    "signature_sha256",
    "state",
}
_AUTHORIZATION_LEDGER_KEYS = {
    "apply_attempt_id",
    "audit_expires_at",
    "authorization_json",
    "authorization_sha256",
    "authorized_at_epoch",
    "image_deployment_intent_id",
    "lock_lease_expires_at",
    "media_record_id",
    "plan_sha256",
    "record_id",
    "record_type",
    "schema_version",
    "state",
}


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AuthorizationError(f"{label} must be a non-empty string")
    return value


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorizationError(f"{label} is not canonical JSON") from exc
    if not isinstance(value, dict):
        raise AuthorizationError(f"{label} must be an object")
    return value


def _authorization_record_id(image_deployment_intent_id: str) -> str:
    return f"{AUTHORIZATION_LEDGER_PREFIX}{image_deployment_intent_id}"


def _authorization_payload(
    *,
    metadata: Mapping[str, str],
    verification: Mapping[str, Any],
    migration_contract_sha256: str,
    reviewed_plan_sha256: str,
    apply_attempt_id: str,
    authorized_at_epoch: int,
    lock_lease_expires_at: int,
    control_commit: str,
) -> dict[str, Any]:
    authorization = {
        "kind": AUTHORIZATION_KIND,
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "state": "AUTHORIZED",
        "record_id": f"{runtime.MEDIA_CUTOVER_LEDGER_PREFIX}{metadata['intent_id']}",
        "image_deployment_intent_id": metadata["intent_id"],
        "apply_attempt_id": apply_attempt_id,
        "plan_sha256": metadata["plan_sha256"],
        "claims_sha256": _string(
            verification.get("claims_sha256"),
            "verified media claims SHA-256",
        ),
        "signature_sha256": _string(
            verification.get("signature_sha256"),
            "verified media signature SHA-256",
        ),
        "kms_key_arn": _string(
            verification.get("kms_key_arn"),
            "verified media KMS key ARN",
        ),
        "migration_contract_sha256": migration_contract_sha256,
        "reviewed_plan_sha256": reviewed_plan_sha256,
        "authorized_at_epoch": authorized_at_epoch,
        "lock_lease_expires_at": lock_lease_expires_at,
        "control_commit": release._sha1(
            control_commit,
            label="media apply control commit",
        ),
    }
    authorization["authorization_sha256"] = runtime.canonical_sha256(
        authorization
    )
    return authorization


def _authorization_ledger_item(
    authorization: Mapping[str, Any],
    *,
    audit_expires_at: int,
) -> dict[str, str | int]:
    intent_id = _string(
        authorization.get("image_deployment_intent_id"),
        "authorization deployment intent ID",
    )
    canonical_json = runtime.canonical_bytes(authorization).decode("utf-8").rstrip(
        "\n"
    )
    return {
        "record_id": _authorization_record_id(intent_id),
        "record_type": AUTHORIZATION_RECORD_TYPE,
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "state": "AUTHORIZED",
        "media_record_id": _string(
            authorization.get("record_id"),
            "authorization media record ID",
        ),
        "image_deployment_intent_id": intent_id,
        "apply_attempt_id": _string(
            authorization.get("apply_attempt_id"),
            "authorization apply attempt ID",
        ),
        "plan_sha256": _string(
            authorization.get("plan_sha256"),
            "authorization plan SHA-256",
        ),
        "authorized_at_epoch": runtime.require_int(
            authorization.get("authorized_at_epoch"),
            "authorization time",
        ),
        "lock_lease_expires_at": runtime.require_int(
            authorization.get("lock_lease_expires_at"),
            "authorization lock expiry",
        ),
        "authorization_sha256": _string(
            authorization.get("authorization_sha256"),
            "authorization SHA-256",
        ),
        "authorization_json": canonical_json,
        "audit_expires_at": audit_expires_at,
    }


def _authorization_from_ledger(
    item: Mapping[str, str | int],
    *,
    metadata: Mapping[str, str],
    image_deployment_intent_id: str,
    migration_contract_sha256: str,
    reviewed_plan_sha256: str,
    apply_attempt_id: str,
    control_commit: str,
) -> dict[str, Any]:
    if set(item) != _AUTHORIZATION_LEDGER_KEYS:
        raise AuthorizationError("durable media authorization schema differs")
    raw_json = _string(
        item.get("authorization_json"),
        "durable media authorization JSON",
    )
    try:
        authorization = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise AuthorizationError(
            "durable media authorization is not canonical JSON"
        ) from exc
    if not isinstance(authorization, dict) or set(authorization) != _AUTHORIZATION_KEYS:
        raise AuthorizationError("durable media authorization payload schema differs")
    if (
        runtime.canonical_bytes(authorization).decode("utf-8").rstrip("\n")
        != raw_json
    ):
        raise AuthorizationError("durable media authorization JSON is not canonical")
    unhashed = dict(authorization)
    authorization_sha256 = _string(
        unhashed.pop("authorization_sha256", None),
        "durable media authorization SHA-256",
    )
    if (
        not re.fullmatch(r"^[0-9a-f]{64}$", authorization_sha256)
        or runtime.canonical_sha256(unhashed) != authorization_sha256
    ):
        raise AuthorizationError("durable media authorization hash differs")
    authorized_at_epoch = runtime.require_int(
        authorization.get("authorized_at_epoch"),
        "durable media authorization time",
    )
    lock_lease_expires_at = runtime.require_int(
        authorization.get("lock_lease_expires_at"),
        "durable media authorization lock expiry",
    )
    audit_expires_at = runtime.require_int(
        item.get("audit_expires_at"),
        "durable media authorization audit expiry",
    )
    expected_control_commit = release._sha1(
        control_commit,
        label="media apply control commit",
    )
    expected_media_record_id = (
        f"{runtime.MEDIA_CUTOVER_LEDGER_PREFIX}{image_deployment_intent_id}"
    )
    if (
        item.get("record_id")
        != _authorization_record_id(image_deployment_intent_id)
        or item.get("record_type") != AUTHORIZATION_RECORD_TYPE
        or item.get("schema_version") != AUTHORIZATION_SCHEMA_VERSION
        or item.get("state") != "AUTHORIZED"
        or item.get("media_record_id") != expected_media_record_id
        or item.get("image_deployment_intent_id")
        != image_deployment_intent_id
        or item.get("apply_attempt_id") != apply_attempt_id
        or item.get("plan_sha256") != metadata["plan_sha256"]
        or item.get("authorized_at_epoch") != authorized_at_epoch
        or item.get("lock_lease_expires_at") != lock_lease_expires_at
        or item.get("authorization_sha256") != authorization_sha256
        or authorization.get("kind") != AUTHORIZATION_KIND
        or authorization.get("schema_version")
        != AUTHORIZATION_SCHEMA_VERSION
        or authorization.get("state") != "AUTHORIZED"
        or authorization.get("record_id") != expected_media_record_id
        or authorization.get("image_deployment_intent_id")
        != image_deployment_intent_id
        or authorization.get("apply_attempt_id") != apply_attempt_id
        or authorization.get("plan_sha256") != metadata["plan_sha256"]
        or authorization.get("migration_contract_sha256")
        != migration_contract_sha256
        or authorization.get("reviewed_plan_sha256")
        != reviewed_plan_sha256
        or authorization.get("control_commit") != expected_control_commit
        or authorization.get("authorization_sha256") != authorization_sha256
        or authorized_at_epoch >= lock_lease_expires_at
        or lock_lease_expires_at >= audit_expires_at
    ):
        raise AuthorizationError(
            "durable media authorization does not bind this exact apply"
        )
    return authorization


def _media_consumed_item(
    before: Mapping[str, str | int],
    *,
    apply_attempt_id: str,
    plan_sha256: str,
    consumed_at_epoch: int,
) -> dict[str, str | int]:
    if before.get("status") != "READY":
        raise AuthorizationError("media evidence is not READY")
    return {
        **dict(before),
        "status": "CONSUMED",
        "apply_attempt_id": apply_attempt_id,
        "plan_sha256": plan_sha256,
        "consumed_at_epoch": consumed_at_epoch,
    }


def _transaction(
    *,
    prepared: Mapping[str, str | int],
    media: Mapping[str, str | int],
    metadata: Mapping[str, str],
    lock_item: Mapping[str, str | int],
    authorization_item: Mapping[str, str | int],
    apply_attempt_id: str,
    control_commit: str,
    now: dt.datetime,
) -> list[dict[str, Any]]:
    now_text = now.isoformat().replace("+00:00", "Z")
    now_epoch = int(now.timestamp())
    expected_control_commit = release._sha1(
        control_commit,
        label="media apply control commit",
    )
    return [
        {
            "Put": {
                "TableName": release.DEPLOYMENT_INTENT_TABLE,
                "Item": release._ddb_item(lock_item),
                "ConditionExpression": (
                    "attribute_not_exists(record_id) OR lease_expires_at < :now"
                ),
                "ExpressionAttributeValues": release._ddb_item({":now": now_epoch}),
            }
        },
        {
            "Update": {
                "TableName": release.DEPLOYMENT_INTENT_TABLE,
                "Key": release._ddb_item(
                    {"record_id": str(prepared["record_id"])}
                ),
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
                "ExpressionAttributeValues": release._ddb_item(
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
                        ":terraform_context": prepared[
                            "terraform_context_sha256"
                        ],
                        ":control_commit": expected_control_commit,
                        ":now": now_epoch,
                    }
                ),
            }
        },
        {
            "Update": {
                "TableName": release.DEPLOYMENT_INTENT_TABLE,
                "Key": release._ddb_item({"record_id": str(media["record_id"])}),
                "UpdateExpression": (
                    "SET #status = :consumed, apply_attempt_id = :attempt, "
                    "plan_sha256 = :plan, consumed_at_epoch = :now"
                ),
                "ConditionExpression": (
                    "#status = :ready "
                    "AND image_deployment_intent_id = :intent "
                    "AND desired_image = :image "
                    "AND claims_sha256 = :media_claims "
                    "AND kms_key_arn = :kms_key "
                    "AND signature_base64 = :signature "
                    "AND attribute_not_exists(apply_attempt_id) "
                    "AND audit_expires_at > :now"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": release._ddb_item(
                    {
                        ":ready": "READY",
                        ":consumed": "CONSUMED",
                        ":attempt": apply_attempt_id,
                        ":plan": metadata["plan_sha256"],
                        ":intent": metadata["intent_id"],
                        ":image": media["desired_image"],
                        ":media_claims": media["claims_sha256"],
                        ":kms_key": media["kms_key_arn"],
                        ":signature": media["signature_base64"],
                        ":now": now_epoch,
                    }
                ),
            }
        },
        {
            "Put": {
                "TableName": release.DEPLOYMENT_INTENT_TABLE,
                "Item": release._ddb_item(authorization_item),
                "ConditionExpression": "attribute_not_exists(record_id)",
            }
        },
    ]


def _validate_consumed_media_binding(
    media: Mapping[str, str | int],
    *,
    authorization: Mapping[str, Any],
    desired_image: str,
) -> None:
    if (
        media.get("record_id") != authorization.get("record_id")
        or media.get("status") != "CONSUMED"
        or media.get("image_deployment_intent_id")
        != authorization.get("image_deployment_intent_id")
        or media.get("desired_image") != desired_image
        or media.get("claims_sha256") != authorization.get("claims_sha256")
        or media.get("kms_key_arn") != authorization.get("kms_key_arn")
        or media.get("apply_attempt_id")
        != authorization.get("apply_attempt_id")
        or media.get("plan_sha256") != authorization.get("plan_sha256")
        or media.get("consumed_at_epoch")
        != authorization.get("authorized_at_epoch")
    ):
        raise AuthorizationError("consumed media evidence binding differs")


def _confirm_durable_authorization(
    *,
    metadata: Mapping[str, str],
    authorization: Mapping[str, Any],
    desired_image: str,
    migration_contract_sha256: str,
    reviewed_plan_sha256: str,
    apply_attempt_id: str,
    control_commit: str,
    now: dt.datetime,
    expected_media: Mapping[str, str | int] | None = None,
) -> dict[str, Any]:
    media_record_id = (
        f"{runtime.MEDIA_CUTOVER_LEDGER_PREFIX}{metadata['intent_id']}"
    )
    authorization_record_id = _authorization_record_id(metadata["intent_id"])
    confirmed_lock = release._dynamodb_get(release.DEPLOYMENT_LOCK_RECORD_ID)
    confirmed_intent = release._dynamodb_get(f"intent#{metadata['intent_id']}")
    confirmed_media = release._dynamodb_get(media_record_id)
    confirmed_authorization = release._dynamodb_get(authorization_record_id)
    if (
        confirmed_lock is None
        or confirmed_intent is None
        or confirmed_media is None
        or confirmed_authorization is None
    ):
        raise AuthorizationError("atomic media authorization was not durably confirmed")
    release._validate_deployment_lock(
        confirmed_lock,
        metadata=metadata,
        apply_attempt_id=apply_attempt_id,
        now=now,
    )
    release._validate_applying_intent(
        confirmed_intent,
        metadata=metadata,
        claims_sha256=metadata["receipt_claims_sha256"],
        apply_attempt_id=apply_attempt_id,
        now=now,
        expected_control_commit=control_commit,
    )
    confirmed_payload = _authorization_from_ledger(
        confirmed_authorization,
        metadata=metadata,
        image_deployment_intent_id=metadata["intent_id"],
        migration_contract_sha256=migration_contract_sha256,
        reviewed_plan_sha256=reviewed_plan_sha256,
        apply_attempt_id=apply_attempt_id,
        control_commit=control_commit,
    )
    if confirmed_payload != authorization:
        raise AuthorizationError("durable media authorization payload differs")
    _validate_consumed_media_binding(
        confirmed_media,
        authorization=confirmed_payload,
        desired_image=desired_image,
    )
    if expected_media is not None and confirmed_media != expected_media:
        raise AuthorizationError("media evidence was not consumed exactly once")
    authorized_at = runtime.require_int(
        confirmed_payload.get("authorized_at_epoch"),
        "confirmed media authorization time",
    )
    expected_started_at = dt.datetime.fromtimestamp(
        authorized_at,
        tz=dt.UTC,
    ).isoformat().replace("+00:00", "Z")
    if (
        confirmed_lock.get("acquired_at") != expected_started_at
        or confirmed_intent.get("apply_started_at") != expected_started_at
        or confirmed_lock.get("lease_expires_at")
        != confirmed_payload.get("lock_lease_expires_at")
    ):
        raise AuthorizationError("media authorization timing binding differs")
    return confirmed_payload


def _recover_media_authorization(
    aws: runtime.AwsCli,
    *,
    durable_item: Mapping[str, str | int],
    metadata: Mapping[str, str],
    media_receipt: Mapping[str, Any],
    desired_image: str,
    image_deployment_intent_id: str,
    migration_contract_sha256: str,
    reviewed_plan_sha256: str,
    apply_attempt_id: str,
    control_commit: str,
) -> dict[str, Any]:
    authorization = _authorization_from_ledger(
        durable_item,
        metadata=metadata,
        image_deployment_intent_id=image_deployment_intent_id,
        migration_contract_sha256=migration_contract_sha256,
        reviewed_plan_sha256=reviewed_plan_sha256,
        apply_attempt_id=apply_attempt_id,
        control_commit=control_commit,
    )
    verification = runtime.verify_media_cutover(
        aws,
        receipt=media_receipt,
        desired_image=desired_image,
        image_deployment_intent_id=image_deployment_intent_id,
        migration_contract_sha256=migration_contract_sha256,
        reviewed_plan_sha256=reviewed_plan_sha256,
        expected_caller_arn=runtime.MEDIA_ATTESTOR_ARN,
        expected_status="CONSUMED",
        apply_attempt_id=apply_attempt_id,
        plan_sha256=metadata["plan_sha256"],
    )
    if (
        authorization.get("claims_sha256") != verification.get("claims_sha256")
        or authorization.get("signature_sha256")
        != verification.get("signature_sha256")
        or authorization.get("kms_key_arn") != verification.get("kms_key_arn")
    ):
        raise AuthorizationError(
            "durable media authorization signature binding differs"
        )
    observed_at = runtime.require_int(
        verification.get("kms_verified_at_epoch"),
        "media recovery AWS time",
    )
    return _confirm_durable_authorization(
        metadata=metadata,
        authorization=authorization,
        desired_image=desired_image,
        migration_contract_sha256=migration_contract_sha256,
        reviewed_plan_sha256=reviewed_plan_sha256,
        apply_attempt_id=apply_attempt_id,
        control_commit=control_commit,
        now=dt.datetime.fromtimestamp(observed_at, tz=dt.UTC),
    )


def authorize_media_apply(
    aws: runtime.AwsCli,
    *,
    plan_path: Path,
    media_receipt: Mapping[str, Any],
    desired_image: str,
    image_deployment_intent_id: str,
    migration_contract_sha256: str,
    reviewed_plan_sha256: str,
    apply_attempt_id: str,
    control_commit: str,
) -> dict[str, Any]:
    metadata = release.deployment_plan_metadata(plan_path)
    attempt_id = release._uuid4(apply_attempt_id, label="media apply attempt ID")
    if (
        metadata["intent_id"] != image_deployment_intent_id
        or attempt_id == image_deployment_intent_id
    ):
        raise AuthorizationError("media evidence does not bind this deployment intent")
    authorization_record_id = _authorization_record_id(metadata["intent_id"])
    durable_authorization = release._dynamodb_get(authorization_record_id)
    if durable_authorization is not None:
        return _recover_media_authorization(
            aws,
            durable_item=durable_authorization,
            metadata=metadata,
            media_receipt=media_receipt,
            desired_image=desired_image,
            image_deployment_intent_id=image_deployment_intent_id,
            migration_contract_sha256=migration_contract_sha256,
            reviewed_plan_sha256=reviewed_plan_sha256,
            apply_attempt_id=attempt_id,
            control_commit=control_commit,
        )
    verification = runtime.verify_media_cutover(
        aws,
        receipt=media_receipt,
        desired_image=desired_image,
        image_deployment_intent_id=image_deployment_intent_id,
        migration_contract_sha256=migration_contract_sha256,
        reviewed_plan_sha256=reviewed_plan_sha256,
        expected_caller_arn=runtime.MEDIA_ATTESTOR_ARN,
    )
    observed_at = runtime.require_int(
        verification.get("kms_verified_at_epoch"),
        "media authorization AWS time",
    )
    now = dt.datetime.fromtimestamp(observed_at, tz=dt.UTC)
    prepared = release._dynamodb_get(f"intent#{metadata['intent_id']}")
    if prepared is None:
        raise AuthorizationError("prepared deployment intent does not exist")
    release._validate_prepared_intent(
        prepared,
        metadata=metadata,
        claims_sha256=metadata["receipt_claims_sha256"],
        now=now,
        expected_control_commit=control_commit,
    )
    media_record_id = f"{runtime.MEDIA_CUTOVER_LEDGER_PREFIX}{metadata['intent_id']}"
    media = release._dynamodb_get(media_record_id)
    if media is None:
        raise AuthorizationError("signed media READY evidence does not exist")
    receipt_claims = media_receipt.get("claims")
    if not isinstance(receipt_claims, Mapping):
        raise AuthorizationError("media receipt claims are missing")
    if (
        media.get("record_id") != media_record_id
        or media.get("status") != "READY"
        or media.get("image_deployment_intent_id") != metadata["intent_id"]
        or media.get("desired_image") != desired_image
        or media.get("claims_sha256") != verification["claims_sha256"]
        or media.get("kms_key_arn") != verification["kms_key_arn"]
        or media.get("signature_base64") != media_receipt.get("signature_base64")
        or receipt_claims.get("expires_at_epoch", 0) <= observed_at
    ):
        raise AuthorizationError("signed media READY evidence binding differs")
    lock_item = release._deployment_lock_item(
        metadata=metadata,
        terraform_context_sha256=str(prepared["terraform_context_sha256"]),
        apply_attempt_id=attempt_id,
        now=now,
    )
    authorization = _authorization_payload(
        metadata=metadata,
        verification=verification,
        migration_contract_sha256=migration_contract_sha256,
        reviewed_plan_sha256=reviewed_plan_sha256,
        apply_attempt_id=attempt_id,
        authorized_at_epoch=observed_at,
        lock_lease_expires_at=runtime.require_int(
            lock_item.get("lease_expires_at"),
            "media authorization lock expiry",
        ),
        control_commit=control_commit,
    )
    authorization_item = _authorization_ledger_item(
        authorization,
        audit_expires_at=runtime.require_int(
            media.get("audit_expires_at"),
            "media authorization audit expiry",
        ),
    )
    transaction = _transaction(
        prepared=prepared,
        media=media,
        metadata=metadata,
        lock_item=lock_item,
        authorization_item=authorization_item,
        apply_attempt_id=attempt_id,
        control_commit=control_commit,
        now=now,
    )
    try:
        release._aws(
            "dynamodb",
            "transact-write-items",
            "--region",
            release.REGION,
            "--transact-items",
            json.dumps(transaction, sort_keys=True, separators=(",", ":")),
            "--client-request-token",
            release._dynamodb_transaction_token(
                attempt_id,
                phase="begin-media-apply",
            ),
            "--return-consumed-capacity",
            "NONE",
            "--output",
            "json",
        )
    except release.EvidenceError as exc:
        durable_authorization = release._dynamodb_get(authorization_record_id)
        if durable_authorization is not None:
            return _recover_media_authorization(
                aws,
                durable_item=durable_authorization,
                metadata=metadata,
                media_receipt=media_receipt,
                desired_image=desired_image,
                image_deployment_intent_id=image_deployment_intent_id,
                migration_contract_sha256=migration_contract_sha256,
                reviewed_plan_sha256=reviewed_plan_sha256,
                apply_attempt_id=attempt_id,
                control_commit=control_commit,
            )
        raise AuthorizationError(
            "media evidence, deployment intent, and shared lock "
            "could not be consumed atomically"
        ) from exc

    expected_media = _media_consumed_item(
        media,
        apply_attempt_id=attempt_id,
        plan_sha256=metadata["plan_sha256"],
        consumed_at_epoch=observed_at,
    )
    return _confirm_durable_authorization(
        metadata=metadata,
        authorization=authorization,
        desired_image=desired_image,
        migration_contract_sha256=migration_contract_sha256,
        reviewed_plan_sha256=reviewed_plan_sha256,
        apply_attempt_id=attempt_id,
        control_commit=control_commit,
        now=now,
        expected_media=expected_media,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aws-bin", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--media-receipt", required=True, type=Path)
    parser.add_argument("--desired-image", required=True)
    parser.add_argument("--image-deployment-intent-id", required=True)
    parser.add_argument("--migration-contract-sha256", required=True)
    parser.add_argument("--reviewed-plan-sha256", required=True)
    parser.add_argument("--apply-attempt-id", required=True)
    parser.add_argument("--control-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not re.fullmatch(r"^[0-9a-f]{40}$", args.control_commit):
        raise AuthorizationError("control commit must be a lowercase Git SHA-1")
    aws_bin = args.aws_bin.resolve(strict=True)
    release.configure_aws_executable(aws_bin)
    aws = runtime.AwsCli(aws_bin)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        output_fd = os.open(args.output, flags, 0o600)
    except OSError as exc:
        raise AuthorizationError("authorization output could not be reserved") from exc
    succeeded = False
    try:
        # Reserve enough blocks before the one-use DynamoDB transaction. This
        # prevents an output-path race and makes post-transaction ENOSPC much
        # less likely while keeping the final file on the already-reserved inode.
        reserved = memoryview(b"\0" * 65536)
        while reserved:
            written = os.write(output_fd, reserved)
            if written <= 0:
                raise AuthorizationError("authorization reservation did not progress")
            reserved = reserved[written:]
        os.fsync(output_fd)
        result = authorize_media_apply(
            aws,
            plan_path=args.plan.resolve(strict=True),
            media_receipt=_load_json(args.media_receipt, "media receipt"),
            desired_image=args.desired_image,
            image_deployment_intent_id=args.image_deployment_intent_id,
            migration_contract_sha256=args.migration_contract_sha256,
            reviewed_plan_sha256=args.reviewed_plan_sha256,
            apply_attempt_id=args.apply_attempt_id,
            control_commit=args.control_commit,
        )
        payload = runtime.canonical_bytes(result)
        if len(payload) > 65536:
            raise AuthorizationError("authorization receipt exceeds reserved space")
        os.lseek(output_fd, 0, os.SEEK_SET)
        view = memoryview(payload)
        while view:
            written = os.write(output_fd, view)
            if written <= 0:
                raise AuthorizationError("authorization output write did not progress")
            view = view[written:]
        os.ftruncate(output_fd, len(payload))
        os.fsync(output_fd)
        succeeded = True
    except OSError as exc:
        raise AuthorizationError("authorization output could not be persisted") from exc
    finally:
        os.close(output_fd)
        if not succeeded:
            try:
                args.output.unlink()
            except FileNotFoundError:
                pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        AuthorizationError,
        release.EvidenceError,
        runtime.ContractError,
    ) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
