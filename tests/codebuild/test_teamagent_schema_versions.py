from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "codebuild" / "teamagent_schema_versions.py"
CODEBUILD = MODULE_PATH.parent
INNER_CONTRACT = CODEBUILD / "teamagent_runtime_contract.json"
OUTER_CONTRACT = CODEBUILD / "teamagent_core_media_release_contract.json"

if str(CODEBUILD) not in sys.path:
    sys.path.insert(0, str(CODEBUILD))


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "teamagent_schema_versions_under_test",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VERSIONS = _load_module()


def _load_consumer(name: str) -> Any:
    path = CODEBUILD / f"{name}.py"
    spec = importlib.util.spec_from_file_location(
        f"{name}_schema_version_integration_under_test",
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_schema_version_tuple_is_complete_and_exact() -> None:
    assert isinstance(VERSIONS.SCHEMA_VERSIONS, VERSIONS.TeamAgentSchemaVersions)
    assert VERSIONS.SCHEMA_VERSIONS._fields == (
        "inner_runtime_contract",
        "outer_core_media_contract",
        "mcp_source_declaration",
        "mcp_release_receipt",
        "external_approval",
        "image_deployment_intent",
    )
    assert len(VERSIONS.SCHEMA_VERSIONS) == 6
    assert tuple(VERSIONS.SCHEMA_VERSIONS) == (5, 3, 5, 3, 1, 1)
    assert all(type(version) is int for version in VERSIONS.SCHEMA_VERSIONS)


def test_atomic_release_schema_tuple_matches_the_authority() -> None:
    expected = tuple(VERSIONS.SCHEMA_VERSIONS[:4])

    assert VERSIONS.ATOMIC_RELEASE_SCHEMA_TUPLE == expected == (5, 3, 5, 3)
    assert VERSIONS.validate_atomic_release_schema_tuple(expected) is expected


def test_live_release_chain_consumers_use_the_atomic_version_authority() -> None:
    source = _load_consumer("source_provenance")
    bundle = _load_consumer("teamagent_bundle_provenance")
    evidence = _load_consumer("release_evidence")
    inner = json.loads(INNER_CONTRACT.read_text(encoding="utf-8"))
    outer = json.loads(OUTER_CONTRACT.read_text(encoding="utf-8"))

    observed = (
        inner["schema_version"],
        outer["schema_version"],
        evidence.SOURCE_DECLARATION_SCHEMA,
        evidence.RELEASE_RECEIPT_SCHEMA,
    )
    assert observed == VERSIONS.ATOMIC_RELEASE_SCHEMA_TUPLE
    assert source.RUNTIME_CONTRACT_SCHEMA_VERSION == inner["schema_version"]
    assert bundle.RUNTIME_CONTRACT_SCHEMA_VERSION == inner["schema_version"]
    assert bundle.CONTRACT_SCHEMA_VERSION == outer["schema_version"]
    assert source.SCHEMA_VERSION == 3  # The unrelated source-manifest schema stays v3.


def test_deployment_intent_schema_uses_the_authority_without_a_literal() -> None:
    evidence = _load_consumer("release_evidence")
    source = (CODEBUILD / "release_evidence.py").read_text(encoding="utf-8")

    assert (
        evidence.DEPLOYMENT_INTENT_SCHEMA
        == VERSIONS.SCHEMA_VERSIONS.image_deployment_intent
        == 1
    )
    assert (
        "DEPLOYMENT_INTENT_SCHEMA = SCHEMA_VERSIONS.image_deployment_intent"
        in source
    )
    assert re.search(
        r"^DEPLOYMENT_INTENT_SCHEMA\s*=\s*\d+\s*$",
        source,
        re.MULTILINE,
    ) is None


@pytest.mark.parametrize(
    "candidate",
    [
        (4, 3, 5, 3),
        (5, 2, 5, 3),
        (5, 3, 4, 3),
        (5, 3, 5, 2),
    ],
)
def test_atomic_release_schema_tuple_rejects_each_half_bump(
    candidate: tuple[int, int, int, int],
) -> None:
    with pytest.raises(ValueError, match="must be bumped atomically"):
        VERSIONS.validate_atomic_release_schema_tuple(candidate)


@pytest.mark.parametrize("candidate", [(), (5, 3, 5), (5, 3, 5, 3, 1)])
def test_atomic_release_schema_tuple_rejects_wrong_length(candidate: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="exactly 4 elements"):
        VERSIONS.validate_atomic_release_schema_tuple(candidate)


@pytest.mark.parametrize(
    "candidate",
    [
        [5, 3, 5, 3],
        "5,3,5,3",
        VERSIONS.TeamAgentSchemaVersions(5, 3, 5, 3, 1, 1),
    ],
)
def test_atomic_release_schema_tuple_rejects_non_builtin_tuple(candidate: object) -> None:
    with pytest.raises(TypeError, match="built-in tuple"):
        VERSIONS.validate_atomic_release_schema_tuple(candidate)


@pytest.mark.parametrize(
    "candidate",
    [
        (True, 3, 5, 3),
        (5, 3.0, 5, 3),
        (5, 3, "5", 3),
        (5, 3, 5, None),
    ],
)
def test_atomic_release_schema_tuple_rejects_non_int_elements(
    candidate: tuple[object, object, object, object],
) -> None:
    with pytest.raises(TypeError, match="must be an int"):
        VERSIONS.validate_atomic_release_schema_tuple(candidate)
