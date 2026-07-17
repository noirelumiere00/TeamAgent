from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "codebuild" / "release_evidence.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("release_evidence_under_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EVIDENCE = _load_module()
COMMIT = "1" * 40
DIGEST = "sha256:" + "2" * 64
CONFIG_DIGEST = "sha256:" + "3" * 64
SBOM_DIGEST = "sha256:" + "4" * 64
PROVENANCE_DIGEST = "sha256:" + "5" * 64
CONTRACT_SHA256 = "6" * 64
KEY_ARN = (
    "arn:aws:kms:ap-northeast-1:718959508629:"
    "key/12345678-1234-1234-1234-123456789abc"
)
NOW = dt.datetime(2026, 7, 17, 6, 0, tzinfo=dt.UTC)
APP_HTML_VERSION_ID = "FTXbcN70D0DCN90TI_hRK1IdQK_HhLee"
APP_HTML_SHA256 = "03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c"
VAULT_MANIFEST_SHA256 = "aa451e744d26e9dc13c170b019307b0eb10d3645267960fbff41c4038e9b909e"
BUILD_INPUTS_SHA256 = "6697acf311f0c9a96b41426e81ae05ad221482a6e6f69799281ad3532c2e78bf"


def _signature(subject_digest: str, marker: str) -> dict[str, Any]:
    return {
        "verified": True,
        "key_arn": KEY_ARN,
        "subject_digest": subject_digest,
        "referrer_digest": "sha256:" + marker * 64,
        "bundle_sha256": marker * 64,
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
                "arn:aws:codebuild:ap-northeast-1:718959508629:"
                "project/teamagent-dev-image-builder"
            ),
            "build_id": "teamagent-dev-image-builder:12345678-1234-1234-1234-123456789abc",
            "source_commit": COMMIT,
        },
        "contract": {
            "path": "infra/codebuild/teamagent_runtime_contract.json",
            "sha256": CONTRACT_SHA256,
            "release_ready": True,
        },
        "source_evidence": {
            "bucket": "teamagent-dev-image-release-evidence",
            "key": f"source-declarations/mcp/{COMMIT}/{'7' * 64}.json",
            "version_id": "source-version-1",
            "sha256": "7" * 64,
            "signature_key": (
                f"source-declarations/mcp/{COMMIT}/{'7' * 64}.json.sig"
            ),
            "signature_version_id": "source-signature-version-1",
        },
        "subjects": [
            {
                "name": "mcp",
                "quarantine_repository": "teamagent-mcp-quarantine",
                "candidate_repository": "teamagent-mcp-verified-candidates",
                "release_repository": "teamagent-mcp",
                "candidate_tag": f"candidate-{COMMIT}",
                "release_tag": f"{'verified' if channel == 'verified-candidate' else channel}-{COMMIT}",
                "digest": DIGEST,
                "media_type": "application/vnd.oci.image.manifest.v1+json",
                "config_digest": CONFIG_DIGEST,
                "platform": {"os": "linux", "architecture": "arm64"},
                "labels": {
                    "org.opencontainers.image.revision": COMMIT,
                    "io.teamagent.build.runtime-contract-sha256": CONTRACT_SHA256,
                },
                "binaries": [
                    {"path": "/usr/bin/python3.14", "sha256": "8" * 64},
                ],
                "scan": {
                    "scanner": "trivy",
                    "actual_image": (
                        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
                        f"teamagent-mcp-quarantine@{DIGEST}"
                    ),
                    "critical": 0,
                    "high": 0,
                    "secrets": 0,
                    "report_sha256": "9" * 64,
                },
                "sbom": {
                    "digest": SBOM_DIGEST,
                    "artifact_type": "application/spdx+json",
                    "payload_sha256": "a" * 64,
                    "signature": _signature(SBOM_DIGEST, "b"),
                },
                "provenance": {
                    "digest": PROVENANCE_DIGEST,
                    "artifact_type": "application/vnd.in-toto+json",
                    "payload_sha256": "c" * 64,
                    "signature": _signature(PROVENANCE_DIGEST, "d"),
                },
                "image_signature": _signature(DIGEST, "e"),
            }
        ],
    }


def test_source_declaration_binds_independent_project_source_version_commit_and_app() -> None:
    declaration = EVIDENCE.source_declaration(
        project_arn=(
            "arn:aws:codebuild:ap-northeast-1:718959508629:"
            "project/teamagent-dev-mcp-source-publisher"
        ),
        build_id="teamagent-dev-mcp-source-publisher:1234",
        commit=COMMIT,
        source_version="source-version-1",
        source_sha256="2" * 64,
        manifest_sha256="3" * 64,
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
    )

    for path, replacement, message in (
        (("publisher", "commit"), "f" * 40, "source commit mismatch"),
        (("source", "version_id"), "other-version", "source archive VersionId mismatch"),
        (
            ("app_html", "sha256"),
            "f" * 64,
            "app HTML is not the production canonical object",
        ),
        (
            ("application_provenance", "vault_manifest_sha256"),
            "f" * 64,
            "Vault manifest SHA-256 is not production canonical",
        ),
        (
            ("application_provenance", "build_inputs_sha256"),
            "f" * 64,
            "build_inputs SHA-256 is not production canonical",
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
            )


def test_source_declaration_uses_current_production_application_allowlist() -> None:
    assert EVIDENCE.APP_HTML_VERSION_ID == APP_HTML_VERSION_ID
    assert EVIDENCE.APP_HTML_SHA256 == APP_HTML_SHA256
    assert EVIDENCE.VAULT_MANIFEST_SHA256 == VAULT_MANIFEST_SHA256
    assert EVIDENCE.BUILD_INPUTS_SHA256 == BUILD_INPUTS_SHA256


def test_verified_receipt_requires_exact_actual_image_evidence() -> None:
    EVIDENCE.validate_release_receipt(
        _receipt(),
        expected_pipeline="mcp",
        expected_commit=COMMIT,
        expected_contract_sha256=CONTRACT_SHA256,
        allowed_channels={"verified-candidate"},
        now=NOW,
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
            lambda value: value["subjects"][0]["sbom"]["signature"].update(
                subject_digest=DIGEST
            ),
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


def test_candidate_locator_has_bounded_window_and_cannot_be_replayed_after_expiry() -> None:
    candidate = _receipt()
    candidate["expires_at"] = "2026-08-16T05:55:00Z"
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
            issued_at="2026-08-16T05:55:00Z",
            expires_at="2026-08-16T06:25:00Z",
        )

    too_long = copy.deepcopy(candidate)
    too_long["expires_at"] = "2026-08-16T05:55:01Z"
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
    assert active["subjects"][0]["release_tag"] == f"active-{COMMIT}"
    candidate_subject = copy.deepcopy(candidate["subjects"][0])
    active_subject = copy.deepcopy(active["subjects"][0])
    candidate_subject.pop("release_tag")
    active_subject.pop("release_tag")
    assert active_subject == candidate_subject
    assert active["source_evidence"] == candidate["source_evidence"]
    assert active["build"] == candidate["build"]
    assert active["contract"] == candidate["contract"]


def test_deploy_accepts_only_signed_active_or_rollback_digest_reference() -> None:
    active = _receipt(channel="active")
    image = (
        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
        f"teamagent-mcp@{DIGEST}"
    )
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
    with pytest.raises(EVIDENCE.EvidenceError, match="active/rollback"):
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
            version = (
                signature_version if object_key.endswith(".sig") else receipt_version
            )
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
            subject = receipt["subjects"][0]
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
            subject = receipt["subjects"][0]
            subject_id = arguments[arguments.index("--subject-id") + 1].removeprefix(
                "imageDigest="
            )
            signature_by_subject = {
                subject["digest"]: subject["image_signature"]["referrer_digest"],
                subject["sbom"]["digest"]: subject["sbom"]["signature"][
                    "referrer_digest"
                ],
                subject["provenance"]["digest"]: subject["provenance"]["signature"][
                    "referrer_digest"
                ],
            }
            signature = {
                "digest": (
                    signature_by_subject[subject_id]
                    if exact_signatures
                    else "sha256:" + "f" * 64
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
                            "io.teamagent.build.payload-sha256": (
                                subject["sbom"]["payload_sha256"]
                            )
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

    monkeypatch.setattr(EVIDENCE, "_aws", fake_aws)
    selected_image = image or (
        "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
        f"teamagent-mcp@{DIGEST}"
    )
    return EVIDENCE._terraform_gate(
        {
            "images_json": json.dumps(
                {"mcp": selected_image, "openclaw": "", "tiktok": ""}
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
            "contract_ready_json": json.dumps(
                {"mcp": True, "openclaw": False, "tiktok": False}
            ),
            "signing_key_arn": KEY_ARN,
            "encryption_key_arn": encryption_key_arn,
        }
    )


def test_terraform_gate_verifies_exact_immutable_signed_active_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt(channel="active")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    receipt["issued_at"] = (now - dt.timedelta(minutes=5)).isoformat().replace(
        "+00:00",
        "Z",
    )
    receipt["expires_at"] = (now + dt.timedelta(minutes=25)).isoformat().replace(
        "+00:00",
        "Z",
    )

    result = _terraform_query(receipt, monkeypatch)

    assert result == {"verified": "true", "verified_pipelines": "mcp"}


def test_terraform_gate_rejects_receipt_before_release_promotion_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt(channel="active")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    receipt["issued_at"] = (now - dt.timedelta(minutes=5)).isoformat().replace(
        "+00:00",
        "Z",
    )
    receipt["expires_at"] = (now + dt.timedelta(minutes=25)).isoformat().replace(
        "+00:00",
        "Z",
    )

    with pytest.raises(EVIDENCE.EvidenceError, match="exact digest is not present"):
        _terraform_query(receipt, monkeypatch, promoted=False)


def test_terraform_gate_rejects_non_receipted_release_signature_referrers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _receipt(channel="active")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    receipt["issued_at"] = (now - dt.timedelta(minutes=5)).isoformat().replace(
        "+00:00",
        "Z",
    )
    receipt["expires_at"] = (now + dt.timedelta(minutes=25)).isoformat().replace(
        "+00:00",
        "Z",
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
            (
                "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com/"
                "teamagent-mcp:latest"
            ),
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
        receipt["issued_at"] = (now - dt.timedelta(hours=2)).isoformat().replace(
            "+00:00",
            "Z",
        )
        receipt["expires_at"] = (now - dt.timedelta(hours=1)).isoformat().replace(
            "+00:00",
            "Z",
        )
    else:
        receipt["issued_at"] = (now - dt.timedelta(minutes=5)).isoformat().replace(
            "+00:00",
            "Z",
        )
        receipt["expires_at"] = (now + dt.timedelta(minutes=25)).isoformat().replace(
            "+00:00",
            "Z",
        )

    with pytest.raises(EVIDENCE.EvidenceError, match=message):
        _terraform_query(
            receipt,
            monkeypatch,
            signature_valid=signature_valid,
            key_commit=key_commit,
            image=image,
        )
