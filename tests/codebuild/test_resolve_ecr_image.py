from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "codebuild" / "resolve_ecr_image.py"


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location("teamagent_ecr_image_resolver", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


resolver = _load_module()


def _digest(raw: bytes | str) -> str:
    payload = raw.encode("utf-8") if isinstance(raw, str) else raw
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write_batch(
    tmp_path: Path, manifest: dict[str, Any], name: str = "batch.json"
) -> tuple[Path, str]:
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    digest = _digest(raw)
    response = {
        "images": [
            {
                "imageId": {"imageDigest": digest},
                "imageManifest": raw,
                "imageManifestMediaType": manifest["mediaType"],
            }
        ],
        "failures": [],
    }
    path = tmp_path / name
    path.write_text(json.dumps(response), encoding="utf-8")
    return path, digest


def _descriptor(digest: str, architecture: str, *, os_name: str = "linux") -> dict[str, Any]:
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": digest,
        "size": 123,
        "platform": {"os": os_name, "architecture": architecture},
    }


def test_index_resolves_exactly_one_linux_arm64_child(tmp_path: Path) -> None:
    arm64_digest = "sha256:" + "a" * 64
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            _descriptor("sha256:" + "b" * 64, "amd64"),
            _descriptor(arm64_digest, "arm64"),
            _descriptor("sha256:" + "c" * 64, "unknown", os_name="unknown"),
        ],
    }
    path, index_digest = _write_batch(tmp_path, index)

    assert (
        resolver.resolve_platform_child(
            path,
            index_digest,
            os_name="linux",
            architecture="arm64",
        )
        == arm64_digest
    )


def test_single_image_manifest_is_already_the_child(tmp_path: Path) -> None:
    config_digest = "sha256:" + "d" * 64
    image = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": config_digest, "size": 456},
        "layers": [],
    }
    path, image_digest = _write_batch(tmp_path, image)

    assert (
        resolver.resolve_platform_child(
            path,
            image_digest,
            os_name="linux",
            architecture="arm64",
        )
        == image_digest
    )
    assert resolver.image_config_digest(path, image_digest) == config_digest


def test_index_with_ambiguous_arm64_children_fails_closed(tmp_path: Path) -> None:
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            _descriptor("sha256:" + "a" * 64, "arm64"),
            _descriptor("sha256:" + "b" * 64, "arm64"),
        ],
    }
    path, index_digest = _write_batch(tmp_path, index)

    with pytest.raises(resolver.ImageContractError, match="exactly one linux/arm64"):
        resolver.resolve_platform_child(
            path,
            index_digest,
            os_name="linux",
            architecture="arm64",
        )


def test_manifest_bytes_must_hash_to_ecr_digest(tmp_path: Path) -> None:
    image = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": "sha256:" + "d" * 64, "size": 456},
        "layers": [],
    }
    path, image_digest = _write_batch(tmp_path, image)
    response = json.loads(path.read_text(encoding="utf-8"))
    response["images"][0]["imageManifest"] += " "
    path.write_text(json.dumps(response), encoding="utf-8")

    with pytest.raises(resolver.ImageContractError, match="do not hash"):
        resolver.resolve_platform_child(
            path,
            image_digest,
            os_name="linux",
            architecture="arm64",
        )


def test_config_bytes_and_platform_are_both_verified(tmp_path: Path) -> None:
    revision = "a" * 40
    config = {
        "architecture": "arm64",
        "os": "linux",
        "config": {"Labels": {"org.opencontainers.image.revision": revision}},
    }
    raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    path = tmp_path / "config.json"
    path.write_bytes(raw)

    resolver.verify_config_platform(
        path,
        _digest(raw),
        os_name="linux",
        architecture="arm64",
        expected_revision=revision,
    )

    with pytest.raises(resolver.ImageContractError, match="platform mismatch"):
        resolver.verify_config_platform(
            path,
            _digest(raw),
            os_name="linux",
            architecture="amd64",
        )

    with pytest.raises(resolver.ImageContractError, match="do not hash"):
        resolver.verify_config_platform(
            path,
            "sha256:" + "f" * 64,
            os_name="linux",
            architecture="arm64",
        )

    with pytest.raises(resolver.ImageContractError, match="revision label mismatch"):
        resolver.verify_config_platform(
            path,
            _digest(raw),
            os_name="linux",
            architecture="arm64",
            expected_revision="b" * 40,
        )
