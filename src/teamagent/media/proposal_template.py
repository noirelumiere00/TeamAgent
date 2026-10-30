"""Pinned integrated-template inventories shared by provisioning and both renderers."""

import hashlib
from pathlib import Path

# 元47頁はKeynote実描画で88字が右端ぎりぎりだったため、80字に抑える。
PR_CANDIDATE_LINE_CHARS = 80
PR_CANDIDATE_MAX_LINES = 5

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
INTEGRATED_TEMPLATE_SLIDE_COUNT = 86
KNOWN_TEMPLATE_SLIDES = {
    "07ea41700933d380295c5b8e713d803c8276c46c94b18008dbd8bf230539f883": 86,
}


def template_auxiliary_keys(path: Path, slide_count: int) -> frozenset[str]:
    """Require the canonical 86-page inventory and reviewed bytes in every renderer."""
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    expected_count = KNOWN_TEMPLATE_SLIDES.get(digest)
    if slide_count != INTEGRATED_TEMPLATE_SLIDE_COUNT or slide_count != expected_count:
        raise ValueError("integrated template slide count or SHA-256 is invalid")
    return BASE_AUXILIARY_KEYS | BU2_AUXILIARY_KEYS
