"""connect-web の起動（``python -m teamagent.connect_web``）が構造化ログを JSON で出すこと。

タスク定義は ``STRUCTLOG_FORMAT=json``（connect_web.tf）だが、connect-web はこれまで
``configure_logging()`` を一度も呼んでおらず、既定の console 形式で出ていた＝CloudWatch の
JSON の metric filter（``$.event = …``）が connect_callback_* を拾えなかった（F0・2026-09-29）。
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from contextlib import redirect_stdout
from typing import Any

import pytest
import structlog

from teamagent.observability import logging_config


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    logging_config._reset_for_tests()
    yield
    logging_config._reset_for_tests()


def test_main_configures_json_logging_before_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.connect_web import __main__ as entrypoint

    monkeypatch.setenv("STRUCTLOG_FORMAT", "json")
    monkeypatch.setattr(entrypoint, "require_runtime_startup", lambda *_a, **_k: None)
    lines: list[str] = []

    def _run(_app: Any, **_kwargs: Any) -> None:
        # uvicorn が動き出した後に callback が出すのと同じ形のイベントを 1 行出す。
        buf = io.StringIO()
        with redirect_stdout(buf):
            structlog.get_logger("teamagent.connect_web.app").warning(
                "connect_callback_scope_partial", missing_count=1, missing=["calendar.events"]
            )
        lines.append(buf.getvalue().strip())

    monkeypatch.setattr(entrypoint.uvicorn, "run", _run)
    entrypoint.main()

    assert logging_config.is_configured()
    doc = json.loads(lines[0])  # JSON として読める＝metric filter の $.event がバインドする
    assert doc["event"] == "connect_callback_scope_partial"
    assert doc["missing_count"] == 1
    assert doc["level"] == "warning"
