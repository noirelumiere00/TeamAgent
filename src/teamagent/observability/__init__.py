"""Observability 層 — Sentry / Metrics / Tracing 等の横断機能。

3層分離（Skill / Adapter / Runtime）の外側に位置付ける横断モジュール。
Skill / Adapter からはこの層を直接 import せず、Runtime（slack_bot.py 等）の
起動時に init し、構造化ログ経由で計装する。
"""

from teamagent.observability.sentry import (
    capture_skill_exception,
    init_sentry,
    scrub_value,
)

__all__ = ["capture_skill_exception", "init_sentry", "scrub_value"]
