"""tests/scripts 共通の隔離。

朝ダイジェストの runner（scripts/run_morning_digest_fargate.py）の main() は起動時に
``configure_logging()`` を呼ぶ（F0: CloudWatch の metric filter が JSON 前提のため）。
これをテストの中で本当に呼ぶと、structlog のプロセス全体の設定が書き換わり、しかも
``cache_logger_on_first_use=True`` で各モジュールのロガーが固定されるので、後続テストの
``capture_logs()`` が何も拾えなくなる（順序依存で赤くなる）。

そこで main() を呼ぶテストでは既定で no-op に差し替える（前例:
tests/mcp_gateway/test_video_algorithm_detach.py の同じ差し替え）。JSON で出ることそのものは
tests/scripts/test_digest_fetch_status.py が **別プロセス** で実際に呼んで確かめる。
"""

from __future__ import annotations

import pytest

import teamagent.observability.logging_config as logging_config


@pytest.fixture(autouse=True)
def _no_global_structlog_configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logging_config, "configure_logging", lambda **_: False)
