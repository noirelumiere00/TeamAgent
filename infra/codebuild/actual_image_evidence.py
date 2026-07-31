#!/usr/bin/env python3
"""Create a strict release-subject record from actual-image verification reports."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from release_evidence import (
    ACCOUNT_ID,
    PIPELINES,
    REFERRER_ARTIFACT_TYPES,
    REGION,
    REGISTRY,
    EvidenceError,
    canonical_bytes,
    release_receipt_schema_for_pipeline,
    subject_tag_suffix,
    validate_approval_evidence,
    validate_release_receipt,
)
from source_provenance import (
    ProvenanceError as RuntimeProvenanceError,
)
from source_provenance import load_runtime_contract
from source_provenance import require_release_ready as require_runtime_release_ready
from source_provenance import verify_oci_revision as verify_core_runtime_oci_revision
from teamagent_bundle_provenance import (
    ProvenanceError as BundleProvenanceError,
)
from teamagent_bundle_provenance import load_contract as load_bundle_contract
from teamagent_bundle_provenance import require_release_ready as require_bundle_release_ready
from teamagent_bundle_provenance import (
    verify_oci_config as verify_teamagent_oci_config,
)

_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_PATH_RE = re.compile(r"/[A-Za-z0-9][A-Za-z0-9_./+-]{0,511}")
_KEY_ARN_RE = re.compile(rf"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/[0-9a-f-]{{36}}")
_COSIGN_SIGNATURE_ARTIFACT_TYPES = {
    "application/vnd.dev.cosign.simplesigning.v1+json",
    "application/vnd.dsse.envelope.v1+json",
}


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load(path: Path, *, label: str) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid {label}: {exc}") from exc


def _load_digest_bound_json(
    path: Path,
    *,
    expected_digest: str,
    label: str,
) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EvidenceError(f"cannot read {label}") from exc
    actual_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual_digest != expected_digest:
        raise EvidenceError(f"{label} bytes do not match the manifest config digest")
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid {label}: {exc}") from exc


def _sha256_file(path: Path, *, label: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise EvidenceError(f"cannot hash {label}") from exc


def _digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise EvidenceError(f"{label} must be a sha256 digest")
    return value


def _sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise EvidenceError(f"{label} must be a lowercase SHA-256")
    return value


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, label: str) -> None:
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing or unknown:
        raise EvidenceError(f"{label} schema mismatch: missing={missing}, unknown={unknown}")


def _trivy_counts(report: Any, *, expected_image: str) -> dict[str, int]:
    value = _mapping(report, label="Trivy report")
    if value.get("ArtifactName") != expected_image:
        raise EvidenceError("Trivy report does not bind the exact quarantine digest")
    if value.get("ArtifactType") not in {"container_image", "image"}:
        raise EvidenceError("Trivy report is not an actual container image scan")
    results = value.get("Results")
    if not isinstance(results, list) or not results:
        raise EvidenceError("Trivy report has no scan results")
    counts = {
        "unknown": 0,
        "low": 0,
        "medium": 0,
        "high": 0,
        "critical": 0,
        "secrets": 0,
    }
    for index, raw_result in enumerate(results):
        result = _mapping(raw_result, label=f"Trivy result[{index}]")
        vulnerabilities = result.get("Vulnerabilities") or []
        if not isinstance(vulnerabilities, list):
            raise EvidenceError("Trivy vulnerabilities must be an array")
        for vulnerability in vulnerabilities:
            item = _mapping(vulnerability, label="Trivy vulnerability")
            severity = str(item.get("Severity") or "").lower()
            if severity not in {"unknown", "low", "medium", "high", "critical"}:
                raise EvidenceError("Trivy vulnerability severity is unsupported")
            counts[severity] += 1
        discovered_secrets = result.get("Secrets") or []
        if not isinstance(discovered_secrets, list):
            raise EvidenceError("Trivy secrets must be an array")
        counts["secrets"] += len(discovered_secrets)
    return counts


def _binary_probes(path: Path) -> list[dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceError("cannot read actual binary probes") from exc
    if not lines:
        raise EvidenceError("actual image must include at least one binary hash probe")
    probes: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 2:
            raise EvidenceError("binary probe must be PATH<TAB>SHA256")
        binary_path, digest = fields
        if not _PATH_RE.fullmatch(binary_path) or ".." in binary_path or binary_path in seen:
            raise EvidenceError("binary probe path is unsafe or duplicate")
        seen.add(binary_path)
        probes.append({"path": binary_path, "sha256": _sha256(digest, label="binary hash")})
    if [probe["path"] for probe in probes] != sorted(seen):
        raise EvidenceError("binary probes must be sorted by path")
    return probes


def _referrer(
    response: Any,
    *,
    artifact_type: str,
    expected_digest: str,
    expected_payload_sha256: str,
    label: str,
) -> str:
    value = _mapping(response, label=f"{label} referrer response")
    if value.get("nextToken") not in {None, ""}:
        raise EvidenceError(f"{label} referrer response was truncated at max-results 50")
    referrers = value.get("referrers")
    if not isinstance(referrers, list):
        raise EvidenceError(f"{label} referrer response is missing referrers")
    expected_digest = _digest(expected_digest, label=f"expected {label} digest")
    expected_payload_sha256 = _sha256(
        expected_payload_sha256,
        label=f"expected {label} payload SHA-256",
    )
    matches: list[str] = []
    for index, raw_referrer in enumerate(referrers):
        referrer = _mapping(raw_referrer, label=f"{label} referrer[{index}]")
        if (
            referrer.get("artifactType") == artifact_type
            and referrer.get("digest") == expected_digest
        ):
            if referrer.get("artifactStatus") != "ACTIVE":
                raise EvidenceError(f"{label} referrer is not ACTIVE")
            annotations = _mapping(
                referrer.get("annotations"),
                label=f"{label} annotations",
            )
            if annotations.get("io.teamagent.build.payload-sha256") != expected_payload_sha256:
                raise EvidenceError(f"{label} referrer does not bind the payload hash")
            matches.append(_digest(referrer.get("digest"), label=f"{label} digest"))
    if len(matches) != 1:
        raise EvidenceError(f"{label} must have exactly one unambiguous referrer")
    return matches[0]


def _signature_referrer(response: Any, *, label: str) -> str:
    value = _mapping(response, label=f"{label} signature referrers")
    if value.get("nextToken") not in {None, ""}:
        raise EvidenceError(f"{label} signature response was truncated")
    referrers = value.get("referrers")
    if not isinstance(referrers, list):
        raise EvidenceError(f"{label} signature response is malformed")
    signatures = [
        item
        for item in referrers
        if isinstance(item, dict)
        and item.get("artifactType") in _COSIGN_SIGNATURE_ARTIFACT_TYPES
        and item.get("artifactStatus") == "ACTIVE"
        and _DIGEST_RE.fullmatch(str(item.get("digest", "")))
    ]
    if len(signatures) != 1:
        raise EvidenceError(f"{label} must have exactly one unambiguous OCI signature referrer")
    return _digest(signatures[0]["digest"], label=f"{label} signature referrer digest")


def _cosign_items(path: Path, *, label: str) -> list[Mapping[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceError(f"cannot read {label} verification output") from exc
    if not raw:
        raise EvidenceError(f"{label} cryptographic verification output is empty")
    try:
        parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        values = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        try:
            values = [
                json.loads(line, object_pairs_hook=_reject_duplicate_keys)
                for line in raw.splitlines()
                if line.strip()
            ]
        except json.JSONDecodeError as exc:
            raise EvidenceError(f"{label} verification output is not JSON") from exc
    if not values or not all(isinstance(item, dict) for item in values):
        raise EvidenceError(f"{label} verification output has no signed claims")
    return values


def _verify_image_signature(path: Path, *, digest: str) -> str:
    values = _cosign_items(path, label="image signature")
    matching = False
    for value in values:
        critical = value.get("critical") or value.get("Critical")
        if not isinstance(critical, dict):
            continue
        image = critical.get("image") or critical.get("Image")
        if not isinstance(image, dict):
            continue
        claim = image.get("docker-manifest-digest") or image.get("Docker-manifest-digest")
        matching = matching or claim == digest
    if not matching:
        raise EvidenceError("cosign image signature does not bind the subject digest")
    return _sha256_file(path, label="image signature verification")


def _contains_exact(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return value == expected
    if isinstance(value, dict):
        return any(_contains_exact(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact(item, expected) for item in value)
    return False


def _verify_artifact_signature(
    path: Path,
    *,
    label: str,
    digest: str,
) -> str:
    return _verify_image_signature(path, digest=digest)


def create_subject(args: argparse.Namespace) -> dict[str, Any]:
    if args.pipeline not in PIPELINES:
        raise EvidenceError("pipeline is not allowlisted")
    expected_subjects: Mapping[str, tuple[str, str, str]] = PIPELINES[args.pipeline]["subjects"]
    if args.name not in expected_subjects:
        raise EvidenceError("subject is not allowlisted for the pipeline")
    (
        quarantine_repository,
        candidate_repository,
        release_repository,
    ) = expected_subjects[args.name]
    if (
        args.quarantine_repository != quarantine_repository
        or args.candidate_repository != candidate_repository
        or args.release_repository != release_repository
    ):
        raise EvidenceError("subject repositories do not match the allowlist")
    if not _SHA1_RE.fullmatch(args.commit):
        raise EvidenceError("commit must be a full lowercase Git SHA")
    contract_sha256 = _sha256(args.contract_sha256, label="contract SHA-256")
    digest = _digest(args.digest, label="subject digest")
    config_digest = _digest(args.config_digest, label="config digest")
    if args.media_type not in {
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    }:
        raise EvidenceError("subject is an index or unsupported manifest type")

    config = _mapping(
        _load_digest_bound_json(
            args.config,
            expected_digest=config_digest,
            label="OCI config",
        ),
        label="OCI config",
    )
    if config.get("os") != "linux" or config.get("architecture") != "arm64":
        raise EvidenceError("actual image config is not linux/arm64")
    config_section = _mapping(config.get("config"), label="OCI config.config")
    labels = _mapping(config_section.get("Labels"), label="OCI labels")
    normalized_labels = {
        str(key): str(value)
        for key, value in labels.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    requested_build_context_sha256 = getattr(args, "build_context_sha256", "")
    requested_runtime_contract = getattr(args, "runtime_contract", None)
    approval_evidence_json = getattr(args, "approval_evidence_json", "")
    normalized_approval_evidence: dict[str, Any] | None = None
    if args.pipeline == "mcp":
        if not isinstance(approval_evidence_json, str) or not approval_evidence_json:
            raise EvidenceError("approval evidence is required for the MCP pipeline")
        try:
            raw_approval_evidence = json.loads(
                approval_evidence_json,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except json.JSONDecodeError as exc:
            raise EvidenceError("approval evidence is not valid JSON") from exc
        normalized_approval_evidence = validate_approval_evidence(
            raw_approval_evidence,
            pipeline="mcp",
            expected_commit=args.commit,
        )
    elif approval_evidence_json:
        raise EvidenceError("approval evidence is only valid for the MCP pipeline")
    if normalized_labels.get("org.opencontainers.image.revision") != args.commit:
        raise EvidenceError("OCI revision does not match the full commit")
    if args.pipeline == "mcp":
        assert normalized_approval_evidence is not None
        if requested_runtime_contract is None:
            raise EvidenceError("runtime contract is required for the MCP pipeline")
        build_context_sha256 = _sha256(
            requested_build_context_sha256,
            label="canonical build context SHA-256",
        )
        if normalized_labels.get("io.teamagent.build.context-sha256") != build_context_sha256:
            raise EvidenceError("OCI context label does not match signed source evidence")
        if (
            normalized_labels.get("io.teamagent.build.release-approval-sha256")
            != normalized_approval_evidence["approval_payload_sha256"]
        ):
            raise EvidenceError("OCI approval label does not match the verified external approval")
    elif requested_build_context_sha256 or requested_runtime_contract is not None:
        raise EvidenceError(
            "build context digest and runtime contract are only valid for the MCP pipeline"
        )
    contract_label = PIPELINES[args.pipeline]["contract_label"]
    if normalized_labels.get(contract_label) != contract_sha256:
        raise EvidenceError("OCI contract label does not match the signed contract")
    if args.pipeline == "mcp":
        runtime_contract_path = Path(requested_runtime_contract)
        runtime_contract_sha256 = _sha256_file(
            runtime_contract_path,
            label="MCP runtime contract",
        )
        try:
            require_bundle_release_ready(load_bundle_contract(args.contract))
        except BundleProvenanceError as exc:
            raise EvidenceError(f"MCP outer contract mismatch: {exc}") from exc
        try:
            require_runtime_release_ready(
                load_runtime_contract(runtime_contract_path),
                label="MCP runtime contract",
            )
        except RuntimeProvenanceError as exc:
            raise EvidenceError(f"MCP runtime contract mismatch: {exc}") from exc
        if args.name == "core":
            try:
                verify_core_runtime_oci_revision(
                    args.config,
                    config_digest,
                    args.commit,
                    runtime_contract_path,
                    runtime_contract_sha256,
                )
            except RuntimeProvenanceError as exc:
                raise EvidenceError(f"MCP core runtime receipt mismatch: {exc}") from exc
        try:
            verify_teamagent_oci_config(
                args.config,
                subject_name=args.name,
                commit=args.commit,
                expected_config_digest=config_digest,
                contract_path=args.contract,
                expected_contract_sha256=contract_sha256,
                runtime_contract_path=runtime_contract_path,
                expected_build_context_sha256=build_context_sha256,
                expected_release_approval_sha256=normalized_approval_evidence[
                    "approval_payload_sha256"
                ],
            )
        except BundleProvenanceError as exc:
            raise EvidenceError(f"MCP OCI core/media interface mismatch: {exc}") from exc

    expected_image = f"{REGISTRY}/{quarantine_repository}@{digest}"
    scan_counts = _trivy_counts(
        _load(args.trivy_report, label="Trivy report"),
        expected_image=expected_image,
    )
    # Enforce the gate the signed contract declares. A contract that declares
    # bundle.scan_gate is held to exactly that (zero Critical/High plus zero
    # secrets); one that declares nothing keeps the all-severities-zero rule.
    # Only an explicit zero gate is accepted, so a contract edit can never permit
    # Critical or High findings. Distroless Debian carries Low/Medium CVEs with
    # no fixed version, which is why all-zero can never pass for such an image.
    scan_gate_contract = _load(args.contract, label="release contract")
    declared_gate = None
    if isinstance(scan_gate_contract, dict) and isinstance(
        scan_gate_contract.get("bundle"), dict
    ):
        declared_gate = scan_gate_contract["bundle"].get("scan_gate")
    if declared_gate is not None:
        if not isinstance(declared_gate, dict) or set(declared_gate) != {
            "critical",
            "high",
        }:
            raise EvidenceError("contract scan gate is malformed")
        for key in ("critical", "high"):
            value = declared_gate[key]
            if isinstance(value, bool) or not isinstance(value, int) or value != 0:
                raise EvidenceError(
                    "contract scan gate must require zero Critical and High"
                )
        blocking = scan_counts["critical"] + scan_counts["high"] + scan_counts["secrets"]
    else:
        blocking = sum(scan_counts.values())
    if blocking:
        raise EvidenceError(f"actual-image gate failed: {scan_counts}")

    spdx = _load(args.sbom, label="actual-image SPDX SBOM")
    if not isinstance(spdx, dict) or spdx.get("spdxVersion") not in {
        "SPDX-2.2",
        "SPDX-2.3",
    }:
        raise EvidenceError("actual-image SBOM is not SPDX JSON")
    packages = spdx.get("packages")
    if not isinstance(packages, list) or not packages:
        raise EvidenceError("actual-image SBOM has no packages")
    provenance = _mapping(
        _load(args.provenance, label="actual-image provenance"),
        label="actual-image provenance",
    )
    if not _contains_exact(provenance, args.commit) or not _contains_exact(
        provenance, contract_sha256
    ):
        raise EvidenceError("provenance does not bind the full commit and contract hash")
    if args.pipeline == "mcp":
        if not _contains_exact(provenance, requested_build_context_sha256):
            raise EvidenceError("provenance does not bind the canonical build context")
        if not _contains_exact(provenance, runtime_contract_sha256):
            raise EvidenceError("provenance does not bind the inner runtime contract")
        assert normalized_approval_evidence is not None
        if not _contains_exact(
            provenance,
            normalized_approval_evidence["approval_payload_sha256"],
        ):
            raise EvidenceError("provenance does not bind the external approval")
    provenance_subjects = provenance.get("subject")
    if not isinstance(provenance_subjects, list) or not any(
        isinstance(subject, dict)
        and isinstance(subject.get("digest"), dict)
        and subject["digest"].get("sha256") == digest.removeprefix("sha256:")
        for subject in provenance_subjects
    ):
        raise EvidenceError("provenance does not bind the actual image digest")

    subject_referrers = _load(args.subject_referrers, label="subject referrers")
    sbom_digest = _referrer(
        subject_referrers,
        artifact_type=REFERRER_ARTIFACT_TYPES["sbom"],
        expected_digest=args.sbom_digest,
        expected_payload_sha256=_sha256_file(
            args.sbom,
            label="actual-image SPDX SBOM",
        ),
        label="SBOM",
    )
    provenance_digest = _referrer(
        subject_referrers,
        artifact_type=REFERRER_ARTIFACT_TYPES["provenance"],
        expected_digest=args.provenance_digest,
        expected_payload_sha256=_sha256_file(
            args.provenance,
            label="actual-image provenance",
        ),
        label="provenance",
    )
    sbom_signature_referrer_digest = _signature_referrer(
        _load(args.sbom_signature_referrers, label="SBOM signature referrers"),
        label="SBOM",
    )
    provenance_signature_referrer_digest = _signature_referrer(
        _load(args.provenance_signature_referrers, label="provenance signature referrers"),
        label="provenance",
    )
    image_signature_referrer_digest = _signature_referrer(
        _load(args.image_signature_referrers, label="image signature referrers"),
        label="image",
    )
    image_signature_sha256 = _verify_image_signature(
        args.image_signature_verification,
        digest=digest,
    )
    _verify_artifact_signature(
        args.sbom_signature_verification,
        label="SBOM signature",
        digest=sbom_digest,
    )
    _verify_artifact_signature(
        args.provenance_signature_verification,
        label="provenance signature",
        digest=provenance_digest,
    )
    key_arn = args.signing_key_arn
    if not _KEY_ARN_RE.fullmatch(key_arn):
        raise EvidenceError("signature key ARN is outside the fixed account/region")

    subject_count = len(expected_subjects)
    suffix = subject_tag_suffix(args.pipeline, args.name, subject_count)
    release_prefix = {
        "verified-candidate": "verified",
        "active": "active",
        "rollback": "rollback",
    }[args.channel]
    subject = {
        "name": args.name,
        "quarantine_repository": quarantine_repository,
        "candidate_repository": candidate_repository,
        "release_repository": release_repository,
        "candidate_tag": (
            args.commit if args.pipeline == "tiktok" else f"candidate-{args.commit}{suffix}"
        ),
        "release_tag": f"{release_prefix}-{args.commit}{suffix}",
        "digest": digest,
        "media_type": args.media_type,
        "config_digest": config_digest,
        "platform": {"os": "linux", "architecture": "arm64"},
        "labels": dict(sorted(normalized_labels.items())),
        "binaries": _binary_probes(args.binary_probes),
        "scan": {
            "scanner": "trivy",
            "actual_image": expected_image,
            **scan_counts,
            "report_sha256": _sha256_file(args.trivy_report, label="Trivy report"),
        },
        "sbom": {
            "digest": sbom_digest,
            "artifact_type": REFERRER_ARTIFACT_TYPES["sbom"],
            "payload_sha256": _sha256_file(args.sbom, label="actual-image SPDX SBOM"),
            "signature": {
                "verified": True,
                "key_arn": key_arn,
                "subject_digest": sbom_digest,
                "referrer_digest": sbom_signature_referrer_digest,
                "bundle_sha256": _sha256_file(
                    args.sbom_signature_verification,
                    label="SBOM signature verification",
                ),
            },
        },
        "provenance": {
            "digest": provenance_digest,
            "artifact_type": REFERRER_ARTIFACT_TYPES["provenance"],
            "payload_sha256": _sha256_file(
                args.provenance,
                label="actual-image provenance",
            ),
            "signature": {
                "verified": True,
                "key_arn": key_arn,
                "subject_digest": provenance_digest,
                "referrer_digest": provenance_signature_referrer_digest,
                "bundle_sha256": _sha256_file(
                    args.provenance_signature_verification,
                    label="provenance signature verification",
                ),
            },
        },
        "image_signature": {
            "verified": True,
            "key_arn": key_arn,
            "subject_digest": digest,
            "referrer_digest": image_signature_referrer_digest,
            "bundle_sha256": image_signature_sha256,
        },
    }
    # Reuse the complete receipt validator to avoid schema drift.
    placeholder_source_prefix = {
        "mcp": f"source-declarations/mcp/{args.commit}/",
        "tiktok": f"source-manifests/tiktok/{args.commit}/",
        "openclaw": f"source-manifests/{args.commit}/",
    }[args.pipeline]
    placeholder_source_key = f"{placeholder_source_prefix}{'0' * 64}.json"
    placeholder_receipt = {
        "schema_version": release_receipt_schema_for_pipeline(args.pipeline),
        "kind": "teamagent.release-receipt",
        "pipeline": args.pipeline,
        "channel": args.channel,
        "issued_at": "2099-01-01T00:00:00Z",
        "expires_at": "2099-01-01T00:30:00Z",
        "build": {
            "project_arn": (
                f"arn:aws:codebuild:{REGION}:{ACCOUNT_ID}:project/"
                f"{PIPELINES[args.pipeline]['build_project']}"
            ),
            "build_id": "schema-validation:placeholder",
            "source_commit": args.commit,
        },
        "contract": {
            "path": PIPELINES[args.pipeline]["contract_path"],
            "sha256": contract_sha256,
            "release_ready": True,
        },
        "source_evidence": {
            "bucket": (
                "teamagent-dev-openclaw-build-evidence"
                if args.pipeline == "openclaw"
                else "teamagent-dev-image-release-evidence"
            ),
            "key": placeholder_source_key,
            "version_id": "schema-validation",
            "sha256": "0" * 64,
            "signature_key": f"{placeholder_source_key}.sig",
            "signature_version_id": "schema-validation-signature",
        },
        "subjects": [subject],
    }
    if args.pipeline == "mcp":
        assert normalized_approval_evidence is not None
        placeholder_receipt["approval_evidence"] = normalized_approval_evidence
    if subject_count == 1:
        validate_release_receipt(
            placeholder_receipt,
            expected_pipeline=args.pipeline,
            expected_commit=args.commit,
            expected_contract_sha256=contract_sha256,
            allowed_channels={args.channel},
            now=dt.datetime(2099, 1, 1, tzinfo=dt.UTC),
        )
    return subject


def _parser() -> argparse.ArgumentParser:
    # Abbreviations are refused: a caller that passed --subject had it silently
    # bound to --subject-referrers by prefix matching, so the real value was
    # discarded and the failure surfaced as an unrelated missing --name.
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--pipeline", choices=sorted(PIPELINES), required=True)
    parser.add_argument(
        "--channel",
        choices=("verified-candidate", "active", "rollback"),
        required=True,
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--quarantine-repository", required=True)
    parser.add_argument("--candidate-repository", required=True)
    parser.add_argument("--release-repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--build-context-sha256", default="")
    parser.add_argument("--runtime-contract", type=Path)
    parser.add_argument("--approval-evidence-json", default="")
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--media-type", required=True)
    parser.add_argument("--config-digest", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--binary-probes", type=Path, required=True)
    parser.add_argument("--trivy-report", type=Path, required=True)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--sbom-digest", required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--provenance-digest", required=True)
    parser.add_argument("--subject-referrers", type=Path, required=True)
    parser.add_argument("--sbom-signature-referrers", type=Path, required=True)
    parser.add_argument("--provenance-signature-referrers", type=Path, required=True)
    parser.add_argument("--image-signature-referrers", type=Path, required=True)
    parser.add_argument("--image-signature-verification", type=Path, required=True)
    parser.add_argument("--sbom-signature-verification", type=Path, required=True)
    parser.add_argument("--provenance-signature-verification", type=Path, required=True)
    parser.add_argument("--signing-key-arn", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        args.output.write_bytes(canonical_bytes(create_subject(args)))
    except (EvidenceError, OSError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
