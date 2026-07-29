"""Pure validation and lookup for the code-owned image consumer registry."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from teamagent_release_approval import canonical_json_bytes

REGISTRY_PATH = Path(__file__).with_name("image_deployment_consumers.json")
REGISTRY_SCHEMA_VERSION = 1
EXPECTED_CONSUMER_COUNT = 8

_ROOT_KEYS = {"schema_version", "consumers"}
_CONSUMER_KEYS = {
    "consumer_id",
    "terraform_task_definition_address",
    "ecs_family",
    "container_name",
    "activator",
    "release_repository",
    "receipt",
    "provisional",
    "provisional_reason",
}
_ACTIVATOR_KEYS = {"type", "identity"}
_RECEIPT_KEYS = {"pipeline", "subject"}
_ACTIVATOR_TYPES = {
    "ecs_service",
    "eventbridge_rule_ecs_target",
    "lambda_taskdef_arn_environment",
}
_TASK_DEFINITION_ADDRESS_RE = re.compile(r"aws_ecs_task_definition\.[a-z][a-z0-9_]*(?:\[0\])?")
_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_REPOSITORY_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*")


class ConsumerRegistryError(ValueError):
    """The code-owned consumer registry is malformed or lacks the requested entry."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ConsumerRegistryError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_nonfinite_json(value: str) -> None:
    raise ConsumerRegistryError(f"non-finite JSON number is forbidden: {value}")


def _require_exact_keys(
    value: object,
    expected: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise ConsumerRegistryError(f"{label} must be a built-in object")
    if any(type(key) is not str for key in value):
        raise ConsumerRegistryError(f"{label} keys must be built-in strings")
    typed = cast(dict[str, Any], value)
    actual = set(typed)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ConsumerRegistryError(
            f"{label} keys must be exact; missing={missing!r}, unknown={unknown!r}"
        )
    return typed


def _require_string(value: object, *, label: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        raise ConsumerRegistryError(f"{label} has an invalid value")
    return value


def parse_consumer_registry(payload: bytes) -> dict[str, Any]:
    """Parse UTF-8 registry JSON without accepting duplicate object keys."""

    if type(payload) is not bytes:
        raise ConsumerRegistryError("consumer registry payload must be bytes")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConsumerRegistryError("consumer registry payload must be UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except ConsumerRegistryError:
        raise
    except (RecursionError, ValueError) as exc:
        raise ConsumerRegistryError("consumer registry payload is not valid JSON") from exc
    return validate_consumer_registry(value)


def validate_consumer_registry(value: object) -> dict[str, Any]:
    """Validate the complete registry shape and its uniqueness constraints."""

    root = _require_exact_keys(value, _ROOT_KEYS, label="consumer registry")
    if type(root["schema_version"]) is not int or root["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise ConsumerRegistryError(
            f"consumer registry schema_version must be {REGISTRY_SCHEMA_VERSION}"
        )
    consumers = root["consumers"]
    if type(consumers) is not list or len(consumers) != EXPECTED_CONSUMER_COUNT:
        raise ConsumerRegistryError(
            f"consumer registry must contain exactly {EXPECTED_CONSUMER_COUNT} consumers"
        )

    seen_consumer_ids: set[str] = set()
    seen_task_definitions: set[str] = set()
    seen_container_names: set[str] = set()
    for index, item in enumerate(consumers):
        label = f"consumer registry consumers[{index}]"
        entry = _require_exact_keys(item, _CONSUMER_KEYS, label=label)
        consumer_id = _require_string(
            entry["consumer_id"],
            label=f"{label}.consumer_id",
            pattern=_IDENTIFIER_RE,
        )
        task_definition = _require_string(
            entry["terraform_task_definition_address"],
            label=f"{label}.terraform_task_definition_address",
            pattern=_TASK_DEFINITION_ADDRESS_RE,
        )
        _require_string(
            entry["ecs_family"],
            label=f"{label}.ecs_family",
            pattern=_IDENTIFIER_RE,
        )
        container_name = _require_string(
            entry["container_name"],
            label=f"{label}.container_name",
            pattern=_IDENTIFIER_RE,
        )
        _require_string(
            entry["release_repository"],
            label=f"{label}.release_repository",
            pattern=_REPOSITORY_RE,
        )

        activator = _require_exact_keys(
            entry["activator"],
            _ACTIVATOR_KEYS,
            label=f"{label}.activator",
        )
        activator_type = _require_string(
            activator["type"],
            label=f"{label}.activator.type",
            pattern=_IDENTIFIER_RE,
        )
        if activator_type not in _ACTIVATOR_TYPES:
            raise ConsumerRegistryError(f"{label}.activator.type is not supported")
        _require_string(
            activator["identity"],
            label=f"{label}.activator.identity",
            pattern=_IDENTIFIER_RE,
        )

        receipt = _require_exact_keys(
            entry["receipt"],
            _RECEIPT_KEYS,
            label=f"{label}.receipt",
        )
        for key in _RECEIPT_KEYS:
            _require_string(
                receipt[key],
                label=f"{label}.receipt.{key}",
                pattern=_IDENTIFIER_RE,
            )

        if type(entry["provisional"]) is not bool:
            raise ConsumerRegistryError(f"{label}.provisional must be a bool")
        reason = entry["provisional_reason"]
        if entry["provisional"]:
            if type(reason) is not str or not reason.strip():
                raise ConsumerRegistryError(
                    f"{label}.provisional_reason must explain a provisional entry"
                )
        elif reason is not None:
            raise ConsumerRegistryError(
                f"{label}.provisional_reason must be null for a non-provisional entry"
            )

        if consumer_id in seen_consumer_ids:
            raise ConsumerRegistryError(f"duplicate consumer_id: {consumer_id}")
        if task_definition in seen_task_definitions:
            raise ConsumerRegistryError(
                f"duplicate terraform task definition address: {task_definition}"
            )
        if container_name in seen_container_names:
            raise ConsumerRegistryError(f"duplicate container_name: {container_name}")
        seen_consumer_ids.add(consumer_id)
        seen_task_definitions.add(task_definition)
        seen_container_names.add(container_name)

    return copy.deepcopy(root)


def load_consumer_registry() -> dict[str, Any]:
    """Load only the registry shipped beside this module."""

    try:
        payload = REGISTRY_PATH.read_bytes()
    except OSError as exc:
        raise ConsumerRegistryError("unable to read the code-owned consumer registry") from exc
    return parse_consumer_registry(payload)


def _consumer_registry_sha256(value: object) -> str:
    registry = validate_consumer_registry(value)
    return hashlib.sha256(canonical_json_bytes(registry)).hexdigest()


def consumer_registry_sha256() -> str:
    """Hash the fixed registry using the release-chain canonical JSON."""

    return _consumer_registry_sha256(load_consumer_registry())


def get_consumer(consumer_id: str) -> dict[str, Any]:
    """Return a defensive copy of the fixed registry entry for ``consumer_id``."""

    if type(consumer_id) is not str:
        raise ConsumerRegistryError("consumer_id must be a string")
    for entry in load_consumer_registry()["consumers"]:
        if entry["consumer_id"] == consumer_id:
            return copy.deepcopy(entry)
    raise ConsumerRegistryError(f"unknown consumer_id: {consumer_id}")


def release_coordinates_for_consumer(consumer_id: str) -> dict[str, str]:
    """Derive receipt and repository coordinates solely from the fixed registry."""

    entry = get_consumer(consumer_id)
    receipt: Mapping[str, str] = entry["receipt"]
    return {
        "pipeline": receipt["pipeline"],
        "subject": receipt["subject"],
        "repository": entry["release_repository"],
    }
