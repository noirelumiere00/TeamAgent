"""Conservative, deterministic client-property resolution.

Document ownership and document classification have different confidence levels:
``client_name`` / ``cls_project`` can establish which client owns an activity, while
``cls_industry`` may describe the campaign or product instead of the company.  A
curated company override therefore wins; otherwise every exact-owner observation
must agree before an industry is promoted to the client card.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping

from teamagent.client_identity import client_identity_key


def identity_value_map(values: Mapping[str, str]) -> dict[str, str]:
    """Key a curated map by conservative client identity and reject conflicts."""

    result: dict[str, str] = {}
    for client, raw_value in values.items():
        key = client_identity_key(client)
        value = unicodedata.normalize("NFC", str(raw_value or "")).strip()
        if not key or not value:
            raise ValueError("client property overrides require non-empty keys and values")
        previous = result.get(key)
        if previous is not None and previous != value:
            raise ValueError("conflicting client property overrides for one identity")
        result[key] = value
    return result


def resolve_client_industry(
    client: object,
    primary_industries: Iterable[object],
    project_industries: Iterable[object],
    overrides_by_identity: Mapping[str, str],
) -> str:
    """Resolve a company industry without majority-voting away disagreements.

    A curated override is authoritative.  Without one, all non-empty industries
    observed on exact primary/project ownership records must agree.  Conflicts are
    returned as an empty string so the UI never presents a guessed company property.
    """

    return resolve_client_industry_with_source(
        client,
        primary_industries,
        project_industries,
        overrides_by_identity,
    )[0]


def resolve_client_industry_with_source(
    client: object,
    primary_industries: Iterable[object],
    project_industries: Iterable[object],
    overrides_by_identity: Mapping[str, str],
) -> tuple[str, str]:
    """Return ``(industry, source)`` for auditable client-note frontmatter."""

    override = overrides_by_identity.get(client_identity_key(client))
    if override is not None:
        return override, "master"
    candidates = {
        unicodedata.normalize("NFC", str(value or "")).strip()
        for value in (*primary_industries, *project_industries)
        if str(value or "").strip()
    }
    if len(candidates) == 1:
        return next(iter(candidates)), "exact_consensus"
    if candidates:
        return "", "conflict"
    return "", "none"
