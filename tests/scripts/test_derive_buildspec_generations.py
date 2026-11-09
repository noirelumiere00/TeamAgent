"""Offline rendering must reproduce the exact content-addressed generations."""

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/deploy"))

from derive_buildspec_generations import render_buildspecs  # noqa: E402


@pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform is not installed")
def test_offline_rendering_matches_the_generation_manifest() -> None:
    manifest = json.loads((ROOT / "infra/deploy/buildspec_generation_inputs.json").read_text())
    bodies = render_buildspecs(ROOT)
    assert set(bodies) == {"mcp_source_publisher", "image_attestor", "image_promoter"}
    for name, body in bodies.items():
        project = f"teamagent-dev-{name.replace('_', '-')}"
        assert (
            hashlib.sha256(body.encode()).hexdigest()
            == manifest["expected_generation_sha256"][project]
        )
        assert "__RELEASE_EVIDENCE_BASE64__" not in body


def test_missing_kms_binding_fails_before_terraform(tmp_path: Path) -> None:
    (tmp_path / "infra/deploy").mkdir(parents=True)
    (tmp_path / "infra/terraform").mkdir()
    (tmp_path / "infra/deploy/buildspec_generation_inputs.json").write_text('{"kms_keys": {}}')
    (tmp_path / "infra/terraform/codebuild.tf").write_bytes(
        (ROOT / "infra/terraform/codebuild.tf").read_bytes()
    )
    with pytest.raises(ValueError, match="every referenced KMS key"):
        render_buildspecs(tmp_path)
