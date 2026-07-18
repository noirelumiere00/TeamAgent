#!/usr/bin/env python3
"""Validate the narrowly scoped Terraform plan that installs release contracts."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CONTROL_UPDATE_KIND = "teamagent.image-release-control-update"
CONTROL_UPDATE_SCHEMA = 1
SHA1_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")

CONTRACT_PATHS = {
    "mcp_runtime": "infra/codebuild/teamagent_runtime_contract.json",
    "mcp_release": "infra/codebuild/teamagent_core_media_release_contract.json",
    "tiktok": "infra/codebuild/tiktok_release_contract.json",
    "openclaw": "infra/codebuild/openclaw_bundle_contract.json",
}
PROJECT_ADDRESSES = {
    "image": "aws_codebuild_project.image",
    "tiktok": "aws_codebuild_project.tiktok_image[0]",
    "mcp_source_publisher": "aws_codebuild_project.mcp_source_publisher",
    "attestor": "aws_codebuild_project.image_attestor",
    "openclaw": "aws_codebuild_project.openclaw_provenance",
}
CONTRACT_CONSUMERS = {
    "mcp_runtime": {
        PROJECT_ADDRESSES["image"]: "sha256",
        PROJECT_ADDRESSES["mcp_source_publisher"]: "base64",
    },
    "mcp_release": {
        PROJECT_ADDRESSES["image"]: "sha256",
        PROJECT_ADDRESSES["mcp_source_publisher"]: "base64",
        PROJECT_ADDRESSES["attestor"]: "base64",
    },
    "tiktok": {
        PROJECT_ADDRESSES["tiktok"]: "base64",
        PROJECT_ADDRESSES["attestor"]: "base64",
    },
    "openclaw": {
        PROJECT_ADDRESSES["openclaw"]: "sha256",
        PROJECT_ADDRESSES["attestor"]: "base64",
    },
}
ALLOWED_ENVIRONMENT_HASHES = {
    "SOURCE_MANIFEST_CONTRACT_SHA256",
    "RELEASE_CONTRACT_SHA256",
}


class ControlUpdateError(ValueError):
    """A contract-control plan could mutate outside its authorization."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ControlUpdateError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _loads(value: str, *, label: str) -> Any:
    try:
        return json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ControlUpdateError(f"{label} is not valid JSON") from exc


def _load(path: Path, *, label: str) -> Any:
    try:
        return _loads(path.read_text(encoding="utf-8"), label=label)
    except (OSError, UnicodeDecodeError) as exc:
        raise ControlUpdateError(f"cannot read {label}: {path}") from exc


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ControlUpdateError(f"{label} must be an object")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _terraform_show(terraform_dir: Path, plan: Path) -> Mapping[str, Any]:
    try:
        completed = subprocess.run(
            ["terraform", "show", "-json", str(plan)],
            cwd=terraform_dir,
            check=True,
            capture_output=True,
            text=True,
            env={
                "PATH": os.environ.get("PATH", ""),
                "TF_IN_AUTOMATION": "1",
            },
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ControlUpdateError("saved control-update plan cannot be inspected") from exc
    return _mapping(
        _loads(completed.stdout, label="saved control-update plan"),
        label="saved control-update plan",
    )


def _resources(module: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_resources = module.get("resources", [])
    if not isinstance(raw_resources, list):
        raise ControlUpdateError("planned resources are malformed")
    result = [_mapping(resource, label="planned resource") for resource in raw_resources]
    raw_children = module.get("child_modules", [])
    if not isinstance(raw_children, list):
        raise ControlUpdateError("planned child modules are malformed")
    for child in raw_children:
        result.extend(_resources(_mapping(child, label="planned child module")))
    return result


def _contains_unknown(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, dict):
        return any(_contains_unknown(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_unknown(item) for item in value)
    return False


def _changed_paths(
    before: Any,
    after: Any,
    prefix: tuple[str | int, ...] = (),
) -> set[tuple[str | int, ...]]:
    if isinstance(before, dict) and isinstance(after, dict):
        result: set[tuple[str | int, ...]] = set()
        for key in before.keys() | after.keys():
            result.update(
                _changed_paths(
                    before.get(key),
                    after.get(key),
                    (*prefix, key),
                )
            )
        return result
    if isinstance(before, list) and isinstance(after, list):
        result = set()
        for index in range(max(len(before), len(after))):
            old = before[index] if index < len(before) else None
            new = after[index] if index < len(after) else None
            result.update(_changed_paths(old, new, (*prefix, index)))
        return result
    return set() if before == after else {prefix}


def _environment_variables(project: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    environment = project.get("environment")
    if not isinstance(environment, list) or len(environment) != 1:
        raise ControlUpdateError("CodeBuild project environment is malformed")
    raw_variables = _mapping(
        environment[0],
        label="CodeBuild project environment",
    ).get("environment_variable", [])
    if not isinstance(raw_variables, list):
        raise ControlUpdateError("CodeBuild project environment variables are malformed")
    variables: dict[str, Mapping[str, Any]] = {}
    for raw_variable in raw_variables:
        variable = _mapping(raw_variable, label="CodeBuild environment variable")
        name = variable.get("name")
        if not isinstance(name, str) or name in variables:
            raise ControlUpdateError("CodeBuild environment variable identity is invalid")
        variables[name] = variable
    return variables


def _buildspec(project: Mapping[str, Any], *, address: str) -> str:
    source = project.get("source")
    if not isinstance(source, list) or len(source) != 1:
        raise ControlUpdateError(f"{address} source is malformed")
    buildspec = _mapping(source[0], label=f"{address} source").get("buildspec")
    if not isinstance(buildspec, str) or not buildspec:
        raise ControlUpdateError(f"{address} buildspec is missing")
    return buildspec


def contract_markers(repo_root: Path) -> dict[str, dict[str, str]]:
    markers: dict[str, dict[str, str]] = {}
    for name, relative in CONTRACT_PATHS.items():
        try:
            body = (repo_root / relative).read_bytes()
        except OSError as exc:
            raise ControlUpdateError(f"release contract is missing: {relative}") from exc
        _mapping(
            _loads(body.decode("utf-8"), label=f"{name} contract"),
            label=f"{name} contract",
        )
        markers[name] = {
            "sha256": hashlib.sha256(body).hexdigest(),
            "base64": base64.b64encode(body).decode("ascii"),
        }
    return markers


def _validate_mutation(
    address: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    changed = _changed_paths(before, after)
    for path in changed:
        if path == ("source", 0, "buildspec"):
            continue
        if (
            address == PROJECT_ADDRESSES["image"]
            and len(path) == 5
            and path[:3] == ("environment", 0, "environment_variable")
            and isinstance(path[3], int)
            and path[4] == "value"
        ):
            before_variables = _environment_variables(before)
            after_variables = _environment_variables(after)
            if before_variables.keys() != after_variables.keys():
                raise ControlUpdateError("CodeBuild environment variable set changed")
            changed_names = {
                name for name in before_variables if before_variables[name] != after_variables[name]
            }
            if not changed_names or not changed_names <= ALLOWED_ENVIRONMENT_HASHES:
                raise ControlUpdateError(
                    "control update changed a non-contract environment variable"
                )
            continue
        raise ControlUpdateError(
            f"control update changes unauthorized project field: {address}:{path}"
        )


def validate_plan(
    plan_path: Path,
    *,
    repo_root: Path,
    control_commit: str,
    plan_json: Mapping[str, Any] | None = None,
    terraform_dir: Path | None = None,
) -> dict[str, Any]:
    if not SHA1_RE.fullmatch(control_commit):
        raise ControlUpdateError("control commit must be a full lowercase Git SHA")
    try:
        plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ControlUpdateError("saved control-update plan cannot be read") from exc
    plan = plan_json or _terraform_show(
        terraform_dir or repo_root / "infra" / "terraform",
        plan_path,
    )
    if (
        plan.get("complete") is not True
        or plan.get("errored") is not False
        or plan.get("applyable") is not True
    ):
        raise ControlUpdateError("control-update plan is incomplete or not applyable")

    planned_values = _mapping(
        plan.get("planned_values"),
        label="planned control-update values",
    )
    root_module = _mapping(
        planned_values.get("root_module"),
        label="planned control-update root module",
    )
    planned_projects = {
        str(resource.get("address")): _mapping(
            resource.get("values"),
            label=f"{resource.get('address')} planned values",
        )
        for resource in _resources(root_module)
        if resource.get("address") in set(PROJECT_ADDRESSES.values())
    }
    missing_projects = sorted(set(PROJECT_ADDRESSES.values()) - planned_projects.keys())
    if missing_projects:
        raise ControlUpdateError(
            f"control-update plan omits embedded-contract consumers: {missing_projects}"
        )

    raw_changes = plan.get("resource_changes")
    if not isinstance(raw_changes, list):
        raise ControlUpdateError("control-update resource changes are missing")
    changed_addresses: list[str] = []
    before_projects: dict[str, Mapping[str, Any]] = {}
    for index, raw_change in enumerate(raw_changes):
        change = _mapping(raw_change, label=f"control-update change[{index}]")
        details = _mapping(
            change.get("change"),
            label=f"control-update change[{index}] details",
        )
        if details.get("importing") not in {None, False}:
            raise ControlUpdateError("control-update plans cannot contain imports")
        actions = details.get("actions")
        if actions == ["no-op"]:
            address = change.get("address")
            if address in set(PROJECT_ADDRESSES.values()):
                before_projects[str(address)] = _mapping(
                    details.get("before"),
                    label=f"{address} prior values",
                )
            continue
        address = change.get("address")
        if address not in set(PROJECT_ADDRESSES.values()) or actions != ["update"]:
            raise ControlUpdateError(
                "control update may only update embedded-contract CodeBuild projects"
            )
        if _contains_unknown(details.get("after_unknown", {})):
            raise ControlUpdateError("control-update project values contain unknowns")
        before = _mapping(details.get("before"), label=f"{address} prior values")
        after = _mapping(details.get("after"), label=f"{address} planned values")
        _validate_mutation(str(address), before, after)
        if after != planned_projects[address]:
            raise ControlUpdateError("resource change differs from planned project values")
        before_projects[str(address)] = before
        changed_addresses.append(str(address))
    if not changed_addresses:
        raise ControlUpdateError("control-update plan contains no contract installation")
    if set(PROJECT_ADDRESSES.values()) - before_projects.keys():
        raise ControlUpdateError("control-update plan lacks prior consumer values")

    markers = contract_markers(repo_root)
    changed_contracts: list[str] = []
    for contract, consumers in CONTRACT_CONSUMERS.items():
        marker = markers[contract]
        changed = False
        for address, marker_kind in consumers.items():
            expected = marker[marker_kind]
            if expected not in _buildspec(planned_projects[address], address=address):
                raise ControlUpdateError(
                    f"{address} does not embed the current {contract} contract"
                )
            if expected not in _buildspec(before_projects[address], address=address):
                changed = True
        if changed:
            changed_contracts.append(contract)
    if not changed_contracts:
        raise ControlUpdateError(
            "control-update stage requires at least one changed embedded contract"
        )

    return {
        "schema_version": CONTROL_UPDATE_SCHEMA,
        "kind": CONTROL_UPDATE_KIND,
        "control_commit": control_commit,
        "plan_sha256": plan_sha256,
        "contract_sha256": {name: values["sha256"] for name, values in sorted(markers.items())},
        "changed_contracts": sorted(changed_contracts),
        "changed_addresses": sorted(changed_addresses),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("authorize", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--terraform-dir", type=Path, required=True)
        command.add_argument("--repo-root", type=Path, required=True)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--control-commit", required=True)
        command.add_argument("--authorization", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        value = validate_plan(
            args.plan.resolve(),
            repo_root=args.repo_root.resolve(),
            control_commit=args.control_commit,
            terraform_dir=args.terraform_dir.resolve(),
        )
        if args.command == "authorize":
            if args.authorization.exists():
                raise ControlUpdateError("control-update authorization already exists")
            args.authorization.write_bytes(_canonical_bytes(value))
        else:
            authorized = _mapping(
                _load(args.authorization, label="control-update authorization"),
                label="control-update authorization",
            )
            if dict(authorized) != value:
                raise ControlUpdateError("saved plan or contract-control authorization has changed")
    except ControlUpdateError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
