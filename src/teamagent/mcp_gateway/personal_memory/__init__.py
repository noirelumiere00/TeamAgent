"""DM 本人メモ v1（M5）の MCP 側。

3 ツール（observe / context / command）は **list_tools に出さない**。plugin が予約
tool_call_id で直接呼び、server._call が dispatch_tool より前にここへ振り分ける
（ToolSpec にも factory にも入れない＝L2 のツール面にも出ない）。

I/O を伴う import（DB・httpx・Slack）は関数の中で行い、server.py の import コストを増やさない。
"""

from __future__ import annotations

from typing import Any, Final

PERSONAL_MEMORY_FLAG_ENV: Final = "USE_PERSONAL_MEMORY"
PERSONAL_MEMORY_TOOL_NAMES: Final = frozenset(
    {"personal_memory_observe", "personal_memory_context", "personal_memory_command"}
)


async def handle_personal_memory(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from teamagent.mcp_gateway.personal_memory.service import handle_personal_memory as _handle

    return await _handle(*args, **kwargs)


__all__ = ["PERSONAL_MEMORY_FLAG_ENV", "PERSONAL_MEMORY_TOOL_NAMES", "handle_personal_memory"]
