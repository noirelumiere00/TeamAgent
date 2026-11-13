"""rev224 tools/list budget and compatibility against c6b73b33 (no external calls)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from teamagent.mcp_gateway.server import _augment_schema

ROOT = Path(__file__).resolve().parents[2]


def _contract(node: Any, *, names: bool = False) -> Any:
    """Ignore only text annotations and omitted empty defaults, never field names."""
    if isinstance(node, list):
        return [_contract(value) for value in node]
    if not isinstance(node, dict):
        return node
    return {
        key: _contract(value, names=not names and key in ("properties", "$defs", "definitions"))
        for key, value in node.items()
        if names
        or not (
            key in ("description", "title") or (key == "default" and (value is None or value == ""))
        )
    }


def test_rev224_tool_budget_and_input_contract(tmp_path: Path) -> None:
    output = tmp_path / "tools.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/routing_eval/dump_tools.py"),
            "--src",
            str(ROOT / "src"),
            "--flags",
            str(ROOT / "scripts/routing_eval/flags_mcp224.json"),
            "--out",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    tools = json.loads(output.read_text(encoding="utf-8"))["tools"]
    # 名前→ハッシュの辞書にすると gitleaks が「oauth_…: 64 桁」を鍵と誤検知するので行の配列で持つ。
    rows = json.loads(
        Path(__file__).with_name("tool_input_contract_mcp224.json").read_text(encoding="utf-8")
    )
    baseline = {row["name"]: row["sha256"] for row in rows}
    assert len(tools) == len(baseline) == 42
    assert {tool["name"] for tool in tools} == baseline.keys()
    for tool in tools:
        encoded = json.dumps(_contract(tool["inputSchema"]), ensure_ascii=False, sort_keys=True)
        assert hashlib.sha256(encoded.encode()).hexdigest() == baseline[tool["name"]], tool["name"]
    count = len(json.dumps(tools, ensure_ascii=False))
    # 37,000 は 2026-11-13 時点の baseline が 36,995 でほぼいっぱいだった。お土産D1の
    # 資料系4ツールへのトリガー語＋相互排他注記（+412）を入れるには上限調整が必須で、
    # 他ツールの文言（routing ポリシーが詰まっている）は触らず 37,500 へ緩めた。
    assert count <= 37_500, f"rev224 tools/list: {count:,} characters (limit 37,500)"


def test_contract_preserves_fields_named_like_annotations() -> None:
    schema = {
        "title": "Input",
        "properties": {"description": {"type": "string"}, "default": {"type": "integer"}},
        "default": None,
        "required": ["description"],
    }
    assert _contract(schema) == {
        "properties": {"description": {"type": "string"}, "default": {"type": "integer"}},
        "required": ["description"],
    }


def test_projection_omits_only_empty_default_annotations() -> None:
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "default": ""},
            "default": {"type": ["string", "null"], "default": None},
            "enabled": {"type": "boolean", "default": False},
            "count": {"type": "integer", "default": 0, "minimum": 0},
            "nested": {"items": {"type": "string", "default": ""}, "type": "array"},
        },
        "required": ["title"],
    }
    before = deepcopy(schema)
    projected = _augment_schema(schema)
    assert schema == before
    properties = projected["properties"]
    assert properties["title"] == {"type": "string"}
    assert properties["default"] == {"type": ["string", "null"]}
    assert properties["enabled"] == schema["properties"]["enabled"]
    assert properties["count"] == schema["properties"]["count"]
    assert properties["nested"]["items"] == {"type": "string"}
    assert projected["required"] == ["title"]
