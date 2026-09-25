"""personal_memory の依存純度と未配線状態を固定する。"""

from __future__ import annotations

import ast
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
_PERSONAL_MEMORY_ROOT = _SOURCE_ROOT / "teamagent" / "personal_memory"

_ALLOWED_STDLIB = frozenset(
    {
        "__future__",
        "collections.abc",
        "dataclasses",
        "enum",
        "re",
        "typing",
        "unicodedata",
    }
)
_PACKAGE = "teamagent.personal_memory"


def _import_nodes(path: Path) -> list[ast.Import | ast.ImportFrom]:
    """ファイル内の静的 import 文を返す。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]


def _is_allowed_module(module: str) -> bool:
    return module in _ALLOWED_STDLIB or module == _PACKAGE or module.startswith(f"{_PACKAGE}.")


def _is_allowed_import(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.Import):
        return all(_is_allowed_module(alias.name) for alias in node.names)

    if node.level:
        return node.level == 1
    return node.module is not None and _is_allowed_module(node.module)


def _imports_personal_memory(node: ast.Import | ast.ImportFrom) -> bool:
    """import 文が personal_memory パッケージを参照するかを返す。"""
    if isinstance(node, ast.Import):
        return any("personal_memory" in alias.name.split(".") for alias in node.names)

    module_parts = (node.module or "").split(".")
    return "personal_memory" in module_parts or any(
        alias.name.split(".", maxsplit=1)[0] == "personal_memory" for alias in node.names
    )


def _location(path: Path, node: ast.Import | ast.ImportFrom) -> str:
    relative_path = path.relative_to(_REPOSITORY_ROOT)
    return f"{relative_path}:{node.lineno}: {ast.unparse(node)}"


def test_personal_memory_uses_only_allowed_imports() -> None:
    """実装は標準ライブラリと同一パッケージにだけ依存する。"""
    paths = sorted(_PERSONAL_MEMORY_ROOT.glob("*.py"))
    violations = [
        _location(path, node)
        for path in paths
        for node in _import_nodes(path)
        if not _is_allowed_import(node)
    ]

    assert paths, "personal_memory の実装ファイルがありません"
    assert not violations, "許可されていない import があります:\n" + "\n".join(violations)


def test_personal_memory_is_not_imported_elsewhere_in_src() -> None:
    """dark 機能として src 内の既存コードへ未配線であることを保証する。"""
    violations = [
        _location(path, node)
        for path in sorted(_SOURCE_ROOT.rglob("*.py"))
        if not path.is_relative_to(_PERSONAL_MEMORY_ROOT)
        for node in _import_nodes(path)
        if _imports_personal_memory(node)
    ]

    assert not violations, "personal_memory が配線されています:\n" + "\n".join(violations)
