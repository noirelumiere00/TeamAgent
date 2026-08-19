#!/usr/bin/env python3
"""PR2-A0 Supply-Chain Adopt の plan validator（fail-closed）。

adopt は「既に S3 へ publish 済みで Object Lock 下にある不変オブジェクトを、Terraform state の
新しい hash-keyed アドレスへ取り込む」だけの操作であり、**AWS 実体を一切変更しない**。
本 validator はその不変条件を plan 段階で機械的に強制する。

既存の sync / runtime migration / activation の allowlist・validator には一切関与しない。
adopt は完全に独立した経路で、許可範囲は sync より **狭い**。

許可する resource_changes は次の3つだけ:

  1. actions == ["no-op"]
  2. actions == ["forget"]  かつ address が mapping の old_address に exact 一致
  3. actions == ["update"] かつ change.importing.id が非 null
     かつ address が mapping の new_address に exact 一致
     かつ change.importing.id が mapping の import_id に exact 一致

これ以外は 1 件でもあれば拒否する（mapping 外アドレス・create・delete・replace を含む）。
さらに mapping の全エントリが過不足なく plan に現れることを要求する（数も exact）。

data source は no-op / read のみ許可する。

使い方:
    supply_chain_adopt_validate.py --plan PLAN_JSON --mapping supply_chain_adoptions.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AdoptValidationError(Exception):
    """adopt plan が不変条件を満たさない。呼び出し側は必ず fail-closed で扱う。"""


def load_mapping(path: Path) -> list[dict[str, Any]]:
    """mapping を読み、構造とフィールド形式を厳密に検証して adoptions を返す。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AdoptValidationError("mapping is not an object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise AdoptValidationError(
            f"unsupported mapping schema_version: {raw.get('schema_version')!r}"
        )
    adoptions = raw.get("adoptions")
    if not isinstance(adoptions, list) or not adoptions:
        raise AdoptValidationError("mapping.adoptions must be a non-empty array")

    required = (
        "old_address",
        "new_address",
        "resource_type",
        "bucket",
        "key",
        "import_id",
        "expected_content_sha256",
    )
    seen_old: set[str] = set()
    seen_new: set[str] = set()
    for index, entry in enumerate(adoptions):
        if not isinstance(entry, dict):
            raise AdoptValidationError(f"adoptions[{index}] is not an object")
        missing = [field for field in required if not isinstance(entry.get(field), str)]
        if missing:
            raise AdoptValidationError(f"adoptions[{index}] is missing fields: {missing}")
        if entry["resource_type"] != "aws_s3_object":
            raise AdoptValidationError(
                f"adoptions[{index}] resource_type must be aws_s3_object"
            )
        sha = entry["expected_content_sha256"]
        if not SHA256_RE.match(sha):
            raise AdoptValidationError(f"adoptions[{index}] expected_content_sha256 is malformed")
        # content-addressed 性: key の basename は必ず expected_content_sha256 でなければならない。
        if not entry["key"].endswith(f"/{sha}.yml"):
            raise AdoptValidationError(
                f"adoptions[{index}] key does not end with its content-addressed sha256"
            )
        # import ID は "<bucket>/<key>" に厳密一致していること。
        if entry["import_id"] != f"{entry['bucket']}/{entry['key']}":
            raise AdoptValidationError(f"adoptions[{index}] import_id != '<bucket>/<key>'")
        # new_address は必ず sha256 を index に持つ hash-keyed アドレスであること。
        if f'["{sha}"]' not in entry["new_address"]:
            raise AdoptValidationError(
                f"adoptions[{index}] new_address is not keyed by its content sha256"
            )
        if entry["old_address"] in seen_old:
            raise AdoptValidationError(f"duplicate old_address: {entry['old_address']}")
        if entry["new_address"] in seen_new:
            raise AdoptValidationError(f"duplicate new_address: {entry['new_address']}")
        seen_old.add(entry["old_address"])
        seen_new.add(entry["new_address"])
    return adoptions


def validate_plan(plan: dict[str, Any], adoptions: list[dict[str, Any]]) -> None:
    """plan JSON が adopt の不変条件を満たすことを検証する。違反は AdoptValidationError。"""
    if not isinstance(plan, dict):
        raise AdoptValidationError("plan is not an object")
    changes = plan.get("resource_changes")
    if changes is None:
        changes = []
    if not isinstance(changes, list):
        raise AdoptValidationError("plan.resource_changes must be an array")

    by_old = {entry["old_address"]: entry for entry in adoptions}
    by_new = {entry["new_address"]: entry for entry in adoptions}
    forgotten: set[str] = set()
    imported: set[str] = set()

    for item in changes:
        if not isinstance(item, dict):
            raise AdoptValidationError("resource_changes entry is not an object")
        address = item.get("address")
        if not isinstance(address, str):
            raise AdoptValidationError("resource_changes entry has no string address")
        change = item.get("change")
        if not isinstance(change, dict):
            raise AdoptValidationError(f"{address}: change is not an object")
        actions = change.get("actions")
        if not isinstance(actions, list):
            raise AdoptValidationError(f"{address}: change.actions is not an array")

        if item.get("mode") == "data":
            if actions not in (["no-op"], ["read"]):
                raise AdoptValidationError(
                    f"{address}: data source action {actions} is not allowed"
                )
            continue

        if "delete" in actions:
            # forget は delete を含まない。ここに来る時点で実体削除の意図がある。
            raise AdoptValidationError(f"{address}: destructive action {actions} is not allowed")

        if actions == ["no-op"]:
            continue

        if actions == ["forget"]:
            entry = by_old.get(address)
            if entry is None:
                raise AdoptValidationError(f"{address}: forget is outside the adopt mapping")
            if address in forgotten:
                raise AdoptValidationError(f"{address}: duplicate forget")
            forgotten.add(address)
            continue

        if actions == ["update"]:
            importing = change.get("importing")
            if not isinstance(importing, dict) or not importing.get("id"):
                raise AdoptValidationError(
                    f"{address}: update without an import is not allowed in adopt"
                )
            entry = by_new.get(address)
            if entry is None:
                raise AdoptValidationError(f"{address}: import target is outside the adopt mapping")
            if importing["id"] != entry["import_id"]:
                raise AdoptValidationError(
                    f"{address}: import id does not match the mapping exactly"
                )
            if address in imported:
                raise AdoptValidationError(f"{address}: duplicate import")
            imported.add(address)
            continue

        raise AdoptValidationError(f"{address}: action {actions} is not allowed in adopt")

    expected_old = set(by_old)
    expected_new = set(by_new)
    if forgotten != expected_old:
        raise AdoptValidationError(
            f"forget set mismatch: missing={sorted(expected_old - forgotten)} "
            f"unexpected={sorted(forgotten - expected_old)}"
        )
    if imported != expected_new:
        raise AdoptValidationError(
            f"import set mismatch: missing={sorted(expected_new - imported)} "
            f"unexpected={sorted(imported - expected_new)}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path, help="terraform show -json の出力")
    parser.add_argument("--mapping", required=True, type=Path, help="supply_chain_adoptions.json")
    args = parser.parse_args(argv)

    try:
        adoptions = load_mapping(args.mapping)
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        validate_plan(plan, adoptions)
    except (AdoptValidationError, json.JSONDecodeError, OSError) as error:
        print(f"adopt plan validation failed: {error}", file=sys.stderr)
        return 1
    print(f"adopt plan validation passed: {len(adoptions)} adoption(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
