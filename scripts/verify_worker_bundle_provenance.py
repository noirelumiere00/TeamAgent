#!/usr/bin/env python3
"""Verify a clean-origin KMS-signed worker archive receipt without exposing details."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_KIND = "teamagent.worker-bundle-provenance"
_ORIGIN = "git@github.com:noirelumiere00/TeamAgent.git"
_KEY_RE = re.compile(r"^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-f-]{36}$")
_SHA1_RE = re.compile(r"^[a-f0-9]{40}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class ProvenanceError(ValueError):
    """Worker provenance is malformed, untrusted, or does not bind the archive."""


@dataclass(frozen=True)
class ProvenanceBinding:
    """Exact KMS-verified receipt, source, signature, key, and artifact binding."""

    artifact_sha256: str
    canonical_receipt_sha256: str
    key_arn: str
    receipt_sha256: str
    signature_sha256: str
    source_branch: str
    source_commit: str
    source_origin: str
    source_tree: str


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProvenanceError("duplicate key")
        value[key] = item
    return value


def _stable_bytes(path: Path, *, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProvenanceError("unreadable provenance input") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 1 or before.st_size > maximum:
            raise ProvenanceError("invalid provenance input")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ProvenanceError("unreadable provenance input") from exc
    finally:
        os.close(descriptor)
    if (
        _stat_identity(before) != _stat_identity(after)
        or len(raw) != before.st_size
        or len(raw) > maximum
    ):
        raise ProvenanceError("provenance input changed while reading")
    return raw


def _stable_sha256(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProvenanceError("unreadable artifact") from exc
    hasher = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 1:
            raise ProvenanceError("invalid artifact")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ProvenanceError("unreadable artifact") from exc
    finally:
        os.close(descriptor)
    if _stat_identity(before) != _stat_identity(after):
        raise ProvenanceError("artifact changed while reading")
    return hasher.hexdigest()


def _load(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError("unreadable provenance") from exc
    if type(value) is not dict:
        raise ProvenanceError("invalid provenance")
    return value


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()


def _signature(raw: bytes) -> bytes:
    try:
        decoded = base64.b64decode(raw, validate=True)
    except binascii.Error:
        decoded = raw
    if not decoded or len(decoded) > 8192:
        raise ProvenanceError("invalid signature")
    return decoded


def verify(
    *,
    artifact: Path,
    receipt_path: Path,
    signature_path: Path,
    expected_key_arn: str,
    kms: Any,
) -> ProvenanceBinding:
    receipt_bytes = _stable_bytes(receipt_path, maximum=1_048_576)
    signature_bytes = _stable_bytes(signature_path, maximum=8192)
    receipt = _load(receipt_bytes)
    if set(receipt) != {"schema", "kind", "source", "artifact", "signing"}:
        raise ProvenanceError("invalid provenance schema")
    source = receipt["source"]
    artifact_claim = receipt["artifact"]
    signing = receipt["signing"]
    if (
        receipt["schema"] != 1
        or receipt["kind"] != _KIND
        or type(source) is not dict
        or set(source) != {"origin", "branch", "commit", "tree", "clean"}
        or source["origin"] != _ORIGIN
        or source["branch"] != "dev"
        or source["clean"] is not True
        or type(source["commit"]) is not str
        or _SHA1_RE.fullmatch(source["commit"]) is None
        or type(source["tree"]) is not str
        or _SHA1_RE.fullmatch(source["tree"]) is None
        or type(artifact_claim) is not dict
        or set(artifact_claim) != {"sha256", "format"}
        or artifact_claim["format"] != "tar.gz"
        or type(artifact_claim["sha256"]) is not str
        or _SHA256_RE.fullmatch(artifact_claim["sha256"]) is None
        or type(signing) is not dict
        or set(signing) != {"key_arn", "algorithm"}
        or type(signing["key_arn"]) is not str
        or _KEY_RE.fullmatch(signing["key_arn"]) is None
        or _KEY_RE.fullmatch(expected_key_arn) is None
        or signing["key_arn"] != expected_key_arn
        or signing["algorithm"] != "RSASSA_PSS_SHA_256"
    ):
        raise ProvenanceError("invalid provenance")
    digest = _stable_sha256(artifact)
    if digest != artifact_claim["sha256"]:
        raise ProvenanceError("artifact binding mismatch")
    response = kms.verify(
        KeyId=signing["key_arn"],
        Message=_canonical(receipt),
        MessageType="RAW",
        Signature=_signature(signature_bytes),
        SigningAlgorithm=signing["algorithm"],
    )
    if (
        type(response) is not dict
        or response.get("SignatureValid") is not True
        or response.get("KeyId") != signing["key_arn"]
        or response.get("SigningAlgorithm") != signing["algorithm"]
    ):
        raise ProvenanceError("signature verification failed")
    return ProvenanceBinding(
        artifact_sha256=digest,
        canonical_receipt_sha256=hashlib.sha256(_canonical(receipt)).hexdigest(),
        key_arn=signing["key_arn"],
        receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
        signature_sha256=hashlib.sha256(signature_bytes).hexdigest(),
        source_branch=source["branch"],
        source_commit=source["commit"],
        source_origin=source["origin"],
        source_tree=source["tree"],
    )


def main(argv: list[str] | None = None, *, kms: Any | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--key-arn", required=True)
    args = parser.parse_args(argv)
    try:
        if kms is None:
            import boto3

            kms = boto3.client("kms", region_name="ap-northeast-1")
        verify(
            artifact=args.artifact,
            receipt_path=args.receipt,
            signature_path=args.signature,
            expected_key_arn=args.key_arn,
            kms=kms,
        )
    except Exception:
        print('{"code":"worker_provenance_invalid","ok":false}')
        return 2
    print('{"code":"ok","ok":true}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
