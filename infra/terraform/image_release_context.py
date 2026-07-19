#!/usr/bin/env python3
"""Capture a fail-closed Terraform context for one image-release saved plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CONTEXT_KIND = "teamagent.image-release-terraform-context"
CONTEXT_SCHEMA = 1
EXPECTED_BACKEND = {
    "type": "s3",
    "bucket": "teamagent-tfstate-718959508629",
    "key": "teamagent/terraform.tfstate",
    "region": "ap-northeast-1",
    "dynamodb_table": "teamagent-tflock",
    "encrypt": True,
}
EXPECTED_WORKSPACE = "default"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
INSTANCE_SELECTOR_RE = re.compile(r'\[(?:[0-9]+|"(?:[^"\\]|\\.)*")\]')


class ContextError(ValueError):
    """The Terraform plan, backend, workspace, or state is outside the contract."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContextError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _loads(value: str, *, label: str) -> Any:
    try:
        return json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ContextError(f"{label} is not valid JSON") from exc


def _load(path: Path, *, label: str) -> Any:
    try:
        return _loads(path.read_text(encoding="utf-8"), label=label)
    except (OSError, UnicodeDecodeError) as exc:
        raise ContextError(f"cannot read {label}: {path}") from exc


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ContextError(f"{label} must be an object")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContextError(f"{label} must be a lowercase SHA-256")
    return value


def _configuration_addresses(module: Mapping[str, Any], prefix: str = "") -> set[str]:
    result: set[str] = set()
    resources = module.get("resources", [])
    if not isinstance(resources, list):
        raise ContextError("Terraform configuration resources are malformed")
    for raw_resource in resources:
        resource = _mapping(raw_resource, label="Terraform configuration resource")
        address = resource.get("address")
        if not isinstance(address, str) or not address:
            raise ContextError("Terraform configuration resource address is invalid")
        result.add(f"{prefix}{address}")
    module_calls = module.get("module_calls", {})
    if not isinstance(module_calls, dict):
        raise ContextError("Terraform configuration module calls are malformed")
    for name, raw_call in module_calls.items():
        if not isinstance(name, str) or not name:
            raise ContextError("Terraform module call name is invalid")
        call = _mapping(raw_call, label=f"Terraform module call {name}")
        child = _mapping(call.get("module"), label=f"Terraform module call {name}.module")
        result.update(_configuration_addresses(child, f"{prefix}module.{name}."))
    return result


def _configuration_address(address: str) -> str:
    return INSTANCE_SELECTOR_RE.sub("", address)


def _raw_state_addresses(state: Mapping[str, Any]) -> list[str]:
    raw_resources = state.get("resources")
    if not isinstance(raw_resources, list):
        raise ContextError("Terraform state resources are malformed")
    addresses: set[str] = set()
    for index, raw_resource in enumerate(raw_resources):
        resource = _mapping(
            raw_resource,
            label=f"Terraform state resource[{index}]",
        )
        mode = resource.get("mode", "managed")
        if mode == "data":
            continue
        if mode != "managed":
            raise ContextError("Terraform state resource mode is unsupported")
        resource_type = resource.get("type")
        name = resource.get("name")
        module = resource.get("module")
        if (
            not isinstance(resource_type, str)
            or not resource_type
            or not isinstance(name, str)
            or not name
            or (module is not None and (not isinstance(module, str) or not module))
        ):
            raise ContextError("Terraform state resource identity is invalid")
        base = f"{resource_type}.{name}"
        if module is not None:
            base = f"{module}.{base}"
        instances = resource.get("instances")
        if not isinstance(instances, list) or not instances:
            raise ContextError(f"Terraform state resource has no instances: {base}")
        for raw_instance in instances:
            instance = _mapping(
                raw_instance,
                label=f"Terraform state resource instance {base}",
            )
            index_key = instance.get("index_key")
            if index_key is None:
                address = base
            elif isinstance(index_key, str):
                address = (
                    f"{base}[{json.dumps(index_key, ensure_ascii=False, separators=(',', ':'))}]"
                )
            elif isinstance(index_key, int) and not isinstance(index_key, bool):
                if index_key < 0:
                    raise ContextError("Terraform state instance index is negative")
                address = f"{base}[{index_key}]"
            else:
                raise ContextError("Terraform state instance index is invalid")
            addresses.add(address)
    if not addresses:
        raise ContextError("Terraform state has no managed resource ownership")
    return sorted(addresses)


def _state_binding(state: Mapping[str, Any]) -> dict[str, Any]:
    if state.get("version") != 4:
        raise ContextError("Terraform state version is not supported")
    lineage = state.get("lineage")
    try:
        if not isinstance(lineage, str) or str(uuid.UUID(lineage)) != lineage:
            raise ValueError
    except ValueError as exc:
        raise ContextError("Terraform state lineage is not a canonical UUID") from exc
    serial = state.get("serial")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 0:
        raise ContextError("Terraform state serial is invalid")
    addresses = _raw_state_addresses(state)
    return {
        "lineage": lineage,
        "serial": serial,
        "managed_address_count": len(addresses),
        "managed_addresses_sha256": hashlib.sha256(_canonical_bytes(addresses)).hexdigest(),
        "_addresses": addresses,
    }


def _backend_binding(metadata: Mapping[str, Any]) -> dict[str, Any]:
    backend = _mapping(metadata.get("backend"), label="Terraform backend metadata")
    backend_type = backend.get("type")
    config = _mapping(backend.get("config"), label="Terraform backend config")
    actual = {
        "type": backend_type,
        "bucket": config.get("bucket"),
        "key": config.get("key"),
        "region": config.get("region"),
        "dynamodb_table": config.get("dynamodb_table"),
        "encrypt": config.get("encrypt"),
    }
    if actual != EXPECTED_BACKEND:
        raise ContextError("Terraform backend identity does not match the fixed state")
    return actual


def build_context(
    *,
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
    backend_metadata: Mapping[str, Any],
    workspace: str,
) -> dict[str, Any]:
    if workspace != EXPECTED_WORKSPACE:
        raise ContextError("Terraform workspace must be exactly default")
    if plan.get("complete") is not True:
        raise ContextError("Terraform saved plan is incomplete")
    if plan.get("errored") is not False:
        raise ContextError("Terraform saved plan is errored or lacks an errored=false marker")
    if plan.get("applyable") is not True:
        raise ContextError("Terraform saved plan is not applyable")

    state_binding = _state_binding(state)
    state_addresses = set(state_binding.pop("_addresses"))
    configuration = _mapping(
        plan.get("configuration"),
        label="Terraform plan configuration",
    )
    configuration_root = _mapping(
        configuration.get("root_module"),
        label="Terraform plan configuration root",
    )
    configuration_addresses = _configuration_addresses(configuration_root)
    raw_changes = plan.get("resource_changes")
    if not isinstance(raw_changes, list):
        raise ContextError("Terraform saved plan lacks resource changes")
    ownership: list[dict[str, Any]] = []
    for index, raw_change in enumerate(raw_changes):
        change = _mapping(raw_change, label=f"Terraform resource change[{index}]")
        if change.get("mode", "managed") != "managed":
            continue
        address = change.get("address")
        if not isinstance(address, str) or not address:
            raise ContextError("Terraform managed resource change address is invalid")
        details = _mapping(
            change.get("change"),
            label=f"Terraform resource change {address}",
        )
        importing = details.get("importing")
        if importing is not None and importing is not False:
            raise ContextError("image release plans cannot contain import operations")
        actions = details.get("actions")
        if (
            not isinstance(actions, list)
            or not actions
            or any(
                action not in {"no-op", "create", "read", "update", "delete"} for action in actions
            )
        ):
            raise ContextError(f"Terraform actions are invalid for {address}")
        in_state = address in state_addresses
        in_configuration = (
            address in configuration_addresses
            or _configuration_address(address) in configuration_addresses
        )
        if not in_state and "create" not in actions:
            raise ContextError(f"Terraform change is not owned by the bound state: {address}")
        if "create" in actions and not in_configuration:
            raise ContextError(
                f"Terraform create is not owned by the reviewed configuration: {address}"
            )
        ownership.append(
            {
                "address": address,
                "actions": actions,
                "state_owned": in_state,
                "configuration_owned": in_configuration,
            }
        )
    ownership.sort(key=lambda item: item["address"])
    if not ownership:
        raise ContextError("Terraform saved plan has no managed resource ownership")
    if len({item["address"] for item in ownership}) != len(ownership):
        raise ContextError("Terraform saved plan has duplicate managed addresses")
    gate_changes = [
        item
        for item in ownership
        if item["address"] == "terraform_data.production_image_release_gate"
    ]
    if len(gate_changes) != 1 or "create" not in gate_changes[0]["actions"]:
        raise ContextError("Terraform saved plan will not run the production release gate")

    return {
        "schema_version": CONTEXT_SCHEMA,
        "kind": CONTEXT_KIND,
        "backend": _backend_binding(backend_metadata),
        "workspace": workspace,
        "state": state_binding,
        "plan": {
            "complete": True,
            "applyable": True,
            "errored": False,
            "managed_change_count": len(ownership),
            "address_ownership_sha256": hashlib.sha256(_canonical_bytes(ownership)).hexdigest(),
        },
    }


def validate_context(value: Any) -> dict[str, Any]:
    context = _mapping(value, label="Terraform image release context")
    expected_keys = {"schema_version", "kind", "backend", "workspace", "state", "plan"}
    if set(context) != expected_keys:
        raise ContextError("Terraform image release context schema mismatch")
    if (
        context["schema_version"] != CONTEXT_SCHEMA
        or context["kind"] != CONTEXT_KIND
        or context["workspace"] != EXPECTED_WORKSPACE
    ):
        raise ContextError("Terraform image release context identity mismatch")
    if dict(_mapping(context["backend"], label="Terraform context backend")) != EXPECTED_BACKEND:
        raise ContextError("Terraform context backend identity mismatch")
    state = _mapping(context["state"], label="Terraform context state")
    if set(state) != {
        "lineage",
        "serial",
        "managed_address_count",
        "managed_addresses_sha256",
    }:
        raise ContextError("Terraform context state schema mismatch")
    try:
        if str(uuid.UUID(str(state["lineage"]))) != state["lineage"]:
            raise ValueError
    except ValueError as exc:
        raise ContextError("Terraform context lineage is invalid") from exc
    if (
        not isinstance(state["serial"], int)
        or isinstance(state["serial"], bool)
        or state["serial"] < 0
        or not isinstance(state["managed_address_count"], int)
        or state["managed_address_count"] <= 0
    ):
        raise ContextError("Terraform context state counters are invalid")
    _sha256(
        state["managed_addresses_sha256"],
        label="Terraform context managed address hash",
    )
    plan = _mapping(context["plan"], label="Terraform context plan")
    if set(plan) != {
        "complete",
        "applyable",
        "errored",
        "managed_change_count",
        "address_ownership_sha256",
    }:
        raise ContextError("Terraform context plan schema mismatch")
    if (
        plan["complete"] is not True
        or plan["applyable"] is not True
        or plan["errored"] is not False
        or not isinstance(plan["managed_change_count"], int)
        or plan["managed_change_count"] <= 0
    ):
        raise ContextError("Terraform context plan markers are invalid")
    _sha256(
        plan["address_ownership_sha256"],
        label="Terraform context address ownership hash",
    )
    return dict(context)


def context_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(validate_context(value))).hexdigest()


def _run(terraform_dir: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["terraform", *arguments],
            cwd=terraform_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ContextError(f"Terraform command failed: {' '.join(arguments)}") from exc
    return completed.stdout


def capture(terraform_dir: Path, plan_path: Path) -> dict[str, Any]:
    metadata_path = terraform_dir / ".terraform" / "terraform.tfstate"
    backend_metadata = _mapping(
        _load(metadata_path, label="Terraform backend metadata"),
        label="Terraform backend metadata",
    )
    plan = _mapping(
        _loads(
            _run(terraform_dir, "show", "-json", str(plan_path)),
            label="Terraform saved plan",
        ),
        label="Terraform saved plan",
    )
    state = _mapping(
        _loads(_run(terraform_dir, "state", "pull"), label="Terraform live state"),
        label="Terraform live state",
    )
    workspace = _run(terraform_dir, "workspace", "show").strip()
    return build_context(
        plan=plan,
        state=state,
        backend_metadata=backend_metadata,
        workspace=workspace,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    capture_command = commands.add_parser("capture")
    capture_command.add_argument("--terraform-dir", type=Path, required=True)
    capture_command.add_argument("--plan", type=Path, required=True)
    capture_command.add_argument("--output", type=Path, required=True)
    validate_command = commands.add_parser("validate")
    validate_command.add_argument("--context", type=Path, required=True)
    hash_command = commands.add_parser("sha256")
    hash_command.add_argument("--context", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "capture":
            value = capture(args.terraform_dir.resolve(), args.plan.resolve())
            args.output.write_bytes(_canonical_bytes(value))
        elif args.command == "validate":
            validate_context(_load(args.context, label="Terraform context"))
        else:
            print(context_sha256(_load(args.context, label="Terraform context")))
    except ContextError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
