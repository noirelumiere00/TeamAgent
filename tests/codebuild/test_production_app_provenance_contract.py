from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_LOG = ROOT / "infra" / "deploy_log.md"

CURRENT_PRODUCTION = {
    "schema_version": 1,
    "app_html_s3_version_id": "FTXbcN70D0DCN90TI_hRK1IdQK_HhLee",
    "app_html_sha256": ("03f8e8cc0adbc397cc636e30fcc8baaffeb1c53502cf74baf1031399cceb391c"),
    "vault_manifest_sha256": ("aa451e744d26e9dc13c170b019307b0eb10d3645267960fbff41c4038e9b909e"),
    "build_inputs_sha256": ("6697acf311f0c9a96b41426e81ae05ad221482a6e6f69799281ad3532c2e78bf"),
}
STALE_ROLLBACK = {
    "app_html_s3_version_id": "I1qOb7Kwl.pMg71wqFxbHnbbTqMWjQcY",
    "app_html_sha256": ("46f0079783cde24b066c7823b7d6672bad12b33debf933a4d7a7ff04b7a3b067"),
    "vault_manifest_sha256": ("15663a838b1bd648443949244c02e66ccfd6cb7b684390baeb1a86efcdd6d4a2"),
    "build_inputs_sha256": ("1ca6f0213155d8d4dbef4220f641dbb38310fe79473f6c013ef4e54dfa6a87e2"),
}
ALL_EVIDENCE_KEYS = frozenset(CURRENT_PRODUCTION) - {"schema_version"}
CANONICAL_CONSUMERS = {
    ROOT / "infra" / "codebuild" / "buildspec.yml": ALL_EVIDENCE_KEYS,
    ROOT / "infra" / "codebuild" / "mcp-source-publisher-buildspec.yml": ALL_EVIDENCE_KEYS,
    ROOT / "infra" / "codebuild" / "image-attestor-buildspec.yml": ALL_EVIDENCE_KEYS,
    ROOT / "infra" / "codebuild" / "release_evidence.py": ALL_EVIDENCE_KEYS,
    ROOT / "infra" / "deploy" / "build_teamagent_image.sh": ALL_EVIDENCE_KEYS,
    ROOT / "infra" / "terraform" / "codebuild.tf": ALL_EVIDENCE_KEYS,
    ROOT / "infra" / "terraform" / "README.md": ALL_EVIDENCE_KEYS,
    ROOT / "infra" / "codebuild" / "verify_actual_image.sh": frozenset({"app_html_sha256"}),
    ROOT / "tests" / "codebuild" / "test_buildspec_contract.py": ALL_EVIDENCE_KEYS,
    ROOT / "tests" / "codebuild" / "test_release_evidence.py": ALL_EVIDENCE_KEYS,
    ROOT / "tests" / "scripts" / "test_build_teamagent_image.py": ALL_EVIDENCE_KEYS,
    ROOT / "tests" / "codebuild" / "test_actual_image_evidence.py": frozenset({"app_html_sha256"}),
    ROOT / "tests" / "codebuild" / "test_launcher_iam_contract.py": frozenset(
        {"app_html_s3_version_id", "app_html_sha256"}
    ),
}
PROVENANCE_RECORD_RE = re.compile(r"^<!-- PRODUCTION_APP_PROVENANCE=(\{.+\}) -->$", re.MULTILINE)


def _latest_production_app_record() -> tuple[dict[str, Any], str]:
    body = DEPLOY_LOG.read_text(encoding="utf-8")
    sections = re.split(r"(?m)^## ", body)
    assert len(sections) > 1, "deploy log has no production entries"

    production_sections = [section.split("\n---\n", maxsplit=1)[0] for section in sections[1:]]
    latest_section = next(
        (section for section in production_sections if "/app" in section.partition("\n")[0]),
        None,
    )
    assert latest_section is not None, "deploy log has no /app production entry"
    heading, separator, details = latest_section.partition("\n")
    assert separator
    assert "/app" in heading
    assert "本番" in heading

    match = PROVENANCE_RECORD_RE.search(details)
    assert match, "latest production entry lacks machine-readable app provenance"
    record = json.loads(match.group(1))

    assert set(record) == set(CURRENT_PRODUCTION)
    assert record["schema_version"] == 1
    for key in ("app_html_sha256", "vault_manifest_sha256", "build_inputs_sha256"):
        assert re.fullmatch(r"[0-9a-f]{64}", record[key])
    assert re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,1024}", record["app_html_s3_version_id"])

    prose = details[: match.start()] + details[match.end() :]
    for key in ALL_EVIDENCE_KEYS:
        assert record[key] in prose, f"{key} marker is not corroborated by latest prose"

    return record, heading


def test_latest_deploy_record_is_the_exact_current_production_allowlist() -> None:
    record, heading = _latest_production_app_record()

    assert heading.startswith("2026-07-17 ")
    assert record == CURRENT_PRODUCTION
    assert set(record.values()).isdisjoint(STALE_ROLLBACK.values())


def test_newer_non_app_deploy_does_not_shadow_current_app_provenance() -> None:
    body = DEPLOY_LOG.read_text(encoding="utf-8")
    latest_section = re.split(r"(?m)^## ", body)[1].split("\n---\n", maxsplit=1)[0]
    latest_heading = latest_section.partition("\n")[0]
    record, app_heading = _latest_production_app_record()

    assert "Slack本人確認" in latest_heading
    assert "task definition `:50`→`:53`" in latest_section
    assert "task definition `:13`→`:14`" in latest_section
    assert "現行image digest" in latest_section
    assert app_heading != latest_heading
    assert record == CURRENT_PRODUCTION


def test_all_canonical_consumers_follow_latest_production_record() -> None:
    record, _ = _latest_production_app_record()

    for path, evidence_keys in CANONICAL_CONSUMERS.items():
        body = path.read_text(encoding="utf-8")
        for key in evidence_keys:
            assert record[key] in body, f"{path.relative_to(ROOT)} does not pin {key}"
        for key, stale_value in STALE_ROLLBACK.items():
            assert stale_value not in body, (
                f"{path.relative_to(ROOT)} still treats stale {key} as canonical"
            )


def test_stale_allowlist_is_preserved_as_explicit_deploy_history() -> None:
    body = DEPLOY_LOG.read_text(encoding="utf-8")

    for stale_value in STALE_ROLLBACK.values():
        assert stale_value in body
