"""personal_memory（guard）の依存純度と、配線先を固定する。"""

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
    """import 文が guard パッケージ（teamagent.personal_memory）を参照するかを返す。

    名前の前方一致で判定する（teamagent.mcp_gateway.personal_memory は別パッケージ）。
    """
    if isinstance(node, ast.Import):
        return any(_is_guard_module(alias.name) for alias in node.names)
    if node.level:
        return False
    module = node.module or ""
    if _is_guard_module(module):
        return True
    return module == "teamagent" and any(alias.name == "personal_memory" for alias in node.names)


def _is_guard_module(module: str) -> bool:
    return module == _PACKAGE or module.startswith(f"{_PACKAGE}.")


# guard を import してよいのは、書き込み前（learner）と読み出し時（service）の再検査だけ
_WIRED_TO = frozenset(
    {
        "src/teamagent/mcp_gateway/personal_memory/learner.py",
        "src/teamagent/mcp_gateway/personal_memory/service.py",
    }
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


def test_personal_memory_is_wired_only_to_learner_and_service() -> None:
    """guard の配線先を固定集合に限る（ほかの経路から本人メモの規則を迂回・流用させない）。"""
    importers = {
        str(path.relative_to(_REPOSITORY_ROOT)): [
            node for node in _import_nodes(path) if _imports_personal_memory(node)
        ]
        for path in sorted(_SOURCE_ROOT.rglob("*.py"))
        if not path.is_relative_to(_PERSONAL_MEMORY_ROOT)
    }
    violations = [
        f"{path}: {ast.unparse(node)}"
        for path, nodes in importers.items()
        if path not in _WIRED_TO
        for node in nodes
    ]

    assert not violations, "personal_memory が許可外に配線されています:\n" + "\n".join(violations)
    # 許可リストが古びない（配線先が実際に import している）
    for path in _WIRED_TO:
        assert importers.get(path), f"{path} が guard を import していません"


def test_guard_module_detection() -> None:
    """判定が別パッケージ（teamagent.mcp_gateway.personal_memory）を誤検出しない。"""
    positives = [
        "import teamagent.personal_memory",
        "import teamagent.personal_memory.guard",
        "from teamagent.personal_memory import guard",
        "from teamagent.personal_memory.guard import check_entry",
        "from teamagent import personal_memory",
    ]
    negatives = [
        "from teamagent.mcp_gateway.personal_memory import gate",
        "import teamagent.mcp_gateway.personal_memory.service",
        "from teamagent.adapters.personal_memory_store import Principal",
        "from . import guard",
    ]
    for source in positives:
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.Import | ast.ImportFrom)
        assert _imports_personal_memory(node), source
    for source in negatives:
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.Import | ast.ImportFrom)
        assert not _imports_personal_memory(node), source
