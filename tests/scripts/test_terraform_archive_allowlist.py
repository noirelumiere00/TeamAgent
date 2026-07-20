"""Lambda archiveをhandler.pyだけへ固定し、worktreeノイズをhashから除外する。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


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
    ("relative_path", "name", "source"),
    [
        (
            "infra/terraform/reminders.tf",
            "reminder_notify",
            "${path.module}/lambda/reminder_notify/handler.py",
        ),
        (
            "infra/terraform/tiktok_acquire.tf",
            "tiktok_dispatch",
            "${path.module}/lambda/tiktok_dispatch/handler.py",
        ),
        (
            "infra/terraform/x_research.tf",
            "x_dispatch",
            "${path.module}/lambda/x_dispatch/handler.py",
        ),
    ],
)
def test_lambda_archives_are_explicit_allowlists(
    relative_path: str,
    name: str,
    source: str,
) -> None:
    block = _archive_block(PROJECT_ROOT / relative_path, name)
    assert re.search(
        rf"(?m)^\s*source_file\s*=\s*{re.escape(json.dumps(source))}\s*$",
        block,
    )
    assert re.search(r'(?m)^\s*output_file_mode\s*=\s*"0644"\s*$', block)
    assert "source_dir" not in block
    assert "excludes" not in block
    assert "source {" not in block


def test_archive_provider_is_exactly_pinned_and_lock_is_tracked() -> None:
    main = (PROJECT_ROOT / "infra/terraform/main.tf").read_text(encoding="utf-8")
    lock_path = PROJECT_ROOT / "infra/terraform/.terraform.lock.hcl"
    lock = lock_path.read_text(encoding="utf-8")
    archive = re.search(
        r"archive = \{(?P<body>.*?)\n\s*\}",
        main,
        flags=re.DOTALL,
    )
    assert archive is not None
    assert 'source  = "hashicorp/archive"' in archive.group("body")
    assert 'version = "= 2.8.0"' in archive.group("body")
    provider = re.search(
        r'provider "registry\.terraform\.io/hashicorp/archive" \{(?P<body>.*?)\n\}',
        lock,
        flags=re.DOTALL,
    )
    assert provider is not None
    assert 'version     = "2.8.0"' in provider.group("body")
    assert 'constraints = "2.8.0"' in provider.group("body")
    tracked = subprocess.run(
        [
            "git",
            "-C",
            str(PROJECT_ROOT),
            "ls-files",
            "--error-unmatch",
            "infra/terraform/.terraform.lock.hcl",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode == 0, tracked.stderr


def _archive_provider_binary() -> Path | None:
    candidates = sorted(
        (
            PROJECT_ROOT
            / "infra/terraform/.terraform/providers/registry.terraform.io/hashicorp/archive"
        ).glob("*/*/terraform-provider-archive*")
    )
    return candidates[-1] if candidates else None


def test_archive_entry_set_and_hash_ignore_all_unlisted_worktree_files(
    tmp_path: Path,
) -> None:
    """Provider実物でsource_fileのZIPがhandler.py一件だけになることを確認する。"""

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
    (dirty / ".DS_Store").write_bytes(b"finder-metadata")
    (dirty / "handler.pyc").write_bytes(b"root-bytecode")
    (dirty / ".handler.py.swp").write_bytes(b"editor-state")

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
  source_file      = {json.dumps(str(clean / "handler.py"))}
  output_path      = {json.dumps(str(module / "clean.zip"))}
  output_file_mode = "0644"
}}

data "archive_file" "dirty" {{
  type             = "zip"
  source_file      = {json.dumps(str(dirty / "handler.py"))}
  output_path      = {json.dumps(str(module / "dirty.zip"))}
  output_file_mode = "0644"
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
    if (
        plan.returncode != 0
        and sys.platform == "darwin"
        and "Failed to load plugin schemas" in plan.stderr
        and "terraform-provider-archive" in plan.stderr
    ):
        pytest.skip("local macOS archive provider cannot start in this host sandbox")
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
    for archive_path in (module / "clean.zip", module / "dirty.zip"):
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.namelist() == ["handler.py"]
