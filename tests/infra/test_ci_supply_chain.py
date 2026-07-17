from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
FULL_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def test_third_party_actions_are_pinned_to_full_commit_shas() -> None:
    for workflow in WORKFLOWS.glob("*.y*ml"):
        for line_number, line in enumerate(workflow.read_text().splitlines(), start=1):
            match = re.match(r"\s*(?:-\s*)?uses:\s*([^\s#]+)", line)
            if match is None:
                continue

            action = match.group(1)
            if action.startswith("./"):
                continue
            _, separator, revision = action.rpartition("@")
            assert separator and FULL_COMMIT_SHA.fullmatch(revision), (
                f"{workflow.relative_to(ROOT)}:{line_number}: "
                "external actions must use a full commit SHA"
            )


def test_gitleaks_container_is_pinned_to_an_immutable_digest() -> None:
    workflow = (WORKFLOWS / "ci.yml").read_text()
    image = re.search(
        r"zricethezav/gitleaks:[^\s\\@]+@sha256:([0-9a-f]{64})\s+detect",
        workflow,
    )
    assert image is not None
