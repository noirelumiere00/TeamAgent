"""Lambda archive allowlistとpycache非依存hash契約を固定する。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
EXCLUDES = 'excludes         = ["__pycache__", "**/__pycache__/**"]'


def _archive_block(path: Path, name: str) -> str:
    body = path.read_text(encoding="utf-8")
    match = re.search(
        rf'data "archive_file" "{re.escape(name)}" \{{(?P<body>.*?)\n\}}',
        body,
        flags=re.DOTALL,
    )
    assert match is not None, f"archive_file.{name}が見つかりません"
    return match.group("body")


@pytest.mark.parametrize(
    ("relative_path", "name", "source_attribute", "source", "has_excludes"),
    [
        (
            "infra/terraform/reminders.tf",
            "reminder_notify",
            "source_dir",
            "${path.module}/lambda/reminder_notify",
            True,
        ),
        (
            "infra/terraform/tiktok_acquire.tf",
            "tiktok_dispatch",
            "source_dir",
            "${path.module}/lambda/tiktok_dispatch",
            True,
        ),
        (
            "infra/terraform/x_research.tf",
            "x_dispatch",
            "source_file",
            "${path.module}/lambda/x_dispatch/handler.py",
            False,
        ),
    ],
)
def test_lambda_archives_are_explicit_allowlists(
    relative_path: str,
    name: str,
    source_attribute: str,
    source: str,
    has_excludes: bool,
) -> None:
    block = _archive_block(PROJECT_ROOT / relative_path, name)
    assert re.search(
        rf"(?m)^\s*{re.escape(source_attribute)}\s*=\s*{re.escape(json.dumps(source))}\s*$",
        block,
    )
    assert re.search(r'(?m)^\s*output_file_mode\s*=\s*"0644"\s*$', block)
    if has_excludes:
        assert EXCLUDES in block
        assert "source_file" not in block
    else:
        assert "source_dir" not in block


@pytest.mark.parametrize(
    "relative_dir",
    [
        "infra/terraform/lambda/reminder_notify",
        "infra/terraform/lambda/tiktok_dispatch",
    ],
)
def test_directory_archives_contain_only_handler_and_ignored_pycache(
    relative_dir: str,
) -> None:
    root = PROJECT_ROOT / relative_dir
    unexpected = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.relative_to(root).as_posix() != "handler.py"
        and "__pycache__" not in path.relative_to(root).parts
    ]
    assert unexpected == []


def _archive_provider_binary() -> Path | None:
    candidates = sorted(
        (
            PROJECT_ROOT
            / "infra/terraform/.terraform/providers/registry.terraform.io/hashicorp/archive"
        ).glob("*/*/terraform-provider-archive*")
    )
    return candidates[-1] if candidates else None


def test_archive_provider_hash_is_stable_with_or_without_pycache(
    tmp_path: Path,
) -> None:
    """Provider実物でexcludesがZIP/hashを安定化することを確認する。"""

    terraform = shutil.which("terraform")
    provider = _archive_provider_binary()
    if terraform is None or provider is None:
        pytest.skip("local Terraform archive provider is unavailable")

    clean = tmp_path / "clean"
    dirty = tmp_path / "dirty"
    clean.mkdir()
    dirty.mkdir()
    handler = b"def handler(event, context):\n    return {'ok': True}\n"
    for directory in (clean, dirty):
        path = directory / "handler.py"
        path.write_bytes(handler)
        path.chmod(0o644)
        os.utime(path, (1_700_000_000, 1_700_000_000))
    pycache = dirty / "__pycache__"
    pycache.mkdir()
    (pycache / "handler.cpython-314.pyc").write_bytes(b"worktree-specific-cache")

    module = tmp_path / "module"
    module.mkdir()
    module.joinpath("main.tf").write_text(
        f"""
terraform {{
  required_providers {{
    archive = {{
      source = "hashicorp/archive"
      version = "= 2.8.0"
    }}
  }}
}}

data "archive_file" "clean" {{
  type             = "zip"
  source_dir       = {json.dumps(str(clean))}
  output_path      = {json.dumps(str(module / "clean.zip"))}
  output_file_mode = "0644"
  excludes         = ["__pycache__", "**/__pycache__/**"]
}}

data "archive_file" "dirty" {{
  type             = "zip"
  source_dir       = {json.dumps(str(dirty))}
  output_path      = {json.dumps(str(module / "dirty.zip"))}
  output_file_mode = "0644"
  excludes         = ["__pycache__", "**/__pycache__/**"]
}}

output "clean_hash" {{
  value = data.archive_file.clean.output_base64sha256
}}

output "dirty_hash" {{
  value = data.archive_file.dirty.output_base64sha256
}}
""",
        encoding="utf-8",
    )
    init = subprocess.run(
        [
            terraform,
            f"-chdir={module}",
            "init",
            "-backend=false",
            "-input=false",
            (f"-plugin-dir={PROJECT_ROOT / 'infra/terraform/.terraform/providers'}"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert init.returncode == 0, init.stdout + init.stderr
    plan_path = module / "archive.tfplan"
    plan = subprocess.run(
        [
            terraform,
            f"-chdir={module}",
            "plan",
            f"-out={plan_path}",
            "-refresh=false",
            "-input=false",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert plan.returncode == 0, plan.stdout + plan.stderr
    shown = subprocess.run(
        [terraform, f"-chdir={module}", "show", "-json", str(plan_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    values = json.loads(shown.stdout)["planned_values"]["outputs"]
    assert values["clean_hash"]["value"] == values["dirty_hash"]["value"]
    assert (module / "clean.zip").read_bytes() == (module / "dirty.zip").read_bytes()
