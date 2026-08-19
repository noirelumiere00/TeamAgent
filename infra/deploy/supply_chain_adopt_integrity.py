#!/usr/bin/env python3
"""PR2-A0 Supply-Chain Adopt の S3 実体 integrity 検証（fail-closed）。

adopt は AWS 実体を一切変更してはならない。本スクリプトは adopt の前後で対象オブジェクトの
同一性を機械的に証明する。`change.importing != null` だけを根拠に「実体変更なし」と判定しない
ための独立した検査であり、guard の adopt 経路から apply の前後で呼ばれる。

取得・比較する項目:
    VersionId / ETag / ContentLength / LastModified / ObjectLockMode /
    ObjectLockRetainUntilDate / body の SHA256

precondition（snapshot 時に必ず検査）:
    SHA256(S3 body) == content-addressed key に埋め込まれた sha256
    （＝ mapping の expected_content_sha256）。1 件でも不一致なら異常終了する。

使い方:
    # adopt 前
    supply_chain_adopt_integrity.py snapshot --mapping M.json --out before.json
    # adopt 後
    supply_chain_adopt_integrity.py snapshot --mapping M.json --out after.json
    supply_chain_adopt_integrity.py compare --before before.json --after after.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# 同ディレクトリの mapping loader を import できるようにする（guard から直接実行されるため）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

# adopt 前後で 1 項目でも変われば activation failure とする比較対象。
COMPARED_FIELDS = (
    "version_id",
    "etag",
    "content_length",
    "last_modified",
    "object_lock_mode",
    "object_lock_retain_until_date",
    "body_sha256",
)


class IntegrityError(Exception):
    """S3 実体の integrity が満たされない。呼び出し側は必ず fail-closed で扱う。"""


def _s3_client(region: str) -> Any:
    import boto3  # 遅延 import: テストは boto3 を要求しない

    return boto3.client("s3", region_name=region)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def probe_object(client: Any, bucket: str, key: str) -> dict[str, Any]:
    """1 オブジェクトの不変性証跡を採取する（body を読んで SHA256 も計算する）。"""
    head = client.head_object(Bucket=bucket, Key=key)
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    return {
        "bucket": bucket,
        "key": key,
        "version_id": head.get("VersionId"),
        "etag": head.get("ETag"),
        "content_length": head.get("ContentLength"),
        "last_modified": _iso(head.get("LastModified")),
        "object_lock_mode": head.get("ObjectLockMode"),
        "object_lock_retain_until_date": _iso(head.get("ObjectLockRetainUntilDate")),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }


def snapshot(mapping_path: Path, region: str, client: Any | None = None) -> dict[str, Any]:
    """mapping の全対象を採取し、content-addressed precondition を検査した snapshot を返す。"""
    from supply_chain_adopt_validate import load_mapping  # 同ディレクトリの mapping loader を共用

    adoptions = load_mapping(mapping_path)
    s3 = client if client is not None else _s3_client(region)

    entries: dict[str, Any] = {}
    for entry in adoptions:
        probe = probe_object(s3, entry["bucket"], entry["key"])
        expected = entry["expected_content_sha256"]
        if probe["body_sha256"] != expected:
            raise IntegrityError(
                f"{entry['key']}: body sha256 {probe['body_sha256']} != "
                f"content-addressed key sha256 {expected}"
            )
        if probe["object_lock_mode"] != "GOVERNANCE":
            raise IntegrityError(
                f"{entry['key']}: object lock mode is "
                f"{probe['object_lock_mode']!r}, expected GOVERNANCE"
            )
        entries[entry["new_address"]] = probe
    return {"schema_version": 1, "objects": entries}


def compare(before: dict[str, Any], after: dict[str, Any]) -> None:
    """adopt 前後の snapshot を比較する。1 項目でも差があれば IntegrityError。"""
    before_objects = before.get("objects")
    after_objects = after.get("objects")
    if not isinstance(before_objects, dict) or not isinstance(after_objects, dict):
        raise IntegrityError("snapshot is malformed: objects must be an object")
    if set(before_objects) != set(after_objects):
        raise IntegrityError(
            f"object set changed: missing={sorted(set(before_objects) - set(after_objects))} "
            f"unexpected={sorted(set(after_objects) - set(before_objects))}"
        )

    drifted: list[str] = []
    for address, before_probe in before_objects.items():
        after_probe = after_objects[address]
        for field in COMPARED_FIELDS:
            if before_probe.get(field) != after_probe.get(field):
                drifted.append(
                    f"{address}.{field}: {before_probe.get(field)!r} -> {after_probe.get(field)!r}"
                )
    if drifted:
        raise IntegrityError("AWS object mutated during adopt:\n  " + "\n  ".join(drifted))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="対象オブジェクトの不変性証跡を採取する")
    snap.add_argument("--mapping", required=True, type=Path)
    snap.add_argument("--out", required=True, type=Path)
    snap.add_argument("--region", default="ap-northeast-1")

    cmp_parser = sub.add_parser("compare", help="adopt 前後の snapshot を比較する")
    cmp_parser.add_argument("--before", required=True, type=Path)
    cmp_parser.add_argument("--after", required=True, type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = snapshot(args.mapping, args.region)
            args.out.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            args.out.chmod(0o600)
            count = len(result["objects"])
            print(f"integrity snapshot written: {args.out} ({count} object(s))")
        else:
            compare(
                json.loads(args.before.read_text(encoding="utf-8")),
                json.loads(args.after.read_text(encoding="utf-8")),
            )
            print("integrity comparison passed: no AWS object mutated during adopt")
    except (IntegrityError, json.JSONDecodeError, OSError) as error:
        print(f"adopt integrity check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
