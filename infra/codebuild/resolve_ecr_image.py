#!/usr/bin/env python3
"""Resolve and verify the exact Linux/arm64 child stored in Amazon ECR.

ECR tags may point either to a single image manifest or to an OCI index.  ECR
cannot scan an index, so callers must resolve the platform child, verify its
config bytes, and submit that child digest to the vulnerability scanner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_REVISION_RE = re.compile(r"[0-9a-f]{40}")
_IMAGE_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}
_INDEX_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}


class ImageContractError(ValueError):
    """An ECR response or OCI object did not satisfy the platform contract."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ImageContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _loads_strict(raw: str, *, label: str) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ImageContractError(f"invalid {label} JSON: {exc}") from exc


def _load_json(path: Path, *, label: str) -> Any:
    try:
        return _loads_strict(path.read_text(encoding="utf-8"), label=label)
    except (OSError, UnicodeDecodeError) as exc:
        raise ImageContractError(f"cannot read {label}: {path}: {exc}") from exc


def _validate_digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ImageContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _batch_manifest(path: Path, expected_digest: str) -> tuple[dict[str, Any], str]:
    expected_digest = _validate_digest(expected_digest, label="expected image digest")
    response = _load_json(path, label="ECR BatchGetImage response")
    if not isinstance(response, dict):
        raise ImageContractError("ECR BatchGetImage response must be an object")
    failures = response.get("failures")
    images = response.get("images")
    if not isinstance(failures, list) or failures:
        raise ImageContractError(f"ECR BatchGetImage returned failures: {failures!r}")
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise ImageContractError("ECR BatchGetImage must return exactly one image")

    image = images[0]
    image_id = image.get("imageId")
    raw = image.get("imageManifest")
    if not isinstance(image_id, dict) or image_id.get("imageDigest") != expected_digest:
        raise ImageContractError("ECR BatchGetImage returned a different image digest")
    if not isinstance(raw, str):
        raise ImageContractError("ECR BatchGetImage imageManifest is missing")
    actual_digest = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if actual_digest != expected_digest:
        raise ImageContractError(
            "ECR manifest bytes do not hash to the requested digest: "
            f"expected={expected_digest}, actual={actual_digest}"
        )

    manifest = _loads_strict(raw, label="OCI manifest")
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2:
        raise ImageContractError("unsupported OCI manifest schema")
    media_type = manifest.get("mediaType")
    if media_type not in _IMAGE_MEDIA_TYPES | _INDEX_MEDIA_TYPES:
        raise ImageContractError(f"unsupported OCI media type: {media_type!r}")
    response_media_type = image.get("imageManifestMediaType")
    if response_media_type is not None and response_media_type != media_type:
        raise ImageContractError("ECR response and manifest media types do not match")
    return manifest, media_type


def _platform_matches(platform: dict[str, Any], os_name: str, architecture: str) -> bool:
    if platform.get("os") != os_name or platform.get("architecture") != architecture:
        return False
    variant = platform.get("variant")
    if architecture == "arm64" and variant not in {None, "", "v8"}:
        return False
    return True


def resolve_platform_child(
    batch_response: Path,
    expected_image_digest: str,
    *,
    os_name: str,
    architecture: str,
) -> str:
    """Return the single image-manifest digest for the requested platform."""

    manifest, media_type = _batch_manifest(batch_response, expected_image_digest)
    if media_type in _IMAGE_MEDIA_TYPES:
        return expected_image_digest

    descriptors = manifest.get("manifests")
    if not isinstance(descriptors, list) or not descriptors:
        raise ImageContractError("OCI index manifests must be a non-empty array")
    matches: list[str] = []
    seen_digests: set[str] = set()
    for index, descriptor in enumerate(descriptors):
        label = f"OCI index manifests[{index}]"
        if not isinstance(descriptor, dict):
            raise ImageContractError(f"{label} must be an object")
        descriptor_media_type = descriptor.get("mediaType")
        if descriptor_media_type not in _IMAGE_MEDIA_TYPES:
            raise ImageContractError(
                f"{label} has unsupported child media type: {descriptor_media_type!r}"
            )
        digest = _validate_digest(descriptor.get("digest"), label=f"{label} digest")
        size = descriptor.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ImageContractError(f"{label} size must be a positive integer")
        if digest in seen_digests:
            raise ImageContractError(f"OCI index contains duplicate child digest: {digest}")
        seen_digests.add(digest)
        platform = descriptor.get("platform")
        if not isinstance(platform, dict):
            raise ImageContractError(f"{label} platform must be an object")
        if _platform_matches(platform, os_name, architecture):
            matches.append(digest)

    if len(matches) != 1:
        raise ImageContractError(
            f"OCI index must contain exactly one {os_name}/{architecture} image; "
            f"found {len(matches)}"
        )
    return matches[0]


def image_config_digest(batch_response: Path, expected_image_digest: str) -> str:
    """Return the config digest from an exact child image manifest."""

    manifest, media_type = _batch_manifest(batch_response, expected_image_digest)
    if media_type not in _IMAGE_MEDIA_TYPES:
        raise ImageContractError("expected an image manifest, not an OCI index")
    config = manifest.get("config")
    if not isinstance(config, dict):
        raise ImageContractError("OCI image manifest config descriptor is missing")
    digest = _validate_digest(config.get("digest"), label="OCI config digest")
    size = config.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ImageContractError("OCI config size must be a positive integer")
    return digest


def verify_config_platform(
    config_path: Path,
    expected_config_digest: str,
    *,
    os_name: str,
    architecture: str,
    expected_revision: str | None = None,
) -> None:
    """Verify config bytes, platform, and optionally the exact source revision."""

    expected_config_digest = _validate_digest(
        expected_config_digest, label="expected config digest"
    )
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise ImageContractError(f"cannot read OCI config: {config_path}: {exc}") from exc
    actual_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual_digest != expected_config_digest:
        raise ImageContractError(
            "OCI config bytes do not hash to the requested digest: "
            f"expected={expected_config_digest}, actual={actual_digest}"
        )
    try:
        config = _loads_strict(raw.decode("utf-8"), label="OCI config")
    except UnicodeDecodeError as exc:
        raise ImageContractError("OCI config is not UTF-8") from exc
    if not isinstance(config, dict):
        raise ImageContractError("OCI config must be an object")
    platform = {
        "os": config.get("os"),
        "architecture": config.get("architecture"),
        "variant": config.get("variant"),
    }
    if not _platform_matches(platform, os_name, architecture):
        raise ImageContractError(
            "OCI config platform mismatch: "
            f"expected={os_name}/{architecture}, "
            f"actual={config.get('os')!r}/{config.get('architecture')!r}, "
            f"variant={config.get('variant')!r}"
        )
    if expected_revision is not None:
        if not _REVISION_RE.fullmatch(expected_revision):
            raise ImageContractError("expected revision must be a full lowercase Git SHA-1")
        image_config = config.get("config")
        if not isinstance(image_config, dict):
            raise ImageContractError("OCI image config section is missing")
        labels = image_config.get("Labels")
        if not isinstance(labels, dict):
            raise ImageContractError("OCI image labels are missing")
        actual_revision = labels.get("org.opencontainers.image.revision")
        if actual_revision != expected_revision:
            raise ImageContractError(
                "OCI revision label mismatch: "
                f"expected={expected_revision!r}, actual={actual_revision!r}"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve-platform")
    resolve.add_argument("--batch-response", type=Path, required=True)
    resolve.add_argument("--expected-image-digest", required=True)
    resolve.add_argument("--os", dest="os_name", required=True)
    resolve.add_argument("--architecture", required=True)

    config_digest = subparsers.add_parser("config-digest")
    config_digest.add_argument("--batch-response", type=Path, required=True)
    config_digest.add_argument("--expected-image-digest", required=True)

    verify = subparsers.add_parser("verify-config-platform")
    verify.add_argument("--config", type=Path, required=True)
    verify.add_argument("--expected-config-digest", required=True)
    verify.add_argument("--os", dest="os_name", required=True)
    verify.add_argument("--architecture", required=True)
    verify.add_argument("--expected-revision")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "resolve-platform":
            print(
                resolve_platform_child(
                    args.batch_response,
                    args.expected_image_digest,
                    os_name=args.os_name,
                    architecture=args.architecture,
                )
            )
        elif args.command == "config-digest":
            print(image_config_digest(args.batch_response, args.expected_image_digest))
        elif args.command == "verify-config-platform":
            verify_config_platform(
                args.config,
                args.expected_config_digest,
                os_name=args.os_name,
                architecture=args.architecture,
                expected_revision=args.expected_revision,
            )
            print(f"OCI config platform verified: {args.os_name}/{args.architecture}")
        else:  # pragma: no cover - argparse enforces a known command.
            raise ImageContractError(f"unsupported command: {args.command}")
    except ImageContractError as exc:
        print(f"FATAL ECR image contract failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
