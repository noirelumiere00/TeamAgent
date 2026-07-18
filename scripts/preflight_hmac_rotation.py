#!/usr/bin/env python3
"""Secret-free HMAC rotation and rendered-task preflight.

The input contains only stable generation identifiers (for example
``SecretsManager ARN@VersionId``), fixed timestamps, and task/domain mappings. It must never contain
secret values. Output is deliberately limited to result codes and scopes so generation identifiers
cannot be copied into logs by this command.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from teamagent.hmac_keyring import (
    MAIL_ACTION_MAX_TOKEN_TTL_S,
    REPORT_LINK_MAX_TOKEN_TTL_S,
    validate_hmac_rotation_transition,
)

_DOMAIN_MAX_TTLS = {
    "mail_action": MAIL_ACTION_MAX_TOKEN_TTL_S,
    "report_link": REPORT_LINK_MAX_TOKEN_TTL_S,
}
_TASK_DOMAINS = {
    "mcp": frozenset({"mail_action", "report_link"}),
    "morning_digest": frozenset({"mail_action"}),
    "connect_web": frozenset({"report_link"}),
    "worker": frozenset({"mail_action", "report_link"}),
}
_RENDERED_ECS_TASKS = frozenset({"mcp", "morning_digest", "connect_web"})
_CONFIG_KEYS = frozenset(
    {
        "primary_generation",
        "previous_generation",
        "rotation_started_at",
    }
)
_DOMAIN_ENV_NAMES = {
    "mail_action": {
        "primary_generation": "MAIL_ACTION_HMAC_PRIMARY_GENERATION",
        "previous_generation": "MAIL_ACTION_HMAC_PREVIOUS_GENERATION",
        "rotation_started_at": "MAIL_ACTION_HMAC_PREVIOUS_ROTATION_STARTED_AT",
        "legacy_marker": "MAIL_ACTION_HMAC_PREVIOUS_IS_LEGACY",
        "ttl": "MAIL_ACTION_TTL_S",
        "primary_secret": "MAIL_ACTION_HMAC_SECRET",
        "previous_secret": "MAIL_ACTION_HMAC_PREVIOUS_SECRET",
    },
    "report_link": {
        "primary_generation": "REPORT_LINK_HMAC_PRIMARY_GENERATION",
        "previous_generation": "REPORT_LINK_HMAC_PREVIOUS_GENERATION",
        "rotation_started_at": "REPORT_LINK_HMAC_PREVIOUS_ROTATION_STARTED_AT",
        "legacy_marker": "REPORT_LINK_HMAC_PREVIOUS_IS_LEGACY",
        "ttl": "REPORT_LINK_TTL_S",
        "primary_secret": "REPORT_LINK_HMAC_SECRET",
        "previous_secret": "REPORT_LINK_HMAC_PREVIOUS_SECRET",
    },
}
_WORKER_EXPORT_RE = re.compile(r"^export ([A-Z][A-Z0-9_]*)='([^']*)'$")
_HMAC_NAME_PREFIXES = ("MAIL_ACTION_HMAC_", "REPORT_LINK_HMAC_")
_HMAC_TTL_NAMES = frozenset({"MAIL_ACTION_TTL_S", "REPORT_LINK_TTL_S"})
_WORKER_RUNTIME_NAMES = frozenset(
    {
        "TEAMAGENT_HMAC_STATE_REQUIRED",
        "TEAMAGENT_HMAC_STATE_TABLE",
        "TEAMAGENT_HMAC_STATE_SCOPE",
        "TEAMAGENT_HMAC_ROTATION_EPOCH",
        "TEAMAGENT_HMAC_PROVENANCE",
        "TEAMAGENT_HMAC_ARTIFACT_SHA256",
        "TEAMAGENT_HMAC_WORKER_ID",
    }
)
_WORKER_TABLE_RE = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
_WORKER_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_WORKER_EPOCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_WORKER_PROVENANCE_RE = re.compile(r"^[a-f0-9]{64}$")
_WORKER_ID_RE = re.compile(r"^i-[a-f0-9]{8,32}$")


def _result(ok: bool, code: str, *, scope: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"ok": ok, "code": code}
    if scope is not None:
        result["scope"] = scope
    return result


def _mapping(value: object) -> dict[str, Any] | None:
    if type(value) is not dict:
        return None
    return value


def _config(value: object) -> dict[str, Any] | None:
    config = _mapping(value)
    if config is None or frozenset(config) != _CONFIG_KEYS:
        return None
    return config


def _stable_generation(value: object) -> str | None:
    if type(value) is not str or not value or len(value) > 2048 or value != value.strip():
        return None
    if any(ord(char) < 0x21 or ord(char) > 0x7E for char in value):
        return None
    return value


def _generation_resource(generation: object) -> str | None:
    stable = _stable_generation(generation)
    if stable is None:
        return None
    resource, separator, version_id = stable.rpartition("@")
    if not separator or not resource or not version_id:
        return None
    return resource


def validate_manifest(manifest: object) -> dict[str, object]:
    """Validate one secret-free transition manifest without exposing its identifiers."""
    root = _mapping(manifest)
    if root is None or frozenset(root) != frozenset(
        {"now", "legacy_database_generation", "domains", "tasks"}
    ):
        return _result(False, "invalid_manifest")

    legacy_database_generation = root["legacy_database_generation"]
    if _stable_generation(legacy_database_generation) is None:
        return _result(False, "invalid_legacy_generation")
    legacy_database_resource = _generation_resource(legacy_database_generation)
    if legacy_database_resource is None:
        return _result(False, "invalid_legacy_generation")

    domains = _mapping(root["domains"])
    if domains is None or frozenset(domains) != frozenset(_DOMAIN_MAX_TTLS):
        return _result(False, "invalid_domains")

    proposed_configs: dict[str, dict[str, Any]] = {}
    for domain, max_ttl in _DOMAIN_MAX_TTLS.items():
        domain_config = _mapping(domains[domain])
        if domain_config is None or frozenset(domain_config) != frozenset({"deployed", "proposed"}):
            return _result(False, "invalid_domain_config", scope=domain)
        deployed = _config(domain_config["deployed"])
        proposed = _config(domain_config["proposed"])
        if deployed is None or proposed is None:
            return _result(False, "invalid_generation_config", scope=domain)
        if _generation_resource(proposed["primary_generation"]) == legacy_database_resource:
            return _result(False, "legacy_primary_forbidden", scope=domain)

        transition = validate_hmac_rotation_transition(
            deployed_primary_generation=deployed["primary_generation"],
            deployed_previous_generation=deployed["previous_generation"],
            deployed_rotation_started_at=deployed["rotation_started_at"],
            proposed_primary_generation=proposed["primary_generation"],
            proposed_previous_generation=proposed["previous_generation"],
            proposed_rotation_started_at=proposed["rotation_started_at"],
            now=root["now"],
            max_token_ttl_s=max_ttl,
        )
        if not transition["ok"]:
            return _result(False, transition["code"], scope=domain)
        proposed_configs[domain] = proposed

    mail_primary_resource = _generation_resource(
        proposed_configs["mail_action"]["primary_generation"]
    )
    report_primary_resource = _generation_resource(
        proposed_configs["report_link"]["primary_generation"]
    )
    if mail_primary_resource is None or report_primary_resource is None:
        return _result(False, "invalid_generation_config")
    if mail_primary_resource == report_primary_resource:
        return _result(False, "purpose_generation_reuse")

    tasks = _mapping(root["tasks"])
    if tasks is None or frozenset(tasks) != frozenset(_TASK_DOMAINS):
        return _result(False, "invalid_tasks")
    for task, required_domains in _TASK_DOMAINS.items():
        task_config = _mapping(tasks[task])
        if task_config is None or frozenset(task_config) != required_domains:
            return _result(False, "invalid_task_domains", scope=task)
        for domain in required_domains:
            rendered = _config(task_config[domain])
            if rendered is None or rendered != proposed_configs[domain]:
                return _result(False, "task_generation_drift", scope=task)

    return _result(True, "ok")


def _named_values(entries: object, *, value_key: str) -> dict[str, object] | None:
    if type(entries) is not list:
        return None
    values: dict[str, object] = {}
    for entry in entries:
        item = _mapping(entry)
        if item is None or type(item.get("name")) is not str or value_key not in item:
            return None
        name = item["name"]
        if name in values:
            return None
        values[name] = item[value_key]
    return values


def _is_hmac_name(name: str) -> bool:
    return name.startswith(_HMAC_NAME_PREFIXES) or name in _HMAC_TTL_NAMES


def _expected_rendered_hmac_names(
    *,
    task: str,
    expected_task: dict[str, Any],
    legacy_database_generation: str,
) -> tuple[frozenset[str], frozenset[str]] | None:
    environment: set[str] = set()
    secrets: set[str] = set()
    for domain in _TASK_DOMAINS[task]:
        config = _config(expected_task.get(domain))
        if config is None:
            return None
        names = _DOMAIN_ENV_NAMES[domain]
        environment.update({names["primary_generation"], names["ttl"]})
        secrets.add(names["primary_secret"])
        previous_generation = config["previous_generation"]
        if previous_generation is None:
            continue
        environment.update(
            {
                names["previous_generation"],
                names["rotation_started_at"],
            }
        )
        secrets.add(names["previous_secret"])
        if previous_generation == legacy_database_generation:
            environment.add(names["legacy_marker"])
    return frozenset(environment), frozenset(secrets)


def _pinned_reference_matches_generation(reference: object, generation: object) -> bool:
    if type(reference) is not str or type(generation) is not str:
        return False
    reference_resource, reference_separator, version_id = reference.rpartition(":::")
    generation_resource, generation_separator, generation_version = generation.rpartition("@")
    return (
        bool(reference_separator)
        and bool(generation_separator)
        and bool(reference_resource)
        and 32 <= len(version_id) <= 64
        and all(char.isascii() and (char.isalnum() or char in "_-") for char in version_id)
        and version_id == generation_version
        and reference_resource == generation_resource
    )


def _rendered_domain_config(
    *,
    domain: str,
    environment: dict[str, object],
    secrets: dict[str, object],
    legacy_database_generation: str,
) -> dict[str, object] | None:
    names = _DOMAIN_ENV_NAMES[domain]
    primary_generation = environment.get(names["primary_generation"])
    primary_reference = secrets.get(names["primary_secret"])
    if not _pinned_reference_matches_generation(primary_reference, primary_generation):
        return None
    if names["primary_secret"] in environment:
        return None

    raw_ttl = environment.get(names["ttl"])
    if (
        type(raw_ttl) is not str
        or len(raw_ttl) > len(str(_DOMAIN_MAX_TTLS[domain]))
        or not raw_ttl.isascii()
        or not raw_ttl.isdecimal()
    ):
        return None
    ttl = int(raw_ttl)
    if ttl < 1 or ttl > _DOMAIN_MAX_TTLS[domain]:
        return None

    previous_generation = environment.get(names["previous_generation"])
    previous_reference = secrets.get(names["previous_secret"])
    rotation_started_at = environment.get(names["rotation_started_at"])
    legacy_marker = environment.get(names["legacy_marker"])
    previous_fields = (
        previous_generation,
        previous_reference,
        rotation_started_at,
    )
    if all(value is None for value in previous_fields):
        if legacy_marker is not None or names["previous_secret"] in environment:
            return None
        return {
            "primary_generation": primary_generation,
            "previous_generation": None,
            "rotation_started_at": None,
        }
    if any(value is None for value in previous_fields):
        return None
    if previous_generation == legacy_database_generation:
        if legacy_marker != "1":
            return None
    elif legacy_marker is not None:
        return None
    if not _pinned_reference_matches_generation(previous_reference, previous_generation):
        return None
    if names["previous_secret"] in environment:
        return None
    if (
        type(rotation_started_at) is not str
        or len(rotation_started_at) > 10
        or not rotation_started_at.isascii()
        or not rotation_started_at.isdecimal()
    ):
        return None
    return {
        "primary_generation": primary_generation,
        "previous_generation": previous_generation,
        "rotation_started_at": int(rotation_started_at),
    }


def validate_rendered_tasks(
    manifest: object,
    rendered_tasks: dict[str, object],
) -> dict[str, object]:
    """Validate selected rendered ECS task definitions against a valid manifest."""
    manifest_result = validate_manifest(manifest)
    if not manifest_result["ok"]:
        return manifest_result
    root = _mapping(manifest)
    if root is None:
        return _result(False, "invalid_manifest")
    expected_tasks = _mapping(root["tasks"])
    if expected_tasks is None:
        return _result(False, "invalid_tasks")
    legacy_database_generation = root["legacy_database_generation"]
    if type(legacy_database_generation) is not str:
        return _result(False, "invalid_legacy_generation")

    for task, definition_value in rendered_tasks.items():
        if task not in _RENDERED_ECS_TASKS:
            return _result(False, "unknown_rendered_task", scope=task)
        definition = _mapping(definition_value)
        if definition is None:
            return _result(False, "invalid_rendered_task", scope=task)
        if "taskDefinition" in definition:
            definition = _mapping(definition["taskDefinition"])
            if definition is None:
                return _result(False, "invalid_rendered_task", scope=task)
        containers = definition.get("containerDefinitions")
        if type(containers) is not list or len(containers) != 1:
            return _result(False, "invalid_rendered_task", scope=task)
        container = _mapping(containers[0])
        if container is None:
            return _result(False, "invalid_rendered_task", scope=task)
        environment = _named_values(container.get("environment"), value_key="value")
        secrets = _named_values(container.get("secrets"), value_key="valueFrom")
        expected_task = _mapping(expected_tasks[task])
        if environment is None or secrets is None or expected_task is None:
            return _result(False, "invalid_rendered_task", scope=task)
        expected_names = _expected_rendered_hmac_names(
            task=task,
            expected_task=expected_task,
            legacy_database_generation=legacy_database_generation,
        )
        if expected_names is None:
            return _result(False, "invalid_rendered_task", scope=task)
        expected_environment_names, expected_secret_names = expected_names
        rendered_environment_names = frozenset(name for name in environment if _is_hmac_name(name))
        rendered_secret_names = frozenset(name for name in secrets if _is_hmac_name(name))
        if (
            rendered_environment_names != expected_environment_names
            or rendered_secret_names != expected_secret_names
        ):
            return _result(False, "rendered_task_drift", scope=task)
        for domain in _TASK_DOMAINS[task]:
            rendered = _rendered_domain_config(
                domain=domain,
                environment=environment,
                secrets=secrets,
                legacy_database_generation=legacy_database_generation,
            )
            if rendered is None or rendered != expected_task[domain]:
                return _result(False, "rendered_task_drift", scope=task)
    return _result(True, "ok")


def _worker_env_names(domain: str) -> dict[str, str]:
    prefix = "MAIL_ACTION" if domain == "mail_action" else "REPORT_LINK"
    return {
        "secret_name": f"{prefix}_HMAC_SECRET_NAME",
        "primary_version": f"{prefix}_HMAC_PRIMARY_VERSION_ID",
        "primary_generation": f"{prefix}_HMAC_PRIMARY_GENERATION",
        "previous_name": f"{prefix}_HMAC_PREVIOUS_SECRET_NAME",
        "previous_version": f"{prefix}_HMAC_PREVIOUS_VERSION_ID",
        "previous_generation": f"{prefix}_HMAC_PREVIOUS_GENERATION",
        "rotation_started_at": f"{prefix}_HMAC_PREVIOUS_ROTATION_STARTED_AT",
        "legacy_marker": f"{prefix}_HMAC_PREVIOUS_IS_LEGACY",
        "ttl": f"{prefix}_TTL_S",
    }


def _parse_worker_env(text: object) -> dict[str, str] | None:
    if type(text) is not str or len(text) > 65_536:
        return None
    expected_names = {
        name for domain in _DOMAIN_MAX_TTLS for name in _worker_env_names(domain).values()
    } | set(_WORKER_RUNTIME_NAMES)
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _WORKER_EXPORT_RE.fullmatch(line)
        if match is None or match.group(1) not in expected_names:
            return None
        name, value = match.groups()
        if name in values:
            return None
        values[name] = value
    return values if frozenset(values) == frozenset(expected_names) else None


def _worker_secret_ref_matches_generation(
    *,
    secret_name: str,
    version_id: str,
    generation: object,
) -> bool:
    if type(generation) is not str:
        return False
    resource, separator, generation_version = generation.rpartition("@")
    _arn_prefix, secret_separator, arn_secret_name = resource.partition(":secret:")
    return (
        bool(separator)
        and bool(secret_separator)
        and 32 <= len(version_id) <= 64
        and all(char.isascii() and (char.isalnum() or char in "_-") for char in version_id)
        and generation_version == version_id
        and (arn_secret_name == secret_name or arn_secret_name.startswith(f"{secret_name}-"))
    )


def validate_worker_env(manifest: object, worker_env_text: object) -> dict[str, object]:
    """Validate the exact secret-free EC2 hmac.env against the worker manifest entry."""
    manifest_result = validate_manifest(manifest)
    if not manifest_result["ok"]:
        return manifest_result
    root = _mapping(manifest)
    values = _parse_worker_env(worker_env_text)
    if root is None or values is None:
        return _result(False, "worker_env_drift", scope="worker")
    if (
        values["TEAMAGENT_HMAC_STATE_REQUIRED"] != "1"
        or _WORKER_TABLE_RE.fullmatch(values["TEAMAGENT_HMAC_STATE_TABLE"]) is None
        or _WORKER_SCOPE_RE.fullmatch(values["TEAMAGENT_HMAC_STATE_SCOPE"]) is None
        or _WORKER_EPOCH_RE.fullmatch(values["TEAMAGENT_HMAC_ROTATION_EPOCH"]) is None
        or _WORKER_PROVENANCE_RE.fullmatch(values["TEAMAGENT_HMAC_PROVENANCE"]) is None
        or _WORKER_PROVENANCE_RE.fullmatch(values["TEAMAGENT_HMAC_ARTIFACT_SHA256"]) is None
        or _WORKER_ID_RE.fullmatch(values["TEAMAGENT_HMAC_WORKER_ID"]) is None
    ):
        return _result(False, "worker_env_drift", scope="worker")
    tasks = _mapping(root["tasks"])
    if tasks is None:
        return _result(False, "invalid_tasks")
    worker = _mapping(tasks["worker"])
    legacy_generation = root["legacy_database_generation"]
    if worker is None or type(legacy_generation) is not str:
        return _result(False, "invalid_task_domains", scope="worker")

    primary_names: list[str] = []
    for domain, maximum_ttl in _DOMAIN_MAX_TTLS.items():
        expected = _config(worker[domain])
        if expected is None:
            return _result(False, "invalid_generation_config", scope="worker")
        names = _worker_env_names(domain)
        primary_name = values[names["secret_name"]]
        primary_version = values[names["primary_version"]]
        primary_generation = values[names["primary_generation"]]
        if (
            not primary_name
            or primary_name.endswith("/database-url")
            or primary_generation != expected["primary_generation"]
            or not _worker_secret_ref_matches_generation(
                secret_name=primary_name,
                version_id=primary_version,
                generation=primary_generation,
            )
        ):
            return _result(False, "worker_env_drift", scope="worker")
        primary_names.append(primary_name)

        raw_ttl = values[names["ttl"]]
        if (
            len(raw_ttl) > len(str(maximum_ttl))
            or not raw_ttl.isascii()
            or not raw_ttl.isdecimal()
            or not 1 <= int(raw_ttl) <= maximum_ttl
        ):
            return _result(False, "worker_env_drift", scope="worker")

        previous_generation = expected["previous_generation"]
        previous_values = (
            values[names["previous_name"]],
            values[names["previous_version"]],
            values[names["previous_generation"]],
            values[names["rotation_started_at"]],
        )
        marker = values[names["legacy_marker"]]
        if previous_generation is None:
            if any(previous_values) or marker:
                return _result(False, "worker_env_drift", scope="worker")
            continue
        if (
            any(not value for value in previous_values)
            or values[names["previous_generation"]] != previous_generation
            or values[names["rotation_started_at"]] != str(expected["rotation_started_at"])
            or not _worker_secret_ref_matches_generation(
                secret_name=values[names["previous_name"]],
                version_id=values[names["previous_version"]],
                generation=previous_generation,
            )
        ):
            return _result(False, "worker_env_drift", scope="worker")
        expected_marker = "1" if previous_generation == legacy_generation else ""
        if marker != expected_marker:
            return _result(False, "worker_env_drift", scope="worker")
        if (marker == "1") != values[names["previous_name"]].endswith("/database-url"):
            return _result(False, "worker_env_drift", scope="worker")

    if len(set(primary_names)) != len(primary_names):
        return _result(False, "worker_env_drift", scope="worker")
    return _result(True, "ok")


def _load_manifest(path: str) -> object:
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate secret-free HMAC generations, T0, and rendered task parity."
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="Path to the reviewed non-secret JSON manifest, or - for stdin.",
    )
    parser.add_argument(
        "--task-definition-json",
        action="append",
        default=[],
        metavar="TASK=PATH",
        help="Also validate a rendered ECS task definition (mcp, morning_digest, or connect_web).",
    )
    parser.add_argument(
        "--worker-env",
        help="Also validate the exact secret-free EC2 hmac.env file.",
    )
    parser.add_argument(
        "--refresh-manifest-now",
        action="store_true",
        help="Replace only manifest.now with the local clock for an immediate live-gate assertion.",
    )
    args = parser.parse_args(argv)
    try:
        manifest = _load_manifest(args.manifest)
        if args.refresh_manifest_now:
            root = _mapping(manifest)
            if root is None:
                raise ValueError("manifest must be an object")
            root["now"] = int(time.time())
        rendered_tasks: dict[str, object] = {}
        for item in args.task_definition_json:
            task, separator, path = item.partition("=")
            if not separator or not task or not path or task in rendered_tasks:
                result = _result(False, "invalid_task_argument")
                break
            rendered_tasks[task] = json.loads(Path(path).read_text(encoding="utf-8"))
        else:
            result = validate_manifest(manifest)
            if result["ok"] and rendered_tasks:
                result = validate_rendered_tasks(manifest, rendered_tasks)
            if result["ok"] and args.worker_env:
                result = validate_worker_env(
                    manifest,
                    Path(args.worker_env).read_text(encoding="utf-8"),
                )
    except (KeyError, OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
        result = _result(False, "manifest_unreadable")
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
