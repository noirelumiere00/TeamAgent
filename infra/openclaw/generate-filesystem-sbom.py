#!/usr/bin/env python3
"""Create and verify an exact whole-filesystem CycloneDX inventory.

Trivy remains the package/vulnerability source.  This companion augments its
CycloneDX document with one canonical component for every object visible in a
merged container export, including directories and links.  The generated
equivalence report is consumed by the release gate and the trusted promoter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROPERTY_PREFIX = "io.teamagent.openclaw.fs."


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _safe_path(raw: str) -> str | None:
    while raw.startswith("./"):
        raw = raw[2:]
    raw = raw.rstrip("/")
    if raw in {"", "."}:
        return None
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or ".." in candidate.parts or "\x00" in raw:
        raise ValueError(f"unsafe rootfs tar path: {raw!r}")
    return candidate.as_posix()


def _kind(member: tarfile.TarInfo) -> str:
    if member.isfile():
        return "file"
    if member.isdir():
        return "directory"
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    if member.ischr():
        return "character-device"
    if member.isblk():
        return "block-device"
    if member.isfifo():
        return "fifo"
    raise ValueError(f"unsupported rootfs tar entry type: {member.name!r}")


def _inventory(rootfs_tar: Path) -> list[dict[str, Any]]:
    entries_by_path: dict[str, dict[str, Any]] = {}
    with tarfile.open(rootfs_tar, mode="r:*") as archive:
        for member in archive:
            relative = _safe_path(member.name)
            if relative is None:
                continue
            kind = _kind(member)
            content_sha256: str | None = None
            if kind == "file":
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError(f"could not read rootfs file: {relative}")
                digest = hashlib.sha256()
                while chunk := extracted.read(1024 * 1024):
                    digest.update(chunk)
                content_sha256 = digest.hexdigest()
            link_target = member.linkname if kind in {"symlink", "hardlink"} else None
            descriptor = {
                "path": relative,
                "type": kind,
                "mode": f"{member.mode & 0o7777:04o}",
                "uid": member.uid,
                "gid": member.gid,
                "size": member.size if kind == "file" else 0,
                "linkTarget": link_target,
                "contentSha256": content_sha256,
            }
            descriptor["descriptorSha256"] = _sha256_bytes(_canonical_bytes(descriptor))
            if relative in entries_by_path:
                raise ValueError(f"duplicate rootfs tar path: {relative!r}")
            entries_by_path[relative] = descriptor
    entries = sorted(entries_by_path.values(), key=lambda item: item["path"])
    if not entries:
        raise ValueError("rootfs inventory is empty")
    return entries


def _property_map(component: dict[str, Any]) -> dict[str, str]:
    properties = component.get("properties")
    if not isinstance(properties, list):
        return {}
    result: dict[str, str] = {}
    for item in properties:
        if (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("value"), str)
        ):
            if item["name"] in result:
                raise ValueError(f"duplicate component property: {item['name']}")
            result[item["name"]] = item["value"]
    return result


def _fs_component(entry: dict[str, Any]) -> dict[str, Any]:
    path_digest = _sha256_bytes(entry["path"].encode())
    properties = [
        {"name": f"{PROPERTY_PREFIX}path", "value": entry["path"]},
        {"name": f"{PROPERTY_PREFIX}type", "value": entry["type"]},
        {"name": f"{PROPERTY_PREFIX}mode", "value": entry["mode"]},
        {"name": f"{PROPERTY_PREFIX}uid", "value": str(entry["uid"])},
        {"name": f"{PROPERTY_PREFIX}gid", "value": str(entry["gid"])},
        {"name": f"{PROPERTY_PREFIX}size", "value": str(entry["size"])},
        {
            "name": f"{PROPERTY_PREFIX}descriptorSha256",
            "value": entry["descriptorSha256"],
        },
    ]
    if entry["linkTarget"] is not None:
        properties.append(
            {
                "name": f"{PROPERTY_PREFIX}linkTarget",
                "value": entry["linkTarget"],
            }
        )
    if entry["contentSha256"] is not None:
        properties.append(
            {
                "name": f"{PROPERTY_PREFIX}contentSha256",
                "value": entry["contentSha256"],
            }
        )
    component: dict[str, Any] = {
        "type": "file",
        "name": f"/{entry['path']}",
        "bom-ref": f"urn:teamagent:openclaw:fs:{path_digest}",
        "properties": properties,
    }
    if entry["contentSha256"] is not None:
        component["hashes"] = [
            {"alg": "SHA-256", "content": entry["contentSha256"]}
        ]
    return component


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _parse_component(component: dict[str, Any]) -> dict[str, Any]:
    props = _property_map(component)
    path = props.get(f"{PROPERTY_PREFIX}path")
    if path is None:
        raise ValueError("filesystem component has no path")
    parsed = {
        "path": path,
        "type": props[f"{PROPERTY_PREFIX}type"],
        "mode": props[f"{PROPERTY_PREFIX}mode"],
        "uid": int(props[f"{PROPERTY_PREFIX}uid"]),
        "gid": int(props[f"{PROPERTY_PREFIX}gid"]),
        "size": int(props[f"{PROPERTY_PREFIX}size"]),
        "linkTarget": props.get(f"{PROPERTY_PREFIX}linkTarget"),
        "contentSha256": props.get(f"{PROPERTY_PREFIX}contentSha256"),
        "descriptorSha256": props[f"{PROPERTY_PREFIX}descriptorSha256"],
    }
    if not SHA256_RE.fullmatch(parsed["descriptorSha256"]):
        raise ValueError(f"invalid descriptor hash for {path}")
    if parsed["contentSha256"] is not None and not SHA256_RE.fullmatch(
        parsed["contentSha256"]
    ):
        raise ValueError(f"invalid content hash for {path}")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rootfs-tar", type=Path, required=True)
    parser.add_argument("--trivy-sbom", type=Path, required=True)
    parser.add_argument("--inventory-output", type=Path, required=True)
    parser.add_argument("--sbom-output", type=Path, required=True)
    parser.add_argument("--equivalence-output", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--config-digest", required=True)
    args = parser.parse_args()

    entries = _inventory(args.rootfs_tar)
    rootfs_tar_sha256 = _sha256_file(args.rootfs_tar)
    inventory = {
        "schemaVersion": 1,
        "subject": {
            "imageId": args.image_id,
            "manifestDigest": args.manifest_digest,
            "configDigest": args.config_digest,
            "rootfsTarSha256": rootfs_tar_sha256,
        },
        "entryCount": len(entries),
        "entries": entries,
    }
    _write_json(args.inventory_output, inventory)
    inventory_sha256 = _sha256_file(args.inventory_output)

    sbom = json.loads(args.trivy_sbom.read_text(encoding="utf-8"))
    if sbom.get("bomFormat") != "CycloneDX":
        raise ValueError("Trivy input is not CycloneDX")
    components = sbom.setdefault("components", [])
    if not isinstance(components, list):
        raise ValueError("CycloneDX components is not an array")
    existing_refs = {
        component.get("bom-ref")
        for component in components
        if isinstance(component, dict) and isinstance(component.get("bom-ref"), str)
    }
    fs_components = [_fs_component(entry) for entry in entries]
    fs_refs = [component["bom-ref"] for component in fs_components]
    if existing_refs.intersection(fs_refs):
        raise ValueError("filesystem bom-ref collides with Trivy component")
    components.extend(fs_components)

    metadata = sbom.setdefault("metadata", {})
    metadata_properties = metadata.setdefault("properties", [])
    metadata_properties.extend(
        [
            {
                "name": "io.teamagent.openclaw.wholeFilesystemInventorySha256",
                "value": inventory_sha256,
            },
            {
                "name": "io.teamagent.openclaw.wholeFilesystemRootfsTarSha256",
                "value": rootfs_tar_sha256,
            },
            {
                "name": "io.teamagent.openclaw.wholeFilesystemEntryCount",
                "value": str(len(entries)),
            },
            {
                "name": "io.teamagent.openclaw.subjectManifestDigest",
                "value": args.manifest_digest,
            },
        ]
    )
    root_ref = metadata.get("component", {}).get("bom-ref")
    if not isinstance(root_ref, str) or not root_ref:
        raise ValueError("CycloneDX metadata component has no bom-ref")
    dependencies = sbom.setdefault("dependencies", [])
    root_dependency = next(
        (
            dependency
            for dependency in dependencies
            if isinstance(dependency, dict) and dependency.get("ref") == root_ref
        ),
        None,
    )
    if root_dependency is None:
        root_dependency = {"ref": root_ref, "dependsOn": []}
        dependencies.append(root_dependency)
    root_dependency["dependsOn"] = sorted(
        set(root_dependency.get("dependsOn", [])) | set(fs_refs)
    )
    sbom.setdefault("compositions", []).append(
        {
            "aggregate": "complete",
            "assemblies": fs_refs,
        }
    )
    _write_json(args.sbom_output, sbom)

    rendered = json.loads(args.sbom_output.read_text(encoding="utf-8"))
    rendered_fs = []
    fs_ref_set = set(fs_refs)
    for component in rendered.get("components", []):
        if component.get("bom-ref") in fs_ref_set:
            rendered_fs.append(_parse_component(component))
    rendered_fs.sort(key=lambda item: item["path"])
    if rendered_fs != entries:
        raise ValueError("CycloneDX filesystem multiset does not equal rootfs inventory")
    if len(fs_refs) != len(set(fs_refs)):
        raise ValueError("CycloneDX filesystem bom-ref set is not unique")
    known_refs = {
        component.get("bom-ref")
        for component in rendered.get("components", [])
        if isinstance(component, dict) and component.get("bom-ref")
    }
    known_refs.add(root_ref)
    dangling = sorted(
        {
            ref
            for dependency in rendered.get("dependencies", [])
            for ref in [
                dependency.get("ref"),
                *dependency.get("dependsOn", []),
            ]
            if ref and ref not in known_refs
        }
    )
    if dangling:
        raise ValueError(f"CycloneDX has dangling dependency refs: {dangling[:10]}")

    equivalence = {
        "schemaVersion": 1,
        "subject": inventory["subject"],
        "inventory": {
            "path": args.inventory_output.name,
            "sha256": inventory_sha256,
            "entryCount": len(entries),
        },
        "sbom": {
            "path": args.sbom_output.name,
            "sha256": _sha256_file(args.sbom_output),
            "filesystemComponentCount": len(rendered_fs),
            "allComponentCount": len(rendered.get("components", [])),
            "bomRefsUnique": True,
            "danglingDependencyRefs": 0,
        },
        "pathTypeModeOwnerSizeLinkContentMultisetExact": True,
        "wholeFilesystemExactMatch": True,
    }
    _write_json(args.equivalence_output, equivalence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
