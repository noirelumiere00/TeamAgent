"""本人メモのログ契約（I11）: 本文を出さない・例外文を出さない。

静的: personal_memory 系モジュールに logger.exception / exc_info が無く、error= は型名だけ。
実行時: observe・context・command・学習・各エラー経路を回し、捕捉が空でないことを先に確かめてから
目印（ZQX-MARKER）が無いことを断言する。
"""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from teamagent.adapters.personal_memory_store import Principal
from teamagent.mcp_gateway.personal_memory import service as pm_service
from teamagent.mcp_gateway.personal_memory.schemas import CommandInput, ContextInput, ObserveInput
from tests.personal_memory.fakes import FakeDirectory, FakeStore, HermesFake, append, make_runtime

ROOT = Path(__file__).resolve().parents[2]
MODULES = [
    *sorted((ROOT / "src/teamagent/mcp_gateway/personal_memory").glob("*.py")),
    ROOT / "src/teamagent/adapters/personal_memory_store.py",
    ROOT / "src/teamagent/adapters/hermes_learn_client.py",
    ROOT / "src/teamagent/adapters/slack_member_directory.py",
]
SERVER = ROOT / "src/teamagent/mcp_gateway/server.py"
LOG_METHODS = {"debug", "info", "warning", "error", "critical", "exception", "msg", "log"}
MARKER = "ZQX-MARKER"
P = Principal("T0123456789", "U0000000A1", "a@vectorinc.co.jp")


def _logger_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in LOG_METHODS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"logger", "log", "_logger"}
    ]


def _is_type_name(node: ast.expr) -> bool:
    """``type(x).__name__``（または f"...{type(x).__name__}"[:n] の固定コード）だけを許す。"""
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.JoinedStr):
        return all(
            isinstance(v, ast.Constant)
            or (isinstance(v, ast.FormattedValue) and _is_type_name(v.value))
            for v in node.values
        )
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "type"
    )


def _pm_trees() -> list[tuple[str, ast.AST]]:
    trees: list[tuple[str, ast.AST]] = [
        (str(p.relative_to(ROOT)), ast.parse(p.read_text(encoding="utf-8"))) for p in MODULES
    ]
    server = ast.parse(SERVER.read_text(encoding="utf-8"))
    for node in ast.walk(server):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "dispatch_personal_memory_tool":
            trees.append(("server.py:dispatch_personal_memory_tool", node))
    assert len(trees) == len(MODULES) + 1
    return trees


def test_no_exception_logging_or_exc_info() -> None:
    violations = []
    for name, tree in _pm_trees():
        for call in _logger_calls(tree):
            assert isinstance(call.func, ast.Attribute)
            if call.func.attr == "exception":
                violations.append(f"{name}:{call.lineno} logger.exception")
            for kw in call.keywords:
                if kw.arg == "exc_info" or kw.arg is None:
                    violations.append(f"{name}:{call.lineno} {kw.arg or '**kwargs'}")
                if kw.arg in {"error", "error_type"} and not _is_type_name(kw.value):
                    violations.append(f"{name}:{call.lineno} {kw.arg}= is not a type name")
    assert not violations, "\n".join(violations)


def test_logger_calls_found() -> None:
    # 空振り防止: 検査対象に logger 呼び出しが実在する
    assert sum(len(_logger_calls(tree)) for _, tree in _pm_trees()) >= 15


# --- 実行時 ----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    pm_service.reset_runtime_for_tests(None)


def _dump(logs: list[dict[str, Any]], caplog: pytest.LogCaptureFixture) -> str:
    return json.dumps(logs, ensure_ascii=False, default=str) + caplog.text


async def test_runtime_logs_have_no_body(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    store = FakeStore()
    store.create(
        P,
        entries=[("user", f"{MARKER} 返事は短め"), ("memory", f"先方の{MARKER}様が窓口")],
    )
    hermes = HermesFake(append(user=[f"{MARKER} 箇条書き"], memory=[f"{MARKER}田中様"]))
    runtime, launcher = make_runtime(store=store, client=hermes, directory=FakeDirectory(["小俣"]))
    pm_service.reset_runtime_for_tests(runtime)
    with structlog.testing.capture_logs() as logs:
        for i in range(5):
            await pm_service.handle_personal_memory(
                pm_service.OBSERVE_TOOL,
                P,
                f"1784424000.{i:06d}",
                ObserveInput(utterance=f"{MARKER} 発話 {i}", has_attachment=False),
            )
        launcher.run_all()
        await pm_service.handle_personal_memory(pm_service.CONTEXT_TOOL, P, "m", ContextInput())
        for action in ["list", "freeze", "resume", "erase_request", "erase_confirm"]:
            await pm_service.handle_personal_memory(
                pm_service.COMMAND_TOOL, P, "m", CommandInput(action=action)
            )
        # 失敗の経路: Hermes の 502、DB 障害、guard の拒否
        store.create(P, entries=[("user", f"{MARKER} 返事は短め")])
        hermes.mode = "timeout"
        for i in range(5, 10):
            await pm_service.handle_personal_memory(
                pm_service.OBSERVE_TOOL,
                P,
                f"1784424000.{i:06d}",
                ObserveInput(utterance=f"{MARKER} 発話 {i}", has_attachment=False),
            )
        launcher.run_all()
        await pm_service.handle_personal_memory(
            pm_service.OBSERVE_TOOL,
            P,
            "1784424000.000099",
            ObserveInput(
                utterance=f"{MARKER} https://a.example と https://b.example", has_attachment=False
            ),
        )
        store.fail = True
        await pm_service.handle_personal_memory(pm_service.CONTEXT_TOOL, P, "m", ContextInput())
        await pm_service.handle_personal_memory(
            pm_service.COMMAND_TOOL, P, "m", CommandInput(action="list")
        )
    events = {e["event"] for e in logs}
    assert {
        "personal_memory_observe",
        "personal_memory_learn_result",
        "personal_memory_command",
        "personal_memory_context_empty",
    } <= events
    dumped = _dump(logs, caplog)
    assert MARKER not in dumped
    assert "田中" not in dumped
    assert "a@vectorinc" not in dumped
    assert "U0000000A1" not in dumped
