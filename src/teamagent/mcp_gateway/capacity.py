"""mcp 全体の同時実行の上限（M7・docs/architecture/hermes_implementation_plan.md）。

``runtime/request_gate.RequestGate``（同時 ≤ N・超過は FIFO で待たせる・待ち上限と
タイムアウト付き）を mcp のツール呼び出しの直前につなぐ。上限を超えたら、無言で詰まらせず
形の決まった「混雑」応答を返す。

- 既定 OFF（``MCP_REQUEST_GATE`` が真のときだけ）。OFF なら今と完全に同じ（素通り）。
- 本人メモの observe / context / command は軽い経路として上限の外で通す（返事の前に 1.2 秒で
  諦める context を、重いツールの待ち行列に並ばせない）。
- 1 プロセスに 1 個。並列数・待ち上限・待ち時間は TD の env で変えられる。
"""

from __future__ import annotations

import json
import os
from typing import Final

import structlog
from mcp.types import TextContent

from teamagent.runtime.request_gate import RequestGate

logger = structlog.get_logger(__name__)

ENABLED_ENV: Final = "MCP_REQUEST_GATE"
CONCURRENCY_ENV: Final = "MCP_REQUEST_GATE_CONCURRENCY"
QUEUE_ENV: Final = "MCP_REQUEST_GATE_QUEUE"
TIMEOUT_ENV: Final = "MCP_REQUEST_GATE_TIMEOUT_S"
DEFAULT_CONCURRENCY: Final = 8
DEFAULT_QUEUE: Final = 32
DEFAULT_TIMEOUT_S: Final = 30.0
OVERLOAD_CODE: Final = "MCP_OVERLOADED"
OVERLOAD_TEXT: Final = (
    "いま Aico が混み合っています。1〜2 分おいて、もう一度同じ依頼を送ってください。"
)


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return min(hi, max(lo, value))


def _float_env(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return min(hi, max(lo, value))


def gate_from_env() -> RequestGate | None:
    """``MCP_REQUEST_GATE`` が真なら RequestGate を 1 個作る。偽なら None（素通り）。"""
    if os.environ.get(ENABLED_ENV, "").strip().lower() not in {"1", "true", "yes"}:
        return None
    gate = RequestGate(
        concurrency=_int_env(CONCURRENCY_ENV, DEFAULT_CONCURRENCY, 1, 64),
        # RequestGate は「空きスロットを取りに行く途中」も waiting に数えるので、
        # 0 だと全員を拒否する（下限 1）。
        queue_max=_int_env(QUEUE_ENV, DEFAULT_QUEUE, 1, 512),
        acquire_timeout_s=_float_env(TIMEOUT_ENV, DEFAULT_TIMEOUT_S, 1.0, 600.0),
    )
    logger.info(
        "mcp_request_gate_enabled",
        concurrency=gate.concurrency,
        queue_max=gate.queue_max,
    )
    return gate


def overloaded(tool: str, reason: str, gate: RequestGate) -> list[TextContent]:
    """混雑の応答（成功と同じ TextContent の JSON・利用者向けの文と固定コードだけ）。"""
    m = gate.metrics
    logger.warning(
        "mcp_request_gate_rejected",
        tool=tool,
        reason=reason,
        in_flight=m.in_flight,
        waiting=m.waiting,
        rejected_queue_full=m.rejected_queue_full,
        rejected_timeout=m.rejected_timeout,
    )
    payload = {"error": OVERLOAD_TEXT, "code": OVERLOAD_CODE}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


__all__ = [
    "CONCURRENCY_ENV",
    "ENABLED_ENV",
    "OVERLOAD_CODE",
    "OVERLOAD_TEXT",
    "QUEUE_ENV",
    "TIMEOUT_ENV",
    "gate_from_env",
    "overloaded",
]
