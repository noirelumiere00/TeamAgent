"""Deterministic provenance checks for ``proposal_deck`` Composer output.

The Composer prompt asks the model to cite only URLs present in the supplied
evidence and to avoid unsupported quantitative claims.  This module enforces
those two rules without network access:

* every citation must exactly match an HTTP(S) URL supplied in ``input.urls``
  or extracted from ``research_material``;
* a placeholder containing a conservative, unit-bearing quantitative claim
  must have at least one matching citation on the same placeholder ID.

Bare years, ordinal/model numbers such as ``R-1``, and other unitless numbers
are intentionally ignored to keep false positives low.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from urllib.parse import urlsplit

from teamagent.skills.proposal_deck.contract import ComposerOutput

_URL_PATTERN = re.compile(r"""https?://[^\s<>"'`|]+""", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = frozenset(".,;:!?、。，；：！？）)]}」』】〉》")

# Arabic/full-width digits with optional thousands separators and decimals, or
# Japanese-number glyphs. A unit remains mandatory, so slide IDs, model names
# (R-1), and plain rankings do not match. ``第1回`` is an ordinal rather than a
# quantitative claim and is deliberately excluded.
_ARABIC_NUMBER = (
    r"(?:[0-9０-９]{1,3}(?:[,，][0-9０-９]{3})+|[0-9０-９]+)"
    r"(?:[.．][0-9０-９]+)?"
)
_KANJI_NUMBER = r"[〇零一二三四五六七八九十百千万億兆]+"
_NUMBER = rf"(?:{_ARABIC_NUMBER}|{_KANJI_NUMBER})"
_RANGE_NUMBER = rf"{_NUMBER}(?:\s*(?:[〜～~]|[-–—]|→|から)\s*{_NUMBER})?"
_ORDINAL_COUNT = re.compile(rf"第\s*{_NUMBER}\s*回")
_DIRECTION_PREFIX = (
    r"(?:(?:前年比|前年同期比|前月比|前週比|対前年差|"
    r"増加率|減少率|上昇率|低下率)\s*)?"
    r"(?:[+＋\-−▲△]\s*)?"
)
_DIRECTION_SUFFIX = (
    r"(?:\s*(?:増加|減少|上昇|低下|向上|悪化|伸長|縮小|"
    r"プラス|マイナス|アップ|ダウン|増|減))?"
)
_QUANTITATIVE_CLAIM = re.compile(
    rf"(?<![第0-9０-９,.，．]){_DIRECTION_PREFIX}{_RANGE_NUMBER}\s*"
    r"(?:"
    r"[%％]|倍|割|ポイント|pt|"
    r"(?:千|万|億|兆)?(?:"
    r"円|件|人|名|社|回|本|枚|投稿|動画|再生|フォロワー|クリック|"
    r"会場|店舗|拠点|地域|都市|か国|ヶ国|カ国|商品|施策|媒体|"
    r"世帯|校|台|個|点|位|種|箇所|ヶ所"
    r")|"
    r"(?:日(?:間)?|週(?:間)?|か月|ヶ月|カ月|月間|年間|年目|"
    r"時間|分|秒|代|歳|度|℃)|"
    r"(?:Gbps|Mbps|GHz|MHz|kHz|kcal|TB|GB|MB|KB|kg|mg|μg|ug|"
    r"km|cm|mm|mL|ml|px|Hz|cal|L|g|m)|"
    r"impressions?|views?|imp|PV|UU|CV|"
    r"千|万|億|兆"
    rf"){_DIRECTION_SUFFIX}",
    re.IGNORECASE,
)
class ProvenanceValidationError(ValueError):
    """Composer output cites unsupported evidence or makes an uncited quantity."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(errors)
        message = "; ".join(self.errors) or "unknown provenance validation error"
        super().__init__(f"provenance validation failed: {message}")


def _is_http_url(value: str) -> bool:
    """Return whether ``value`` is an exact, whitespace-free HTTP(S) URL."""

    if not value or value != value.strip() or any(char.isspace() for char in value):
        return False
    parsed = urlsplit(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _trim_extracted_url(value: str) -> str:
    """Remove prose/Markdown closing punctuation without canonicalizing a URL."""

    candidate = value
    while candidate and candidate[-1] in _TRAILING_URL_PUNCTUATION:
        closing = candidate[-1]
        if closing == ")" and candidate.count("(") >= candidate.count(")"):
            break
        candidate = candidate[:-1]
    return candidate


def extract_evidence_urls(
    input_urls: Iterable[str],
    research_material: str,
) -> frozenset[str]:
    """Collect exact evidence URLs from explicit inputs and research text.

    Slack ``<https://example.test|label>`` and Markdown
    ``[label](https://example.test)`` forms are reduced to the URL token only.
    No URL normalization or external resolution is performed.
    """

    found: set[str] = set()
    for raw in input_urls:
        candidate = raw.strip()
        if _is_http_url(candidate):
            found.add(candidate)
    for match in _URL_PATTERN.finditer(research_material):
        candidate = _trim_extracted_url(match.group(0))
        if _is_http_url(candidate):
            found.add(candidate)
    return frozenset(found)


def has_quantitative_claim(text: str) -> bool:
    """Return whether text contains a conservative unit-bearing quantity."""

    # URL paths/query strings are evidence locators, not prose claims.
    return bool(iter_quantitative_claims(text))


def iter_quantitative_claims(text: str) -> tuple[str, ...]:
    """Return unique unit-bearing claim strings in source order.

    The exact surface form is retained so a proposal-builder caller can require
    that the model copied a quantity from a source-backed input object instead
    of inventing or normalizing it.
    """

    prose = _URL_PATTERN.sub("", text)
    prose = _ORDINAL_COUNT.sub("", prose)
    return tuple(dict.fromkeys(match.group(0) for match in _QUANTITATIVE_CLAIM.finditer(prose)))


def redact_quantitative_claims(text: str, replacement: str) -> tuple[str, bool]:
    """Replace the same metric claims enforced by ``validate_provenance``."""

    pieces: list[str] = []
    cursor = 0
    count = 0
    # URL paths/query strings are evidence locators and must not be rewritten.
    for match in _URL_PATTERN.finditer(text):
        redacted, replaced = _QUANTITATIVE_CLAIM.subn(
            replacement,
            text[cursor : match.start()],
        )
        pieces.extend((redacted, match.group(0)))
        count += replaced
        cursor = match.end()
    redacted, replaced = _QUANTITATIVE_CLAIM.subn(replacement, text[cursor:])
    pieces.append(redacted)
    return "".join(pieces), count + replaced > 0


def validate_provenance(
    output: ComposerOutput,
    *,
    evidence_urls: Collection[str],
    quantitative_evidence: Mapping[str, Collection[str]] | None = None,
) -> None:
    """Validate citation exactness and same-placeholder quantitative support.

    When ``quantitative_evidence`` is supplied, every exact unit-bearing claim
    must also be present in that mapping and at least one citation on the same
    placeholder must belong to the mapped source object.  This is the stricter
    proposal-builder path; ``None`` preserves the existing proposal_deck
    behavior for callers that only provide free-form research.
    """

    allowed = frozenset(evidence_urls)
    errors: list[str] = []
    valid_citations: dict[int, list[str]] = {}

    for placeholder_id in sorted(output.citations_per_placeholder):
        for citation in output.citations_per_placeholder[placeholder_id]:
            if citation not in allowed:
                errors.append(
                    f"placeholder {{{placeholder_id}}} citation is not present "
                    f"in the input evidence URLs: {citation!r}"
                )
                continue
            valid_citations.setdefault(placeholder_id, []).append(citation)

    claim_sources = (
        {
            claim: frozenset(url for url in urls if url in allowed)
            for claim, urls in quantitative_evidence.items()
        }
        if quantitative_evidence is not None
        else None
    )
    for placeholder_id in sorted(output.placeholders):
        text = output.placeholders[placeholder_id]
        claims = iter_quantitative_claims(text)
        if claims and not valid_citations.get(placeholder_id):
            errors.append(
                f"placeholder {{{placeholder_id}}} contains a quantitative claim "
                "but has no matching evidence citation on the same ID"
            )
            continue
        if claim_sources is None:
            continue
        citations = frozenset(valid_citations.get(placeholder_id, ()))
        for claim in claims:
            sources = claim_sources.get(claim, frozenset())
            if not sources:
                errors.append(
                    f"placeholder {{{placeholder_id}}} quantitative claim {claim!r} "
                    "is not present in source-backed input evidence"
                )
            elif not citations.intersection(sources):
                errors.append(
                    f"placeholder {{{placeholder_id}}} quantitative claim {claim!r} "
                    "does not cite its source object on the same ID"
                )

    if errors:
        raise ProvenanceValidationError(errors)


def validate_composer_provenance(
    output: ComposerOutput,
    *,
    input_urls: Iterable[str],
    research_material: str,
    quantitative_evidence: Mapping[str, Collection[str]] | None = None,
) -> None:
    """Convenience wrapper used by ``ProposalDeckSkill``."""

    validate_provenance(
        output,
        evidence_urls=extract_evidence_urls(input_urls, research_material),
        quantitative_evidence=quantitative_evidence,
    )


__all__ = [
    "ProvenanceValidationError",
    "extract_evidence_urls",
    "has_quantitative_claim",
    "iter_quantitative_claims",
    "redact_quantitative_claims",
    "validate_composer_provenance",
    "validate_provenance",
]
