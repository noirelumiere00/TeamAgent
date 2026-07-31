"""Normalized confidential-term matching for rendered proposal outputs."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from urllib.parse import unquote, urlsplit

_URI_PATTERN = re.compile(
    r"(?:https?|slack|gdrive)://[^\s<>{}\\^`\"']+",
    flags=re.IGNORECASE,
)
_PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
_MAX_PERCENT_DECODE_ROUNDS = 5


def _prose_pattern(term: str) -> re.Pattern[str]:
    candidate = unicodedata.normalize("NFKC", term).casefold()
    escaped = re.escape(candidate)
    if candidate.isascii() and candidate.isalnum() and len(candidate) <= 3:
        escaped = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    return re.compile(escaped)


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _decoded_variants(value: str) -> tuple[tuple[str, ...], bool]:
    """Decode percent escapes to a fixed point and report cap ambiguity.

    NFKC is applied after every round so an encoded full-width brand cannot
    bypass comparison.  If five rounds are insufficient, callers reject the
    value rather than guessing whether a deeper encoding hides a forbidden term.
    """

    variants = [_normalize(value)]
    decoded = variants[0]
    for _ in range(_MAX_PERCENT_DECODE_ROUNDS):
        next_decoded = _normalize(unquote(decoded))
        if next_decoded == decoded:
            break
        variants.append(next_decoded)
        decoded = next_decoded
    next_after_cap = _normalize(unquote(decoded))
    ambiguous = (
        next_after_cap != decoded
        or (
            len(variants) > _MAX_PERCENT_DECODE_ROUNDS
            and _PERCENT_ESCAPE.search(decoded) is not None
        )
    )
    for candidate in tuple(variants):
        try:
            hostname = urlsplit(candidate).hostname
            if hostname and hostname.isascii():
                variants.append(_normalize(hostname.encode("ascii").decode("idna")))
        except (UnicodeError, ValueError):
            continue
    return tuple(dict.fromkeys(variants)), ambiguous


def contains_forbidden_term(text: str, terms: Iterable[str]) -> bool:
    """Match prose with token boundaries and URI values with fail-closed substrings.

    A short Latin brand such as ``X`` must not match an unrelated prose word, but
    URLs are identifiers rather than prose. Therefore a brand embedded in
    ``acmeproduct.example`` or a percent-encoded/IDNA URI is still confidential.
    """

    normalized = _normalize(text)
    normalized_terms = [
        (term, _normalize(term))
        for term in terms
        if _normalize(term)
    ]
    if not normalized_terms:
        return False
    prose_variants, prose_ambiguous = _decoded_variants(normalized)
    uri_variants: list[str] = []
    uri_ambiguous = False
    for uri in _URI_PATTERN.findall(normalized):
        variants, ambiguous = _decoded_variants(uri)
        uri_variants.extend(variants)
        uri_ambiguous = uri_ambiguous or ambiguous
    if prose_ambiguous or uri_ambiguous:
        return True
    for term, needle in normalized_terms:
        pattern = _prose_pattern(term)
        if any(pattern.search(candidate) for candidate in prose_variants):
            return True
        if any(needle in candidate for candidate in uri_variants):
            return True
    return False


__all__ = ["contains_forbidden_term"]
