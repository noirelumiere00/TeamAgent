from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "codebuild" / "release_evidence.py"
AUTHORIZE_LAUNCHER = ROOT / "infra" / "deploy" / "authorize_image_release.sh"
CODEBUILD = MODULE_PATH.parent

# release_evidence resolves the registry at call time via __import__, so the
# directory has to stay importable even though the tests load it explicitly.
if str(CODEBUILD) not in sys.path:
    sys.path.insert(0, str(CODEBUILD))


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Registered under its real name so the module under test and these tests share
# one registry object rather than two copies that could drift apart.
CONSUMERS = _load_module("image_deployment_consumers", CODEBUILD / "image_deployment_consumers.py")
EVIDENCE = _load_module("release_evidence_under_test", MODULE_PATH)
COMMIT = "1" * 40
CONTRACT_SHA256 = "6" * 64
BUILD_CONTEXT_SHA256 = "7" * 64
KEY_ARN = "arn:aws:kms:ap-northeast-1:718959508629:key/12345678-1234-1234-1234-123456789abc"
APPROVAL_KEY_ARN = (
    "arn:aws:kms:ap-northeast-1:718959508629:key/11111111-1111-4111-8111-111111111111"
)
APPROVAL_ENCRYPTION_KEY_ARN = (
    "arn:aws:kms:ap-northeast-1:718959508629:key/22222222-2222-4222-8222-222222222222"
)
NOW = dt.datetime(2026, 7, 17, 6, 0, tzinfo=dt.UTC)
APP_HTML_VERSION_ID = "TFuClUMRy.qrlxsNdtZpHBazdrCQEsLE"
APP_HTML_SHA256 = "16cf0fedabf6c7f940724730cb21d394d9e2d15201bfd92335241feda432b831"
VAULT_MANIFEST_SHA256 = "1f4829847329226250f7e8414d8ff28e4731deaa948b8988f5164f011ac1871d"
BUILD_INPUTS_SHA256 = "c73aaeef3d1f49d839982c78d72d6e4d985489ebc2a86104b5690b295d9df6fa"
BAKED_APP_HTML_VERSION_ID = "approved-baked-fallback-version-1"
BAKED_APP_HTML_SHA256 = "716ac25a96516efd6443277c903102d514f3f86729f8706baea41ee48f0ecdeb"
INTENT_ID = "11111111-1111-4111-8111-111111111111"
ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"
EMPTY_SHARED_LEDGER_SHA256 = hashlib.sha256(EVIDENCE.canonical_bytes({})).hexdigest()
EMPTY_TRANSITION_SHA256 = hashlib.sha256(
    EVIDENCE.canonical_bytes({"delete": [], "replace": []})
).hexdigest()
APPROVAL_PAYLOAD_SHA256 = "8" * 64
APPROVAL_SIGNATURE_SHA256 = "9" * 64
FORCED_GATE_SHA256 = "a" * 64


def _approval_evidence(*, commit: str = COMMIT) -> dict[str, Any]:
    key = f"approval-records/mcp/{commit}/{APPROVAL_PAYLOAD_SHA256}.json"
    return {
        "payload": {
            "bucket": EVIDENCE.EVIDENCE_BUCKET,
            "key": key,
            "version_id": "approval-payload-version-1",
            "sha256": APPROVAL_PAYLOAD_SHA256,
        },
        "signature": {
            "bucket": EVIDENCE.EVIDENCE_BUCKET,
            "key": f"{key}.sig",
            "version_id": "approval-signature-version-1",
            "sha256": APPROVAL_SIGNATURE_SHA256,
        },
        "approval_payload_sha256": APPROVAL_PAYLOAD_SHA256,
        "forced_gate_sha256": FORCED_GATE_SHA256,
    }


APPROVAL_OBSERVATIONS = {
    "core.base.builder.arm64.digest": f"sha256:{'1' * 64}",
    "core.binary.python.sha256": "2" * 64,
    "media.binary.chromium.sha256": "3" * 64,
    "media.binary.ffmpeg.sha256": "4" * 64,
    "media.binary.node.sha256": "5" * 64,
    "media.binary.python.sha256": "6" * 64,
}


def _external_approval_payload(
    *,
    commit: str,
    tree_oid: str,
    inner_sha256: str,
    outer_sha256: str,
    expires_at: str = "2026-07-17T07:00:00Z",
    state: str = "PASSED",
    observations: Mapping[str, str] = APPROVAL_OBSERVATIONS,
    approved_at: str = "2026-07-17T05:00:00Z",
    observed_at: str = "2026-07-17T04:00:00Z",
    forced_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if forced_gate is not None:
        pass
    elif state == "PASSED":
        forced_gate = {
            "gate_version": 1,
            "state": state,
            "drill_manifest": {
                "payload": {
                    "bucket": EVIDENCE.EVIDENCE_BUCKET,
                    "key": "forced-rollback/manifests/drill-1.json",
                    "version_id": "drill-payload-version-1",
                    "sha256": "b" * 64,
                },
                "signature": {
                    "key": "forced-rollback/manifests/drill-1.json.sig",
                    "version_id": "drill-signature-version-1",
                    "sha256": "c" * 64,
                },
                "kms_key_arn": KEY_ARN,
                "signing_algorithm": "RSASSA_PSS_SHA_256",
            },
        }
    else:
        forced_gate = {
            "gate_version": 1,
            "state": state,
            "provisional_campaign": {
                "campaign_id": "initial-cutover-r1-r2",
                "phase": "R1",
                "payload_version_id": "campaign-payload-version-1",
                "payload_sha256": "d" * 64,
                "signature_version_id": "campaign-signature-version-1",
                "kms_key_arn": KEY_ARN,
                "expires_at_utc": "2026-07-17T06:30:00Z",
            },
        }
    return {
        "schema_version": 1,
        "kind": "teamagent.core-media-release-approval",
        "approval_id": "11111111-1111-4111-8111-111111111111",
        "pipeline": "mcp",
        "environment": "dev",
        "approved_at_utc": approved_at,
        "expires_at_utc": expires_at,
        "approved_by": EVIDENCE.APPROVAL_APPROVED_BY_ARN,
        "source_commit": commit,
        "source_tree_oid": tree_oid,
        "contracts": {
            "inner": {"schema_version": 5, "raw_sha256": inner_sha256},
            "outer": {"schema_version": 3, "raw_sha256": outer_sha256},
        },
        "observations": [
            {
                "key": key,
                "value": value,
                "observed_at_utc": observed_at,
                "source": f"contract://immutable/{key}",
            }
            for key, value in observations.items()
        ],
        "decision": "APPROVED: exact release evidence matched",
        "gates": {"forced_rollback_evidence": forced_gate},
        "approval_authority": {
            "publisher_project_arn": EVIDENCE.APPROVAL_PUBLISHER_PROJECT_ARN,
            "publisher_build_id": (
                "teamagent-dev-approval-publisher:11111111-1111-4111-8111-111111111111"
            ),
            "kms_key_arn": APPROVAL_KEY_ARN,
        },
    }


def _install_approval_aws_fake(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: dict[str, Any],
    expected_locator_commit: str,
    wrong_head_version: bool = False,
    non_compliance: bool = False,
    tamper_payload: bool = False,
    noncanonical_payload: bool = False,
    tamper_signature: bool = False,
    signature_valid: bool = True,
    retention_until: str = "2036-07-18T00:00:00+00:00",
    signature_retention_until: str | None = None,
    mock_observations: bool = True,
) -> tuple[dict[str, Any], bytes]:
    canonical_payload_bytes = EVIDENCE.approval_canonical_json_bytes(payload)
    payload_bytes = (
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        if noncanonical_payload
        else canonical_payload_bytes
    )
    signature_bytes = b"test-approval-signature"
    payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    signature_sha256 = hashlib.sha256(signature_bytes).hexdigest()
    payload_key = f"approval-records/mcp/{expected_locator_commit}/{payload_sha256}.json"
    payload_version = "approval-payload-version-1"
    signature_version = "approval-signature-version-1"
    locators = {
        "mcp": {
            "payload": {
                "bucket": EVIDENCE.EVIDENCE_BUCKET,
                "key": payload_key,
                "version_id": payload_version,
                "sha256": payload_sha256,
            },
            "signature": {
                "bucket": EVIDENCE.EVIDENCE_BUCKET,
                "key": f"{payload_key}.sig",
                "version_id": signature_version,
                "sha256": signature_sha256,
            },
        }
    }

    def fake_aws(*arguments: str, output: Path | None = None) -> str:
        if arguments[:2] == ("s3api", "head-object"):
            object_key = arguments[arguments.index("--key") + 1]
            expected_version = signature_version if object_key.endswith(".sig") else payload_version
            return json.dumps(
                {
                    "VersionId": ("wrong-version" if wrong_head_version else expected_version),
                    "ObjectLockMode": ("NONE" if non_compliance else "GOVERNANCE"),
                    "ObjectLockRetainUntilDate": (
                        signature_retention_until
                        if object_key.endswith(".sig") and signature_retention_until is not None
                        else retention_until
                    ),
                    "ServerSideEncryption": "aws:kms",
                    "SSEKMSKeyId": APPROVAL_ENCRYPTION_KEY_ARN,
                }
            )
        if arguments[:2] == ("s3api", "get-object"):
            assert output is not None
            object_key = arguments[arguments.index("--key") + 1]
            if object_key.endswith(".sig"):
                output.write_bytes(signature_bytes + (b"-tampered" if tamper_signature else b""))
                return signature_version
            output.write_bytes(payload_bytes + (b" " if tamper_payload else b""))
            return payload_version
        if arguments[:2] == ("kms", "verify"):
            digest_path = Path(arguments[arguments.index("--message") + 1].removeprefix("fileb://"))
            signature_path = Path(
                arguments[arguments.index("--signature") + 1].removeprefix("fileb://")
            )
            assert digest_path.read_bytes() == hashlib.sha256(payload_bytes).digest()
            assert signature_path.read_bytes() == signature_bytes
            return json.dumps({"SignatureValid": signature_valid})
        raise AssertionError(f"unexpected AWS fake call: {arguments[:2]}")

    monkeypatch.setattr(EVIDENCE, "_aws", fake_aws)
    if mock_observations:
        monkeypatch.setattr(
            EVIDENCE,
            "_approval_observation_values",
            lambda *_: dict(APPROVAL_OBSERVATIONS),
        )
    return locators, payload_bytes


def test_release_authorizer_contract_mapping_matches_the_evidence_pipeline_map() -> None:
    body = AUTHORIZE_LAUNCHER.read_text(encoding="utf-8")
    assert set(EVIDENCE.PIPELINES) == {"mcp", "tiktok", "openclaw"}
    for definition in EVIDENCE.PIPELINES.values():
        assert f'CONTRACT="$CONTROL_ROOT/{definition["contract_path"]}"' in body


def test_release_authorizer_generates_the_current_consumer_gate_variables() -> None:
    body = AUTHORIZE_LAUNCHER.read_text(encoding="utf-8")

    assert "--consumer-manifest" in body
    assert "--terraform-gate-vars-out" in body
    assert "--verified-approval-out" in body
    assert "--verified-record-out" in body
    assert "validate-consumer-manifest" in body
    for variable in (
        "image_deployment_consumer_manifest",
        "image_release_receipt_catalog",
        "image_release_consumer_receipt_bindings",
    ):
        assert variable in body
    assert "image_release_evidence" not in body


def _hex(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _digest(label: str) -> str:
    return f"sha256:{_hex(label)}"


CORE_DIGEST = _digest("core-image")
MEDIA_DIGEST = _digest("media-image")
DIGEST = CORE_DIGEST

APPLICATION = {
    "bucket": "teamagent-dev-raw-files",
    "key": "codebuild/connect-web-app.html",
    "version_id": APP_HTML_VERSION_ID,
    "sha256": APP_HTML_SHA256,
    "vault_manifest_sha256": VAULT_MANIFEST_SHA256,
    "build_inputs_sha256": BUILD_INPUTS_SHA256,
    "baked_fallback_version_id": BAKED_APP_HTML_VERSION_ID,
    "baked_fallback_sha256": BAKED_APP_HTML_SHA256,
}
APPLICATION_BINDING = hashlib.sha256(
    EVIDENCE.canonical_bytes(
        {
            "schema_version": 1,
            "app_html": {
                "bucket": APPLICATION["bucket"],
                "key": APPLICATION["key"],
                "version_id": APPLICATION["version_id"],
                "sha256": APPLICATION["sha256"],
            },
            "application_provenance": {
                "vault_manifest_sha256": APPLICATION["vault_manifest_sha256"],
                "build_inputs_sha256": APPLICATION["build_inputs_sha256"],
            },
            "baked_fallback": {
                "version_id": APPLICATION["baked_fallback_version_id"],
                "sha256": APPLICATION["baked_fallback_sha256"],
            },
        }
    )
).hexdigest()


def _consumer_snapshot(
    image: str,
    execution: int | str | bool,
    *,
    consumer: Mapping[str, Any],
    task_definition_arn: str,
) -> dict[str, Any]:
    """Shape one live/before/after row the way its activator reports execution.

    Kept out of the per-consumer loop so the activator never leaks in from a
    later iteration -- a closure here would make every consumer describe the
    last one, and the manifest comparison would still look green.
    """
    activator_type = consumer["activator"]["type"]
    if activator_type == "ecs_service":
        activation = {
            "desired_count": execution,
            "task_definition_arn": task_definition_arn,
        }
    elif activator_type == "eventbridge_rule_ecs_target":
        activation = {
            "state": execution,
            "task_definition_arn": task_definition_arn,
        }
    else:
        activation = {
            "event_source_mapping_enabled": execution,
            "task_definition_arn": task_definition_arn,
        }
    return {
        "image": image,
        "task_definition_arn": task_definition_arn,
        "task_definition": {
            "container_definitions": [
                {
                    "name": consumer["container_name"],
                    "image": image,
                    "command": ["python", "-m", "worker"],
                    "entryPoint": [],
                    "environment": [
                        {"name": "A_FIRST", "value": "first"},
                        {"name": "Z_LAST", "value": "last"},
                    ],
                    "secrets": [],
                    "user": "1000",
                    "privileged": False,
                    "readonlyRootFilesystem": True,
                    "linuxParameters": {"initProcessEnabled": True},
                    "mountPoints": [],
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {
                            "awslogs-region": EVIDENCE.REGION,
                            "awslogs-stream-prefix": consumer["consumer_id"],
                        },
                    },
                }
            ],
            "task_role_arn": (
                f"arn:aws:iam::{EVIDENCE.ACCOUNT_ID}:role/teamagent-{consumer['ecs_family']}-task"
            ),
            "execution_role_arn": (
                f"arn:aws:iam::{EVIDENCE.ACCOUNT_ID}:role/"
                f"teamagent-{consumer['ecs_family']}-execution"
            ),
            "network_mode": "awsvpc",
            "cpu": "256",
            "memory": "512",
            "volumes": [],
        },
        "activation": activation,
    }


def _consumer_manifest(
    *,
    image_changes: Mapping[str, tuple[str, str]] | None = None,
    activation_changes: Mapping[str, tuple[int | str | bool, int | str | bool]] | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    image_changes = image_changes or {}
    activation_changes = activation_changes or {}
    rows: list[dict[str, Any]] = []
    for consumer in CONSUMERS.load_consumer_registry()["consumers"]:
        consumer_id = consumer["consumer_id"]
        repository = consumer["release_repository"]
        default_digest = {
            "teamagent-mcp": CORE_DIGEST,
            "teamagent-openclaw": _digest("openclaw-image"),
            "teamagent-media-worker": MEDIA_DIGEST,
        }[repository]
        default_image = f"{EVIDENCE.REGISTRY}/{repository}@{default_digest}"
        before_image, after_image = image_changes.get(
            consumer_id,
            (default_image, default_image),
        )
        activator_type = consumer["activator"]["type"]
        default_execution: int | str | bool
        if activator_type == "ecs_service":
            default_execution = 1
        elif activator_type == "eventbridge_rule_ecs_target":
            default_execution = "ENABLED" if consumer_id == "morning_digest" else "DISABLED"
        else:
            default_execution = True
        before_execution, after_execution = activation_changes.get(
            consumer_id,
            (default_execution, default_execution),
        )
        task_definition_arn = (
            f"arn:aws:ecs:{EVIDENCE.REGION}:{EVIDENCE.ACCOUNT_ID}:"
            f"task-definition/{consumer['ecs_family']}:1"
        )

        rows.append(
            {
                "consumer_id": consumer_id,
                "terraform_task_definition_address": consumer["terraform_task_definition_address"],
                "ecs_family": consumer["ecs_family"],
                "container_name": consumer["container_name"],
                "activator": copy.deepcopy(consumer["activator"]),
                "release_repository": repository,
                "receipt": copy.deepcopy(consumer["receipt"]),
                "live": _consumer_snapshot(
                    before_image,
                    before_execution,
                    consumer=consumer,
                    task_definition_arn=task_definition_arn,
                ),
                "before": _consumer_snapshot(
                    before_image,
                    before_execution,
                    consumer=consumer,
                    task_definition_arn=task_definition_arn,
                ),
                "after": _consumer_snapshot(
                    after_image,
                    after_execution,
                    consumer=consumer,
                    task_definition_arn=task_definition_arn,
                ),
            }
        )
    derived_mode = (
        "receipt-required" if image_changes or activation_changes else "no-image-transition"
    )
    return {
        "schema_version": 1,
        "registry_sha256": CONSUMERS.consumer_registry_sha256(),
        "mode": mode or derived_mode,
        "consumers": rows,
    }


def _receipt_locator(
    claim_id: str,
    *,
    pipeline: str = "mcp",
    version_suffix: str = "1",
) -> dict[str, str]:
    key = f"release-receipts/{pipeline}/{COMMIT}/{claim_id}.json"
    return {
        "bucket": EVIDENCE.EVIDENCE_BUCKET,
        "key": key,
        "version_id": f"receipt-version-{version_suffix}",
        "signature_key": f"{key}.sig",
        "signature_version_id": f"signature-version-{version_suffix}",
    }


def _signature(subject_digest: str, marker: str) -> dict[str, Any]:
    return {
        "verified": True,
        "key_arn": KEY_ARN,
        "subject_digest": subject_digest,
        "referrer_digest": _digest(f"{marker}-signature-referrer"),
        "bundle_sha256": _hex(f"{marker}-signature-bundle"),
    }


def _subject(name: str, *, channel: str) -> dict[str, Any]:
    if name == "core":
        quarantine = "teamagent-mcp-quarantine"
        candidate = "teamagent-mcp-verified-candidates"
        release = "teamagent-mcp"
        runtime_kind = "core"
        digest = CORE_DIGEST
        binaries = [
            {
                "path": "/app/src/teamagent/connect_web/static/app.html",
                "sha256": BAKED_APP_HTML_SHA256,
            },
            {"path": "/usr/bin/python3.14", "sha256": _hex("core-python")},
        ]
    else:
        quarantine = "teamagent-media-worker-quarantine"
        candidate = "teamagent-media-worker-verified-candidates"
        release = "teamagent-media-worker"
        runtime_kind = "media-worker"
        digest = MEDIA_DIGEST
        binaries = [
            {"path": "/usr/bin/ffmpeg", "sha256": _hex("media-ffmpeg")},
            {"path": "/usr/bin/node", "sha256": _hex("media-node")},
        ]
    sbom_digest = _digest(f"{name}-sbom")
    provenance_digest = _digest(f"{name}-provenance")
    labels = {
        "org.opencontainers.image.revision": COMMIT,
        "org.opencontainers.image.ref.name": "dev",
        "io.teamagent.runtime.kind": runtime_kind,
        "io.teamagent.build.release-contract-sha256": CONTRACT_SHA256,
        "io.teamagent.build.app-provenance-sha256": APPLICATION_BINDING,
        "io.teamagent.build.context-sha256": BUILD_CONTEXT_SHA256,
        "io.teamagent.build.release-approval-sha256": APPROVAL_PAYLOAD_SHA256,
    }
    if name == "core":
        labels.update(
            {
                "io.teamagent.contract.app-html-source": "s3",
                "io.teamagent.contract.app-html-version-id": APP_HTML_VERSION_ID,
                "io.teamagent.contract.app-html-sha256": APP_HTML_SHA256,
                "io.teamagent.contract.app-html-manifest-sha256": (VAULT_MANIFEST_SHA256),
                "io.teamagent.contract.app-html-build-inputs-sha256": (BUILD_INPUTS_SHA256),
                "io.teamagent.contract.baked-app-html-sha256": BAKED_APP_HTML_SHA256,
                "io.teamagent.contract.baked-app-html-version-id": (BAKED_APP_HTML_VERSION_ID),
            }
        )
    return {
        "name": name,
        "quarantine_repository": quarantine,
        "candidate_repository": candidate,
        "release_repository": release,
        "candidate_tag": f"candidate-{COMMIT}-{name}",
        "release_tag": (
            f"{'verified' if channel == 'verified-candidate' else channel}-{COMMIT}-{name}"
        ),
        "digest": digest,
        "media_type": "application/vnd.oci.image.manifest.v1+json",
        "config_digest": _digest(f"{name}-config"),
        "platform": {"os": "linux", "architecture": "arm64"},
        "labels": labels,
        "binaries": binaries,
        "scan": {
            "scanner": "trivy",
            "actual_image": (
                f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/{quarantine}@{digest}"
            ),
            "unknown": 0,
            "low": 0,
            "medium": 0,
            "critical": 0,
            "high": 0,
            "secrets": 0,
            "report_sha256": _hex(f"{name}-scan-report"),
        },
        "sbom": {
            "digest": sbom_digest,
            "artifact_type": "application/spdx+json",
            "payload_sha256": _hex(f"{name}-sbom-payload"),
            "signature": _signature(sbom_digest, f"{name}-sbom"),
        },
        "provenance": {
            "digest": provenance_digest,
            "artifact_type": "application/vnd.in-toto+json",
            "payload_sha256": _hex(f"{name}-provenance-payload"),
            "signature": _signature(provenance_digest, f"{name}-provenance"),
        },
        "image_signature": _signature(digest, f"{name}-image"),
    }


def _receipt(*, channel: str = "verified-candidate") -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE.RELEASE_RECEIPT_SCHEMA,
        "kind": "teamagent.release-receipt",
        "pipeline": "mcp",
        "channel": channel,
        "issued_at": "2026-07-17T05:55:00Z",
        "expires_at": "2026-07-17T06:25:00Z",
        "build": {
            "project_arn": (
                "arn:aws:codebuild:ap-northeast-1:718959508629:project/teamagent-dev-image-builder"
            ),
            "build_id": "teamagent-dev-image-builder:12345678-1234-1234-1234-123456789abc",
            "source_commit": COMMIT,
        },
        "contract": {
            "path": "infra/codebuild/teamagent_core_media_release_contract.json",
            "sha256": CONTRACT_SHA256,
            "release_ready": True,
        },
        "source_evidence": {
            "bucket": "teamagent-dev-image-release-evidence",
            "key": f"source-declarations/mcp/{COMMIT}/{'7' * 64}.json",
            "version_id": "source-version-1",
            "sha256": "7" * 64,
            "signature_key": (f"source-declarations/mcp/{COMMIT}/{'7' * 64}.json.sig"),
            "signature_version_id": "source-signature-version-1",
        },
        "approval_evidence": _approval_evidence(),
        "subjects": [
            _subject("core", channel=channel),
            _subject("media", channel=channel),
        ],
    }


def _legacy_tiktok_receipt() -> dict[str, Any]:
    receipt = _receipt()
    receipt["schema_version"] = 2
    receipt["pipeline"] = "tiktok"
    del receipt["approval_evidence"]
    receipt["build"]["project_arn"] = (
        "arn:aws:codebuild:ap-northeast-1:718959508629:project/teamagent-dev-tiktok-image-builder"
    )
    receipt["contract"]["path"] = "infra/codebuild/tiktok_release_contract.json"
    source_key = f"source-manifests/tiktok/{COMMIT}/{'7' * 64}.json"
    receipt["source_evidence"]["key"] = source_key
    receipt["source_evidence"]["signature_key"] = f"{source_key}.sig"
    subject = receipt["subjects"][0]
    subject["name"] = "tiktok"
    subject["quarantine_repository"] = "teamagent-dev-tiktok-acquire-quarantine"
    subject["candidate_repository"] = "teamagent-dev-tiktok-acquire-verified-candidates"
    subject["release_repository"] = "teamagent-dev-tiktok-acquire"
    subject["candidate_tag"] = COMMIT
    subject["release_tag"] = f"verified-{COMMIT}"
    subject["labels"]["io.teamagent.build.contract-sha256"] = CONTRACT_SHA256
    subject["scan"]["actual_image"] = (
        f"{EVIDENCE.REGISTRY}/teamagent-dev-tiktok-acquire-quarantine@{subject['digest']}"
    )
    receipt["subjects"] = [subject]
    return receipt


def _source_declaration() -> dict[str, Any]:
    publisher_build_id = "teamagent-dev-mcp-source-publisher:1234"
    context_key = f"source-contexts/mcp/{COMMIT}/{'4' * 64}/{publisher_build_id}.tar"
    return EVIDENCE.source_declaration(
        project_arn=(
            "arn:aws:codebuild:ap-northeast-1:718959508629:"
            "project/teamagent-dev-mcp-source-publisher"
        ),
        build_id=publisher_build_id,
        commit=COMMIT,
        source_version="source-version-1",
        source_sha256="2" * 64,
        manifest_sha256="3" * 64,
        build_context_key=context_key,
        build_context_version="context-version-1",
        build_context_sha256="4" * 64,
        source_tree_oid="5" * 40,
        remote_head_oid=COMMIT,
        remote_base_oid="6" * 40,
        merge_base_oid="6" * 40,
        app_version=APP_HTML_VERSION_ID,
        app_sha256=APP_HTML_SHA256,
        vault_manifest_sha256=VAULT_MANIFEST_SHA256,
        build_inputs_sha256=BUILD_INPUTS_SHA256,
        contract_sha256=CONTRACT_SHA256,
        approval_evidence=_approval_evidence(),
    )


def test_source_declaration_binds_independent_project_source_version_commit_and_app() -> None:
    declaration = _source_declaration()

    EVIDENCE.validate_source_declaration(
        declaration,
        expected_commit=COMMIT,
        expected_source_version="source-version-1",
        expected_app_version=APP_HTML_VERSION_ID,
        expected_app_sha256=APP_HTML_SHA256,
        expected_vault_manifest_sha256=VAULT_MANIFEST_SHA256,
        expected_build_inputs_sha256=BUILD_INPUTS_SHA256,
        expected_contract_sha256=CONTRACT_SHA256,
        expected_build_context_sha256="4" * 64,
        expected_build_context_version="context-version-1",
        expected_remote_base_oid="6" * 40,
    )

    for path, replacement, message in (
        (("publisher", "commit"), "f" * 40, "remote head does not bind"),
        (("source", "version_id"), "other-version", "source archive VersionId mismatch"),
        (
            ("app_html", "sha256"),
            "f" * 64,
            "app HTML SHA-256 mismatch",
        ),
        (
            ("application_provenance", "vault_manifest_sha256"),
            "f" * 64,
            "Vault manifest SHA-256 mismatch",
        ),
        (
            ("application_provenance", "build_inputs_sha256"),
            "f" * 64,
            "build_inputs SHA-256 mismatch",
        ),
        (
            ("build_context", "canonical_tar_sha256"),
            "f" * 64,
            "canonical build context key does not bind",
        ),
        (
            ("build_context", "version_id"),
            "other-version",
            "canonical build context VersionId mismatch",
        ),
        (
            ("remote", "base_oid"),
            "f" * 40,
            "protected base is not the reviewed merge-base",
        ),
    ):
        hostile = copy.deepcopy(declaration)
        hostile[path[0]][path[1]] = replacement
        with pytest.raises(EVIDENCE.EvidenceError, match=message):
            EVIDENCE.validate_source_declaration(
                hostile,
                expected_commit=COMMIT,
                expected_source_version="source-version-1",
                expected_app_version=APP_HTML_VERSION_ID,
                expected_app_sha256=APP_HTML_SHA256,
                expected_vault_manifest_sha256=VAULT_MANIFEST_SHA256,
                expected_build_inputs_sha256=BUILD_INPUTS_SHA256,
                expected_contract_sha256=CONTRACT_SHA256,
                expected_build_context_sha256="4" * 64,
                expected_build_context_version="context-version-1",
                expected_remote_base_oid="6" * 40,
            )


def test_source_declaration_v5_requires_exact_approval_evidence() -> None:
    declaration = _source_declaration()
    assert declaration["schema_version"] == 5

    old = copy.deepcopy(declaration)
    old["schema_version"] = 4
    with pytest.raises(EVIDENCE.EvidenceError, match="unsupported source declaration schema"):
        EVIDENCE.validate_source_declaration(old)

    missing = copy.deepcopy(declaration)
    del missing["approval_evidence"]
    with pytest.raises(EVIDENCE.EvidenceError, match="schema mismatch"):
        EVIDENCE.validate_source_declaration(missing)

    inconsistent = copy.deepcopy(declaration)
    inconsistent["approval_evidence"]["approval_payload_sha256"] = "f" * 64
    with pytest.raises(EVIDENCE.EvidenceError, match="inconsistent"):
        EVIDENCE.validate_source_declaration(inconsistent)


def test_source_approval_binding_returns_tree_and_rejects_locator_substitution() -> None:
    declaration = _source_declaration()
    assert (
        EVIDENCE.verify_source_approval_binding(
            declaration,
            expected_commit=COMMIT,
            expected_contract_sha256=CONTRACT_SHA256,
            expected_approval_evidence=_approval_evidence(),
        )
        == "5" * 40
    )

    substituted = _approval_evidence()
    substituted["signature"]["sha256"] = "f" * 64
    with pytest.raises(EVIDENCE.EvidenceError, match="approval evidence mismatch"):
        EVIDENCE.verify_source_approval_binding(
            declaration,
            expected_commit=COMMIT,
            expected_contract_sha256=CONTRACT_SHA256,
            expected_approval_evidence=substituted,
        )


def test_mcp_app_anchors_are_loaded_from_the_embedded_release_contract() -> None:
    helper = MODULE_PATH.read_text(encoding="utf-8")
    attestor = (ROOT / "infra" / "codebuild" / "image-attestor-buildspec.yml").read_text(
        encoding="utf-8"
    )

    for duplicated_literal in (
        APP_HTML_VERSION_ID,
        APP_HTML_SHA256,
        VAULT_MANIFEST_SHA256,
        BUILD_INPUTS_SHA256,
        BAKED_APP_HTML_SHA256,
    ):
        assert duplicated_literal not in helper
        assert duplicated_literal not in attestor
    for contract_path in (
        ".app_html.production.app_html_s3_version_id",
        ".app_html.production.app_html_sha256",
        ".app_html.production.vault_manifest_sha256",
        ".app_html.production.build_inputs_sha256",
    ):
        assert contract_path in attestor


def test_verified_receipt_requires_exact_actual_image_evidence() -> None:
    EVIDENCE.validate_release_receipt(
        _receipt(),
        expected_pipeline="mcp",
        expected_commit=COMMIT,
        expected_contract_sha256=CONTRACT_SHA256,
        allowed_channels={"verified-candidate"},
        now=NOW,
    )


def test_mcp_receipt_v3_requires_approval_while_non_mcp_v2_remains_legacy() -> None:
    receipt = _receipt()
    assert receipt["schema_version"] == 3
    EVIDENCE.validate_release_receipt(
        receipt,
        expected_pipeline="mcp",
        expected_commit=COMMIT,
        expected_contract_sha256=CONTRACT_SHA256,
        allowed_channels={"verified-candidate"},
        now=NOW,
    )

    old_mcp = copy.deepcopy(receipt)
    old_mcp["schema_version"] = 2
    with pytest.raises(EVIDENCE.EvidenceError, match="unsupported release receipt schema"):
        EVIDENCE.validate_release_receipt(old_mcp, now=NOW)

    missing = copy.deepcopy(receipt)
    del missing["approval_evidence"]
    with pytest.raises(EVIDENCE.EvidenceError, match="schema mismatch"):
        EVIDENCE.validate_release_receipt(missing, now=NOW)

    legacy = _legacy_tiktok_receipt()
    EVIDENCE.validate_release_receipt(
        legacy,
        expected_pipeline="tiktok",
        expected_commit=COMMIT,
        expected_contract_sha256=CONTRACT_SHA256,
        allowed_channels={"verified-candidate"},
        now=NOW,
    )
    assert EVIDENCE.release_receipt_schema_for_pipeline("mcp") == 3
    assert EVIDENCE.release_receipt_schema_for_pipeline("tiktok") == 2
    assert EVIDENCE.release_receipt_schema_for_pipeline("openclaw") == 2

    legacy_with_approval = copy.deepcopy(legacy)
    legacy_with_approval["approval_evidence"] = _approval_evidence()
    with pytest.raises(EVIDENCE.EvidenceError, match="schema mismatch"):
        EVIDENCE.validate_release_receipt(legacy_with_approval, now=NOW)


def test_mcp_receipt_rejects_image_approval_label_drift() -> None:
    receipt = _receipt()
    receipt["subjects"][0]["labels"]["io.teamagent.build.release-approval-sha256"] = "f" * 64

    with pytest.raises(EVIDENCE.EvidenceError, match="approval label"):
        EVIDENCE.validate_release_receipt(
            receipt,
            expected_pipeline="mcp",
            expected_commit=COMMIT,
            expected_contract_sha256=CONTRACT_SHA256,
            allowed_channels={"verified-candidate"},
            now=NOW,
        )


def _approval_contract_paths(tmp_path: Path) -> tuple[Path, Path, str, str]:
    runtime_contract = tmp_path / "teamagent_runtime_contract.json"
    contract = tmp_path / "teamagent_core_media_release_contract.json"
    runtime_contract.write_bytes(b'{"test":"inner-v5"}\n')
    contract.write_bytes(b'{"test":"outer-v3"}\n')
    return (
        runtime_contract,
        contract,
        hashlib.sha256(runtime_contract.read_bytes()).hexdigest(),
        hashlib.sha256(contract.read_bytes()).hexdigest(),
    )


def test_assert_approved_release_fetches_and_verifies_exact_immutable_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_contract, contract, inner_sha256, outer_sha256 = _approval_contract_paths(tmp_path)
    tree_oid = "2" * 40
    payload = _external_approval_payload(
        commit=COMMIT,
        tree_oid=tree_oid,
        inner_sha256=inner_sha256,
        outer_sha256=outer_sha256,
    )
    locators, payload_bytes = _install_approval_aws_fake(
        monkeypatch,
        payload=payload,
        expected_locator_commit=COMMIT,
    )
    verified_record = tmp_path / "verified-release-approval.json"

    evidence = EVIDENCE.assert_approved_release(
        operation="build",
        approval_locators=locators,
        approval_signing_key_arn=APPROVAL_KEY_ARN,
        approval_encryption_key_arn=APPROVAL_ENCRYPTION_KEY_ARN,
        expected_commit=COMMIT,
        expected_tree_oid=tree_oid,
        expected_inner_sha256=inner_sha256,
        expected_outer_sha256=outer_sha256,
        expected_pipeline="mcp",
        expected_environment="dev",
        runtime_contract_path=runtime_contract,
        contract_path=contract,
        now=NOW,
        verified_record_out=verified_record,
    )

    assert evidence["payload"] == locators["mcp"]["payload"]
    assert evidence["signature"] == locators["mcp"]["signature"]
    assert evidence["approval_payload_sha256"] == hashlib.sha256(payload_bytes).hexdigest()
    assert (
        evidence["forced_gate_sha256"]
        == hashlib.sha256(
            EVIDENCE.approval_canonical_json_bytes(payload["gates"]["forced_rollback_evidence"])
        ).hexdigest()
    )
    forced_gate_sha256 = hashlib.sha256(
        EVIDENCE.approval_canonical_json_bytes(payload["gates"]["forced_rollback_evidence"])
    ).hexdigest()
    assert verified_record.read_bytes() == EVIDENCE.canonical_bytes(
        {
            "approval_id": payload["approval_id"],
            "approved_at_utc": payload["approved_at_utc"],
            "approved_by": payload["approved_by"],
            "decision": payload["decision"],
            "expires_at_utc": payload["expires_at_utc"],
            "forced_gate_sha256": forced_gate_sha256,
            "payload": locators["mcp"]["payload"],
            "pipeline": payload["pipeline"],
            "signature": locators["mcp"]["signature"],
            "source_commit": payload["source_commit"],
        }
    )
    assert verified_record.stat().st_mode & 0o777 == 0o600


def test_assert_approved_release_cli_prints_canonical_approval_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_contract, contract, inner_sha256, outer_sha256 = _approval_contract_paths(tmp_path)
    tree_oid = "2" * 40
    payload = _external_approval_payload(
        commit=COMMIT,
        tree_oid=tree_oid,
        inner_sha256=inner_sha256,
        outer_sha256=outer_sha256,
    )
    locators, _ = _install_approval_aws_fake(
        monkeypatch,
        payload=payload,
        expected_locator_commit=COMMIT,
    )

    result = EVIDENCE.main(
        [
            "assert-approved-release",
            "--operation",
            "build",
            "--approval-locators-json",
            json.dumps(locators),
            "--approval-signing-key-arn",
            APPROVAL_KEY_ARN,
            "--approval-encryption-key-arn",
            APPROVAL_ENCRYPTION_KEY_ARN,
            "--expected-commit",
            COMMIT,
            "--expected-tree-oid",
            tree_oid,
            "--expected-inner-sha256",
            inner_sha256,
            "--expected-outer-sha256",
            outer_sha256,
            "--expected-pipeline",
            "mcp",
            "--expected-environment",
            "dev",
            "--runtime-contract",
            str(runtime_contract),
            "--contract",
            str(contract),
            "--now",
            "2026-07-17T06:00:00Z",
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert output == (
        json.dumps(
            json.loads(output),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    assert json.loads(output)["payload"] == locators["mcp"]["payload"]


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "approval locators schema mismatch"),
        ("expired", "approval is expired"),
        ("tampered", "raw SHA-256 mismatch"),
        ("noncanonical", "bytes are not canonical"),
        ("wrong-version", "not immutable exact approval evidence"),
        ("wrong-key", "approval_authority mismatch"),
        ("non-compliance", "not immutable exact approval evidence"),
        ("retention-short", "shorter than 3650 days"),
        ("retention-mismatch", "retention differ"),
        ("signature-tampered", "raw SHA-256 mismatch"),
        ("signature-invalid", "KMS signature is invalid"),
    ],
)
def test_assert_approved_release_fails_closed(
    case: str,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_contract, contract, inner_sha256, outer_sha256 = _approval_contract_paths(tmp_path)
    tree_oid = "2" * 40
    payload = _external_approval_payload(
        commit=COMMIT,
        tree_oid=tree_oid,
        inner_sha256=inner_sha256,
        outer_sha256=outer_sha256,
        expires_at=("2026-07-17T05:30:00Z" if case == "expired" else "2026-07-17T07:00:00Z"),
    )
    locators, _ = _install_approval_aws_fake(
        monkeypatch,
        payload=payload,
        expected_locator_commit=COMMIT,
        wrong_head_version=case == "wrong-version",
        non_compliance=case == "non-compliance",
        tamper_payload=case == "tampered",
        noncanonical_payload=case == "noncanonical",
        tamper_signature=case == "signature-tampered",
        signature_valid=case != "signature-invalid",
        retention_until=(
            "2026-08-01T00:00:00+00:00"
            if case == "retention-short"
            else "2036-07-18T00:00:00+00:00"
        ),
        signature_retention_until=(
            "2036-07-19T00:00:00+00:00" if case == "retention-mismatch" else None
        ),
    )
    signing_key_arn = (
        "arn:aws:kms:ap-northeast-1:718959508629:key/33333333-3333-4333-8333-333333333333"
        if case == "wrong-key"
        else APPROVAL_KEY_ARN
    )

    with pytest.raises(EVIDENCE.EvidenceError, match=message):
        EVIDENCE.assert_approved_release(
            operation="build",
            approval_locators={} if case == "missing" else locators,
            approval_signing_key_arn=signing_key_arn,
            approval_encryption_key_arn=APPROVAL_ENCRYPTION_KEY_ARN,
            expected_commit=COMMIT,
            expected_tree_oid=tree_oid,
            expected_inner_sha256=inner_sha256,
            expected_outer_sha256=outer_sha256,
            expected_pipeline="mcp",
            expected_environment="dev",
            runtime_contract_path=runtime_contract,
            contract_path=contract,
            now=NOW,
        )


def test_drill_operation_requires_provisional_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_contract, contract, inner_sha256, outer_sha256 = _approval_contract_paths(tmp_path)
    payload = _external_approval_payload(
        commit=COMMIT,
        tree_oid="2" * 40,
        inner_sha256=inner_sha256,
        outer_sha256=outer_sha256,
    )
    locators, _ = _install_approval_aws_fake(
        monkeypatch,
        payload=payload,
        expected_locator_commit=COMMIT,
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="forced rollback state mismatch"):
        EVIDENCE.assert_approved_release(
            operation="drill",
            approval_locators=locators,
            approval_signing_key_arn=APPROVAL_KEY_ARN,
            approval_encryption_key_arn=APPROVAL_ENCRYPTION_KEY_ARN,
            expected_commit=COMMIT,
            expected_tree_oid="2" * 40,
            expected_inner_sha256=inner_sha256,
            expected_outer_sha256=outer_sha256,
            expected_pipeline="mcp",
            expected_environment="dev",
            runtime_contract_path=runtime_contract,
            contract_path=contract,
            now=NOW,
        )


def test_real_git_commit_tree_external_approval_and_fake_transport_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    codebuild = repository / "infra" / "codebuild"
    codebuild.mkdir(parents=True)
    runtime_contract = codebuild / "teamagent_runtime_contract.json"
    contract = codebuild / "teamagent_core_media_release_contract.json"
    runtime_contract.write_bytes((CODEBUILD / "teamagent_runtime_contract.json").read_bytes())
    contract.write_bytes((CODEBUILD / "teamagent_core_media_release_contract.json").read_bytes())
    (repository / "README.md").write_text("release fixture\n", encoding="utf-8")

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "release-evidence@example.invalid")
    git("config", "user.name", "Release Evidence Fixture")
    git("add", ".")
    git("commit", "-q", "-m", "contract v5 v3")
    commit = git("rev-parse", "HEAD")
    tree_oid = git("rev-parse", f"{commit}^{{tree}}")
    inner_sha256 = hashlib.sha256(
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "show",
                f"{commit}:infra/codebuild/teamagent_runtime_contract.json",
            ],
            check=True,
            capture_output=True,
        ).stdout
    ).hexdigest()
    outer_sha256 = hashlib.sha256(
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "show",
                f"{commit}:infra/codebuild/teamagent_core_media_release_contract.json",
            ],
            check=True,
            capture_output=True,
        ).stdout
    ).hexdigest()
    payload = _external_approval_payload(
        commit=commit,
        tree_oid=tree_oid,
        inner_sha256=inner_sha256,
        outer_sha256=outer_sha256,
        observations=EVIDENCE._approval_observation_values(
            runtime_contract,
            contract,
        ),
    )
    raw_payload = EVIDENCE.approval_canonical_json_bytes(payload)
    external_approval = tmp_path / "external-approval.json"
    external_approval.write_bytes(raw_payload)
    assert repository not in external_approval.parents
    assert git("rev-parse", "HEAD") == commit
    assert git("rev-parse", "HEAD^{tree}") == tree_oid
    assert (
        "external-approval.json"
        not in git(
            "ls-tree",
            "-r",
            "--name-only",
            commit,
        ).splitlines()
    )

    locators, _ = _install_approval_aws_fake(
        monkeypatch,
        payload=payload,
        expected_locator_commit=commit,
        mock_observations=False,
    )
    result = EVIDENCE.assert_approved_release(
        operation="build",
        approval_locators=locators,
        approval_signing_key_arn=APPROVAL_KEY_ARN,
        approval_encryption_key_arn=APPROVAL_ENCRYPTION_KEY_ARN,
        expected_commit=commit,
        expected_tree_oid=tree_oid,
        expected_inner_sha256=inner_sha256,
        expected_outer_sha256=outer_sha256,
        expected_pipeline="mcp",
        expected_environment="dev",
        runtime_contract_path=runtime_contract,
        contract_path=contract,
        now=NOW,
    )
    assert result["approval_payload_sha256"] == hashlib.sha256(raw_payload).hexdigest()

    (repository / "README.md").write_text("unrelated second commit\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "unrelated code change")
    second_commit = git("rev-parse", "HEAD")
    second_tree_oid = git("rev-parse", "HEAD^{tree}")
    second_locators, _ = _install_approval_aws_fake(
        monkeypatch,
        payload=payload,
        expected_locator_commit=second_commit,
        mock_observations=False,
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="source_commit mismatch"):
        EVIDENCE.assert_approved_release(
            operation="build",
            approval_locators=second_locators,
            approval_signing_key_arn=APPROVAL_KEY_ARN,
            approval_encryption_key_arn=APPROVAL_ENCRYPTION_KEY_ARN,
            expected_commit=second_commit,
            expected_tree_oid=second_tree_oid,
            expected_inner_sha256=inner_sha256,
            expected_outer_sha256=outer_sha256,
            expected_pipeline="mcp",
            expected_environment="dev",
            runtime_contract_path=runtime_contract,
            contract_path=contract,
            now=NOW,
        )


def test_verified_candidate_locator_remains_re_attestable_after_thirty_days() -> None:
    locator = _receipt()
    issued_at = NOW - dt.timedelta(minutes=5)
    locator["issued_at"] = issued_at.isoformat().replace("+00:00", "Z")
    locator["expires_at"] = (issued_at + dt.timedelta(days=3650)).isoformat().replace("+00:00", "Z")

    EVIDENCE.validate_release_receipt(
        locator,
        expected_pipeline="mcp",
        expected_commit=COMMIT,
        expected_contract_sha256=CONTRACT_SHA256,
        allowed_channels={"verified-candidate"},
        now=issued_at + dt.timedelta(days=31),
    )


def test_receipt_and_application_require_exact_baked_fallback_version() -> None:
    receipt = _receipt(channel="active")
    EVIDENCE.validate_release_receipt(
        receipt,
        expected_pipeline="mcp",
        expected_commit=COMMIT,
        expected_contract_sha256=CONTRACT_SHA256,
        allowed_channels={"active"},
        now=NOW,
    )
    EVIDENCE._validate_mcp_deployment_application(receipt, APPLICATION)

    missing = copy.deepcopy(receipt)
    missing["subjects"][0]["labels"].pop("io.teamagent.contract.baked-app-html-version-id")
    with pytest.raises(EVIDENCE.EvidenceError, match="VersionId"):
        EVIDENCE.validate_release_receipt(
            missing,
            expected_pipeline="mcp",
            expected_commit=COMMIT,
            expected_contract_sha256=CONTRACT_SHA256,
            allowed_channels={"active"},
            now=NOW,
        )

    mismatched = copy.deepcopy(receipt)
    mismatched["subjects"][0]["labels"]["io.teamagent.contract.baked-app-html-version-id"] = (
        "different-fallback-version"
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="application contract"):
        EVIDENCE._validate_mcp_deployment_application(
            mismatched,
            APPLICATION,
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: value["subjects"][0]["image_signature"].update(verified=False),
            "cryptographically verified",
        ),
        (
            lambda value: value["build"].update(source_commit="f" * 40),
            "source commit mismatch",
        ),
        (
            lambda value: value["subjects"][0].update(release_tag="latest"),
            "release_tag is not canonical",
        ),
        (
            lambda value: value["subjects"][0].update(
                media_type="application/vnd.oci.image.index.v1+json"
            ),
            "single scan-capable image manifest",
        ),
        (
            lambda value: value["subjects"][0]["scan"].update(secrets=1),
            "secrets must be exactly zero",
        ),
        (
            lambda value: value["subjects"][0]["sbom"]["signature"].update(subject_digest=DIGEST),
            "does not bind the expected subject digest",
        ),
    ],
)
def test_receipt_rejects_unsigned_wrong_commit_arbitrary_tag_index_and_weak_scan(
    mutate: Any,
    message: str,
) -> None:
    receipt = _receipt()
    mutate(receipt)
    with pytest.raises(EVIDENCE.EvidenceError, match=message):
        EVIDENCE.validate_release_receipt(
            receipt,
            expected_pipeline="mcp",
            expected_commit=COMMIT,
            expected_contract_sha256=CONTRACT_SHA256,
            allowed_channels={"verified-candidate"},
            now=NOW,
        )


def test_stale_receipt_is_rejected() -> None:
    with pytest.raises(EVIDENCE.EvidenceError, match="stale"):
        EVIDENCE.validate_release_receipt(
            _receipt(),
            expected_pipeline="mcp",
            expected_commit=COMMIT,
            expected_contract_sha256=CONTRACT_SHA256,
            allowed_channels={"verified-candidate"},
            now=NOW + dt.timedelta(hours=2),
        )


def test_durable_candidate_locator_remains_bounded_and_cannot_outlive_expiry() -> None:
    candidate = _receipt()
    issued_at = dt.datetime(2026, 7, 17, 5, 55, tzinfo=dt.UTC)
    expires_at = issued_at + dt.timedelta(days=3650)
    candidate["expires_at"] = expires_at.isoformat().replace("+00:00", "Z")
    EVIDENCE.validate_release_receipt(
        candidate,
        expected_pipeline="mcp",
        expected_commit=COMMIT,
        expected_contract_sha256=CONTRACT_SHA256,
        allowed_channels={"verified-candidate"},
        now=NOW,
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="stale"):
        EVIDENCE.authorize_release_receipt(
            candidate,
            channel="rollback",
            issued_at=expires_at.isoformat().replace("+00:00", "Z"),
            expires_at=(expires_at + dt.timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        )

    too_long = copy.deepcopy(candidate)
    too_long["expires_at"] = (
        (expires_at + dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="validity window"):
        EVIDENCE.validate_release_receipt(
            too_long,
            expected_pipeline="mcp",
            expected_commit=COMMIT,
            expected_contract_sha256=CONTRACT_SHA256,
            allowed_channels={"verified-candidate"},
            now=NOW,
        )


def test_fresh_active_receipt_is_derived_without_weakening_candidate_evidence() -> None:
    candidate = _receipt()
    active = EVIDENCE.authorize_release_receipt(
        candidate,
        channel="active",
        issued_at="2026-07-17T06:00:00Z",
        expires_at="2026-07-17T06:30:00Z",
    )

    assert active["channel"] == "active"
    assert active["issued_at"] == "2026-07-17T06:00:00Z"
    assert active["expires_at"] == "2026-07-17T06:30:00Z"
    for index, name in enumerate(("core", "media")):
        assert active["subjects"][index]["release_tag"] == f"active-{COMMIT}-{name}"
        candidate_subject = copy.deepcopy(candidate["subjects"][index])
        active_subject = copy.deepcopy(active["subjects"][index])
        candidate_subject.pop("release_tag")
        active_subject.pop("release_tag")
        assert active_subject == candidate_subject
    assert active["source_evidence"] == candidate["source_evidence"]
    assert active["build"] == candidate["build"]
    assert active["contract"] == candidate["contract"]


def test_deploy_accepts_only_signed_active_or_rollback_digest_reference() -> None:
    active = _receipt(channel="active")
    image = f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@{DIGEST}"
    EVIDENCE.validate_deploy_reference(
        active,
        consumer_id="mcp",
        image=image,
        contract_sha256=CONTRACT_SHA256,
        now=NOW,
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="channel"):
        EVIDENCE.validate_deploy_reference(
            _receipt(),
            consumer_id="mcp",
            image=image,
            contract_sha256=CONTRACT_SHA256,
            now=NOW,
        )
    with pytest.raises(EVIDENCE.EvidenceError, match="does not match"):
        EVIDENCE.validate_deploy_reference(
            active,
            consumer_id="mcp",
            image=image.replace(DIGEST, "sha256:" + "f" * 64),
            contract_sha256=CONTRACT_SHA256,
            now=NOW,
        )


def test_deploy_reference_requires_the_registry_expected_subject() -> None:
    receipt = _receipt(channel="active")
    other_receipt_subject_uri = f"{EVIDENCE.REGISTRY}/teamagent-media-worker@{MEDIA_DIGEST}"
    with pytest.raises(EVIDENCE.EvidenceError, match="code-owned registry"):
        EVIDENCE.validate_deploy_reference(
            receipt,
            consumer_id="mcp",
            image=other_receipt_subject_uri,
            contract_sha256=CONTRACT_SHA256,
            now=NOW,
        )


def test_lifecycle_preview_cannot_expire_active_or_rollback_digests() -> None:
    safe = {
        "lifecyclePolicyPreviewResults": [
            {
                "imageDigest": "sha256:" + "f" * 64,
                "action": {"type": "EXPIRE"},
            }
        ]
    }
    EVIDENCE.validate_lifecycle_preview(safe, protected_digests={DIGEST})

    unsafe = copy.deepcopy(safe)
    unsafe["lifecyclePolicyPreviewResults"][0]["imageDigest"] = DIGEST
    with pytest.raises(EVIDENCE.EvidenceError, match="protected release graph"):
        EVIDENCE.validate_lifecycle_preview(unsafe, protected_digests={DIGEST})

    with pytest.raises(EVIDENCE.EvidenceError, match="truncated"):
        EVIDENCE.validate_lifecycle_preview(
            {
                "nextToken": "unread-page",
                "lifecyclePolicyPreviewResults": [],
            },
            protected_digests={DIGEST},
        )


def _terraform_query(
    receipt: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    signature_valid: bool = True,
    key_commit: str = COMMIT,
    image: str | None = None,
    promoted: bool = True,
    exact_signatures: bool = True,
    lifecycle_policy_present: bool = False,
    consumer_manifest: Mapping[str, Any] | None = None,
    consumer_id: str = "mcp",
) -> dict[str, str]:
    receipt_bytes = EVIDENCE.canonical_bytes(receipt)
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    key = f"release-receipts/mcp/{key_commit}/{receipt_sha256}.json"
    receipt_version = "receipt-version-1"
    signature_version = "signature-version-1"
    encryption_key_arn = KEY_ARN
    retained = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat()

    def fake_aws(*arguments: str, output: Path | None = None) -> str:
        if arguments[:2] == ("s3api", "head-object"):
            object_key = arguments[arguments.index("--key") + 1]
            version = signature_version if object_key.endswith(".sig") else receipt_version
            return json.dumps(
                {
                    "VersionId": version,
                    "ObjectLockMode": "GOVERNANCE",
                    "ObjectLockRetainUntilDate": retained,
                    "ServerSideEncryption": "aws:kms",
                    "SSEKMSKeyId": encryption_key_arn,
                }
            )
        if arguments[:2] == ("s3api", "get-object"):
            assert output is not None
            object_key = arguments[arguments.index("--key") + 1]
            if object_key.endswith(".sig"):
                output.write_bytes(b"test-signature")
                return signature_version
            output.write_bytes(receipt_bytes)
            return receipt_version
        if arguments[:2] == ("kms", "verify"):
            return json.dumps({"SignatureValid": signature_valid})
        if arguments[:2] == ("ecr", "batch-get-image"):
            if not promoted:
                return json.dumps(
                    {
                        "images": [],
                        "failures": [{"failureCode": "ImageNotFound"}],
                    }
                )
            repository = arguments[arguments.index("--repository-name") + 1]
            subject = next(
                item for item in receipt["subjects"] if item["release_repository"] == repository
            )
            return json.dumps(
                {
                    "images": [
                        {
                            "registryId": "718959508629",
                            "repositoryName": subject["release_repository"],
                            "imageId": {"imageDigest": subject["digest"]},
                            "imageManifest": '{"schemaVersion":2}',
                            "imageManifestMediaType": subject["media_type"],
                        }
                    ],
                    "failures": [],
                }
            )
        if arguments[:2] == ("ecr", "list-image-referrers"):
            repository = arguments[arguments.index("--repository-name") + 1]
            subject = next(
                item for item in receipt["subjects"] if item["release_repository"] == repository
            )
            subject_id = arguments[arguments.index("--subject-id") + 1].removeprefix("imageDigest=")
            signature_by_subject = {
                subject["digest"]: subject["image_signature"]["referrer_digest"],
                subject["sbom"]["digest"]: subject["sbom"]["signature"]["referrer_digest"],
                subject["provenance"]["digest"]: subject["provenance"]["signature"][
                    "referrer_digest"
                ],
            }
            if subject_id not in signature_by_subject:
                return json.dumps({"referrers": []})
            signature = {
                "digest": (
                    signature_by_subject[subject_id] if exact_signatures else "sha256:" + "f" * 64
                ),
                "artifactType": "application/vnd.dev.cosign.simplesigning.v1+json",
                "artifactStatus": "ACTIVE",
            }
            if subject_id == subject["digest"]:
                referrers = [
                    {
                        "digest": subject["sbom"]["digest"],
                        "artifactType": subject["sbom"]["artifact_type"],
                        "artifactStatus": "ACTIVE",
                        "annotations": {
                            "io.teamagent.build.payload-sha256": (subject["sbom"]["payload_sha256"])
                        },
                    },
                    {
                        "digest": subject["provenance"]["digest"],
                        "artifactType": subject["provenance"]["artifact_type"],
                        "artifactStatus": "ACTIVE",
                        "annotations": {
                            "io.teamagent.build.payload-sha256": (
                                subject["provenance"]["payload_sha256"]
                            )
                        },
                    },
                    signature,
                ]
            else:
                referrers = [signature]
            return json.dumps({"referrers": referrers})
        raise AssertionError(f"unexpected local AWS stub call: {arguments[:2]}")

    def fake_lifecycle(repository: str, *, label: str) -> None:
        assert repository
        assert label
        if lifecycle_policy_present:
            raise EVIDENCE.EvidenceError(
                f"{label} release repository must not have an ECR lifecycle policy"
            )

    monkeypatch.setattr(EVIDENCE, "_aws", fake_aws)
    monkeypatch.setattr(
        EVIDENCE,
        "_assert_no_release_lifecycle_policy",
        fake_lifecycle,
    )
    selected_image = image or (
        f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@{DIGEST}"
    )
    manifest = (
        dict(consumer_manifest)
        if consumer_manifest is not None
        else (
            _consumer_manifest(
                image_changes={
                    "mcp": (
                        f"{EVIDENCE.REGISTRY}/teamagent-mcp@{_digest('previous-core-image')}",
                        selected_image,
                    )
                }
            )
        )
    )
    return EVIDENCE._terraform_gate(
        {
            "consumer_manifest_json": json.dumps(manifest),
            "receipt_catalog_json": json.dumps(
                {
                    receipt_sha256: {
                        "bucket": "teamagent-dev-image-release-evidence",
                        "key": key,
                        "version_id": receipt_version,
                        "signature_key": f"{key}.sig",
                        "signature_version_id": signature_version,
                    }
                }
            ),
            "consumer_receipt_bindings_json": json.dumps({consumer_id: receipt_sha256}),
            "contracts_json": json.dumps({"mcp": CONTRACT_SHA256}),
            "contract_ready_json": json.dumps({"mcp": True}),
            "application_json": json.dumps({"mcp": APPLICATION}),
            "shared_generation_ledger_json": json.dumps({}),
            "signing_key_arn": KEY_ARN,
            "encryption_key_arn": encryption_key_arn,
            "deployment_intent_id": INTENT_ID,
        }
    )


def test_terraform_gate_verifies_exact_immutable_signed_active_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt(channel="active")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    receipt["issued_at"] = (
        (now - dt.timedelta(minutes=5))
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )
    receipt["expires_at"] = (
        (now + dt.timedelta(minutes=25))
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )

    result = _terraform_query(receipt, monkeypatch)

    assert result["verified"] == "true"
    assert result["verified_pipelines"] == "mcp"
    assert json.loads(result["release_channels_json"]) == {"mcp": "active"}
    assert len(result["deployment_context_sha256"]) == 64
    assert len(result["receipt_claims_sha256"]) == 64


def test_terraform_gate_requires_active_receipt_when_execution_increases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt(channel="rollback")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    receipt["issued_at"] = (now - dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    receipt["expires_at"] = (now + dt.timedelta(minutes=25)).isoformat().replace("+00:00", "Z")
    manifest = _consumer_manifest(activation_changes={"canary": ("DISABLED", "ENABLED")})

    with pytest.raises(EVIDENCE.EvidenceError, match="fresh active receipt"):
        _terraform_query(
            receipt,
            monkeypatch,
            consumer_manifest=manifest,
            consumer_id="canary",
        )


def test_terraform_gate_rejects_receipt_before_release_promotion_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt(channel="active")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    receipt["issued_at"] = (
        (now - dt.timedelta(minutes=5))
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )
    receipt["expires_at"] = (
        (now + dt.timedelta(minutes=25))
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )

    with pytest.raises(EVIDENCE.EvidenceError, match="exact digest is not present"):
        _terraform_query(receipt, monkeypatch, promoted=False)


def test_terraform_gate_rejects_any_release_repository_lifecycle_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt(channel="active")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    receipt["issued_at"] = (
        (now - dt.timedelta(minutes=5))
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )
    receipt["expires_at"] = (
        (now + dt.timedelta(minutes=25))
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )

    with pytest.raises(EVIDENCE.EvidenceError, match="must not have"):
        _terraform_query(
            receipt,
            monkeypatch,
            lifecycle_policy_present=True,
        )


def test_terraform_gate_rejects_non_receipted_release_signature_referrers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt(channel="active")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    receipt["issued_at"] = (
        (now - dt.timedelta(minutes=5))
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )
    receipt["expires_at"] = (
        (now + dt.timedelta(minutes=25))
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )

    with pytest.raises(EVIDENCE.EvidenceError, match=r"exact .* signature"):
        _terraform_query(receipt, monkeypatch, exact_signatures=False)


@pytest.mark.parametrize(
    ("mutation", "signature_valid", "key_commit", "image", "message"),
    [
        (None, False, COMMIT, None, "KMS signature is invalid"),
        (None, True, "f" * 40, None, "commit does not match"),
        # A tag never reaches the receipt comparison any more: the consumer
        # manifest only accepts digest URIs, so it is refused one layer earlier.
        (
            None,
            True,
            COMMIT,
            ("718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp:latest"),
            "consumer manifest is invalid",
        ),
        # Keep the receipt comparison itself exercised through the gate: a
        # well-formed digest in the right repository that the signed receipt
        # never covers must still be refused.
        (
            None,
            True,
            COMMIT,
            ("718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@sha256:" + "b" * 64),
            "does not match its registry-fixed receipt subject",
        ),
        ("stale", True, COMMIT, None, "stale"),
    ],
)
def test_terraform_gate_rejects_unsigned_wrong_commit_tag_and_old_receipt(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str | None,
    signature_valid: bool,
    key_commit: str,
    image: str | None,
    message: str,
) -> None:
    receipt = _receipt(channel="active")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    if mutation == "stale":
        receipt["issued_at"] = (
            (now - dt.timedelta(hours=2))
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )
        receipt["expires_at"] = (
            (now - dt.timedelta(hours=1))
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )
    else:
        receipt["issued_at"] = (
            (now - dt.timedelta(minutes=5))
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )
        receipt["expires_at"] = (
            (now + dt.timedelta(minutes=25))
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )

    with pytest.raises(EVIDENCE.EvidenceError, match=message):
        _terraform_query(
            receipt,
            monkeypatch,
            signature_valid=signature_valid,
            key_commit=key_commit,
            image=image,
        )


def _saved_gate_query(*, intent_id: str = INTENT_ID) -> dict[str, str]:
    claim_id = "a" * 64
    manifest = _consumer_manifest(
        image_changes={
            "mcp": (
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{_digest('previous-core')}",
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{DIGEST}",
            )
        }
    )
    return {
        "consumer_manifest_json": json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "receipt_catalog_json": json.dumps(
            {claim_id: _receipt_locator(claim_id)},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "consumer_receipt_bindings_json": json.dumps(
            {"mcp": claim_id},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "contracts_json": json.dumps(
            {"mcp": CONTRACT_SHA256},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "contract_ready_json": json.dumps(
            {"mcp": True},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "application_json": json.dumps(
            {"mcp": APPLICATION},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "shared_generation_ledger_json": "{}",
        "signing_key_arn": KEY_ARN,
        "encryption_key_arn": KEY_ARN,
        "deployment_intent_id": intent_id,
    }


def _no_image_gate_query(*, intent_id: str = INTENT_ID) -> dict[str, str]:
    return {
        "consumer_manifest_json": json.dumps(_consumer_manifest()),
        "receipt_catalog_json": "{}",
        "consumer_receipt_bindings_json": "{}",
        "contracts_json": json.dumps({"mcp": CONTRACT_SHA256, "openclaw": "a" * 64}),
        "contract_ready_json": json.dumps({"mcp": True, "openclaw": True}),
        "application_json": json.dumps({"mcp": APPLICATION}),
        "shared_generation_ledger_json": "{}",
        "signing_key_arn": KEY_ARN,
        "encryption_key_arn": KEY_ARN,
        "deployment_intent_id": intent_id,
    }


DEFAULT_GATE_QUERY = _saved_gate_query()
DEFAULT_GATE_QUERY_SHA256 = hashlib.sha256(EVIDENCE.canonical_bytes(DEFAULT_GATE_QUERY)).hexdigest()
DEFAULT_GATE_QUERY_JSON = json.dumps(
    DEFAULT_GATE_QUERY,
    sort_keys=True,
    separators=(",", ":"),
)
DEFAULT_CONSUMER_MANIFEST = EVIDENCE._validate_gate_consumer_manifest(
    json.loads(DEFAULT_GATE_QUERY["consumer_manifest_json"])
)
DEFAULT_RECEIPT_CATALOG = json.loads(DEFAULT_GATE_QUERY["receipt_catalog_json"])
DEFAULT_CONSUMER_RECEIPT_BINDINGS = json.loads(DEFAULT_GATE_QUERY["consumer_receipt_bindings_json"])
DEFAULT_CONSUMER_MANIFEST_SHA256 = hashlib.sha256(
    EVIDENCE.canonical_bytes(DEFAULT_CONSUMER_MANIFEST)
).hexdigest()
DEFAULT_RELEASE_EVIDENCE_BINDING_SHA256 = hashlib.sha256(
    EVIDENCE.canonical_bytes(
        {
            "receipt_catalog": DEFAULT_RECEIPT_CATALOG,
            "consumer_receipt_bindings": DEFAULT_CONSUMER_RECEIPT_BINDINGS,
            "release_channels": {"mcp": "active"},
        }
    )
).hexdigest()
RECEIPT_AUTHORIZATION_EXPIRES_AT = str(int((NOW + dt.timedelta(minutes=25)).timestamp()))


def _gate_metadata_fields(
    query: dict[str, str] | None = None,
    *,
    release_channels: Mapping[str, str] | None = None,
) -> dict[str, str]:
    selected = query if query is not None else DEFAULT_GATE_QUERY
    (
        consumer_manifest,
        receipt_catalog,
        consumer_receipt_bindings,
        _,
        _,
        _,
        _,
        _,
        _,
        _,
    ) = EVIDENCE._parse_terraform_gate_query(selected)
    selected_release_channels = dict(
        release_channels
        if release_channels is not None
        else {consumer_id: "active" for consumer_id in consumer_receipt_bindings}
    )
    return {
        "gate_query_sha256": hashlib.sha256(EVIDENCE.canonical_bytes(selected)).hexdigest(),
        "gate_query_json": json.dumps(
            selected,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "receipt_authorization_expires_at": (RECEIPT_AUTHORIZATION_EXPIRES_AT),
        "deployment_mode": consumer_manifest["mode"],
        "consumer_manifest_sha256": hashlib.sha256(
            EVIDENCE.canonical_bytes(consumer_manifest)
        ).hexdigest(),
        "release_evidence_binding_sha256": hashlib.sha256(
            EVIDENCE.canonical_bytes(
                {
                    "receipt_catalog": receipt_catalog,
                    "consumer_receipt_bindings": consumer_receipt_bindings,
                    "release_channels": selected_release_channels,
                }
            )
        ).hexdigest(),
    }


def _plan_json(
    *,
    intent_id: str = INTENT_ID,
    context_sha256: str = "a" * 64,
    claims_sha256: str = "b" * 64,
    actions: list[str] | None = None,
) -> dict[str, Any]:
    query = _saved_gate_query(intent_id=intent_id)
    consumer_manifest = json.loads(query["consumer_manifest_json"])
    receipt_catalog = json.loads(query["receipt_catalog_json"])
    consumer_receipt_bindings = json.loads(query["consumer_receipt_bindings_json"])
    return {
        "complete": True,
        "applyable": True,
        "errored": False,
        "planned_values": {
            "root_module": {
                "resources": [
                    {
                        "address": "terraform_data.production_image_release_gate",
                        "mode": "managed",
                        "type": "terraform_data",
                        "name": "production_image_release_gate",
                        "values": {
                            "input": {
                                "deployment_intent_id": intent_id,
                                "deployment_context_sha256": context_sha256,
                                "receipt_claims_sha256": claims_sha256,
                                "consumer_manifest": consumer_manifest,
                                "receipt_catalog": receipt_catalog,
                                "consumer_receipt_bindings": (consumer_receipt_bindings),
                                "release_channels": {"mcp": "active"},
                                "application_provenance": {
                                    "mcp": APPLICATION,
                                },
                                "shared_generation_ledger": {},
                                "hmac_release_bindings": {},
                                "deployment_gate_query": query,
                                "receipt_authorization_expires_at": (
                                    RECEIPT_AUTHORIZATION_EXPIRES_AT
                                ),
                                "deployment_mode": "receipt-required",
                            }
                        },
                    }
                ]
            }
        },
        "resource_changes": [
            {
                "address": "terraform_data.production_image_release_gate",
                "change": {"actions": actions or ["delete", "create"]},
            }
        ],
        "variables": {
            "image_deployment_intent_id": {"value": intent_id},
        },
    }


def test_saved_plan_metadata_binds_intent_context_claims_and_plan_hash(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "release.tfplan"
    plan.write_bytes(b"opaque saved terraform plan")

    metadata = EVIDENCE.deployment_plan_metadata(
        plan,
        plan_json=_plan_json(),
    )

    assert metadata == {
        "intent_id": INTENT_ID,
        "plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "deployment_context_sha256": "a" * 64,
        "receipt_claims_sha256": "b" * 64,
        "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        "gate_query_sha256": DEFAULT_GATE_QUERY_SHA256,
        "gate_query_json": DEFAULT_GATE_QUERY_JSON,
        "receipt_authorization_expires_at": (RECEIPT_AUTHORIZATION_EXPIRES_AT),
        "deployment_mode": "receipt-required",
        "consumer_manifest_sha256": DEFAULT_CONSUMER_MANIFEST_SHA256,
        "release_evidence_binding_sha256": (DEFAULT_RELEASE_EVIDENCE_BINDING_SHA256),
    }

    allowed_import = _plan_json()
    allowed_import["resource_changes"].append(
        {
            "address": ("aws_cloudwatch_log_group.ecs_containerinsights_teamagent"),
            "change": {
                "actions": ["update"],
                "importing": {"id": ("/aws/ecs/containerinsights/teamagent-dev/performance")},
            },
        }
    )
    assert (
        EVIDENCE.deployment_plan_metadata(
            plan,
            plan_json=allowed_import,
        )
        == metadata
    )
    allowed_import["resource_changes"][-1]["change"]["actions"] = [
        "delete",
        "create",
    ]
    with pytest.raises(EVIDENCE.EvidenceError, match="exact existing-log"):
        EVIDENCE.deployment_plan_metadata(
            plan,
            plan_json=allowed_import,
        )

    with pytest.raises(EVIDENCE.EvidenceError, match="will not run"):
        EVIDENCE.deployment_plan_metadata(
            plan,
            plan_json=_plan_json(actions=["no-op"]),
        )
    incomplete = _plan_json()
    incomplete["complete"] = False
    with pytest.raises(EVIDENCE.EvidenceError, match="incomplete"):
        EVIDENCE.deployment_plan_metadata(plan, plan_json=incomplete)
    imported = _plan_json()
    imported["resource_changes"][0]["change"]["importing"] = {"id": "hostile"}
    with pytest.raises(EVIDENCE.EvidenceError, match="exact existing-log"):
        EVIDENCE.deployment_plan_metadata(plan, plan_json=imported)


def test_saved_plan_destructive_delete_requires_matching_rollback_receipt(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "release.tfplan"
    plan.write_bytes(b"opaque saved terraform plan")
    active = _plan_json()
    active["resource_changes"].append(
        {
            "address": "aws_ecs_service.mcp[0]",
            "mode": "managed",
            "change": {"actions": ["delete"]},
        }
    )

    with pytest.raises(EVIDENCE.EvidenceError, match="fresh rollback receipt"):
        EVIDENCE.deployment_plan_metadata(plan, plan_json=active)

    rollback = copy.deepcopy(active)
    rollback["planned_values"]["root_module"]["resources"][0]["values"]["input"][
        "release_channels"
    ] = {"mcp": "rollback"}
    metadata = EVIDENCE.deployment_plan_metadata(plan, plan_json=rollback)
    assert (
        metadata["plan_transition_sha256"]
        != hashlib.sha256(EVIDENCE.canonical_bytes({"delete": [], "replace": []})).hexdigest()
    )


def test_saved_plan_classifies_replacements_and_allows_only_digest_preserving_rollforward(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "release.tfplan"
    plan.write_bytes(b"opaque saved terraform plan")
    selected_image = f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@{DIGEST}"
    safe_rollforward = _plan_json()
    safe_rollforward["resource_changes"].append(
        {
            "address": "aws_ecs_task_definition.mcp",
            "mode": "managed",
            "change": {
                "actions": ["create", "delete"],
                "after": {
                    "container_definitions": json.dumps([{"name": "mcp", "image": selected_image}])
                },
            },
        }
    )
    metadata = EVIDENCE.deployment_plan_metadata(
        plan,
        plan_json=safe_rollforward,
    )
    assert metadata["plan_transition_sha256"] != EMPTY_TRANSITION_SHA256

    destructive_replacement = _plan_json()
    destructive_replacement["resource_changes"].append(
        {
            "address": "aws_ecs_service.mcp[0]",
            "mode": "managed",
            "change": {"actions": ["delete", "create"], "after": {}},
        }
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="fresh rollback receipt"):
        EVIDENCE.deployment_plan_metadata(
            plan,
            plan_json=destructive_replacement,
        )

    destructive_replacement["planned_values"]["root_module"]["resources"][0]["values"]["input"][
        "release_channels"
    ] = {"mcp": "rollback"}
    EVIDENCE.deployment_plan_metadata(
        plan,
        plan_json=destructive_replacement,
    )


def test_saved_plan_allows_only_exact_hmac_gate_replacements(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "release.tfplan"
    plan.write_bytes(b"opaque saved terraform plan")
    hmac_plan = _plan_json()
    hmac_plan["resource_changes"].extend(
        {
            "address": address,
            "mode": "managed",
            "change": {
                "actions": ["create", "delete"],
                "after": {"input": {"workload": "exactly-validated-by-runtime-guard"}},
            },
        }
        for address in sorted(EVIDENCE.HMAC_RUNTIME_GATE_ADDRESSES)
    )

    metadata = EVIDENCE.deployment_plan_metadata(plan, plan_json=hmac_plan)

    assert metadata["plan_transition_sha256"] != EMPTY_TRANSITION_SHA256

    near_match = _plan_json()
    near_match["resource_changes"].append(
        {
            "address": 'terraform_data.hmac_live_task_gate["mcp"]-unreviewed',
            "mode": "managed",
            "change": {"actions": ["create", "delete"], "after": {}},
        }
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="unscoped destructive"):
        EVIDENCE.deployment_plan_metadata(plan, plan_json=near_match)


def test_saved_plan_binds_generic_media_task_replacement_to_mcp_media_receipt(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "release.tfplan"
    plan.write_bytes(b"opaque saved terraform plan")
    media_rollforward = _plan_json()
    gate_input = media_rollforward["planned_values"]["root_module"]["resources"][0]["values"][
        "input"
    ]
    tiktok_image = next(
        consumer["after"]["image"]
        for consumer in gate_input["consumer_manifest"]["consumers"]
        if consumer["consumer_id"] == "tiktok_acquire"
    )
    media_rollforward["resource_changes"].append(
        {
            "address": "aws_ecs_task_definition.tiktok_acquire[0]",
            "mode": "managed",
            "change": {
                "actions": ["create", "delete"],
                "after": {
                    "container_definitions": json.dumps(
                        [
                            {
                                "name": "acquire",
                                "image": tiktok_image,
                            }
                        ]
                    )
                },
            },
        }
    )

    metadata = EVIDENCE.deployment_plan_metadata(
        plan,
        plan_json=media_rollforward,
    )

    assert metadata["plan_transition_sha256"] != EMPTY_TRANSITION_SHA256

    media_rollforward["resource_changes"][-1]["change"]["after"]["container_definitions"] = (
        json.dumps([{"name": "acquire", "image": "legacy-image"}])
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="fresh rollback receipt"):
        EVIDENCE.deployment_plan_metadata(
            plan,
            plan_json=media_rollforward,
        )


def test_saved_plan_rejects_image_empty_or_unscoped_destructive_state(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "release.tfplan"
    plan.write_bytes(b"opaque saved terraform plan")
    image_empty = _plan_json()
    gate_input = image_empty["planned_values"]["root_module"]["resources"][0]["values"]["input"]
    mcp_consumer = next(
        consumer
        for consumer in gate_input["consumer_manifest"]["consumers"]
        if consumer["consumer_id"] == "mcp"
    )
    mcp_consumer["after"]["image"] = ""
    image_empty["resource_changes"].append(
        {
            "address": "aws_ecs_service.mcp[0]",
            "mode": "managed",
            "change": {"actions": ["delete"]},
        }
    )
    gate_input["deployment_gate_query"]["consumer_manifest_json"] = json.dumps(
        gate_input["consumer_manifest"],
        sort_keys=True,
        separators=(",", ":"),
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="consumer manifest"):
        EVIDENCE.deployment_plan_metadata(plan, plan_json=image_empty)

    unscoped = _plan_json()
    unscoped["planned_values"]["root_module"]["resources"][0]["values"]["input"][
        "release_channels"
    ] = {"mcp": "rollback"}
    unscoped["resource_changes"].append(
        {
            "address": "aws_s3_bucket.unreviewed",
            "mode": "managed",
            "change": {"actions": ["delete"]},
        }
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="unscoped destructive"):
        EVIDENCE.deployment_plan_metadata(plan, plan_json=unscoped)


def test_shared_generation_ledger_metadata_is_exact_non_secret_and_context_bound(
    tmp_path: Path,
) -> None:
    binding = {
        "table_arn": (
            "arn:aws:dynamodb:ap-northeast-1:718959508629:"
            "table/teamagent-dev-shared-generation-ledger"
        ),
        "generation": 42,
        "high_water_t0": "2026-07-17T05:59:00Z",
        "stage": "reviewed",
    }
    assert EVIDENCE._validate_shared_generation_ledger_binding(binding) == binding

    claim_id = "a" * 64
    manifest = _consumer_manifest(
        image_changes={
            "mcp": (
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{_digest('previous-core')}",
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{DIGEST}",
            )
        }
    )
    first, _, _ = EVIDENCE._deployment_binding(
        consumer_manifest=manifest,
        receipt_catalog={claim_id: _receipt_locator(claim_id)},
        consumer_receipt_bindings={"mcp": claim_id},
        contracts={"mcp": CONTRACT_SHA256},
        application={"mcp": APPLICATION},
        shared_generation_ledger=binding,
        release_channels={"mcp": "active"},
        intent_id=INTENT_ID,
    )
    second, _, _ = EVIDENCE._deployment_binding(
        consumer_manifest=manifest,
        receipt_catalog={claim_id: _receipt_locator(claim_id)},
        consumer_receipt_bindings={"mcp": claim_id},
        contracts={"mcp": CONTRACT_SHA256},
        application={"mcp": APPLICATION},
        shared_generation_ledger=dict(binding, generation=43),
        release_channels={"mcp": "active"},
        intent_id=INTENT_ID,
    )
    assert first != second

    hostile = dict(binding, secret_value="must-not-bind-secrets")
    with pytest.raises(EVIDENCE.EvidenceError, match="schema mismatch"):
        EVIDENCE._validate_shared_generation_ledger_binding(hostile)
    wrong_account = dict(
        binding,
        table_arn=binding["table_arn"].replace("718959508629", "000000000000"),
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="fixed account/region"):
        EVIDENCE._validate_shared_generation_ledger_binding(wrong_account)

    plan = tmp_path / "release.tfplan"
    plan.write_bytes(b"opaque saved terraform plan")
    plan_json = _plan_json()
    plan_json["planned_values"]["root_module"]["resources"][0]["values"]["input"][
        "shared_generation_ledger"
    ] = binding
    plan_json["planned_values"]["root_module"]["resources"][0]["values"]["input"][
        "deployment_gate_query"
    ]["shared_generation_ledger_json"] = json.dumps(
        binding,
        sort_keys=True,
        separators=(",", ":"),
    )
    metadata = EVIDENCE.deployment_plan_metadata(plan, plan_json=plan_json)
    assert (
        metadata["shared_ledger_sha256"]
        == hashlib.sha256(EVIDENCE.canonical_bytes(binding)).hexdigest()
    )


def test_receipt_claim_identity_survives_reuploaded_s3_versions() -> None:
    receipt_sha256 = "a" * 64
    first_reference = _receipt_locator(receipt_sha256, version_suffix="1")
    second_reference = _receipt_locator(receipt_sha256, version_suffix="2")
    manifest = _consumer_manifest(
        image_changes={
            "mcp": (
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{_digest('previous-core')}",
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{DIGEST}",
            )
        }
    )
    binding_arguments = {
        "consumer_manifest": manifest,
        "consumer_receipt_bindings": {"mcp": receipt_sha256},
        "contracts": {"mcp": CONTRACT_SHA256},
        "application": {"mcp": APPLICATION},
        "shared_generation_ledger": {},
        "release_channels": {"mcp": "active"},
        "intent_id": INTENT_ID,
    }

    first_context, first_claims, first_claims_sha256 = EVIDENCE._deployment_binding(
        receipt_catalog={receipt_sha256: first_reference},
        **binding_arguments,
    )
    second_context, second_claims, second_claims_sha256 = EVIDENCE._deployment_binding(
        receipt_catalog={receipt_sha256: second_reference},
        **binding_arguments,
    )

    assert first_context != second_context
    assert first_claims == second_claims == [receipt_sha256]
    assert first_claims_sha256 == second_claims_sha256


def test_consumer_bindings_fail_closed_for_missing_extra_unknown_and_unused_claims() -> None:
    claim_id = "a" * 64
    unused_claim_id = "b" * 64
    manifest = _consumer_manifest(
        image_changes={
            "mcp": (
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{_digest('previous-core')}",
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{CORE_DIGEST}",
            )
        }
    )
    valid_catalog = {claim_id: _receipt_locator(claim_id)}

    with pytest.raises(EVIDENCE.EvidenceError, match="exactly match"):
        EVIDENCE._deployment_receipt_inputs(
            consumer_manifest=manifest,
            receipt_catalog=valid_catalog,
            consumer_receipt_bindings={},
        )
    with pytest.raises(EVIDENCE.EvidenceError, match="exactly match"):
        EVIDENCE._deployment_receipt_inputs(
            consumer_manifest=manifest,
            receipt_catalog=valid_catalog,
            consumer_receipt_bindings={
                "mcp": claim_id,
                "connect_web": claim_id,
            },
        )
    with pytest.raises(EVIDENCE.EvidenceError, match="exactly the claims"):
        EVIDENCE._deployment_receipt_inputs(
            consumer_manifest=manifest,
            receipt_catalog={},
            consumer_receipt_bindings={"mcp": claim_id},
        )
    with pytest.raises(EVIDENCE.EvidenceError, match="exactly the claims"):
        EVIDENCE._deployment_receipt_inputs(
            consumer_manifest=manifest,
            receipt_catalog={
                **valid_catalog,
                unused_claim_id: _receipt_locator(unused_claim_id),
            },
            consumer_receipt_bindings={"mcp": claim_id},
        )


def test_task_definition_change_requires_a_consumer_receipt_binding() -> None:
    claim_id = "c" * 64
    manifest = _consumer_manifest(mode="receipt-required")
    mcp = next(consumer for consumer in manifest["consumers"] if consumer["consumer_id"] == "mcp")
    mcp["after"]["task_definition"]["container_definitions"][0]["command"] = [
        "/bin/sh",
        "-c",
        "exit 1",
    ]

    mode, _, _, bindings, claim_ids = EVIDENCE._deployment_receipt_inputs(
        consumer_manifest=manifest,
        receipt_catalog={claim_id: _receipt_locator(claim_id)},
        consumer_receipt_bindings={"mcp": claim_id},
    )

    assert mode == "receipt-required"
    assert bindings == {"mcp": claim_id}
    assert claim_ids == [claim_id]


def test_disabled_consumer_enablement_requires_a_receipt_binding() -> None:
    claim_id = "c" * 64
    manifest = _consumer_manifest(mode="receipt-required")
    canary = next(
        consumer for consumer in manifest["consumers"] if consumer["consumer_id"] == "canary"
    )
    canary["live"] = {"absent": True}
    canary["before"] = {"absent": True}

    mode, _, _, bindings, claim_ids = EVIDENCE._deployment_receipt_inputs(
        consumer_manifest=manifest,
        receipt_catalog={claim_id: _receipt_locator(claim_id)},
        consumer_receipt_bindings={"canary": claim_id},
    )

    assert mode == "receipt-required"
    assert bindings == {"canary": claim_id}
    assert claim_ids == [claim_id]
    assert EVIDENCE._consumer_execution_increased(canary) is True


def test_no_image_transition_is_derived_and_is_the_only_empty_claim_variant() -> None:
    result = EVIDENCE._terraform_gate(
        _no_image_gate_query(),
        now=NOW,
    )

    assert result["deployment_mode"] == "no-image-transition"
    assert json.loads(result["release_channels_json"]) == {}
    assert (
        result["receipt_claims_sha256"] == hashlib.sha256(EVIDENCE.canonical_bytes([])).hexdigest()
    )
    assert result["receipt_authorization_expires_at"] == str(
        int((NOW + dt.timedelta(hours=1)).timestamp())
    )

    enable_manifest = _consumer_manifest(
        activation_changes={"canary": ("DISABLED", "ENABLED")},
        mode="no-image-transition",
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="consumer manifest"):
        EVIDENCE._deployment_receipt_inputs(
            consumer_manifest=enable_manifest,
            receipt_catalog={},
            consumer_receipt_bindings={},
        )


def test_no_image_transition_rejects_false_ready_with_empty_bindings() -> None:
    query = _no_image_gate_query()
    assert json.loads(query["consumer_receipt_bindings_json"]) == {}
    query["contract_ready_json"] = json.dumps(
        {"mcp": False, "openclaw": True},
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(EVIDENCE.EvidenceError, match=r"mcp release\.ready is false"):
        EVIDENCE._terraform_gate(query, now=NOW)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "contracts_json",
            json.dumps({"mcp": CONTRACT_SHA256}),
            "contracts do not exactly match",
        ),
        (
            "contracts_json",
            json.dumps({"mcp": "invalid", "openclaw": "a" * 64}),
            "mcp contract SHA-256",
        ),
        (
            "application_json",
            "{}",
            "application provenance does not exactly match",
        ),
        (
            "application_json",
            json.dumps({"mcp": APPLICATION, "openclaw": APPLICATION}),
            "application provenance does not exactly match",
        ),
        (
            "application_json",
            json.dumps(
                {
                    "mcp": {
                        **APPLICATION,
                        "sha256": "invalid",
                    }
                }
            ),
            "MCP deployment app SHA-256",
        ),
    ],
)
def test_no_image_transition_validates_contract_and_application_without_claims(
    field: str,
    value: str,
    message: str,
) -> None:
    query = _no_image_gate_query()
    query[field] = value

    with pytest.raises(EVIDENCE.EvidenceError, match=message):
        EVIDENCE._terraform_gate(query, now=NOW)


def test_no_image_transition_ignores_a_disabled_only_pipeline() -> None:
    query = _no_image_gate_query()
    manifest = _consumer_manifest(mode="no-image-transition")
    openclaw = next(
        consumer for consumer in manifest["consumers"] if consumer["consumer_id"] == "openclaw"
    )
    for phase in ("live", "before", "after"):
        openclaw[phase] = {"absent": True}
    query["consumer_manifest_json"] = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
    )
    query["contracts_json"] = json.dumps({"mcp": CONTRACT_SHA256})
    query["contract_ready_json"] = json.dumps({"mcp": True})

    result = EVIDENCE._terraform_gate(query, now=NOW)

    assert result["verified_pipelines"] == "mcp"
    assert result["deployment_mode"] == "no-image-transition"


def test_no_image_apply_revalidation_cannot_extend_or_reuse_the_saved_expiry() -> None:
    query = _no_image_gate_query()
    verified = EVIDENCE._terraform_gate(query, now=NOW)
    metadata: dict[str, Any] = {
        "intent_id": INTENT_ID,
        "gate_query_sha256": hashlib.sha256(EVIDENCE.canonical_bytes(query)).hexdigest(),
        "receipt_authorization_expires_at": int((NOW + dt.timedelta(minutes=30)).timestamp()),
        "deployment_mode": "no-image-transition",
        "deployment_context_sha256": verified["deployment_context_sha256"],
        "receipt_claims_sha256": verified["receipt_claims_sha256"],
        "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
    }
    assert (
        EVIDENCE._verified_receipt_claims_for_saved_plan(
            metadata=metadata,
            query=query,
            now=NOW,
        )
        == []
    )

    stale = dict(
        metadata,
        receipt_authorization_expires_at=int(NOW.timestamp()),
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="apply-time evidence"):
        EVIDENCE._verified_receipt_claims_for_saved_plan(
            metadata=stale,
            query=query,
            now=NOW,
        )
    overlong = dict(
        metadata,
        receipt_authorization_expires_at=int(
            (
                NOW + dt.timedelta(seconds=EVIDENCE.MAX_DEPLOYMENT_INTENT_LIFETIME_SECONDS + 1)
            ).timestamp()
        ),
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="apply-time evidence"):
        EVIDENCE._verified_receipt_claims_for_saved_plan(
            metadata=overlong,
            query=query,
            now=NOW,
        )


def test_shared_consumer_claim_is_unique_per_subject_and_repository() -> None:
    claim_id = "c" * 64
    shared_manifest = _consumer_manifest(
        image_changes={
            "mcp": (
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{_digest('old-mcp')}",
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{CORE_DIGEST}",
            ),
            "connect_web": (
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{_digest('old-connect')}",
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{CORE_DIGEST}",
            ),
        }
    )
    _, claim_ids, claims_sha256 = EVIDENCE._deployment_binding(
        consumer_manifest=shared_manifest,
        receipt_catalog={claim_id: _receipt_locator(claim_id)},
        consumer_receipt_bindings={
            "mcp": claim_id,
            "connect_web": claim_id,
        },
        contracts={"mcp": CONTRACT_SHA256},
        application={"mcp": APPLICATION},
        shared_generation_ledger={},
        release_channels={"mcp": "active", "connect_web": "active"},
        intent_id=INTENT_ID,
    )
    assert claim_ids == [claim_id]
    assert claims_sha256 == hashlib.sha256(EVIDENCE.canonical_bytes([claim_id])).hexdigest()
    assert EVIDENCE._canonical_receipt_claim_ids(["f" * 64, claim_id, "f" * 64]) == [
        claim_id,
        "f" * 64,
    ]

    multi_subject_manifest = _consumer_manifest(
        image_changes={
            "mcp": (
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{_digest('old-mcp')}",
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{CORE_DIGEST}",
            ),
            "tiktok_acquire": (
                (f"{EVIDENCE.REGISTRY}/teamagent-media-worker@{_digest('old-media')}"),
                (f"{EVIDENCE.REGISTRY}/teamagent-media-worker@{MEDIA_DIGEST}"),
            ),
        }
    )
    _, _, _, multi_subject_bindings, multi_subject_claim_ids = EVIDENCE._deployment_receipt_inputs(
        consumer_manifest=multi_subject_manifest,
        receipt_catalog={claim_id: _receipt_locator(claim_id)},
        consumer_receipt_bindings={
            "mcp": claim_id,
            "tiktok_acquire": claim_id,
        },
    )
    assert multi_subject_bindings == {
        "mcp": claim_id,
        "tiktok_acquire": claim_id,
    }
    assert multi_subject_claim_ids == [claim_id]

    cross_pipeline_manifest = _consumer_manifest(
        image_changes={
            "mcp": (
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{_digest('old-mcp')}",
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{CORE_DIGEST}",
            ),
            "openclaw": (
                (f"{EVIDENCE.REGISTRY}/teamagent-openclaw@{_digest('old-openclaw')}"),
                (f"{EVIDENCE.REGISTRY}/teamagent-openclaw@{_digest('new-openclaw')}"),
            ),
        }
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="different pipelines"):
        EVIDENCE._deployment_receipt_inputs(
            consumer_manifest=cross_pipeline_manifest,
            receipt_catalog={claim_id: _receipt_locator(claim_id)},
            consumer_receipt_bindings={
                "mcp": claim_id,
                "openclaw": claim_id,
            },
        )

    split_manifest = _consumer_manifest(
        image_changes={
            "mcp": (
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{_digest('old-mcp')}",
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{CORE_DIGEST}",
            ),
            "connect_web": (
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{_digest('old-connect')}",
                f"{EVIDENCE.REGISTRY}/teamagent-mcp@{_digest('other-core')}",
            ),
        }
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="different digests"):
        EVIDENCE._deployment_receipt_inputs(
            consumer_manifest=split_manifest,
            receipt_catalog={claim_id: _receipt_locator(claim_id)},
            consumer_receipt_bindings={
                "mcp": claim_id,
                "connect_web": claim_id,
            },
        )

    second_claim_id = "d" * 64
    with pytest.raises(EVIDENCE.EvidenceError, match="different digests"):
        EVIDENCE._deployment_receipt_inputs(
            consumer_manifest=split_manifest,
            receipt_catalog={
                claim_id: _receipt_locator(claim_id),
                second_claim_id: _receipt_locator(second_claim_id),
            },
            consumer_receipt_bindings={
                "mcp": claim_id,
                "connect_web": second_claim_id,
            },
        )


def _prepared_intent(
    *,
    intent_id: str,
    plan_sha256: str,
    context_sha256: str,
    claims_sha256: str,
    gate_query_sha256: str = DEFAULT_GATE_QUERY_SHA256,
    shared_ledger_sha256: str = EMPTY_SHARED_LEDGER_SHA256,
    consumer_manifest_sha256: str = DEFAULT_CONSUMER_MANIFEST_SHA256,
    release_evidence_binding_sha256: str = (DEFAULT_RELEASE_EVIDENCE_BINDING_SHA256),
    deployment_mode: str = "receipt-required",
    receipt_authorization_expires_at: str = RECEIPT_AUTHORIZATION_EXPIRES_AT,
) -> dict[str, str | int]:
    return {
        "record_id": f"intent#{intent_id}",
        "record_type": EVIDENCE.DEPLOYMENT_INTENT_KIND,
        "schema_version": EVIDENCE.DEPLOYMENT_INTENT_SCHEMA,
        "intent_id": intent_id,
        "state": "PREPARED",
        "plan_sha256": plan_sha256,
        "deployment_context_sha256": context_sha256,
        "receipt_claims_sha256": claims_sha256,
        "shared_ledger_sha256": shared_ledger_sha256,
        "gate_query_sha256": gate_query_sha256,
        "terraform_context_sha256": "c" * 64,
        "backend_workspace_sha256": "d" * 64,
        "state_lineage": "11111111-1111-4111-8111-111111111111",
        "state_serial": 1234,
        "state_addresses_sha256": "e" * 64,
        "plan_addresses_sha256": "f" * 64,
        "runtime_images_sha256": "9" * 64,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        "consumer_manifest_sha256": consumer_manifest_sha256,
        "consumer_count": 8,
        "consumer_comparison_sha256": "1" * 64,
        "release_evidence_binding_sha256": release_evidence_binding_sha256,
        "deployment_mode": deployment_mode,
        "control_commit": COMMIT,
        "prepared_at": "2026-07-17T06:00:00Z",
        "authorization_expires_at": int(receipt_authorization_expires_at),
        "audit_expires_at": int((NOW + dt.timedelta(days=90)).timestamp()),
    }


def _apply_lock(
    metadata: dict[str, str],
    *,
    apply_attempt_id: str,
) -> dict[str, str | int]:
    return EVIDENCE._deployment_lock_item(
        metadata=metadata,
        terraform_context_sha256="c" * 64,
        apply_attempt_id=apply_attempt_id,
        now=NOW,
    )


def _applying_intent(
    *,
    intent_id: str,
    plan_sha256: str,
    context_sha256: str,
    claims_sha256: str,
    apply_attempt_id: str,
    gate_query_sha256: str = DEFAULT_GATE_QUERY_SHA256,
    shared_ledger_sha256: str = EMPTY_SHARED_LEDGER_SHA256,
    consumer_manifest_sha256: str = DEFAULT_CONSUMER_MANIFEST_SHA256,
    release_evidence_binding_sha256: str = (DEFAULT_RELEASE_EVIDENCE_BINDING_SHA256),
    deployment_mode: str = "receipt-required",
    receipt_authorization_expires_at: str = RECEIPT_AUTHORIZATION_EXPIRES_AT,
) -> dict[str, str | int]:
    intent = _prepared_intent(
        intent_id=intent_id,
        plan_sha256=plan_sha256,
        context_sha256=context_sha256,
        claims_sha256=claims_sha256,
        gate_query_sha256=gate_query_sha256,
        shared_ledger_sha256=shared_ledger_sha256,
        consumer_manifest_sha256=consumer_manifest_sha256,
        release_evidence_binding_sha256=release_evidence_binding_sha256,
        deployment_mode=deployment_mode,
        receipt_authorization_expires_at=receipt_authorization_expires_at,
    )
    intent.update(
        {
            "state": "APPLYING",
            "apply_attempt_id": apply_attempt_id,
            "apply_started_at": NOW.isoformat().replace("+00:00", "Z"),
        }
    )
    return intent


def _terraform_context() -> dict[str, str | int]:
    return {
        "terraform_context_sha256": "c" * 64,
        "backend_workspace_sha256": "d" * 64,
        "state_lineage": "11111111-1111-4111-8111-111111111111",
        "state_serial": 1234,
        "state_addresses_sha256": "e" * 64,
        "plan_addresses_sha256": "f" * 64,
        "runtime_images_sha256": "9" * 64,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        "consumer_manifest_sha256": DEFAULT_CONSUMER_MANIFEST_SHA256,
        "consumer_count": 8,
        "consumer_comparison_sha256": "1" * 64,
        "release_evidence_binding_sha256": (DEFAULT_RELEASE_EVIDENCE_BINDING_SHA256),
        "deployment_mode": "receipt-required",
    }


def _terraform_context_document() -> dict[str, Any]:
    return {
        "schema_version": 3,
        "kind": "teamagent.image-release-terraform-context",
        "backend": {
            "type": "s3",
            "bucket": "teamagent-tfstate-718959508629",
            "key": "teamagent/terraform.tfstate",
            "region": "ap-northeast-1",
            "dynamodb_table": "teamagent-tflock",
            "encrypt": True,
        },
        "workspace": "default",
        "state": {
            "lineage": "11111111-1111-4111-8111-111111111111",
            "serial": 1234,
            "managed_address_count": 22,
            "managed_addresses_sha256": "e" * 64,
        },
        "consumer_manifest": copy.deepcopy(DEFAULT_CONSUMER_MANIFEST),
        "plan": {
            "complete": True,
            "applyable": True,
            "errored": False,
            "managed_change_count": 22,
            "address_ownership_sha256": "f" * 64,
            "runtime_images_sha256": "9" * 64,
            "consumer_manifest_sha256": DEFAULT_CONSUMER_MANIFEST_SHA256,
            "consumer_count": 8,
            "consumer_comparison_sha256": "1" * 64,
            "release_evidence_binding_sha256": (DEFAULT_RELEASE_EVIDENCE_BINDING_SHA256),
            "delete_change_count": 0,
            "replace_change_count": 0,
            "transition_sha256": EMPTY_TRANSITION_SHA256,
        },
    }


def test_terraform_context_metadata_validates_and_summarizes_exact_context(
    tmp_path: Path,
) -> None:
    context = _terraform_context_document()
    context_path = tmp_path / "terraform-context.json"
    context_path.write_text(
        json.dumps(context, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    metadata = EVIDENCE.terraform_context_metadata(context_path)

    assert metadata == {
        "terraform_context_sha256": hashlib.sha256(EVIDENCE.canonical_bytes(context)).hexdigest(),
        "backend_workspace_sha256": hashlib.sha256(
            EVIDENCE.canonical_bytes(
                {
                    "backend": context["backend"],
                    "workspace": context["workspace"],
                }
            )
        ).hexdigest(),
        "state_lineage": "11111111-1111-4111-8111-111111111111",
        "state_serial": 1234,
        "state_addresses_sha256": "e" * 64,
        "plan_addresses_sha256": "f" * 64,
        "runtime_images_sha256": "9" * 64,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        "consumer_manifest_sha256": DEFAULT_CONSUMER_MANIFEST_SHA256,
        "consumer_count": 8,
        "consumer_comparison_sha256": "1" * 64,
        "release_evidence_binding_sha256": (DEFAULT_RELEASE_EVIDENCE_BINDING_SHA256),
        "deployment_mode": "receipt-required",
    }

    invalid = copy.deepcopy(context)
    invalid["plan"]["consumer_manifest_sha256"] = "0" * 64
    context_path.write_text(
        json.dumps(invalid, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(
        EVIDENCE.EvidenceError,
        match="Terraform runtime context is invalid",
    ):
        EVIDENCE.terraform_context_metadata(context_path)


def test_prepare_deployment_intent_binds_context_and_persists_prepared_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = {
        "intent_id": INTENT_ID,
        "plan_sha256": "d" * 64,
        "deployment_context_sha256": "a" * 64,
        "receipt_claims_sha256": "b" * 64,
        "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        **_gate_metadata_fields(),
    }
    terraform_context = _terraform_context()
    persisted: list[dict[str, str | int]] = []

    def plan_metadata(
        plan_path: Path,
        *,
        plan_json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert plan_path == Path("release.tfplan")
        assert plan_json == {"saved": "plan"}
        return copy.deepcopy(metadata)

    def context_metadata(context_path: Path) -> dict[str, str | int]:
        assert context_path == Path("terraform-context.json")
        return copy.deepcopy(terraform_context)

    monkeypatch.setattr(EVIDENCE, "deployment_plan_metadata", plan_metadata)
    monkeypatch.setattr(EVIDENCE, "terraform_context_metadata", context_metadata)
    monkeypatch.setattr(
        EVIDENCE,
        "_dynamodb_put_prepared_intent",
        lambda item: persisted.append(copy.deepcopy(item)),
    )

    prepared = EVIDENCE.prepare_deployment_intent(
        Path("release.tfplan"),
        control_commit=COMMIT,
        terraform_context_path=Path("terraform-context.json"),
        now=NOW,
        plan_json={"saved": "plan"},
    )

    assert prepared == _prepared_intent(
        intent_id=INTENT_ID,
        plan_sha256="d" * 64,
        context_sha256="a" * 64,
        claims_sha256="b" * 64,
    )
    assert persisted == [prepared]

    mismatched_context = dict(
        terraform_context,
        consumer_manifest_sha256="0" * 64,
    )
    monkeypatch.setattr(
        EVIDENCE,
        "terraform_context_metadata",
        lambda _: mismatched_context,
    )
    with pytest.raises(
        EVIDENCE.EvidenceError,
        match="consumer manifest differs from the saved plan",
    ):
        EVIDENCE.prepare_deployment_intent(
            Path("release.tfplan"),
            control_commit=COMMIT,
            terraform_context_path=Path("terraform-context.json"),
            now=NOW,
            plan_json={"saved": "plan"},
        )
    assert persisted == [prepared]


def test_preflight_revalidates_and_consumes_before_returning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_id = "c" * 64
    claims_sha256 = hashlib.sha256(EVIDENCE.canonical_bytes([claim_id])).hexdigest()
    metadata = {
        "intent_id": INTENT_ID,
        "plan_sha256": "d" * 64,
        "deployment_context_sha256": "e" * 64,
        "receipt_claims_sha256": claims_sha256,
        "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        **_gate_metadata_fields(),
    }
    applying = _applying_intent(
        intent_id=INTENT_ID,
        plan_sha256=metadata["plan_sha256"],
        context_sha256=metadata["deployment_context_sha256"],
        claims_sha256=claims_sha256,
        apply_attempt_id=ATTEMPT_ID,
    )
    lock = _apply_lock(metadata, apply_attempt_id=ATTEMPT_ID)
    store = {
        f"intent#{INTENT_ID}": applying,
        EVIDENCE.DEPLOYMENT_LOCK_RECORD_ID: lock,
    }
    order: list[str] = []

    monkeypatch.setattr(
        EVIDENCE,
        "deployment_plan_metadata",
        lambda *args, **kwargs: metadata,
    )
    monkeypatch.setattr(
        EVIDENCE,
        "terraform_context_metadata",
        lambda *args, **kwargs: _terraform_context(),
    )
    monkeypatch.setattr(
        EVIDENCE,
        "_dynamodb_get",
        lambda record_id: copy.deepcopy(store.get(record_id)),
    )

    def verify(
        *,
        metadata: dict[str, str],
        query: dict[str, str],
        now: dt.datetime,
    ) -> list[str]:
        order.append("fresh-receipt-verification")
        assert metadata["plan_sha256"] == "d" * 64
        assert query == DEFAULT_GATE_QUERY
        assert now == NOW
        return [claim_id]

    def consume(
        *,
        metadata: dict[str, str],
        receipt_claim_ids: list[str],
        apply_attempt_id: str,
        now: dt.datetime,
        expected_control_commit: str | None = None,
        expected_terraform_context_sha256: str | None = None,
    ) -> dict[str, str | int]:
        order.append("atomic-one-use-consume")
        assert receipt_claim_ids == [claim_id]
        assert apply_attempt_id == ATTEMPT_ID
        assert now == NOW
        assert expected_control_commit == COMMIT
        assert expected_terraform_context_sha256 == "c" * 64
        return {**applying, "state": "CONSUMED"}

    monkeypatch.setattr(
        EVIDENCE,
        "_verified_receipt_claims_for_saved_plan",
        verify,
    )
    monkeypatch.setattr(
        EVIDENCE,
        "_consume_applying_deployment_intent",
        consume,
    )

    consumed = EVIDENCE.validate_deployment_preflight(
        Path("unused.tfplan"),
        terraform_context_path=Path("unused-context.json"),
        apply_attempt_id=ATTEMPT_ID,
        control_commit=COMMIT,
        now=NOW,
    )

    assert consumed["state"] == "CONSUMED"
    assert order == [
        "fresh-receipt-verification",
        "atomic-one-use-consume",
    ]


def test_preflight_stale_receipt_fails_before_atomic_consume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = {
        "intent_id": INTENT_ID,
        "plan_sha256": "d" * 64,
        "deployment_context_sha256": "e" * 64,
        "receipt_claims_sha256": "b" * 64,
        "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        **_gate_metadata_fields(),
    }
    applying = _applying_intent(
        intent_id=INTENT_ID,
        plan_sha256=metadata["plan_sha256"],
        context_sha256=metadata["deployment_context_sha256"],
        claims_sha256=metadata["receipt_claims_sha256"],
        apply_attempt_id=ATTEMPT_ID,
    )
    store = {
        f"intent#{INTENT_ID}": applying,
        EVIDENCE.DEPLOYMENT_LOCK_RECORD_ID: _apply_lock(
            metadata,
            apply_attempt_id=ATTEMPT_ID,
        ),
    }
    monkeypatch.setattr(
        EVIDENCE,
        "deployment_plan_metadata",
        lambda *args, **kwargs: metadata,
    )
    monkeypatch.setattr(
        EVIDENCE,
        "terraform_context_metadata",
        lambda *args, **kwargs: _terraform_context(),
    )
    monkeypatch.setattr(
        EVIDENCE,
        "_dynamodb_get",
        lambda record_id: copy.deepcopy(store.get(record_id)),
    )

    def stale(**_: Any) -> list[str]:
        raise EVIDENCE.EvidenceError("release receipt is stale")

    def must_not_consume(**_: Any) -> dict[str, str | int]:
        raise AssertionError("stale receipt reached the atomic consume")

    monkeypatch.setattr(
        EVIDENCE,
        "_verified_receipt_claims_for_saved_plan",
        stale,
    )
    monkeypatch.setattr(
        EVIDENCE,
        "_consume_applying_deployment_intent",
        must_not_consume,
    )

    with pytest.raises(EVIDENCE.EvidenceError, match="receipt is stale"):
        EVIDENCE.validate_deployment_preflight(
            Path("unused.tfplan"),
            terraform_context_path=Path("unused-context.json"),
            apply_attempt_id=ATTEMPT_ID,
            control_commit=COMMIT,
            now=NOW,
        )


def test_apply_attempt_and_shared_lock_start_in_one_conditional_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims_sha256 = "b" * 64
    metadata = {
        "intent_id": INTENT_ID,
        "plan_sha256": "d" * 64,
        "deployment_context_sha256": "a" * 64,
        "receipt_claims_sha256": claims_sha256,
        "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        **_gate_metadata_fields(),
    }
    prepared = _prepared_intent(
        intent_id=INTENT_ID,
        plan_sha256=metadata["plan_sha256"],
        context_sha256=metadata["deployment_context_sha256"],
        claims_sha256=claims_sha256,
    )
    lock = _apply_lock(metadata, apply_attempt_id=ATTEMPT_ID)
    captured: tuple[str, ...] = ()

    def fake_aws(*arguments: str, output: Path | None = None) -> str:
        nonlocal captured
        assert output is None
        captured = arguments
        return "{}"

    monkeypatch.setattr(EVIDENCE, "_aws", fake_aws)
    EVIDENCE._dynamodb_transact_begin_apply(
        prepared=prepared,
        metadata=metadata,
        lock_item=lock,
        apply_attempt_id=ATTEMPT_ID,
        control_commit=COMMIT,
        now=NOW,
    )

    assert captured[:2] == ("dynamodb", "transact-write-items")
    transaction = json.loads(captured[captured.index("--transact-items") + 1])
    assert len(transaction) == 2
    assert transaction[0]["Put"]["ConditionExpression"] == (
        "attribute_not_exists(record_id) OR lease_expires_at < :now"
    )
    transition = transaction[1]["Update"]
    assert "#state = :prepared" in transition["ConditionExpression"]
    assert "authorization_expires_at > :now" in transition["ConditionExpression"]
    assert "terraform_context_sha256 = :terraform_context" in (transition["ConditionExpression"])
    assert "control_commit = :control_commit" in transition["ConditionExpression"]
    assert transition["ExpressionAttributeValues"][":control_commit"] == {"S": COMMIT}
    assert "SET #state = :applying" in transition["UpdateExpression"]
    assert "apply_attempt_id = :attempt" in transition["UpdateExpression"]
    begin_token = captured[captured.index("--client-request-token") + 1]
    assert begin_token == EVIDENCE._dynamodb_transaction_token(
        ATTEMPT_ID,
        phase="begin-apply",
    )
    assert begin_token != EVIDENCE._dynamodb_transaction_token(
        ATTEMPT_ID,
        phase="consume-authorization",
    )


def test_apply_rejects_a_different_checkout_before_starting_the_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = {
        "intent_id": INTENT_ID,
        "plan_sha256": "d" * 64,
        "deployment_context_sha256": "a" * 64,
        "receipt_claims_sha256": "b" * 64,
        "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        **_gate_metadata_fields(),
    }
    prepared = _prepared_intent(
        intent_id=INTENT_ID,
        plan_sha256=metadata["plan_sha256"],
        context_sha256=metadata["deployment_context_sha256"],
        claims_sha256=metadata["receipt_claims_sha256"],
    )
    monkeypatch.setattr(
        EVIDENCE,
        "deployment_plan_metadata",
        lambda *args, **kwargs: metadata,
    )
    monkeypatch.setattr(
        EVIDENCE,
        "_dynamodb_get",
        lambda record_id: copy.deepcopy(prepared),
    )

    def must_not_start(**_: Any) -> None:
        raise AssertionError("mismatched control commit reached the applying transition")

    monkeypatch.setattr(EVIDENCE, "_dynamodb_transact_begin_apply", must_not_start)

    with pytest.raises(
        EVIDENCE.EvidenceError,
        match="control commit differs from the apply checkout",
    ):
        EVIDENCE.acquire_deployment_lock(
            Path("unused.tfplan"),
            apply_attempt_id=ATTEMPT_ID,
            control_commit="f" * 40,
            now=NOW,
        )


def test_same_intent_and_same_receipt_cannot_authorize_two_deployments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_id = "c" * 64
    claims_sha256 = hashlib.sha256(EVIDENCE.canonical_bytes([claim_id])).hexdigest()
    metadata = {
        "intent_id": INTENT_ID,
        "plan_sha256": "d" * 64,
        "deployment_context_sha256": "e" * 64,
        "receipt_claims_sha256": claims_sha256,
        "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        **_gate_metadata_fields(),
    }
    second_intent_id = "33333333-3333-4333-8333-333333333333"
    second_metadata = dict(metadata, intent_id=second_intent_id, plan_sha256="f" * 64)
    store: dict[str, dict[str, str | int]] = {
        f"intent#{INTENT_ID}": _applying_intent(
            intent_id=INTENT_ID,
            plan_sha256=metadata["plan_sha256"],
            context_sha256=metadata["deployment_context_sha256"],
            claims_sha256=claims_sha256,
            apply_attempt_id=ATTEMPT_ID,
        ),
        f"intent#{second_intent_id}": _applying_intent(
            intent_id=second_intent_id,
            plan_sha256=second_metadata["plan_sha256"],
            context_sha256=second_metadata["deployment_context_sha256"],
            claims_sha256=claims_sha256,
            apply_attempt_id="55555555-5555-4555-8555-555555555555",
        ),
        EVIDENCE.DEPLOYMENT_LOCK_RECORD_ID: _apply_lock(
            metadata,
            apply_attempt_id=ATTEMPT_ID,
        ),
    }

    def fake_get(record_id: str) -> dict[str, str | int] | None:
        value = store.get(record_id)
        return copy.deepcopy(value) if value is not None else None

    def fake_transact(
        *,
        applying: dict[str, str | int],
        metadata: dict[str, str],
        receipt_claim_ids: list[str],
        apply_attempt_id: str,
        now: dt.datetime,
    ) -> None:
        intent = store[str(applying["record_id"])]
        if intent["state"] != "APPLYING":
            raise EVIDENCE.EvidenceError("conditional intent transition failed")
        if any(f"receipt#{claim}" in store for claim in receipt_claim_ids):
            raise EVIDENCE.EvidenceError("conditional receipt claim failed")
        consumed_at = now.isoformat().replace("+00:00", "Z")
        intent.update(
            {
                "state": "CONSUMED",
                "apply_attempt_id": apply_attempt_id,
                "consumed_at": consumed_at,
            }
        )
        for claim in receipt_claim_ids:
            store[f"receipt#{claim}"] = {
                "record_id": f"receipt#{claim}",
                "record_type": "teamagent.release-receipt-claim",
                "schema_version": EVIDENCE.DEPLOYMENT_INTENT_SCHEMA,
                "receipt_claim_id": claim,
                "intent_id": metadata["intent_id"],
                "plan_sha256": metadata["plan_sha256"],
                "deployment_context_sha256": metadata["deployment_context_sha256"],
                "receipt_claims_sha256": metadata["receipt_claims_sha256"],
                "gate_query_sha256": metadata["gate_query_sha256"],
                "terraform_context_sha256": applying["terraform_context_sha256"],
                "apply_attempt_id": apply_attempt_id,
                "consumed_at": consumed_at,
                "audit_expires_at": applying["audit_expires_at"],
            }

    monkeypatch.setattr(EVIDENCE, "_dynamodb_get", fake_get)
    monkeypatch.setattr(EVIDENCE, "_dynamodb_transact_consume", fake_transact)

    consumed = EVIDENCE._consume_applying_deployment_intent(
        metadata=metadata,
        receipt_claim_ids=[claim_id],
        apply_attempt_id=ATTEMPT_ID,
        now=NOW,
    )
    assert consumed["state"] == "CONSUMED"

    with pytest.raises(EVIDENCE.EvidenceError, match="intent has already"):
        EVIDENCE._consume_applying_deployment_intent(
            metadata=metadata,
            receipt_claim_ids=[claim_id],
            apply_attempt_id="44444444-4444-4444-8444-444444444444",
            now=NOW,
        )

    second_attempt_id = "55555555-5555-4555-8555-555555555555"
    store[EVIDENCE.DEPLOYMENT_LOCK_RECORD_ID] = _apply_lock(
        second_metadata,
        apply_attempt_id=second_attempt_id,
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="receipt has already"):
        EVIDENCE._consume_applying_deployment_intent(
            metadata=second_metadata,
            receipt_claim_ids=[claim_id],
            apply_attempt_id=second_attempt_id,
            now=NOW,
        )


def test_expired_prepared_intent_cannot_reach_the_atomic_consume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_id = "c" * 64
    claims_sha256 = hashlib.sha256(EVIDENCE.canonical_bytes([claim_id])).hexdigest()
    metadata = {
        "intent_id": INTENT_ID,
        "plan_sha256": "d" * 64,
        "deployment_context_sha256": "e" * 64,
        "receipt_claims_sha256": claims_sha256,
        "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        **_gate_metadata_fields(),
    }
    expired = _applying_intent(
        intent_id=INTENT_ID,
        plan_sha256=metadata["plan_sha256"],
        context_sha256=metadata["deployment_context_sha256"],
        claims_sha256=claims_sha256,
        apply_attempt_id=ATTEMPT_ID,
    )
    expired["authorization_expires_at"] = int((NOW - dt.timedelta(seconds=1)).timestamp())
    monkeypatch.setattr(EVIDENCE, "_dynamodb_get", lambda record_id: copy.deepcopy(expired))

    def must_not_consume(**_: Any) -> None:
        raise AssertionError("expired intent reached the consume transaction")

    monkeypatch.setattr(EVIDENCE, "_dynamodb_transact_consume", must_not_consume)
    with pytest.raises(EVIDENCE.EvidenceError, match="intent is stale"):
        EVIDENCE._consume_applying_deployment_intent(
            metadata=metadata,
            receipt_claim_ids=[claim_id],
            apply_attempt_id=ATTEMPT_ID,
            now=NOW,
        )


def test_apply_time_revalidates_receipt_before_consuming_intent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan = tmp_path / "release.tfplan"
    plan.write_bytes(b"opaque saved terraform plan")
    monkeypatch.setattr(
        EVIDENCE,
        "deployment_plan_metadata",
        lambda *args, **kwargs: {
            "intent_id": INTENT_ID,
            "plan_sha256": "d" * 64,
            "deployment_context_sha256": "e" * 64,
            "receipt_claims_sha256": "f" * 64,
            "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
            "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
            **_gate_metadata_fields(),
        },
    )

    def stale_receipt(
        _: Any,
        *,
        now: dt.datetime | None = None,
    ) -> dict[str, str]:
        assert now is not None
        raise EVIDENCE.EvidenceError("release receipt is stale")

    monkeypatch.setattr(EVIDENCE, "_terraform_gate", stale_receipt)

    def must_not_consume(**_: Any) -> dict[str, str | int]:
        raise AssertionError("stale receipt reached deployment intent consumption")

    monkeypatch.setattr(
        EVIDENCE,
        "_consume_applying_deployment_intent",
        must_not_consume,
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="receipt is stale"):
        EVIDENCE.consume_deployment_intent(
            plan,
            query={},
            apply_attempt_id=ATTEMPT_ID,
        )


def test_consume_deployment_intent_revalidates_and_consumes_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = copy.deepcopy(DEFAULT_GATE_QUERY)
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
    ) = EVIDENCE._parse_terraform_gate_query(query)
    release_channels = {"mcp": "active"}
    context_sha256, canonical_claims, claims_sha256 = EVIDENCE._deployment_binding(
        consumer_manifest=consumer_manifest,
        receipt_catalog=receipt_catalog,
        consumer_receipt_bindings=consumer_receipt_bindings,
        contracts=contracts,
        application=application,
        shared_generation_ledger=shared_generation_ledger,
        release_channels=release_channels,
        intent_id=intent_id,
    )
    gate_metadata = _gate_metadata_fields(
        query,
        release_channels=release_channels,
    )
    metadata = {
        "intent_id": intent_id,
        "plan_sha256": "d" * 64,
        "deployment_context_sha256": context_sha256,
        "receipt_claims_sha256": claims_sha256,
        "shared_ledger_sha256": hashlib.sha256(
            EVIDENCE.canonical_bytes(shared_generation_ledger)
        ).hexdigest(),
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        **gate_metadata,
    }
    store: dict[str, dict[str, str | int]] = {
        f"intent#{intent_id}": _applying_intent(
            intent_id=intent_id,
            plan_sha256=metadata["plan_sha256"],
            context_sha256=context_sha256,
            claims_sha256=claims_sha256,
            apply_attempt_id=ATTEMPT_ID,
            gate_query_sha256=metadata["gate_query_sha256"],
            shared_ledger_sha256=metadata["shared_ledger_sha256"],
            consumer_manifest_sha256=metadata["consumer_manifest_sha256"],
            release_evidence_binding_sha256=metadata["release_evidence_binding_sha256"],
            deployment_mode=metadata["deployment_mode"],
            receipt_authorization_expires_at=metadata["receipt_authorization_expires_at"],
        ),
        EVIDENCE.DEPLOYMENT_LOCK_RECORD_ID: _apply_lock(
            metadata,
            apply_attempt_id=ATTEMPT_ID,
        ),
    }
    consumed_claims: list[str] = []

    def fake_get(record_id: str) -> dict[str, str | int] | None:
        item = store.get(record_id)
        return copy.deepcopy(item) if item is not None else None

    def fake_gate(
        gate_query: Mapping[str, Any],
        *,
        now: dt.datetime | None = None,
    ) -> dict[str, str]:
        assert gate_query == query
        assert now == NOW
        return {
            "deployment_mode": metadata["deployment_mode"],
            "deployment_context_sha256": context_sha256,
            "receipt_claims_sha256": claims_sha256,
            "receipt_authorization_expires_at": metadata["receipt_authorization_expires_at"],
            "release_channels_json": json.dumps(
                release_channels,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }

    def fake_transact(
        *,
        applying: Mapping[str, str | int],
        metadata: Mapping[str, str],
        receipt_claim_ids: list[str],
        apply_attempt_id: str,
        now: dt.datetime,
    ) -> None:
        consumed_claims.extend(receipt_claim_ids)
        consumed_at = now.isoformat().replace("+00:00", "Z")
        intent = store[str(applying["record_id"])]
        intent.update(
            {
                "state": "CONSUMED",
                "apply_attempt_id": apply_attempt_id,
                "consumed_at": consumed_at,
            }
        )
        for claim_id in receipt_claim_ids:
            store[f"receipt#{claim_id}"] = {
                "record_id": f"receipt#{claim_id}",
                "record_type": "teamagent.release-receipt-claim",
                "schema_version": EVIDENCE.DEPLOYMENT_INTENT_SCHEMA,
                "receipt_claim_id": claim_id,
                "intent_id": metadata["intent_id"],
                "plan_sha256": metadata["plan_sha256"],
                "deployment_context_sha256": metadata["deployment_context_sha256"],
                "receipt_claims_sha256": metadata["receipt_claims_sha256"],
                "gate_query_sha256": metadata["gate_query_sha256"],
                "terraform_context_sha256": applying["terraform_context_sha256"],
                "apply_attempt_id": apply_attempt_id,
                "consumed_at": consumed_at,
                "audit_expires_at": applying["audit_expires_at"],
            }

    monkeypatch.setattr(
        EVIDENCE,
        "deployment_plan_metadata",
        lambda *args, **kwargs: copy.deepcopy(metadata),
    )
    monkeypatch.setattr(EVIDENCE, "_terraform_gate", fake_gate)
    monkeypatch.setattr(EVIDENCE, "_dynamodb_get", fake_get)
    monkeypatch.setattr(EVIDENCE, "_dynamodb_transact_consume", fake_transact)

    consumed = EVIDENCE.consume_deployment_intent(
        Path("unused.tfplan"),
        query=query,
        apply_attempt_id=ATTEMPT_ID,
        now=NOW,
    )

    assert consumed["state"] == "CONSUMED"
    assert consumed["apply_attempt_id"] == ATTEMPT_ID
    assert consumed_claims == canonical_claims
    assert (
        sorted(
            record_id.removeprefix("receipt#")
            for record_id in store
            if record_id.startswith("receipt#")
        )
        == canonical_claims
    )


def test_receipt_consumption_uses_one_conditional_dynamodb_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_id = "c" * 64
    claims_sha256 = hashlib.sha256(EVIDENCE.canonical_bytes([claim_id])).hexdigest()
    metadata = {
        "intent_id": INTENT_ID,
        "plan_sha256": "d" * 64,
        "deployment_context_sha256": "e" * 64,
        "receipt_claims_sha256": claims_sha256,
        "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        **_gate_metadata_fields(),
    }
    applying = _applying_intent(
        intent_id=INTENT_ID,
        plan_sha256=metadata["plan_sha256"],
        context_sha256=metadata["deployment_context_sha256"],
        claims_sha256=claims_sha256,
        apply_attempt_id=ATTEMPT_ID,
    )
    captured: tuple[str, ...] = ()

    def fake_aws(*arguments: str, output: Path | None = None) -> str:
        nonlocal captured
        assert output is None
        captured = arguments
        return "{}"

    monkeypatch.setattr(EVIDENCE, "_aws", fake_aws)
    EVIDENCE._dynamodb_transact_consume(
        applying=applying,
        metadata=metadata,
        receipt_claim_ids=[claim_id],
        apply_attempt_id=ATTEMPT_ID,
        now=NOW,
    )

    assert captured[:2] == ("dynamodb", "transact-write-items")
    transaction = json.loads(captured[captured.index("--transact-items") + 1])
    assert len(transaction) == 3
    assert (
        "lease_expires_at > :now_epoch" in transaction[0]["ConditionCheck"]["ConditionExpression"]
    )
    lock_condition = transaction[0]["ConditionCheck"]["ConditionExpression"]
    assert "deployment_context_sha256 = :context" in lock_condition
    assert "receipt_claims_sha256 = :claims" in lock_condition
    assert "gate_query_sha256 = :gate_query" in lock_condition
    assert "terraform_context_sha256 = :terraform_context" in lock_condition
    assert "#state = :applying" in transaction[1]["Update"]["ConditionExpression"]
    assert "apply_attempt_id = :attempt" in transaction[1]["Update"]["ConditionExpression"]
    assert (
        "authorization_expires_at > :now_epoch" in transaction[1]["Update"]["ConditionExpression"]
    )
    assert transaction[2]["Put"]["ConditionExpression"] == "attribute_not_exists(record_id)"
    claim_item = transaction[2]["Put"]["Item"]
    assert claim_item["deployment_context_sha256"] == {"S": "e" * 64}
    assert claim_item["receipt_claims_sha256"] == {"S": claims_sha256}
    assert claim_item["gate_query_sha256"] == {"S": DEFAULT_GATE_QUERY_SHA256}
    assert claim_item["terraform_context_sha256"] == {"S": "c" * 64}
    assert claim_item["apply_attempt_id"] == {"S": ATTEMPT_ID}
    consume_token = captured[captured.index("--client-request-token") + 1]
    assert consume_token == EVIDENCE._dynamodb_transaction_token(
        ATTEMPT_ID,
        phase="consume-authorization",
    )
    assert consume_token != EVIDENCE._dynamodb_transaction_token(
        ATTEMPT_ID,
        phase="begin-apply",
    )


def test_duplicate_consumer_bindings_reach_dynamodb_as_one_distinct_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_id = "c" * 64
    claims_sha256 = hashlib.sha256(EVIDENCE.canonical_bytes([claim_id])).hexdigest()
    metadata = {
        "intent_id": INTENT_ID,
        "plan_sha256": "d" * 64,
        "deployment_context_sha256": "e" * 64,
        "receipt_claims_sha256": claims_sha256,
        "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        **_gate_metadata_fields(),
    }
    applying = _applying_intent(
        intent_id=INTENT_ID,
        plan_sha256=metadata["plan_sha256"],
        context_sha256=metadata["deployment_context_sha256"],
        claims_sha256=claims_sha256,
        apply_attempt_id=ATTEMPT_ID,
    )
    lock = _apply_lock(metadata, apply_attempt_id=ATTEMPT_ID)
    seen_claims: list[str] = []

    monkeypatch.setattr(
        EVIDENCE,
        "_dynamodb_get",
        lambda record_id: copy.deepcopy(
            applying
            if record_id == f"intent#{INTENT_ID}"
            else lock
            if record_id == EVIDENCE.DEPLOYMENT_LOCK_RECORD_ID
            else None
        ),
    )

    def capture_distinct_claims(**kwargs: Any) -> None:
        seen_claims.extend(kwargs["receipt_claim_ids"])
        raise RuntimeError("stop after canonical claim capture")

    monkeypatch.setattr(
        EVIDENCE,
        "_dynamodb_transact_consume",
        capture_distinct_claims,
    )
    with pytest.raises(RuntimeError, match="canonical claim capture"):
        EVIDENCE._consume_applying_deployment_intent(
            metadata=metadata,
            receipt_claim_ids=[claim_id, claim_id],
            apply_attempt_id=ATTEMPT_ID,
            now=NOW,
        )
    assert seen_claims == [claim_id]


def test_unlisted_referrer_is_part_of_protected_lifecycle_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlisted = "sha256:" + "f" * 64

    def fake_referrers(
        repository: str,
        subject_digest: str,
        *,
        label: str,
    ) -> list[dict[str, Any]]:
        assert repository == "teamagent-mcp"
        assert label
        if subject_digest == DIGEST:
            return [
                {
                    "digest": unlisted,
                    "artifactType": "application/example",
                    "artifactStatus": "ACTIVE",
                }
            ]
        return []

    monkeypatch.setattr(EVIDENCE, "_release_referrers", fake_referrers)
    graph = EVIDENCE._release_referrer_graph(
        "teamagent-mcp",
        DIGEST,
        label="active release",
    )
    assert unlisted in graph

    preview = {
        "lifecyclePolicyPreviewResults": [
            {
                "imageDigest": unlisted,
                "action": {"type": "EXPIRE"},
            }
        ]
    }
    with pytest.raises(EVIDENCE.EvidenceError, match="protected release graph"):
        EVIDENCE.validate_lifecycle_preview(
            preview,
            protected_digests=set(graph),
        )


def test_release_lifecycle_absence_check_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run_without_policy(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        env: dict[str, str],
    ) -> Any:
        assert check is False
        assert capture_output is True
        assert text is True
        calls.append((command, env))
        return type(
            "Result",
            (),
            {
                "returncode": 254,
                "stderr": "LifecyclePolicyNotFoundException",
            },
        )()

    monkeypatch.setattr(EVIDENCE.subprocess, "run", run_without_policy)
    EVIDENCE._assert_no_release_lifecycle_policy(
        "teamagent-mcp",
        label="active core",
    )
    command, environment = calls[0]
    assert command[:3] == ["aws", "ecr", "get-lifecycle-policy"]
    assert command[command.index("--repository-name") + 1] == "teamagent-mcp"
    assert environment["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] == "true"

    def run_with_policy(*args: Any, **kwargs: Any) -> Any:
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(EVIDENCE.subprocess, "run", run_with_policy)
    with pytest.raises(EVIDENCE.EvidenceError, match="must not have"):
        EVIDENCE._assert_no_release_lifecycle_policy(
            "teamagent-mcp",
            label="active core",
        )

    def run_ambiguous(*args: Any, **kwargs: Any) -> Any:
        return type("Result", (), {"returncode": 254, "stderr": "AccessDenied"})()

    monkeypatch.setattr(EVIDENCE.subprocess, "run", run_ambiguous)
    with pytest.raises(EVIDENCE.EvidenceError, match="could not be verified"):
        EVIDENCE._assert_no_release_lifecycle_policy(
            "teamagent-mcp",
            label="active core",
        )


# --- Initial-release exemption ------------------------------------------------
#
# These pin behaviour that nothing pinned before: the strictness of build and
# authorize was never covered by a test, so a green suite proved nothing about
# it.  The dates below are deliberately wall-clock literals rather than values
# derived from the module constants -- deriving them would let a two-line diff
# that slides the sunset into the future stay green.

_EXEMPT_WINDOW_NOW = dt.datetime(2026, 8, 20, 6, 0, tzinfo=dt.UTC)
_EXEMPT_APPROVED_AT = "2026-08-20T05:00:00Z"
_EXEMPT_OBSERVED_AT = "2026-08-20T04:00:00Z"
_EXEMPT_EXPIRES_AT = "2026-08-20T07:00:00Z"
_EXEMPT_RETENTION_UNTIL = "2036-10-01T00:00:00+00:00"


def _pinned_exemption_gate(**overrides: Any) -> dict[str, Any]:
    campaign = {
        "campaign_id": EVIDENCE.INITIAL_RELEASE_EXEMPTION_CAMPAIGN_ID,
        "phase": "R1",
        "payload_version_id": EVIDENCE.INITIAL_RELEASE_EXEMPTION_CAMPAIGN_ID,
        "payload_sha256": "0" * 64,
        "signature_version_id": EVIDENCE.INITIAL_RELEASE_EXEMPTION_CAMPAIGN_ID,
        "kms_key_arn": APPROVAL_KEY_ARN,
        "expires_at_utc": EVIDENCE.INITIAL_RELEASE_EXEMPTION_CAMPAIGN_EXPIRES_AT_UTC,
    }
    campaign.update(overrides)
    return {
        "gate_version": 1,
        "state": "PROVISIONAL_INITIAL_RELEASE",
        "provisional_campaign": campaign,
    }


def _exemption_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    operation: str,
    gate: Mapping[str, Any],
    now: dt.datetime,
    approved_at: str = _EXEMPT_APPROVED_AT,
    observed_at: str = _EXEMPT_OBSERVED_AT,
    expires_at: str = _EXEMPT_EXPIRES_AT,
    retention_until: str = _EXEMPT_RETENTION_UNTIL,
) -> Any:
    runtime_contract, contract, inner_sha256, outer_sha256 = _approval_contract_paths(tmp_path)
    payload = _external_approval_payload(
        commit=COMMIT,
        tree_oid="2" * 40,
        inner_sha256=inner_sha256,
        outer_sha256=outer_sha256,
        approved_at=approved_at,
        observed_at=observed_at,
        expires_at=expires_at,
        forced_gate=gate,
    )
    locators, _ = _install_approval_aws_fake(
        monkeypatch,
        payload=payload,
        expected_locator_commit=COMMIT,
        retention_until=retention_until,
    )
    return EVIDENCE.assert_approved_release(
        operation=operation,
        approval_locators=locators,
        approval_signing_key_arn=APPROVAL_KEY_ARN,
        approval_encryption_key_arn=APPROVAL_ENCRYPTION_KEY_ARN,
        expected_commit=COMMIT,
        expected_tree_oid="2" * 40,
        expected_inner_sha256=inner_sha256,
        expected_outer_sha256=outer_sha256,
        expected_pipeline="mcp",
        expected_environment="dev",
        runtime_contract_path=runtime_contract,
        contract_path=contract,
        now=now,
    )


@pytest.mark.parametrize("operation", ["build", "authorize"])
def test_exemption_accepts_pinned_provisional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    evidence = _exemption_call(
        tmp_path,
        monkeypatch,
        operation=operation,
        gate=_pinned_exemption_gate(),
        now=_EXEMPT_WINDOW_NOW,
    )
    assert evidence is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"campaign_id": "some-other-campaign"},
        {"phase": "R2"},
        {"payload_sha256": "1" * 64},
        {"payload_version_id": "another-version"},
        {"signature_version_id": "another-version"},
        {"kms_key_arn": KEY_ARN},
        {"expires_at_utc": "2026-09-22T00:00:01Z"},
    ],
)
def test_exemption_rejects_any_unpinned_gate_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: Mapping[str, Any],
) -> None:
    with pytest.raises(EVIDENCE.EvidenceError):
        _exemption_call(
            tmp_path,
            monkeypatch,
            operation="build",
            gate=_pinned_exemption_gate(**overrides),
            now=_EXEMPT_WINDOW_NOW,
        )


@pytest.mark.parametrize("operation", ["terraform-plan", "terraform-apply"])
def test_exemption_is_not_granted_to_other_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    with pytest.raises(EVIDENCE.EvidenceError, match="forced rollback state mismatch"):
        _exemption_call(
            tmp_path,
            monkeypatch,
            operation=operation,
            gate=_pinned_exemption_gate(),
            now=_EXEMPT_WINDOW_NOW,
        )


def test_exemption_closes_at_the_code_sunset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Wall-clock literals on purpose: sliding the sunset constant must turn this
    # red.  The approval itself is dated just before the sunset so that it is
    # still live at that instant -- otherwise "approval is expired" would fire
    # first and the test would pass for the wrong reason.
    with pytest.raises(EVIDENCE.EvidenceError, match="forced rollback state mismatch"):
        _exemption_call(
            tmp_path,
            monkeypatch,
            operation="build",
            gate=_pinned_exemption_gate(),
            now=dt.datetime(2026, 9, 15, 0, 0, tzinfo=dt.UTC),
            approved_at="2026-09-14T23:30:00Z",
            observed_at="2026-09-14T23:00:00Z",
            expires_at="2026-09-15T00:30:00Z",
            retention_until="2036-12-01T00:00:00+00:00",
        )


def test_exemption_sunset_precedes_the_campaign_expiry(tmp_path: Path) -> None:
    # If these were equal, the campaign expiry would fire first and deleting the
    # sunset guard would not turn anything red.
    assert (
        EVIDENCE.INITIAL_RELEASE_EXEMPTION_SUNSET_UTC
        < EVIDENCE.INITIAL_RELEASE_EXEMPTION_CAMPAIGN_EXPIRES_AT_UTC
    )


def test_exemption_does_not_widen_drill_or_passed_paths() -> None:
    assert set(EVIDENCE.INITIAL_RELEASE_EXEMPT_STATES) == {"build", "authorize"}
    assert EVIDENCE.APPROVAL_OPERATION_STATES["build"] == "PASSED"
    assert EVIDENCE.APPROVAL_OPERATION_STATES["authorize"] == "PASSED"
    assert EVIDENCE.APPROVAL_OPERATION_STATES["drill"] == "PROVISIONAL_INITIAL_RELEASE"


def test_passed_path_still_works_inside_the_exemption_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_contract, contract, inner_sha256, outer_sha256 = _approval_contract_paths(tmp_path)
    payload = _external_approval_payload(
        commit=COMMIT,
        tree_oid="2" * 40,
        inner_sha256=inner_sha256,
        outer_sha256=outer_sha256,
        approved_at=_EXEMPT_APPROVED_AT,
        observed_at=_EXEMPT_OBSERVED_AT,
        expires_at=_EXEMPT_EXPIRES_AT,
    )
    locators, _ = _install_approval_aws_fake(
        monkeypatch,
        payload=payload,
        expected_locator_commit=COMMIT,
        retention_until=_EXEMPT_RETENTION_UNTIL,
    )
    evidence = EVIDENCE.assert_approved_release(
        operation="build",
        approval_locators=locators,
        approval_signing_key_arn=APPROVAL_KEY_ARN,
        approval_encryption_key_arn=APPROVAL_ENCRYPTION_KEY_ARN,
        expected_commit=COMMIT,
        expected_tree_oid="2" * 40,
        expected_inner_sha256=inner_sha256,
        expected_outer_sha256=outer_sha256,
        expected_pipeline="mcp",
        expected_environment="dev",
        runtime_contract_path=runtime_contract,
        contract_path=contract,
        now=_EXEMPT_WINDOW_NOW,
    )
    assert evidence is not None
