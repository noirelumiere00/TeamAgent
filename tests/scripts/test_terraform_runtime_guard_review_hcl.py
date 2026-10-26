"""Review/final admission and the apply blocker, evaluated without AWS or a plan."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TF_ROOT = Path(__file__).resolve().parents[2] / "infra" / "terraform"


def _block(path: Path, marker: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(marker)} \{{\n.*?^\}}", path.read_text())
    assert match is not None, f"missing Terraform block: {marker}"
    return match.group()


def _admission_expression() -> str:
    match = re.search(
        r"(?ms)^  runtime_migration_review_authorized = (.*?)(?=^  \w+\s*=|^\})",
        (TF_ROOT / "runtime_guard.tf").read_text(),
    )
    assert match is not None, "missing review/final migration admission condition"
    return match.group(1).strip()


def _terraform(directory: Path, *args: str, expression: str | None = None) -> str:
    binary = shutil.which("terraform")
    if binary is None:
        pytest.skip("offline HCL evaluation requires Terraform")
    env = {key: value for key, value in os.environ.items() if not key.startswith("TF_")}
    env.update(
        LC_ALL="C",
        LANG="C",
        TF_CLI_CONFIG_FILE="/dev/null",
        TF_DATA_DIR=str(directory / "data"),
        TF_IN_AUTOMATION="1",
        CHECKPOINT_DISABLE="1",
    )
    result = subprocess.run(
        [binary, *args],
        cwd=directory,
        env=env,
        input=expression,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize(
    ("review", "migration", "authorized"),
    [
        (True, {"enabled": False, "reviewed_plan": None}, True),
        (True, {"enabled": True, "reviewed_plan": None}, False),
        (True, {"enabled": False, "reviewed_plan": {}}, False),
        (True, {"reviewed_plan": None}, False),
        (False, {"enabled": True, "reviewed_plan": {}}, True),
        (False, {"enabled": False, "reviewed_plan": {}}, False),
        (False, {"enabled": True, "reviewed_plan": None}, False),
        (False, {}, False),
    ],
)
def test_migration_admission_uses_production_hcl(
    tmp_path: Path, review: bool, migration: dict[str, object], authorized: bool
) -> None:
    variable = _block(TF_ROOT / "runtime_guard.tf", 'variable "runtime_guard_review_only"')
    (tmp_path / "main.tf").write_text(
        variable
        + '\nvariable "migration_json" { type = string }\n'
        + "locals {\n"
        + "  runtime_selected_migration = jsondecode(var.migration_json)\n"
        + f"  runtime_migration_review_authorized = {_admission_expression()}\n"
        + "}\n"
    )
    assert (
        _terraform(
            tmp_path,
            "console",
            "-no-color",
            f"-var=runtime_guard_review_only={str(review).lower()}",
            f"-var=migration_json={json.dumps(migration)}",
            expression="local.runtime_migration_review_authorized\n",
        )
        == str(authorized).lower()
    )


def test_review_blocker_is_apply_time_false_and_valid_hcl(tmp_path: Path) -> None:
    variable = _block(TF_ROOT / "runtime_guard.tf", 'variable "runtime_guard_review_only"')
    blocker = _block(
        TF_ROOT / "runtime_guard.tf",
        'resource "terraform_data" "runtime_guard_review_blocker"',
    )
    assert re.search(r"count\s*=\s*var\.runtime_guard_review_only\s*\?\s*1\s*:\s*0", blocker)
    condition = re.search(r"(?m)^\s*condition\s*=\s*(.+)$", blocker)
    assert condition is not None
    assert "timestamp()" in condition.group(1), "blocker must defer until apply"
    (tmp_path / "main.tf").write_text(
        variable + '\nvariable "aws_region" { default = "ap-northeast-1" }\n' + blocker + "\n"
    )
    _terraform(tmp_path, "validate", "-no-color")
    assert (
        _terraform(
            tmp_path,
            "console",
            "-no-color",
            "-var=runtime_guard_review_only=true",
            expression=condition.group(1) + "\n",
        )
        == "false"
    )


@pytest.mark.parametrize("review", [True, False])
@pytest.mark.parametrize(
    ("filename", "region_key", "expected"),
    [
        ("main.tf", "aws_region", "ap-northeast-1"),
        ("budgets.tf", "billing_region", "us-east-1"),
    ],
)
def test_aws_provider_regions_depend_on_known_blocker_input_only_for_review(
    tmp_path: Path, review: bool, filename: str, region_key: str, expected: str
) -> None:
    provider = _block(TF_ROOT / filename, 'provider "aws"')
    region = re.search(r"(?ms)^  region\s*=\s*(.*?)(?=^  \w+\s*=|^  default_tags|^\})", provider)
    assert region is not None
    expression = region.group(1).strip()
    reference = f"terraform_data.runtime_guard_review_blocker[0].input.{region_key}"
    assert reference in expression, "AWS provider must await the apply blocker"
    assert "var.runtime_guard_review_only" in expression
    blocker = _block(
        TF_ROOT / "runtime_guard.tf",
        'resource "terraform_data" "runtime_guard_review_blocker"',
    )
    inputs = re.search(r"(?ms)^  input = (\{.*?^  \})", blocker)
    assert inputs is not None, "provider region inputs must be known before apply"
    expression = expression.replace(
        "terraform_data.runtime_guard_review_blocker[0].input", "local.blocker_input"
    )
    variable = _block(TF_ROOT / "runtime_guard.tf", 'variable "runtime_guard_review_only"')
    (tmp_path / "main.tf").write_text(
        variable
        + '\nvariable "aws_region" { default = "ap-northeast-1" }\n'
        + "locals {\n"
        + f"  blocker_input = {inputs.group(1)}\n"
        + f"  provider_region = {expression}\n"
        + "}\n"
    )
    assert _terraform(
        tmp_path,
        "console",
        "-no-color",
        f"-var=runtime_guard_review_only={str(review).lower()}",
        expression="local.provider_region\n",
    ) == json.dumps(expected)


def test_every_aws_provider_configuration_is_covered() -> None:
    providers = [
        path.name
        for path in TF_ROOT.glob("*.tf")
        for _match in re.finditer(r'(?m)^provider "aws" \{', path.read_text())
    ]
    assert sorted(providers) == ["budgets.tf", "main.tf"]


def test_production_gate_blocks_local_exec_mutation_hooks_for_review() -> None:
    gate = _block(
        TF_ROOT / "image_release_gate.tf",
        'resource "terraform_data" "production_image_release_gate"',
    )
    assert "depends_on = [terraform_data.runtime_guard_review_blocker]" in gate


def test_review_admission_is_wired_into_migration_and_rejected_for_sync() -> None:
    body = (TF_ROOT / "runtime_guard.tf").read_text()
    verified = body[
        body.index("  runtime_guard_verified =") : body.index("  runtime_guard_error =")
    ]
    assert "local.runtime_migration_review_authorized" in verified
    sync, migration = verified.split("      ) :", maxsplit=1)
    assert "!var.runtime_guard_review_only" in sync
    assert "try(local.runtime_selected_migration.enabled, false)" not in migration
