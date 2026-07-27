from __future__ import annotations

import copy
import hashlib
import importlib.util
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
CODEBUILD = ROOT / "infra" / "codebuild"
TERRAFORM = ROOT / "infra" / "terraform"
MODULE_PATH = CODEBUILD / "image_deployment_consumers.py"
REGISTRY_PATH = CODEBUILD / "image_deployment_consumers.json"

if str(CODEBUILD) not in sys.path:
    sys.path.insert(0, str(CODEBUILD))


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "image_deployment_consumers_under_test",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CONSUMERS = _load_module()
REGISTRY = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

ROOT_KEYS = {"schema_version", "consumers"}
CONSUMER_KEYS = {
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
ACTIVATOR_KEYS = {"type", "identity"}
RECEIPT_KEYS = {"pipeline", "subject"}
EXPECTED_CONSUMERS = {
    "mcp": {
        "terraform_task_definition_address": "aws_ecs_task_definition.mcp",
        "ecs_family": "teamagent-dev-mcp",
        "container_name": "teamagent-mcp",
        "activator": {"type": "ecs_service", "identity": "teamagent-dev-mcp"},
        "release_repository": "teamagent-mcp",
        "receipt": {"pipeline": "mcp", "subject": "core"},
    },
    "connect_web": {
        "terraform_task_definition_address": "aws_ecs_task_definition.connect_web[0]",
        "ecs_family": "teamagent-dev-connect-web",
        "container_name": "connect-web",
        "activator": {
            "type": "ecs_service",
            "identity": "teamagent-dev-connect-web",
        },
        "release_repository": "teamagent-mcp",
        "receipt": {"pipeline": "mcp", "subject": "core"},
    },
    "openclaw": {
        "terraform_task_definition_address": "aws_ecs_task_definition.openclaw[0]",
        "ecs_family": "teamagent-dev-openclaw",
        "container_name": "openclaw",
        "activator": {"type": "ecs_service", "identity": "teamagent-dev-openclaw"},
        "release_repository": "teamagent-openclaw",
        "receipt": {"pipeline": "openclaw", "subject": "core"},
    },
    "canary": {
        "terraform_task_definition_address": "aws_ecs_task_definition.canary[0]",
        "ecs_family": "teamagent-dev-canary",
        "container_name": "canary",
        "activator": {
            "type": "eventbridge_rule_ecs_target",
            "identity": "teamagent-dev-canary-hourly",
        },
        "release_repository": "teamagent-mcp",
        "receipt": {"pipeline": "mcp", "subject": "core"},
    },
    "ingest": {
        "terraform_task_definition_address": "aws_ecs_task_definition.ingest[0]",
        "ecs_family": "teamagent-dev-ingest",
        "container_name": "ingest",
        "activator": {
            "type": "eventbridge_rule_ecs_target",
            "identity": "teamagent-dev-ingest-weekly",
        },
        "release_repository": "teamagent-mcp",
        "receipt": {"pipeline": "mcp", "subject": "core"},
    },
    "morning_digest": {
        "terraform_task_definition_address": "aws_ecs_task_definition.morning_digest[0]",
        "ecs_family": "teamagent-dev-morning-digest",
        "container_name": "morning-digest",
        "activator": {
            "type": "eventbridge_rule_ecs_target",
            "identity": "teamagent-dev-morning-digest-weekday",
        },
        "release_repository": "teamagent-mcp",
        "receipt": {"pipeline": "mcp", "subject": "core"},
    },
    "x_buzz_worker": {
        "terraform_task_definition_address": "aws_ecs_task_definition.x_buzz_worker[0]",
        "ecs_family": "teamagent-dev-x-buzz-worker",
        "container_name": "worker",
        "activator": {
            "type": "lambda_taskdef_arn_environment",
            "identity": "teamagent-dev-x-buzz-dispatch",
        },
        "release_repository": "teamagent-mcp",
        "receipt": {"pipeline": "mcp", "subject": "core"},
    },
    "tiktok_acquire": {
        "terraform_task_definition_address": "aws_ecs_task_definition.tiktok_acquire[0]",
        "ecs_family": "teamagent-dev-tiktok-acquire",
        "container_name": "acquire",
        "activator": {
            "type": "lambda_taskdef_arn_environment",
            "identity": "teamagent-dev-tiktok-acquire-dispatch",
        },
        "release_repository": "teamagent-media-worker",
        "receipt": {"pipeline": "mcp", "subject": "media"},
    },
}

_FAMILY_EXPRESSIONS = {
    '"${var.project_name}-${var.environment}-mcp"': "teamagent-dev-mcp",
    '"${var.project_name}-${var.environment}-connect-web"': "teamagent-dev-connect-web",
    '"${var.project_name}-${var.environment}-openclaw"': "teamagent-dev-openclaw",
    '"${var.project_name}-${var.environment}-canary"': "teamagent-dev-canary",
    '"${var.project_name}-${var.environment}-ingest"': "teamagent-dev-ingest",
    '"${var.project_name}-${var.environment}-morning-digest"': ("teamagent-dev-morning-digest"),
    '"${local.xr_name}-worker"': "teamagent-dev-x-buzz-worker",
    "local.tk_name": "teamagent-dev-tiktok-acquire",
}


def _hcl_block(body: str, opening_brace: int) -> str:
    depth = 0
    in_string = False
    escaped = False
    line_comment = False
    block_comment = False
    index = opening_brace
    while index < len(body):
        character = body[index]
        following = body[index + 1] if index + 1 < len(body) else ""
        if line_comment:
            if character == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if character == "*" and following == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
        elif character == "#":
            line_comment = True
        elif character == "/" and following == "/":
            line_comment = True
            index += 1
        elif character == "/" and following == "*":
            block_comment = True
            index += 1
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return body[opening_brace : index + 1]
        index += 1
    raise AssertionError("unterminated HCL block")


def _terraform_task_definitions() -> dict[str, str]:
    declaration = re.compile(r'resource\s+"aws_ecs_task_definition"\s+"([^"]+)"\s*(\{)')
    discovered: dict[str, str] = {}
    for path in sorted(TERRAFORM.rglob("*.tf")):
        if ".terraform" in path.parts:
            continue
        body = path.read_text(encoding="utf-8")
        for match in declaration.finditer(body):
            block = _hcl_block(body, match.start(2))
            address = f"aws_ecs_task_definition.{match.group(1)}"
            if re.search(r"^\s*count\s*=", block, re.MULTILINE):
                address += "[0]"
            assert address not in discovered
            discovered[address] = block
    return discovered


def _reverse_key_order(value: Any) -> Any:
    if type(value) is dict:
        return {key: _reverse_key_order(value[key]) for key in reversed(list(value))}
    if type(value) is list:
        return [_reverse_key_order(item) for item in value]
    return value


def test_registry_is_the_exact_eight_consumer_contract() -> None:
    assert set(REGISTRY) == ROOT_KEYS
    assert REGISTRY["schema_version"] == 1
    assert len(REGISTRY["consumers"]) == 8

    observed: dict[str, dict[str, Any]] = {}
    for entry in REGISTRY["consumers"]:
        assert set(entry) == CONSUMER_KEYS
        assert set(entry["activator"]) == ACTIVATOR_KEYS
        assert set(entry["receipt"]) == RECEIPT_KEYS
        consumer_id = entry["consumer_id"]
        observed[consumer_id] = {
            key: entry[key]
            for key in (
                "terraform_task_definition_address",
                "ecs_family",
                "container_name",
                "activator",
                "release_repository",
                "receipt",
            )
        }

    assert observed == EXPECTED_CONSUMERS
    # No consumer is provisional: x_buzz was the last one, and its live task
    # definition was read on 2026-07-27 (container "worker" on teamagent-mcp).
    assert not {entry["consumer_id"] for entry in REGISTRY["consumers"] if entry["provisional"]}
    assert CONSUMERS.validate_consumer_registry(REGISTRY) == REGISTRY


def test_registry_rejects_unknown_missing_and_duplicate_json_keys() -> None:
    unknown = copy.deepcopy(REGISTRY)
    unknown["consumers"][0]["caller_pipeline"] = "mcp"
    with pytest.raises(CONSUMERS.ConsumerRegistryError, match="keys must be exact"):
        CONSUMERS.validate_consumer_registry(unknown)

    missing = copy.deepcopy(REGISTRY)
    del missing["consumers"][0]["receipt"]["subject"]
    with pytest.raises(CONSUMERS.ConsumerRegistryError, match="keys must be exact"):
        CONSUMERS.validate_consumer_registry(missing)

    raw_duplicate = b'{"schema_version":1,"schema_version":1,"consumers":[]}'
    with pytest.raises(CONSUMERS.ConsumerRegistryError, match="duplicate JSON key"):
        CONSUMERS.parse_consumer_registry(raw_duplicate)


@pytest.mark.parametrize(
    "field",
    [
        "consumer_id",
        "terraform_task_definition_address",
        "container_name",
    ],
)
def test_registry_rejects_duplicate_consumer_ownership(field: str) -> None:
    duplicate = copy.deepcopy(REGISTRY)
    duplicate["consumers"][1][field] = duplicate["consumers"][0][field]

    with pytest.raises(CONSUMERS.ConsumerRegistryError, match="duplicate"):
        CONSUMERS.validate_consumer_registry(duplicate)


def test_consumer_registry_sha256_is_canonical_and_key_order_independent() -> None:
    reordered = _reverse_key_order(REGISTRY)
    expected = hashlib.sha256(CONSUMERS.canonical_json_bytes(REGISTRY)).hexdigest()

    assert CONSUMERS.consumer_registry_sha256() == expected
    assert CONSUMERS._consumer_registry_sha256(REGISTRY) == expected
    assert CONSUMERS._consumer_registry_sha256(reordered) == expected
    assert CONSUMERS.canonical_json_bytes(REGISTRY).endswith(b"\n")
    assert not CONSUMERS.canonical_json_bytes(REGISTRY).endswith(b"\n\n")


def test_registry_closes_over_every_terraform_ecs_task_definition() -> None:
    task_definitions = _terraform_task_definitions()
    registry_by_address = {
        entry["terraform_task_definition_address"]: entry for entry in REGISTRY["consumers"]
    }

    assert set(registry_by_address) == set(task_definitions)
    terraform_families: set[str] = set()
    for address, block in task_definitions.items():
        family_match = re.search(r"^\s*family\s*=\s*(.+?)\s*$", block, re.MULTILINE)
        assert family_match, f"{address} has no family"
        family_expression = family_match.group(1)
        assert family_expression in _FAMILY_EXPRESSIONS, (
            f"{address} uses an unreviewed family expression"
        )
        terraform_families.add(_FAMILY_EXPRESSIONS[family_expression])

    assert {entry["ecs_family"] for entry in REGISTRY["consumers"]} == terraform_families


def test_registry_container_names_exist_in_terraform_container_definitions() -> None:
    task_definitions = _terraform_task_definitions()
    for entry in REGISTRY["consumers"]:
        address = entry["terraform_task_definition_address"]
        block = task_definitions[address]
        container_definitions = block[block.index("container_definitions") :]
        first_name = re.search(
            r"\bname\s*=\s*\"([^\"]+)\"",
            container_definitions,
        )
        assert first_name, f"{address} has no literal container name"
        assert first_name.group(1) == entry["container_name"]


def test_release_coordinates_cannot_be_overridden_by_caller_input() -> None:
    hostile_query = {
        "consumer_id": "tiktok_acquire",
        "pipeline": "openclaw",
        "subject": "core",
        "repository": "teamagent-openclaw",
    }
    assert tuple(inspect.signature(CONSUMERS.release_coordinates_for_consumer).parameters) == (
        "consumer_id",
    )
    assert CONSUMERS.release_coordinates_for_consumer(hostile_query["consumer_id"]) == {
        "pipeline": "mcp",
        "subject": "media",
        "repository": "teamagent-media-worker",
    }

    returned = CONSUMERS.get_consumer("tiktok_acquire")
    returned["receipt"]["pipeline"] = hostile_query["pipeline"]
    returned["receipt"]["subject"] = hostile_query["subject"]
    returned["release_repository"] = hostile_query["repository"]
    assert CONSUMERS.release_coordinates_for_consumer("tiktok_acquire") == {
        "pipeline": "mcp",
        "subject": "media",
        "repository": "teamagent-media-worker",
    }


def test_x_buzz_registry_coordinates_are_confirmed_against_the_live_image() -> None:
    """x_buzz runs the MCP image, so it is an MCP consumer rather than its own lane.

    Terraform already refused anything but teamagent-mcp for x_buzz_image, and the
    live task definition was read on 2026-07-27 to confirm it:
    teamagent-dev-x-buzz-worker:1 runs container "worker" on
    teamagent-mcp@sha256:1747d2d0…, a third distinct digest from the same
    repository as mcp and connect_web.
    """
    x_buzz = CONSUMERS.get_consumer("x_buzz_worker")

    assert x_buzz["provisional"] is False
    assert x_buzz["provisional_reason"] is None
    assert x_buzz["container_name"] == "worker"
    assert CONSUMERS.release_coordinates_for_consumer("x_buzz_worker") == {
        "pipeline": "mcp",
        "subject": "core",
        "repository": "teamagent-mcp",
    }
