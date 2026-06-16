"""observability/logging_config.py のテスト — JSON / console 切替を固定。

structlog の出力を capture して、STRUCTLOG_FORMAT=json のとき
`event`/`level`/`timestamp` と付随キーがトップレベル JSON キーになることを検証する
（＝CloudWatch metric filter の `$.cost_usd` 等がバインドする前提）。AWS 非依存。
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest
import structlog

from teamagent.observability import logging_config


@pytest.fixture(autouse=True)
def _reset() -> None:
    """各テスト前後で structlog をリセット（他テストへ設定が漏れないように）。"""
    logging_config._reset_for_tests()
    yield
    logging_config._reset_for_tests()


def _emit_and_capture(**kw: object) -> str:
    """configure 済みの状態で 1 行ログを出し、stdout 文字列を返す。"""
    buf = io.StringIO()
    with redirect_stdout(buf):
        log = structlog.get_logger("test")
        log.info("bedrock_converse", latency_ms=1234, cost_usd=0.05, **kw)
    return buf.getvalue().strip()


def test_json_mode_emits_toplevel_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTLOG_FORMAT", "json")
    assert logging_config.configure_logging() is True
    line = _emit_and_capture(cache_read_input_tokens=0)
    doc = json.loads(line)  # JSON としてパースできる＝JSONRenderer 有効
    # CloudWatch の $.event / $.latency_ms / $.cost_usd がバインドできるトップレベルキー
    assert doc["event"] == "bedrock_converse"
    assert doc["latency_ms"] == 1234
    assert doc["cost_usd"] == 0.05
    assert doc["cache_read_input_tokens"] == 0
    assert doc["level"] == "info"
    assert "timestamp" in doc


def test_console_mode_is_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTLOG_FORMAT", "console")
    assert logging_config.configure_logging() is False
    line = _emit_and_capture()
    # console 形式は JSON としてパースできない（人間可読）
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
    assert "bedrock_converse" in line


def test_default_is_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRUCTLOG_FORMAT", raising=False)
    assert logging_config.configure_logging() is False


def test_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTLOG_FORMAT", "json")
    assert logging_config.configure_logging() is True
    assert logging_config.is_configured() is True
    # 2 回目は no-op（例外なく True を返す）
    assert logging_config.configure_logging() is True


def test_error_level_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    """level=error が JSON に出る（McpToolError filter の `$.level="error"` 用）。"""
    monkeypatch.setenv("STRUCTLOG_FORMAT", "json")
    logging_config.configure_logging()
    buf = io.StringIO()
    with redirect_stdout(buf):
        structlog.get_logger("t").error("mcp_tool_error", reason="boom")
    doc = json.loads(buf.getvalue().strip())
    assert doc["level"] == "error"
    assert doc["event"] == "mcp_tool_error"
