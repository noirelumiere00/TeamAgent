"""structlog の出力フォーマット設定（console / JSON 切替）。

背景（2026-06-16 調査で判明）:
リポジトリで `structlog.configure()` が一度も呼ばれておらず、本番でも既定の
ConsoleRenderer（人間可読・JSON でない）でログが出ていた。一方 CloudWatch の
metric filter は JSON セレクタ（`{ $.cost_usd = * }` / `{ $.event = "mcp_tool_error" }`）
を前提にしているため、console 形式ログには一切マッチせず、コスト/エラー/なりすまし
アラームが **永久に発火しない**状態だった（`treat_missing_data="notBreaching"`）。

本モジュールで起動時に `configure_logging()` を呼び、env `STRUCTLOG_FORMAT=json` の
ときだけ JSONRenderer に切り替える。これで `event`/`level`/`timestamp` と付随キー
（`latency_ms`/`cost_usd`/`cache_read_input_tokens` 等）が**トップレベル JSON キー**
として出力され、既存の metric filter がそのままバインドする（terraform 変更不要）。

設計指針（observability/sentry.py の流儀に合わせる）:
- DSN/フラグ未設定でも安全（既定 console＝ローカル/テストは人間可読のまま）
- 多重呼び出しは無害（idempotent）
- `_reset_for_tests()` でテスト時に再 configure 可能
"""

from __future__ import annotations

import logging
import os
from typing import Any

import structlog

# 重複初期化防止（プロセス内 idempotent）
_CONFIGURED: bool = False


def is_configured() -> bool:
    """configure_logging() 済みかを返す。テスト用。"""
    return _CONFIGURED


def _reset_for_tests() -> None:
    """テスト専用：再 configure できる状態に戻す。本番では呼ばない。"""
    global _CONFIGURED
    _CONFIGURED = False
    structlog.reset_defaults()


def _use_json() -> bool:
    """env STRUCTLOG_FORMAT が 'json' のとき True（既定 console）。"""
    return os.environ.get("STRUCTLOG_FORMAT", "console").strip().lower() == "json"


def configure_logging(*, force: bool = False) -> bool:
    """structlog のプロセス全体の出力フォーマットを設定する。

    env `STRUCTLOG_FORMAT=json` → JSONRenderer（本番 CloudWatch 向け）。
    それ以外（既定）→ ConsoleRenderer（ローカル/テストの人間可読）。

    共通 processors（両モード）:
      merge_contextvars → add_log_level → TimeStamper(iso, key="timestamp")
      → StackInfoRenderer → format_exc_info → (renderer)

    Args:
        force: True なら既に configure 済みでも再設定する。

    Returns:
        True: JSON モードで configure / False: console モードで configure。
    """
    global _CONFIGURED
    use_json = _use_json()

    if _CONFIGURED and not force:
        return use_json

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", key="timestamp"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any
    if use_json:
        # event_dict をそのまま JSON 化 → `event`/`level`/`timestamp` と
        # 付随キーがトップレベルキーになり、CloudWatch の `$.field` と一致する。
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        # 標準ロギングと混ざらないよう PrintLogger（stdout）に出す。
        # uvicorn 等の stdlib logging は別系統だが、構造化イベントはこちらで一貫 JSON 化。
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True
    return use_json


__all__ = ["configure_logging", "is_configured"]
