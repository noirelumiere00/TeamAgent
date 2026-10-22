"""Pinned integrated-template inventories shared by provisioning and both renderers."""

import hashlib
from pathlib import Path

BASE_AUXILIARY_KEYS = frozenset(
    {
        "PB-ACCOUNTS",
        "PB-CASES",
        "PB-CLIENT-NAME",
        "PB-DATETIME",
        "PB-EXPERIENCE",
        "PB-KEY-MESSAGE",
        "PB-MONTH",
        "PB-PRODUCT-NAME",
    }
)
BU2_AUXILIARY_KEYS = frozenset(
    {
        f"PB-BU2-PLAN-{plan}-{field}"
        for plan in ("TRIAL", "A", "B", "C")
        for field in ("COUNT", "VIEWS", "UNIT-PRICE", "PERIOD", "AMOUNT")
    }
    | {
        f"PB-BU2-SIM-{field}"
        for field in ("VIEWS", "CONTACTS", "REACH", "RATE", "RESULT", "ASSUMPTION")
    }
    | {"PB-BU2-ESTIMATE-NOTES"}
    | {f"PB-BU2-HEARING-{number}" for number in range(1, 7)}
)
KNOWN_TEMPLATE_SLIDES = {
    "62d39a2b76e0dedc6cdaae8de5cd224b80c5b93ddbb3beb2f01add67c27f0490": 83,
    "07ea41700933d380295c5b8e713d803c8276c46c94b18008dbd8bf230539f883": 86,
}


def template_auxiliary_keys(path: Path, slide_count: int) -> frozenset[str]:
    """The BU2 extension requires the reviewed bytes, not just a matching page count."""
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    expected_count = KNOWN_TEMPLATE_SLIDES.get(digest)
    if slide_count != expected_count:
        raise ValueError("integrated template slide count or SHA-256 is invalid")
    return BASE_AUXILIARY_KEYS | BU2_AUXILIARY_KEYS if expected_count == 86 else BASE_AUXILIARY_KEYS
