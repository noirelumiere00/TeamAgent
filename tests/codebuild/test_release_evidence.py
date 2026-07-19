from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "codebuild" / "release_evidence.py"
AUTHORIZE_LAUNCHER = ROOT / "infra" / "deploy" / "authorize_image_release.sh"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("release_evidence_under_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EVIDENCE = _load_module()
COMMIT = "1" * 40
CONTRACT_SHA256 = "6" * 64
BUILD_CONTEXT_SHA256 = "7" * 64
KEY_ARN = "arn:aws:kms:ap-northeast-1:718959508629:key/12345678-1234-1234-1234-123456789abc"
NOW = dt.datetime(2026, 7, 17, 6, 0, tzinfo=dt.UTC)
APP_HTML_VERSION_ID = "FTXbcN70D0DCN90TI_hRK1IdQK_HhLee"
APP_HTML_SHA256 = "03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c"
VAULT_MANIFEST_SHA256 = "aa451e744d26e9dc13c170b019307b0eb10d3645267960fbff41c4038e9b909e"
BUILD_INPUTS_SHA256 = "6697acf311f0c9a96b41426e81ae05ad221482a6e6f69799281ad3532c2e78bf"
BAKED_APP_HTML_VERSION_ID = "approved-baked-fallback-version-1"
BAKED_APP_HTML_SHA256 = "716ac25a96516efd6443277c903102d514f3f86729f8706baea41ee48f0ecdeb"
INTENT_ID = "11111111-1111-4111-8111-111111111111"
ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"
EMPTY_SHARED_LEDGER_SHA256 = hashlib.sha256(EVIDENCE.canonical_bytes({})).hexdigest()
EMPTY_TRANSITION_SHA256 = hashlib.sha256(
    EVIDENCE.canonical_bytes({"delete": [], "replace": []})
).hexdigest()


def test_release_authorizer_contract_mapping_matches_the_evidence_pipeline_map() -> None:
    body = AUTHORIZE_LAUNCHER.read_text(encoding="utf-8")
    launcher_mapping = dict(
        re.findall(
            r'^\s*(mcp|tiktok|openclaw)\) CONTRACT="\$CONTROL_ROOT/([^"]+)" ;;$',
            body,
            re.MULTILINE,
        )
    )

    assert launcher_mapping == {
        pipeline: definition["contract_path"] for pipeline, definition in EVIDENCE.PIPELINES.items()
    }


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
        "subjects": [
            _subject("core", channel=channel),
            _subject("media", channel=channel),
        ],
    }


def test_source_declaration_binds_independent_project_source_version_commit_and_app() -> None:
    publisher_build_id = "teamagent-dev-mcp-source-publisher:1234"
    context_key = f"source-contexts/mcp/{COMMIT}/{'4' * 64}/{publisher_build_id}.tar"
    declaration = EVIDENCE.source_declaration(
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
    )

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
        pipeline="mcp",
        image=image,
        contract_sha256=CONTRACT_SHA256,
        now=NOW,
    )
    with pytest.raises(EVIDENCE.EvidenceError, match="channel"):
        EVIDENCE.validate_deploy_reference(
            _receipt(),
            pipeline="mcp",
            image=image,
            contract_sha256=CONTRACT_SHA256,
            now=NOW,
        )
    with pytest.raises(EVIDENCE.EvidenceError, match="does not match"):
        EVIDENCE.validate_deploy_reference(
            active,
            pipeline="mcp",
            image=image.replace(DIGEST, "sha256:" + "f" * 64),
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
                    "ObjectLockMode": "COMPLIANCE",
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
    return EVIDENCE._terraform_gate(
        {
            "images_json": json.dumps({"mcp": selected_image, "openclaw": "", "tiktok": ""}),
            "mcp_media_image": (
                "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
                f"teamagent-media-worker@{MEDIA_DIGEST}"
            ),
            "evidence_json": json.dumps(
                {
                    "mcp": {
                        "bucket": "teamagent-dev-image-release-evidence",
                        "key": key,
                        "version_id": receipt_version,
                        "signature_key": f"{key}.sig",
                        "signature_version_id": signature_version,
                    }
                }
            ),
            "contracts_json": json.dumps(
                {
                    "mcp": CONTRACT_SHA256,
                    "openclaw": "a" * 64,
                    "tiktok": "b" * 64,
                }
            ),
            "contract_ready_json": json.dumps({"mcp": True, "openclaw": False, "tiktok": False}),
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
        (
            None,
            True,
            COMMIT,
            ("718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp:latest"),
            "does not match signed release evidence",
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
    images = {
        "mcp": (f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@{DIGEST}"),
        "openclaw": "",
        "tiktok": "",
    }
    return {
        "images_json": json.dumps(images, sort_keys=True, separators=(",", ":")),
        "evidence_json": "{}",
        "contracts_json": "{}",
        "contract_ready_json": "{}",
        "application_json": json.dumps(
            {"mcp": APPLICATION},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "shared_generation_ledger_json": "{}",
        "mcp_media_image": (
            "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
            f"teamagent-media-worker@{MEDIA_DIGEST}"
        ),
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
RECEIPT_AUTHORIZATION_EXPIRES_AT = str(int((NOW + dt.timedelta(minutes=25)).timestamp()))


def _gate_metadata_fields(
    query: dict[str, str] | None = None,
) -> dict[str, str]:
    selected = query or DEFAULT_GATE_QUERY
    return {
        "gate_query_sha256": hashlib.sha256(EVIDENCE.canonical_bytes(selected)).hexdigest(),
        "gate_query_json": json.dumps(
            selected,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "receipt_authorization_expires_at": (RECEIPT_AUTHORIZATION_EXPIRES_AT),
    }


def _plan_json(
    *,
    intent_id: str = INTENT_ID,
    context_sha256: str = "a" * 64,
    claims_sha256: str = "b" * 64,
    actions: list[str] | None = None,
) -> dict[str, Any]:
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
                                "requested_images": {
                                    "mcp": (
                                        "718959508629.dkr.ecr.ap-northeast-1."
                                        f"amazonaws.com/teamagent-mcp@{DIGEST}"
                                    ),
                                    "openclaw": "",
                                    "tiktok": "",
                                },
                                "requested_media_image": (
                                    "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
                                    f"teamagent-media-worker@{MEDIA_DIGEST}"
                                ),
                                "release_channels": {"mcp": "active"},
                                "application_provenance": {
                                    "mcp": APPLICATION,
                                },
                                "shared_generation_ledger": {},
                                "hmac_release_bindings": {},
                                "deployment_gate_query": _saved_gate_query(intent_id=intent_id),
                                "receipt_authorization_expires_at": (
                                    RECEIPT_AUTHORIZATION_EXPIRES_AT
                                ),
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


def test_saved_plan_binds_generic_media_task_replacement_to_mcp_media_receipt(
    tmp_path: Path,
) -> None:
    plan = tmp_path / "release.tfplan"
    plan.write_bytes(b"opaque saved terraform plan")
    media_rollforward = _plan_json()
    gate_input = media_rollforward["planned_values"]["root_module"]["resources"][0]["values"][
        "input"
    ]
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
                                "image": gate_input["requested_media_image"],
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
    gate_input["requested_images"] = {
        "mcp": "",
        "openclaw": (
            f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-openclaw@{DIGEST}"
        ),
        "tiktok": "",
    }
    gate_input["requested_media_image"] = ""
    gate_input["release_channels"] = {"openclaw": "rollback"}
    image_empty["resource_changes"].append(
        {
            "address": "aws_ecs_service.mcp[0]",
            "mode": "managed",
            "change": {"actions": ["delete"]},
        }
    )
    gate_input["deployment_gate_query"]["images_json"] = json.dumps(
        gate_input["requested_images"],
        sort_keys=True,
        separators=(",", ":"),
    )
    gate_input["deployment_gate_query"]["mcp_media_image"] = ""
    with pytest.raises(EVIDENCE.EvidenceError, match="image-empty"):
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

    images = {"mcp": (f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@{DIGEST}")}
    evidence = {
        "mcp": {
            "bucket": "teamagent-dev-image-release-evidence",
            "key": f"release-receipts/mcp/{COMMIT}/{'a' * 64}.json",
            "version_id": "receipt-version-1",
            "signature_key": (f"release-receipts/mcp/{COMMIT}/{'a' * 64}.json.sig"),
            "signature_version_id": "signature-version-1",
        }
    }
    first, _, _ = EVIDENCE._deployment_binding(
        images=images,
        evidence=evidence,
        contracts={"mcp": CONTRACT_SHA256},
        application={"mcp": APPLICATION},
        shared_generation_ledger=binding,
        mcp_media_image="",
        release_channels={"mcp": "active"},
        intent_id=INTENT_ID,
    )
    second, _, _ = EVIDENCE._deployment_binding(
        images=images,
        evidence=evidence,
        contracts={"mcp": CONTRACT_SHA256},
        application={"mcp": APPLICATION},
        shared_generation_ledger=dict(binding, generation=43),
        mcp_media_image="",
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
    images = {"mcp": (f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@{DIGEST}")}
    receipt_sha256 = "a" * 64
    key = f"release-receipts/mcp/{COMMIT}/{receipt_sha256}.json"
    first_reference = {
        "bucket": EVIDENCE.EVIDENCE_BUCKET,
        "key": key,
        "version_id": "receipt-version-1",
        "signature_key": f"{key}.sig",
        "signature_version_id": "signature-version-1",
    }
    second_reference = {
        **first_reference,
        "version_id": "receipt-version-2",
        "signature_version_id": "signature-version-2",
    }
    binding_arguments = {
        "images": images,
        "contracts": {"mcp": CONTRACT_SHA256},
        "application": {"mcp": APPLICATION},
        "shared_generation_ledger": {},
        "mcp_media_image": "",
        "release_channels": {"mcp": "active"},
        "intent_id": INTENT_ID,
    }

    first_context, first_claims, first_claims_sha256 = EVIDENCE._deployment_binding(
        evidence={"mcp": first_reference},
        **binding_arguments,
    )
    second_context, second_claims, second_claims_sha256 = EVIDENCE._deployment_binding(
        evidence={"mcp": second_reference},
        **binding_arguments,
    )

    assert first_context != second_context
    assert first_claims == second_claims == [receipt_sha256]
    assert first_claims_sha256 == second_claims_sha256


def test_multi_pipeline_claims_are_canonical_from_plan_binding_through_atomic_consume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_by_pipeline = {
        "mcp": "f" * 64,
        "tiktok": "0" * 64,
    }
    canonical_claims = sorted(claim_by_pipeline.values())

    def reference(pipeline: str) -> dict[str, str]:
        key = f"release-receipts/{pipeline}/{COMMIT}/{claim_by_pipeline[pipeline]}.json"
        return {
            "bucket": EVIDENCE.EVIDENCE_BUCKET,
            "key": key,
            "version_id": f"{pipeline}-receipt-version",
            "signature_key": f"{key}.sig",
            "signature_version_id": f"{pipeline}-signature-version",
        }

    images = {
        "mcp": (f"718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/teamagent-mcp@{CORE_DIGEST}"),
        "tiktok": (
            "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
            f"teamagent-dev-tiktok-acquire@{_digest('tiktok-image')}"
        ),
    }
    evidence = {pipeline: reference(pipeline) for pipeline in claim_by_pipeline}
    contracts = {
        "mcp": CONTRACT_SHA256,
        "tiktok": "7" * 64,
    }
    application = {"mcp": APPLICATION}
    context_sha256, _, _ = EVIDENCE._deployment_binding(
        images=images,
        evidence=evidence,
        contracts=contracts,
        application=application,
        shared_generation_ledger={},
        mcp_media_image=(
            "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
            f"teamagent-media-worker@{MEDIA_DIGEST}"
        ),
        release_channels={"mcp": "active", "tiktok": "active"},
        intent_id=INTENT_ID,
    )
    claims_sha256 = hashlib.sha256(EVIDENCE.canonical_bytes(canonical_claims)).hexdigest()
    metadata = {
        "intent_id": INTENT_ID,
        "plan_sha256": "d" * 64,
        "deployment_context_sha256": context_sha256,
        "receipt_claims_sha256": claims_sha256,
        "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        **_gate_metadata_fields(),
    }
    query = {
        "images_json": json.dumps(images),
        "mcp_media_image": (
            "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
            f"teamagent-media-worker@{MEDIA_DIGEST}"
        ),
        "evidence_json": json.dumps(evidence),
        "contracts_json": json.dumps(contracts),
        "contract_ready_json": json.dumps({"mcp": True, "tiktok": True}),
        "application_json": json.dumps(application),
        "shared_generation_ledger_json": json.dumps({}),
        "signing_key_arn": KEY_ARN,
        "encryption_key_arn": KEY_ARN,
        "deployment_intent_id": INTENT_ID,
    }
    metadata.update(_gate_metadata_fields(query))
    store: dict[str, dict[str, str | int]] = {
        f"intent#{INTENT_ID}": _applying_intent(
            intent_id=INTENT_ID,
            plan_sha256=metadata["plan_sha256"],
            context_sha256=context_sha256,
            claims_sha256=claims_sha256,
            apply_attempt_id=ATTEMPT_ID,
            gate_query_sha256=metadata["gate_query_sha256"],
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

    def fake_transact(
        *,
        applying: dict[str, str | int],
        metadata: dict[str, str],
        receipt_claim_ids: list[str],
        apply_attempt_id: str,
        now: dt.datetime,
    ) -> None:
        consumed_claims.extend(receipt_claim_ids)
        intent = store[str(applying["record_id"])]
        consumed_at = now.isoformat().replace("+00:00", "Z")
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
        lambda *args, **kwargs: metadata,
    )
    monkeypatch.setattr(
        EVIDENCE,
        "_terraform_gate",
        lambda gate_query, *, now: {
            "deployment_context_sha256": context_sha256,
            "receipt_claims_sha256": claims_sha256,
            "receipt_authorization_expires_at": (RECEIPT_AUTHORIZATION_EXPIRES_AT),
            "release_channels_json": json.dumps({"mcp": "active", "tiktok": "active"}),
        },
    )
    monkeypatch.setattr(EVIDENCE, "_dynamodb_get", fake_get)
    monkeypatch.setattr(EVIDENCE, "_dynamodb_transact_consume", fake_transact)

    consumed = EVIDENCE.consume_deployment_intent(
        Path("unused.tfplan"),
        query=query,
        apply_attempt_id=ATTEMPT_ID,
        now=NOW,
    )

    assert consumed["state"] == "CONSUMED"
    assert consumed_claims == canonical_claims


def _prepared_intent(
    *,
    intent_id: str,
    plan_sha256: str,
    context_sha256: str,
    claims_sha256: str,
    gate_query_sha256: str = DEFAULT_GATE_QUERY_SHA256,
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
        "shared_ledger_sha256": EMPTY_SHARED_LEDGER_SHA256,
        "gate_query_sha256": gate_query_sha256,
        "terraform_context_sha256": "c" * 64,
        "backend_workspace_sha256": "d" * 64,
        "state_lineage": "11111111-1111-4111-8111-111111111111",
        "state_serial": 1234,
        "state_addresses_sha256": "e" * 64,
        "plan_addresses_sha256": "f" * 64,
        "runtime_images_sha256": "9" * 64,
        "plan_transition_sha256": EMPTY_TRANSITION_SHA256,
        "control_commit": COMMIT,
        "prepared_at": "2026-07-17T06:00:00Z",
        "authorization_expires_at": int(RECEIPT_AUTHORIZATION_EXPIRES_AT),
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
) -> dict[str, str | int]:
    intent = _prepared_intent(
        intent_id=intent_id,
        plan_sha256=plan_sha256,
        context_sha256=context_sha256,
        claims_sha256=claims_sha256,
        gate_query_sha256=gate_query_sha256,
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
    }


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
