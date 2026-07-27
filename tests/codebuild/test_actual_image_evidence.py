from __future__ import annotations

import argparse
import base64
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
ATTESTOR_BUILDSPEC = CODEBUILD / "image-attestor-buildspec.yml"
MCP_CONTRACT = CODEBUILD / "teamagent_core_media_release_contract.json"
MCP_RUNTIME_CONTRACT = CODEBUILD / "teamagent_runtime_contract.json"

if str(CODEBUILD) not in sys.path:
    sys.path.insert(0, str(CODEBUILD))

BUNDLE_PROVENANCE = importlib.import_module("teamagent_bundle_provenance")


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
BUILD_CONTEXT_SHA256 = "8" * 64
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
    low: int = 0,
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
    ] + [{"Severity": "LOW", "VulnerabilityID": f"CVE-LOW-{index}"} for index in range(low)]
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
                "buildContextSha256": BUILD_CONTEXT_SHA256,
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


def _mcp_fixture(tmp_path: Path, *, subject_name: str) -> argparse.Namespace:
    args = _fixture(tmp_path)
    (
        args.quarantine_repository,
        args.candidate_repository,
        args.release_repository,
    ) = EVIDENCE.PIPELINES["mcp"]["subjects"][subject_name]
    args.pipeline = "mcp"
    args.name = subject_name
    runtime_contract = json.loads(MCP_RUNTIME_CONTRACT.read_text(encoding="utf-8"))
    runtime_contract["release"] = {"ready": True, "blocked_reason": ""}
    args.runtime_contract = tmp_path / "runtime-contract.json"
    _write_json(args.runtime_contract, runtime_contract)

    contract = json.loads(MCP_CONTRACT.read_text(encoding="utf-8"))
    contract["source_runtime_contract"]["sha256"] = hashlib.sha256(
        args.runtime_contract.read_bytes()
    ).hexdigest()
    contract["release"] = {"ready": True, "blocked_reason": ""}
    args.contract = tmp_path / "contract.json"
    _write_json(args.contract, contract)
    args.contract_sha256 = hashlib.sha256(args.contract.read_bytes()).hexdigest()
    approval_payload_sha256 = "a" * 64
    args.approval_evidence_json = json.dumps(
        {
            "payload": {
                "bucket": "teamagent-dev-image-release-evidence",
                "key": (
                    f"approval-records/mcp/{COMMIT}/"
                    f"{approval_payload_sha256}.json"
                ),
                "version_id": "approval-payload-version",
                "sha256": approval_payload_sha256,
            },
            "signature": {
                "bucket": "teamagent-dev-image-release-evidence",
                "key": (
                    f"approval-records/mcp/{COMMIT}/"
                    f"{approval_payload_sha256}.json.sig"
                ),
                "version_id": "approval-signature-version",
                "sha256": "b" * 64,
            },
            "approval_payload_sha256": approval_payload_sha256,
            "forced_gate_sha256": "c" * 64,
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    contract = BUNDLE_PROVENANCE.load_contract(args.contract)
    runtime_contract = BUNDLE_PROVENANCE._load_runtime_contract(args.runtime_contract)
    subject = next(
        value for value in contract["subjects"] if value["name"] == subject_name
    )
    production = contract["app_html"]["production"]
    record = {"schema_version": 1, **production}
    fallback = contract["app_html"]["baked_fallback"]
    build_arguments = {
        "GIT_COMMIT": COMMIT,
        "GIT_BRANCH": "dev",
        "BUILD_CONTEXT_SHA256": BUILD_CONTEXT_SHA256,
        "RELEASE_CONTRACT_SHA256": args.contract_sha256,
        "APP_PROVENANCE_SHA256": BUNDLE_PROVENANCE.application_provenance_sha256(
            contract,
            record,
        ),
        "APP_HTML_SOURCE": "s3",
        "APP_HTML_SHA256": record["app_html_sha256"],
        "APP_HTML_VERSION_ID": record["app_html_s3_version_id"],
        "APP_HTML_MANIFEST_SHA256": record["vault_manifest_sha256"],
        "APP_HTML_BUILD_INPUTS_SHA256": record["build_inputs_sha256"],
        "BAKED_APP_HTML_SHA256": fallback["sha256"],
        "BAKED_APP_HTML_VERSION_ID": fallback["s3_version_id"],
    }
    labels = dict(BUNDLE_PROVENANCE.COMMON_STATIC_TEAMAGENT_LABELS)
    for label_name, binding in subject["required_label_bindings"].items():
        labels[label_name] = (
            subject["runtime_kind"]
            if binding == subject["runtime_kind"]
            else build_arguments[binding]
        )
    labels.update(
        {
            assertion["oci_label"]: assertion["value"]
            for assertion in subject["source_assertions"]
        }
    )
    labels["io.teamagent.build.release-approval-sha256"] = approval_payload_sha256
    if subject_name == "core":
        labels.update(
            BUNDLE_PROVENANCE._runtime_expected_labels(
                runtime_contract,
                contract["source_runtime_contract"]["sha256"],
            )
        )
    _write_json(
        args.config,
        {
            "architecture": "arm64",
            "os": "linux",
            "config": {"Labels": labels},
        },
    )
    args.config_digest = "sha256:" + hashlib.sha256(args.config.read_bytes()).hexdigest()

    args.binary_probes.write_text(
        "".join(
            f"{probe['path']}\t{probe['sha256']}\n"
            for probe in BUNDLE_PROVENANCE.binary_probes(contract, subject_name)
        ),
        encoding="utf-8",
    )
    trivy = json.loads(args.trivy_report.read_text(encoding="utf-8"))
    trivy["ArtifactName"] = f"{REGISTRY}/{args.quarantine_repository}@{args.digest}"
    _write_json(args.trivy_report, trivy)
    provenance = json.loads(args.provenance.read_text(encoding="utf-8"))
    provenance["subject"][0]["name"] = f"mcp/{subject_name}"
    provenance["predicate"]["contractSha256"] = args.contract_sha256
    provenance["predicate"]["runtimeContractSha256"] = hashlib.sha256(
        args.runtime_contract.read_bytes()
    ).hexdigest()
    provenance["predicate"]["releaseApprovalSha256"] = approval_payload_sha256
    _write_json(args.provenance, provenance)
    provenance_sha256 = hashlib.sha256(args.provenance.read_bytes()).hexdigest()
    subject_referrers = json.loads(args.subject_referrers.read_text(encoding="utf-8"))
    provenance_referrer = next(
        referrer
        for referrer in subject_referrers["referrers"]
        if referrer["artifactType"] == "application/vnd.in-toto+json"
    )
    provenance_referrer["annotations"][
        "io.teamagent.build.payload-sha256"
    ] = provenance_sha256
    _write_json(args.subject_referrers, subject_referrers)
    args.build_context_sha256 = BUILD_CONTEXT_SHA256
    return args


def test_actual_image_subject_binds_digest_platform_labels_binaries_and_signed_referrers(
    tmp_path: Path,
) -> None:
    subject = EVIDENCE.create_subject(_fixture(tmp_path))

    assert subject["digest"] == DIGEST
    assert subject["platform"] == {"os": "linux", "architecture": "arm64"}
    assert subject["scan"]["actual_image"] == f"{REGISTRY}/{QUARANTINE}@{DIGEST}"
    assert subject["scan"]["unknown"] == 0
    assert subject["scan"]["low"] == 0
    assert subject["scan"]["medium"] == 0
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


@pytest.mark.parametrize("subject_name", ["core", "media"])
def test_mcp_subject_verification_binds_exact_manifest_config_digest(
    tmp_path: Path,
    subject_name: str,
) -> None:
    args = _mcp_fixture(tmp_path, subject_name=subject_name)

    subject = EVIDENCE.create_subject(args)

    assert subject["name"] == subject_name
    assert subject["labels"]["io.teamagent.build.context-sha256"] == BUILD_CONTEXT_SHA256
    assert (
        subject["labels"]["io.teamagent.build.release-contract-sha256"]
        == args.contract_sha256
    )
    assert (
        subject["labels"]["io.teamagent.build.release-approval-sha256"]
        == "a" * 64
    )


@pytest.mark.parametrize(
    "mutation",
    ["label", "provenance"],
)
def test_mcp_subject_rejects_external_approval_binding_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    args = _mcp_fixture(tmp_path, subject_name="media")
    if mutation == "label":
        config = json.loads(args.config.read_text(encoding="utf-8"))
        config["config"]["Labels"][
            "io.teamagent.build.release-approval-sha256"
        ] = "0" * 64
        _write_json(args.config, config)
        args.config_digest = (
            "sha256:" + hashlib.sha256(args.config.read_bytes()).hexdigest()
        )
        expected = "OCI approval label"
    else:
        provenance = json.loads(args.provenance.read_text(encoding="utf-8"))
        provenance["predicate"]["releaseApprovalSha256"] = "0" * 64
        _write_json(args.provenance, provenance)
        provenance_sha256 = hashlib.sha256(args.provenance.read_bytes()).hexdigest()
        referrers = json.loads(
            args.subject_referrers.read_text(encoding="utf-8")
        )
        referrer = next(
            item
            for item in referrers["referrers"]
            if item["artifactType"] == "application/vnd.in-toto+json"
        )
        referrer["annotations"][
            "io.teamagent.build.payload-sha256"
        ] = provenance_sha256
        _write_json(args.subject_referrers, referrers)
        expected = "provenance does not bind the external approval"

    with pytest.raises(EVIDENCE.EvidenceError, match=expected):
        EVIDENCE.create_subject(args)


def test_mcp_subject_rejects_missing_inner_runtime_contract(tmp_path: Path) -> None:
    args = _mcp_fixture(tmp_path, subject_name="core")
    args.runtime_contract = None

    with pytest.raises(
        EVIDENCE.EvidenceError,
        match="runtime contract is required for the MCP pipeline",
    ):
        EVIDENCE.create_subject(args)


def test_mcp_subject_rejects_blocked_outer_contract(tmp_path: Path) -> None:
    args = _mcp_fixture(tmp_path, subject_name="core")
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    contract["release"] = {
        "ready": False,
        "blocked_reason": "test keeps the outer contract statically blocked",
    }
    _write_json(args.contract, contract)
    args.contract_sha256 = hashlib.sha256(args.contract.read_bytes()).hexdigest()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    config["config"]["Labels"][
        "io.teamagent.build.release-contract-sha256"
    ] = args.contract_sha256
    _write_json(args.config, config)
    args.config_digest = "sha256:" + hashlib.sha256(args.config.read_bytes()).hexdigest()

    with pytest.raises(EVIDENCE.EvidenceError, match="MCP outer contract mismatch"):
        EVIDENCE.create_subject(args)


@pytest.mark.parametrize(
    ("subject_name", "label_name"),
    [
        ("core", "io.teamagent.contract.python-binary-sha256"),
        ("media", "io.teamagent.contract.chromium-binary-sha256"),
    ],
)
def test_mcp_subject_rejects_builder_attestor_full_label_fixture_drift(
    tmp_path: Path,
    subject_name: str,
    label_name: str,
) -> None:
    args = _mcp_fixture(tmp_path, subject_name=subject_name)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    config["config"]["Labels"][label_name] = "0" * 64
    _write_json(args.config, config)
    args.config_digest = "sha256:" + hashlib.sha256(args.config.read_bytes()).hexdigest()

    with pytest.raises(
        EVIDENCE.EvidenceError,
        match=r"MCP (?:core runtime receipt|OCI core/media interface) mismatch",
    ):
        EVIDENCE.create_subject(args)


@pytest.mark.parametrize(
    "mutation",
    ["receipt-bytes", "receipt-sha256", "runtime-contract-sha256"],
)
def test_mcp_core_subject_rejects_aggregate_runtime_receipt_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    args = _mcp_fixture(tmp_path, subject_name="core")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    labels = config["config"]["Labels"]
    if mutation == "receipt-bytes":
        receipt = json.loads(
            base64.b64decode(
                labels["io.teamagent.build.runtime-receipt"],
                validate=True,
            )
        )
        receipt["values"]["binary.python.sha256"] = "0" * 64
        receipt_bytes = json.dumps(
            receipt,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        labels["io.teamagent.build.runtime-receipt"] = base64.b64encode(
            receipt_bytes
        ).decode()
        labels["io.teamagent.build.runtime-receipt-sha256"] = hashlib.sha256(
            receipt_bytes
        ).hexdigest()
    elif mutation == "receipt-sha256":
        labels["io.teamagent.build.runtime-receipt-sha256"] = "0" * 64
    else:
        labels["io.teamagent.build.runtime-contract-sha256"] = "0" * 64
    _write_json(args.config, config)
    args.config_digest = "sha256:" + hashlib.sha256(args.config.read_bytes()).hexdigest()

    with pytest.raises(
        EVIDENCE.EvidenceError,
        match="MCP core runtime receipt mismatch",
    ):
        EVIDENCE.create_subject(args)


def test_actual_image_subject_rejects_semantically_equal_config_with_different_bytes(
    tmp_path: Path,
) -> None:
    args = _fixture(tmp_path)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    args.config.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(
        EVIDENCE.EvidenceError,
        match="OCI config bytes do not match the manifest config digest",
    ):
        EVIDENCE.create_subject(args)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"critical": 1}, "actual-image gate failed"),
        ({"low": 1}, "actual-image gate failed"),
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
    attestor = ATTESTOR_BUILDSPEC.read_text(encoding="utf-8")

    scan = body.index("trivy image")
    scan_gate = body.index("actual-image gate failed")
    guard = body.index("actual-image gates failed before any image or attestation signing")
    sign = body.index("cosign sign --yes")
    assert scan < scan_gate < guard < sign
    assert "--scanners vuln,secret" in body
    assert "--severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL" in body
    assert '"$IMAGE"' in body
    assert "application/vnd.oci.image.index.v1+json" not in body
    assert body.count("aws ecr list-image-referrers") == body.count("--max-results 50")
    assert '"$BUNDLE_PROVENANCE_HELPER" binary-probes' in body
    assert '--subject "$SUBJECT_NAME"' in body
    assert '--runtime-contract "$RUNTIME_CONTRACT"' in body
    assert "inner runtime contract bytes do not match the outer contract pin" in body
    assert '"buildContextSha256"' in body
    assert '"runtimeContractSha256"' in body
    assert "__MCP_RUNTIME_CONTRACT_BASE64__" in attestor
    assert '--runtime-contract "$MCP_RUNTIME_CONTRACT"' in attestor
    assert "embedded inner runtime contract does not match the outer pin" in attestor
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
