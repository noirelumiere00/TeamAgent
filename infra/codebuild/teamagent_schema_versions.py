"""Authoritative schema versions for the TeamAgent release chain.

The source-manifest ``SCHEMA_VERSION`` in ``source_provenance.py`` is a
different schema and intentionally does not belong in this tuple.
"""

from __future__ import annotations

from typing import Final, NamedTuple, cast


class TeamAgentSchemaVersions(NamedTuple):
    """Schema versions that must move through the release chain together."""

    inner_runtime_contract: int
    outer_core_media_contract: int
    mcp_source_declaration: int
    mcp_release_receipt: int
    external_approval: int
    image_deployment_intent: int


SCHEMA_VERSIONS: Final = TeamAgentSchemaVersions(
    inner_runtime_contract=5,
    outer_core_media_contract=3,
    mcp_source_declaration=5,
    mcp_release_receipt=3,
    external_approval=1,
    image_deployment_intent=1,
)

ATOMIC_RELEASE_SCHEMA_TUPLE: Final[tuple[int, int, int, int]] = (
    SCHEMA_VERSIONS.inner_runtime_contract,
    SCHEMA_VERSIONS.outer_core_media_contract,
    SCHEMA_VERSIONS.mcp_source_declaration,
    SCHEMA_VERSIONS.mcp_release_receipt,
)


def validate_atomic_release_schema_tuple(candidate: object) -> tuple[int, int, int, int]:
    """Return the validated four-schema tuple or reject a partial version bump."""

    if type(candidate) is not tuple:
        raise TypeError("release schema versions must be a built-in tuple")
    if len(candidate) != len(ATOMIC_RELEASE_SCHEMA_TUPLE):
        raise ValueError(
            "release schema versions must contain exactly "
            f"{len(ATOMIC_RELEASE_SCHEMA_TUPLE)} elements"
        )
    if any(type(version) is not int for version in candidate):
        raise TypeError("each release schema version must be an int")

    typed_candidate = cast(tuple[int, int, int, int], candidate)
    if typed_candidate != ATOMIC_RELEASE_SCHEMA_TUPLE:
        raise ValueError(
            f"release schema versions must be bumped atomically to {ATOMIC_RELEASE_SCHEMA_TUPLE!r}"
        )
    return typed_candidate
