"""Lambda archive_fileがworktree生成物をzipへ混入させない契約を固定する。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
EXPECTED_EXCLUDES = '["__pycache__", "**/__pycache__/**"]'


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
    ("relative_path", "name"),
    [
        ("infra/terraform/reminders.tf", "reminder_notify"),
        ("infra/terraform/tiktok_acquire.tf", "tiktok_dispatch"),
        ("infra/terraform/x_research.tf", "x_dispatch"),
    ],
)
def test_lambda_archives_exclude_root_and_nested_pycache(relative_path: str, name: str) -> None:
    block = _archive_block(PROJECT_ROOT / relative_path, name)
    excludes = re.findall(r"^\s*excludes\s*=\s*(.+)$", block, flags=re.MULTILINE)
    assert excludes == [EXPECTED_EXCLUDES]


def test_both_patterns_are_required_for_archive_provider_doublestar() -> None:
    """root直下と再帰配置の双方を明示してproviderのglob解釈差を封じる。"""
    assert "__pycache__" in EXPECTED_EXCLUDES
    assert "**/__pycache__/**" in EXPECTED_EXCLUDES
