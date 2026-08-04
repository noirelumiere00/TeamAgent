"""Parse Gemini v3 research and enforce numeric-claim provenance."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from teamagent.skills.proposal_builder.schema import (
    EvidenceReference,
    EvidenceRegistry,
    GeminiResearch,
    ResearchIssue,
    SanitizationResult,
)
from teamagent.skills.proposal_deck.provenance import (
    has_quantitative_claim,
    iter_quantitative_claims,
    redact_quantitative_claims,
)

_CODE_FENCE_RE = re.compile(
    r"\A\s*```(?:json)?[ \t]*\r?\n?(.*?)\r?\n?```\s*\Z",
    flags=re.IGNORECASE | re.DOTALL,
)
_URL_RE = re.compile(r"https?://[^\s<>{}\\^`\"']+", flags=re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}、。；：！？）】」』＞"
_UNVERIFIED_REPLACEMENT = "要確認（出典URL未取得）"
_PLANNING_FIELD_NAMES = frozenset(
    {
        "purpose",
        "channel",
        "moment",
        "target_categories",
    }
)


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE_RE.fullmatch(text)
    return (match.group(1) if match else text).strip()


def _fill_missing_values_before_comma(text: str) -> str:
    """Replace only a structural ``:,`` empty value, never the same bytes in a string."""

    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue

        output.append(char)
        if char != ":":
            index += 1
            continue

        lookahead = index + 1
        while lookahead < len(text) and text[lookahead].isspace():
            output.append(text[lookahead])
            lookahead += 1
        if lookahead < len(text) and text[lookahead] == ",":
            output.append("null")
        index = lookahead
    return "".join(output)


def _remove_trailing_commas(text: str) -> str:
    """Remove only structural commas immediately before ``}`` or ``]``."""

    output: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue

        if char == ",":
            lookahead = index + 1
            while lookahead < len(text) and text[lookahead].isspace():
                lookahead += 1
            if lookahead < len(text) and text[lookahead] in "}]":
                output.extend(text[index + 1 : lookahead])
                index = lookahead
                continue
        output.append(char)
        index += 1
    return "".join(output)


def repair_json_syntax(text: str) -> str:
    """Apply the narrowly allowed Gemini JSON syntax repairs.

    The repair deliberately does not fix quotes, infer missing fields, or supply
    business content. Pydantic remains responsible for rejecting incomplete data.
    """

    stripped = _strip_code_fence(text)
    with_nulls = _fill_missing_values_before_comma(stripped)
    return _remove_trailing_commas(with_nulls)


def parse_gemini_research(raw: Mapping[str, Any] | str) -> GeminiResearch:
    """Parse a dict or Gemini JSON string into the strict v3 model.

    A mapping is copied before validation. A string receives syntax-only repair
    before ``json.loads``; semantic gaps are never filled.
    """

    if isinstance(raw, str):
        payload: Any = json.loads(repair_json_syntax(raw))
    elif isinstance(raw, Mapping):
        payload = copy.deepcopy(dict(raw))
    else:
        raise TypeError("Gemini research must be a mapping or JSON string")
    return GeminiResearch.model_validate(payload, strict=True)


def _json_path(parent: str, key: object) -> str:
    if isinstance(key, int):
        return f"{parent}[{key}]"
    name = str(key)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return f"{parent}.{name}"
    return f"{parent}[{json.dumps(name, ensure_ascii=False)}]"


def _valid_http_url(value: str) -> bool:
    candidate = value.strip()
    if not candidate or any(char.isspace() for char in candidate):
        return False
    if "..." in candidate or "…" in candidate:
        return False
    try:
        parsed = urlsplit(candidate)
        host = parsed.hostname or ""
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return False
    if host == "..." or ".." in host or host.startswith(".") or host.endswith("."):
        return False
    # Evidence is external; a bare hostname such as "example" is not an
    # independently checkable source URL.
    return "." in host


def _urls_in_string(value: str) -> list[str]:
    found: list[str] = []
    for match in _URL_RE.finditer(value):
        candidate = match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        if _valid_http_url(candidate):
            found.append(candidate)
    return found


def _as_plain_data(raw: GeminiResearch | Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(raw, GeminiResearch):
        return raw.model_dump(mode="python", by_alias=True)
    if isinstance(raw, str):
        return parse_gemini_research(raw).model_dump(mode="python", by_alias=True)
    if isinstance(raw, Mapping):
        return copy.deepcopy(dict(raw))
    raise TypeError("research must be GeminiResearch, a mapping, or JSON string")


def build_evidence_registry(
    raw: GeminiResearch | Mapping[str, Any] | str,
) -> EvidenceRegistry:
    """Recursively collect valid HTTP(S) URLs under their nearest JSON object."""

    data = _as_plain_data(raw)
    references: list[EvidenceReference] = []

    def visit(node: object, path: str, owner_path: str | None) -> None:
        if isinstance(node, Mapping):
            current_owner = path
            for key, child in node.items():
                child_path = _json_path(path, key)
                if isinstance(child, (Mapping, list)):
                    visit(child, child_path, current_owner)
                elif isinstance(child, str):
                    references.extend(
                        EvidenceReference(
                            object_path=current_owner,
                            path=child_path,
                            url=url,
                        )
                        for url in _urls_in_string(child)
                    )
            return
        if isinstance(node, list):
            for index, child in enumerate(node):
                child_path = _json_path(path, index)
                if isinstance(child, (Mapping, list)):
                    visit(child, child_path, owner_path)
                elif isinstance(child, str) and owner_path is not None:
                    references.extend(
                        EvidenceReference(
                            object_path=owner_path,
                            path=child_path,
                            url=url,
                        )
                        for url in _urls_in_string(child)
                    )

    visit(data, "$", None)
    return EvidenceRegistry(references=references)


def _is_numeric_claim(value: object, *, field_name: str | None) -> bool:
    if not isinstance(value, str):
        return False
    if field_name == "research_date" or field_name in _PLANNING_FIELD_NAMES:
        return False
    return has_quantitative_claim(value)


def sanitize_unverified_numbers(
    raw: GeminiResearch | Mapping[str, Any] | str,
) -> SanitizationResult:
    """Replace numeric claims lacking same-object evidence in a copied payload.

    The original model/mapping/string is never modified. A valid URL elsewhere
    in the document or an ancestor object does not launder a leaf claim: the URL
    must belong to the leaf's nearest containing JSON object.
    """

    data = _as_plain_data(raw)
    registry = build_evidence_registry(data)
    urls_by_object = {
        ref.object_path: registry.urls_for_object(ref.object_path)
        for ref in registry.references
    }
    issues: list[ResearchIssue] = []

    def visit(node: object, path: str, owner_path: str | None, field_name: str | None) -> object:
        if isinstance(node, Mapping):
            current_owner = path
            return {
                str(key): visit(
                    child,
                    _json_path(path, key),
                    current_owner,
                    str(key),
                )
                for key, child in node.items()
            }
        if isinstance(node, list):
            return [
                visit(child, _json_path(path, index), owner_path, field_name)
                for index, child in enumerate(node)
            ]
        if _is_numeric_claim(node, field_name=field_name) and not urls_by_object.get(
            owner_path or ""
        ):
            issues.append(
                ResearchIssue(
                    path=path,
                    code="unverified_numeric_claim",
                    message="数値主張に同一オブジェクト内の出典URLがありません",
                )
            )
            return _UNVERIFIED_REPLACEMENT
        return copy.deepcopy(node)

    sanitized = visit(data, "$", None, None)
    if not isinstance(sanitized, dict):
        raise TypeError("research root must be an object")
    return SanitizationResult(
        sanitized=sanitized,
        evidence_registry=registry,
        issues=issues,
    )


def build_quantitative_evidence(
    sanitized: Mapping[str, Any],
    registry: EvidenceRegistry,
) -> dict[str, list[str]]:
    """Map exact retained quantities to URLs from their nearest source object."""

    urls_by_object = {
        ref.object_path: registry.urls_for_object(ref.object_path)
        for ref in registry.references
    }
    claims: dict[str, list[str]] = {}

    def visit(node: object, path: str, owner_path: str | None) -> None:
        if isinstance(node, Mapping):
            current_owner = path
            for key, child in node.items():
                visit(child, _json_path(path, key), current_owner)
            return
        if isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, _json_path(path, index), owner_path)
            return
        if not isinstance(node, str):
            return
        sources = urls_by_object.get(owner_path or "", ())
        if not sources:
            return
        for claim in iter_quantitative_claims(node):
            target = claims.setdefault(claim, [])
            target.extend(url for url in sources if url not in target)

    visit(sanitized, "$", None)
    return claims


def redact_unverified_quantities(text: str) -> tuple[str, bool]:
    """Remove unit-bearing quantities from text that has no usable source URL."""

    return redact_quantitative_claims(text, _UNVERIFIED_REPLACEMENT)


__all__ = [
    "build_evidence_registry",
    "build_quantitative_evidence",
    "parse_gemini_research",
    "redact_unverified_quantities",
    "repair_json_syntax",
    "sanitize_unverified_numbers",
]
