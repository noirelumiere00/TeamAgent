#!/usr/bin/env python3
"""Extract and verify an exact, reviewable Terraform mutation contract.

The runtime migration manifest must enumerate every non-no-op resource change
and every non-no-op drift entry.  Each row binds the complete Terraform
``change`` object as well as the individual before/after/action fields, so a
new address, changed action order, unknown value, replacement path, or hidden
schema field cannot ride through an address-only allowlist.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_HEX64 = frozenset("0123456789abcdef")
_ENTRY_KEYS = {
    "actions",
    "address",
    "after_sensitive_sha256",
    "after_sha256",
    "after_unknown_sha256",
    "before_sensitive_sha256",
    "before_sha256",
    "change_sha256",
    "deposed",
    "generated_config_sha256",
    "importing_sha256",
    "index",
    "mode",
    "module_address",
    "name",
    "provider_name",
    "replace_paths_sha256",
    "type",
}
_CONTRACT_KEYS = {
    "action_invocations_sha256",
    "deferred_changes_sha256",
    "format_version",
    "output_changes_sha256",
    "resource_changes",
    "resource_drift",
    "schema_version",
    "terraform_version",
}


class ContractError(RuntimeError):
    """The plan or reviewed contract is incomplete or non-exact."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("Terraform plan contains a non-canonical JSON value") from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ContractError(f"{label} has a non-string key")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ContractError(f"{label} must be a non-empty printable string")
    return value


def _actions(change: Mapping[str, Any], label: str) -> list[str]:
    raw = change.get("actions")
    if (
        not isinstance(raw, list)
        or not raw
        or any(not isinstance(action, str) or not action for action in raw)
    ):
        raise ContractError(f"{label} actions are invalid")
    return list(raw)


def _entry(value: Any, label: str) -> dict[str, Any]:
    resource = _mapping(value, label)
    change = _mapping(resource.get("change"), f"{label}.change")
    actions = _actions(change, label)
    index = resource.get("index")
    if index is not None and (isinstance(index, bool) or not isinstance(index, (int, str))):
        raise ContractError(f"{label}.index is invalid")
    module_address = resource.get("module_address")
    if module_address is not None:
        _string(module_address, f"{label}.module_address")
    deposed = resource.get("deposed")
    if deposed is not None:
        _string(deposed, f"{label}.deposed")
    return {
        "address": _string(resource.get("address"), f"{label}.address"),
        "module_address": module_address,
        "mode": _string(resource.get("mode"), f"{label}.mode"),
        "type": _string(resource.get("type"), f"{label}.type"),
        "name": _string(resource.get("name"), f"{label}.name"),
        "index": index,
        "provider_name": _string(
            resource.get("provider_name"),
            f"{label}.provider_name",
        ),
        "deposed": deposed,
        "actions": actions,
        "before_sha256": _sha256(change.get("before")),
        "after_sha256": _sha256(change.get("after")),
        "after_unknown_sha256": _sha256(change.get("after_unknown")),
        "before_sensitive_sha256": _sha256(change.get("before_sensitive")),
        "after_sensitive_sha256": _sha256(change.get("after_sensitive")),
        "replace_paths_sha256": _sha256(change.get("replace_paths")),
        "importing_sha256": _sha256(change.get("importing")),
        "generated_config_sha256": _sha256(resource.get("generated_config")),
        "change_sha256": _sha256(change),
    }


def _changed_entries(plan: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    raw = plan.get(field, [])
    if not isinstance(raw, list):
        raise ContractError(f"Terraform {field} must be an array")
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for position, value in enumerate(raw):
        entry = _entry(value, f"{field}[{position}]")
        identity = (entry["address"], entry["deposed"])
        if identity in seen:
            raise ContractError(f"Terraform {field} contains a duplicate address")
        seen.add(identity)
        if entry["actions"] != ["no-op"]:
            entries.append(entry)
    return sorted(
        entries,
        key=lambda item: (
            str(item["address"]),
            "" if item["deposed"] is None else str(item["deposed"]),
        ),
    )


def extract_contract(plan: Mapping[str, Any]) -> dict[str, Any]:
    format_version = _string(plan.get("format_version"), "format_version")
    terraform_version = _string(plan.get("terraform_version"), "terraform_version")
    if plan.get("errored") is not False or plan.get("complete") is not True:
        raise ContractError("Terraform plan is errored or incomplete")
    if plan.get("applyable") is not True:
        raise ContractError("Terraform plan is not applyable")
    return {
        "schema_version": 1,
        "format_version": format_version,
        "terraform_version": terraform_version,
        "resource_changes": _changed_entries(plan, "resource_changes"),
        "resource_drift": _changed_entries(plan, "resource_drift"),
        "output_changes_sha256": _sha256(plan.get("output_changes", {})),
        "deferred_changes_sha256": _sha256(plan.get("deferred_changes", [])),
        "action_invocations_sha256": _sha256(plan.get("action_invocations", [])),
    }


def validate_reviewed_contract(value: Mapping[str, Any]) -> None:
    if set(value) != _CONTRACT_KEYS or value.get("schema_version") != 1:
        raise ContractError("reviewed plan contract schema is invalid")
    _string(value.get("format_version"), "reviewed format_version")
    _string(value.get("terraform_version"), "reviewed terraform_version")
    for field in (
        "output_changes_sha256",
        "deferred_changes_sha256",
        "action_invocations_sha256",
    ):
        digest = value.get(field)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in _HEX64 for char in digest)
        ):
            raise ContractError(f"reviewed {field} is invalid")
    for field in ("resource_changes", "resource_drift"):
        rows = value.get(field)
        if not isinstance(rows, list):
            raise ContractError(f"reviewed {field} must be an array")
        identities: list[tuple[str, str | None]] = []
        for position, raw in enumerate(rows):
            row = _mapping(raw, f"reviewed {field}[{position}]")
            if set(row) != _ENTRY_KEYS:
                raise ContractError(f"reviewed {field}[{position}] schema is invalid")
            address = _string(row.get("address"), "reviewed address")
            deposed = row.get("deposed")
            if deposed is not None:
                _string(deposed, "reviewed deposed")
            identities.append((address, deposed))
            actions = row.get("actions")
            if (
                not isinstance(actions, list)
                or actions == ["no-op"]
                or any(not isinstance(action, str) or not action for action in actions)
            ):
                raise ContractError("reviewed actions are invalid")
            for key in _ENTRY_KEYS:
                if key.endswith("_sha256"):
                    digest = row.get(key)
                    if (
                        not isinstance(digest, str)
                        or len(digest) != 64
                        or any(char not in _HEX64 for char in digest)
                    ):
                        raise ContractError(f"reviewed {key} is invalid")
        if identities != sorted(identities, key=lambda item: (item[0], item[1] or "")):
            raise ContractError(f"reviewed {field} is not sorted")
        if len(identities) != len(set(identities)):
            raise ContractError(f"reviewed {field} contains duplicate addresses")


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"JSON root is not an object: {path}")
    return value


def _write_stdout(value: Mapping[str, Any]) -> None:
    os.write(1, _canonical(value) + b"\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract")
    extract.add_argument("--plan", required=True, type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("--plan", required=True, type=Path)
    verify.add_argument("--reviewed", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    actual = extract_contract(_load(args.plan))
    if args.command == "extract":
        _write_stdout(actual)
        return 0
    reviewed = _load(args.reviewed)
    validate_reviewed_contract(reviewed)
    if not hmac.compare_digest(_canonical(actual), _canonical(reviewed)):
        raise ContractError("Terraform plan differs from the exact reviewed contract")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
