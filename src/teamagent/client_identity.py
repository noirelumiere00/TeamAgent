"""Deterministic client identity matching shared by export and presentation layers.

The key deliberately removes legal forms only at name boundaries and honorifics only
at the end.  Broad substring replacement is unsafe for ownership decisions because,
for example, ``熱さまシート`` contains ``さま`` as part of the product name.
"""

from __future__ import annotations

import re
import unicodedata

CLIENT_LEGAL_FORMS: tuple[str, ...] = (
    "地方独立行政法人",
    "特定非営利活動法人",
    "社会保険労務士法人",
    "公益社団法人",
    "一般社団法人",
    "公益財団法人",
    "一般財団法人",
    "国立大学法人",
    "独立行政法人",
    "社会福祉法人",
    "農事組合法人",
    "弁護士法人",
    "税理士法人",
    "行政書士法人",
    "司法書士法人",
    "地方公共団体",
    "医療法人",
    "学校法人",
    "宗教法人",
    "監査法人",
    "株式会社",
    "株式會社",
    "有限会社",
    "合同会社",
    "合名会社",
    "合資会社",
    "(株)",
    "(有)",
)
CLIENT_HONORIFICS: tuple[str, ...] = ("御中", "さま", "様", "殿")
_CLIENT_SEPARATORS_RE = re.compile(r"[\s・･]+")


def client_identity_key(value: object) -> str:
    """Return a conservative comparison key for client ownership relationships.

    NFKC/case folding and spacing variants are absorbed.  Corporate forms may be
    prefixed or suffixed, while honorifics are accepted only as suffixes.  No token is
    removed from the middle of a name, so unrelated words cannot collapse together.
    """

    key = _CLIENT_SEPARATORS_RE.sub(
        "", unicodedata.normalize("NFKC", str(value or "")).strip()
    ).casefold()
    if not key:
        return ""

    legal_forms = tuple(
        _CLIENT_SEPARATORS_RE.sub("", unicodedata.normalize("NFKC", form)).casefold()
        for form in CLIENT_LEGAL_FORMS
    )
    honorifics = tuple(
        unicodedata.normalize("NFKC", honorific).casefold() for honorific in CLIENT_HONORIFICS
    )
    changed = True
    while key and changed:
        changed = False
        for honorific in honorifics:
            if key.endswith(honorific) and len(key) > len(honorific):
                key = key[: -len(honorific)]
                changed = True
                break
        for form in legal_forms:
            if key.startswith(form) and len(key) > len(form):
                key = key[len(form) :]
                changed = True
                break
            if key.endswith(form) and len(key) > len(form):
                key = key[: -len(form)]
                changed = True
                break
    return key
