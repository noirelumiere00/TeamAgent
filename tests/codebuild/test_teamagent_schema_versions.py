from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "infra" / "codebuild" / "teamagent_schema_versions.py"


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
    assert tuple(VERSIONS.SCHEMA_VERSIONS) == (5, 3, 5, 3, 1, 2)
    assert all(type(version) is int for version in VERSIONS.SCHEMA_VERSIONS)


def test_atomic_release_schema_tuple_matches_the_authority() -> None:
    expected = tuple(VERSIONS.SCHEMA_VERSIONS[:4])

    assert VERSIONS.ATOMIC_RELEASE_SCHEMA_TUPLE == expected == (5, 3, 5, 3)
    assert VERSIONS.validate_atomic_release_schema_tuple(expected) is expected


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
        VERSIONS.TeamAgentSchemaVersions(5, 3, 5, 3, 1, 2),
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
