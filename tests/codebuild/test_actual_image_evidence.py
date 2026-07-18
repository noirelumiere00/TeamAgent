from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
CODEBUILD = ROOT / "infra" / "codebuild"
MODULE_PATH = CODEBUILD / "actual_image_evidence.py"
VERIFY_SCRIPT = CODEBUILD / "verify_actual_image.sh"

if str(CODEBUILD) not in sys.path:
    sys.path.insert(0, str(CODEBUILD))


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "actual_image_evidence_under_test",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EVIDENCE = _load_module()
COMMIT = "1" * 40
DIGEST = "sha256:" + "2" * 64
SBOM_DIGEST = "sha256:" + "4" * 64
PROVENANCE_DIGEST = "sha256:" + "5" * 64
CONTRACT_SHA256 = "6" * 64
KEY_ARN = "arn:aws:kms:ap-northeast-1:718959508629:key/12345678-1234-1234-1234-123456789abc"
REGISTRY = "718959508629.dkr.ecr.ap-northeast-1.amazonaws.com"
QUARANTINE = "teamagent-dev-tiktok-acquire-quarantine"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _fixture(
    tmp_path: Path,
    *,
    critical: int = 0,
    media_type: str = "application/vnd.oci.image.manifest.v1+json",
    architecture: str = "arm64",
    truncated_referrers: bool = False,
    image_signature_digest: str = DIGEST,
    duplicate_image_signature_referrer: bool = False,
) -> argparse.Namespace:
    contract = tmp_path / "contract.json"
    contract.write_text("{}\n", encoding="utf-8")

    config = tmp_path / "config.json"
    _write_json(
        config,
        {
            "architecture": architecture,
            "os": "linux",
            "config": {
                "Labels": {
                    "org.opencontainers.image.revision": COMMIT,
                    "io.teamagent.build.contract-sha256": CONTRACT_SHA256,
                }
            },
        },
    )
    config_digest = "sha256:" + hashlib.sha256(config.read_bytes()).hexdigest()

    binary_probes = tmp_path / "binary-probes.tsv"
    binary_probes.write_text(f"/usr/bin/tiktok-worker\t{'7' * 64}\n", encoding="utf-8")

    vulnerabilities = [
        {"Severity": "CRITICAL", "VulnerabilityID": f"CVE-{index}"} for index in range(critical)
    ]
    trivy = tmp_path / "trivy.json"
    _write_json(
        trivy,
        {
            "ArtifactName": f"{REGISTRY}/{QUARANTINE}@{DIGEST}",
            "ArtifactType": "container_image",
            "Results": [
                {
                    "Target": "actual-image",
                    "Vulnerabilities": vulnerabilities,
                    "Secrets": [],
                }
            ],
        },
    )

    sbom = tmp_path / "sbom.json"
    _write_json(
        sbom,
        {
            "spdxVersion": "SPDX-2.3",
            "packages": [{"name": "tiktok-worker", "versionInfo": "1"}],
        },
    )
    provenance = tmp_path / "provenance.json"
    _write_json(
        provenance,
        {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": [
                {
                    "name": "tiktok/tiktok",
                    "digest": {"sha256": DIGEST.removeprefix("sha256:")},
                }
            ],
            "predicate": {
                "sourceCommit": COMMIT,
                "contractSha256": CONTRACT_SHA256,
            },
        },
    )
    sbom_sha256 = hashlib.sha256(sbom.read_bytes()).hexdigest()
    provenance_sha256 = hashlib.sha256(provenance.read_bytes()).hexdigest()

    image_signature_referrer = {
        "artifactType": "application/vnd.dev.cosign.simplesigning.v1+json",
        "artifactStatus": "ACTIVE",
        "digest": "sha256:" + "9" * 64,
    }
    sbom_signature_referrer = {
        "artifactType": "application/vnd.dev.cosign.simplesigning.v1+json",
        "artifactStatus": "ACTIVE",
        "digest": "sha256:" + "a" * 64,
    }
    provenance_signature_referrer = {
        "artifactType": "application/vnd.dev.cosign.simplesigning.v1+json",
        "artifactStatus": "ACTIVE",
        "digest": "sha256:" + "b" * 64,
    }
    subject_referrers = tmp_path / "subject-referrers.json"
    subject_response: dict[str, Any] = {
        "referrers": [
            {
                "artifactType": "application/spdx+json",
                "artifactStatus": "ACTIVE",
                "digest": SBOM_DIGEST,
                "annotations": {
                    "io.teamagent.build.payload-sha256": sbom_sha256,
                },
            },
            {
                "artifactType": "application/vnd.in-toto+json",
                "artifactStatus": "ACTIVE",
                "digest": PROVENANCE_DIGEST,
                "annotations": {
                    "io.teamagent.build.payload-sha256": provenance_sha256,
                },
            },
            image_signature_referrer,
        ]
    }
    if duplicate_image_signature_referrer:
        subject_response["referrers"].append(
            {
                **image_signature_referrer,
                "digest": "sha256:" + "c" * 64,
            }
        )
    if truncated_referrers:
        subject_response["nextToken"] = "more-results"
    _write_json(subject_referrers, subject_response)

    sbom_signature_referrers = tmp_path / "sbom-signature-referrers.json"
    provenance_signature_referrers = tmp_path / "provenance-signature-referrers.json"
    _write_json(
        sbom_signature_referrers,
        {"referrers": [sbom_signature_referrer]},
    )
    _write_json(
        provenance_signature_referrers,
        {"referrers": [provenance_signature_referrer]},
    )

    def signature_verification(path: Path, digest: str) -> None:
        _write_json(
            path,
            [
                {
                    "critical": {
                        "image": {"docker-manifest-digest": digest},
                    }
                }
            ],
        )

    image_signature = tmp_path / "image-signature.json"
    sbom_signature = tmp_path / "sbom-signature.json"
    provenance_signature = tmp_path / "provenance-signature.json"
    signature_verification(image_signature, image_signature_digest)
    signature_verification(sbom_signature, SBOM_DIGEST)
    signature_verification(provenance_signature, PROVENANCE_DIGEST)

    return argparse.Namespace(
        pipeline="tiktok",
        channel="verified-candidate",
        name="tiktok",
        quarantine_repository=QUARANTINE,
        candidate_repository="teamagent-dev-tiktok-acquire-verified-candidates",
        release_repository="teamagent-dev-tiktok-acquire",
        commit=COMMIT,
        contract_sha256=CONTRACT_SHA256,
        contract=contract,
        digest=DIGEST,
        media_type=media_type,
        config_digest=config_digest,
        config=config,
        binary_probes=binary_probes,
        trivy_report=trivy,
        sbom=sbom,
        sbom_digest=SBOM_DIGEST,
        provenance=provenance,
        provenance_digest=PROVENANCE_DIGEST,
        subject_referrers=subject_referrers,
        sbom_signature_referrers=sbom_signature_referrers,
        provenance_signature_referrers=provenance_signature_referrers,
        image_signature_referrers=subject_referrers,
        image_signature_verification=image_signature,
        sbom_signature_verification=sbom_signature,
        provenance_signature_verification=provenance_signature,
        signing_key_arn=KEY_ARN,
        output=tmp_path / "subject.json",
    )


def test_actual_image_subject_binds_digest_platform_labels_binaries_and_signed_referrers(
    tmp_path: Path,
) -> None:
    subject = EVIDENCE.create_subject(_fixture(tmp_path))

    assert subject["digest"] == DIGEST
    assert subject["platform"] == {"os": "linux", "architecture": "arm64"}
    assert subject["scan"]["actual_image"] == f"{REGISTRY}/{QUARANTINE}@{DIGEST}"
    assert subject["scan"]["critical"] == 0
    assert subject["scan"]["high"] == 0
    assert subject["scan"]["secrets"] == 0
    assert subject["binaries"] == [{"path": "/usr/bin/tiktok-worker", "sha256": "7" * 64}]
    assert subject["sbom"]["signature"]["subject_digest"] == SBOM_DIGEST
    assert subject["sbom"]["signature"]["referrer_digest"] == "sha256:" + "a" * 64
    assert subject["provenance"]["signature"]["subject_digest"] == PROVENANCE_DIGEST
    assert subject["provenance"]["signature"]["referrer_digest"] == "sha256:" + "b" * 64
    assert subject["image_signature"]["subject_digest"] == DIGEST
    assert subject["image_signature"]["referrer_digest"] == "sha256:" + "9" * 64


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"critical": 1}, "actual-image gate failed"),
        (
            {"media_type": "application/vnd.oci.image.index.v1+json"},
            "index or unsupported",
        ),
        ({"architecture": "amd64"}, "not linux/arm64"),
        ({"truncated_referrers": True}, "truncated"),
        (
            {"image_signature_digest": "sha256:" + "f" * 64},
            "does not bind the subject digest",
        ),
        (
            {"duplicate_image_signature_referrer": True},
            "exactly one unambiguous OCI signature referrer",
        ),
    ],
)
def test_actual_image_subject_rejects_weak_or_ambiguous_evidence(
    tmp_path: Path,
    overrides: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(EVIDENCE.EvidenceError, match=message):
        EVIDENCE.create_subject(_fixture(tmp_path, **overrides))


def test_actual_image_verifier_scans_before_signing_and_never_accepts_indexes() -> None:
    body = VERIFY_SCRIPT.read_text(encoding="utf-8")

    scan = body.index("trivy image")
    scan_gate = body.index("actual-image gate failed")
    guard = body.index("actual-image gates failed before any image or attestation signing")
    sign = body.index("cosign sign --yes")
    assert scan < scan_gate < guard < sign
    assert "--scanners vuln,secret" in body
    assert "--severity" not in body
    assert '"$IMAGE"' in body
    assert "application/vnd.oci.image.index.v1+json" not in body
    assert body.count("aws ecr list-image-referrers") == body.count("--max-results 50")
    assert '"$BUNDLE_PROVENANCE_HELPER" binary-probes' in body
    assert '--subject "$SUBJECT_NAME"' in body
    assert (
        "mcp:core:teamagent-mcp-quarantine:teamagent-mcp-verified-candidates:teamagent-mcp"
    ) in body
    assert (
        "mcp:media:teamagent-media-worker-quarantine:"
        "teamagent-media-worker-verified-candidates:teamagent-media-worker"
    ) in body
    assert 'KMS_URI="awskms:///$SIGNING_KEY_ARN"' in body
    assert body.count("cosign verify --experimental-oci11") == 3
    assert "application/spdx+json" in body
    assert "application/vnd.in-toto+json" in body
    assert "unset TRIVY_DB_REPOSITORY TRIVY_JAVA_DB_REPOSITORY" in body
    assert 'TRIVY_DB_REPOSITORY="public.ecr.aws/aquasecurity/trivy-db:2"' in body
