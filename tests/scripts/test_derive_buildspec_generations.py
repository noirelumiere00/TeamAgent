"""Offline generation rendering uses the same Terraform expressions as deployment."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/deploy"))

import derive_buildspec_generations as generation  # noqa: E402


def manifest() -> dict:
    return json.loads((ROOT / "infra/deploy/buildspec_generation_inputs.json").read_text())


def test_derivation_only_runs_console_without_aws_or_terraform_overrides(monkeypatch):
    expected = {name: "a" * 64 for name in generation.BUILDSPECS}
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "must-not-reach-terraform")
    monkeypatch.setenv("TF_CLI_ARGS_console", "must-not-reach-terraform")

    def console(command, **kwargs):
        assert command[0] == "terraform"
        assert command[2:] == ["console", "-no-color"]
        temporary = Path(command[1].removeprefix("-chdir="))
        assert sorted(path.name for path in temporary.iterdir()) == ["main.tf"]
        module = (temporary / "main.tf").read_text()
        assert "aws_kms_key." not in module
        assert "provider " not in module
        assert "backend " not in module
        assert "AWS_ACCESS_KEY_ID" not in kwargs["env"]
        assert "TF_CLI_ARGS_console" not in kwargs["env"]
        assert kwargs["env"]["CHECKPOINT_DISABLE"] == "1"
        return SimpleNamespace(returncode=0, stdout=json.dumps(json.dumps(expected)))

    monkeypatch.setattr(generation.subprocess, "run", console)
    assert generation.derive(ROOT, manifest()) == expected


def test_current_hashes_and_release_contract_mutation(tmp_path):
    if not shutil.which("terraform"):
        pytest.skip("offline generation rendering requires Terraform")
    expected = manifest()["expected_generation_sha256"]
    actual = generation.derive(ROOT, manifest())
    assert {name: actual[name] for name in expected} == expected

    shutil.copytree(ROOT / "infra/codebuild", tmp_path / "infra/codebuild")
    module = tmp_path / "infra/terraform"
    module.mkdir()
    for name in ("codebuild.tf", "mcp_approval.tf"):
        shutil.copy(ROOT / "infra/terraform" / name, module / name)
    contract = tmp_path / "infra/codebuild/teamagent_core_media_release_contract.json"
    contract.write_bytes(contract.read_bytes() + b"\n")
    changed = generation.derive(tmp_path, manifest())
    assert {name for name in actual if actual[name] != changed[name]} == set(
        generation.BUILDSPECS
    ) - {"teamagent-dev-approval-publisher"}


def test_missing_frozen_kms_key_fails_closed():
    invalid = manifest()
    invalid["kms_keys"].pop("mcp_source_publisher_signing")
    with pytest.raises(KeyError, match="mcp_source_publisher_signing"):
        generation.terraform_module(ROOT, invalid)
