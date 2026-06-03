"""方式B: Claude Agent SDK on Bedrock でオーケストレーターを回す統合.

既存 Skill を `@tool` で in-process ツール化し、SDK 自身のエージェントループに委ねる
（自前 loop.py は使わない＝SDKがループの主体）。6-bis 準拠のため、SDK が返す
`AssistantMessage.usage`（**呼び出し毎**のトークン）から cost/token を記録する。

⚠️ 実行要件（ライブ）:
  - 同梱 Node CLI（SDK が spawn）→ Node 24 系
  - Bedrock: env `CLAUDE_CODE_USE_BEDROCK=1` + AWS 資格情報 + `AWS_REGION`
    + inference profile ID（`model` 引数）。本番 Bot と同じ Bedrock を指す。
  `run_sdk_agent()` はこれらが揃って初めて動く。

オフライン検証可能な部分: `usage_to_record()` / `log_cost_record()`（6-bis のコスト抽出ロジック）。
tests/orchestrator/test_sdk_cost_logging.py が SDK の実メッセージ型で検証する。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from teamagent.skills.base import SkillContext

from .tools import ToolSpec

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Price:
    """Bedrock の概算単価（USD / 1M tokens）。PoC 既定値（Sonnet 4.6 想定）。

    cache_read は入力の 0.1x、cache_write(=creation) は 1.25x（6-bis-5 / Bedrock 仕様）。
    実値は config 化して差し替える。
    """

    input_per_mtok: float = 3.0
    output_per_mtok: float = 15.0
    cache_read_per_mtok: float = 0.30
    cache_write_per_mtok: float = 3.75


@dataclass(frozen=True)
class CostRecord:
    """1 Bedrock 呼び出し分の 6-bis ログレコード。"""

    request_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float


def usage_to_record(
    usage: dict[str, Any],
    *,
    model: str,
    request_id: str,
    price: Price | None = None,
) -> CostRecord:
    """SDK の `AssistantMessage.usage`（呼び出し毎）→ 6-bis レコードに変換（純関数）。

    Anthropic 標準の usage キー（input_tokens / output_tokens /
    cache_read_input_tokens / cache_creation_input_tokens）を吸収する。
    """
    p = price or Price()
    it = int(usage.get("input_tokens", 0) or 0)
    ot = int(usage.get("output_tokens", 0) or 0)
    cr = int(usage.get("cache_read_input_tokens", 0) or 0)
    cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
    cost = (
        it * p.input_per_mtok
        + ot * p.output_per_mtok
        + cr * p.cache_read_per_mtok
        + cw * p.cache_write_per_mtok
    ) / 1_000_000
    return CostRecord(
        request_id=request_id,
        model=model,
        input_tokens=it,
        output_tokens=ot,
        cache_read_tokens=cr,
        cache_creation_tokens=cw,
        cost_usd=round(cost, 6),
    )


def log_cost_record(rec: CostRecord) -> None:
    """6-bis-6: Bedrock 呼び出し毎に usage/cost を request_id 付きで必ず構造化ログ。"""
    logger.info(
        "bedrock_usage",
        request_id=rec.request_id,
        model=rec.model,
        input_tokens=rec.input_tokens,
        output_tokens=rec.output_tokens,
        cache_read_tokens=rec.cache_read_tokens,
        cache_creation_tokens=rec.cache_creation_tokens,
        cost_usd=rec.cost_usd,
    )


@dataclass
class SdkAgentResult:
    answer: str
    cost_records: list[CostRecord] = field(default_factory=list)
    total_cost_usd: float = 0.0
    session_total_cost_usd: float | None = None  # ResultMessage 由来（SDK 集計）
    num_turns: int = 0
    stopped_reason: str = "final"


def _make_handler(
    spec: ToolSpec, request_id: str
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Skill を SDK ツールハンドラへ変換。request_id を SkillContext で伝播。"""

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        ctx = SkillContext(request_id=request_id)
        skill = spec.instantiate()
        skill_input = spec.input_schema(**args)
        output = skill.run(skill_input, ctx)
        # Skill 内部が Bedrock を叩いた場合のコストも 6-bis ログ（取りこぼし防止）.
        skill_cost = float(getattr(output, "total_cost_usd", 0.0) or 0.0)
        if skill_cost:
            logger.info(
                "skill_cost", request_id=request_id, skill=spec.name, cost_usd=skill_cost
            )
        return {"content": [{"type": "text", "text": output.model_dump_json()}]}

    return handler


def build_skill_tools(specs: list[ToolSpec], request_id: str) -> list[Any]:
    """ToolSpec 群 → SDK MCP ツール群（Pydantic input_schema を JSON Schema で渡す）。"""
    sdk_tools: list[Any] = []
    for spec in specs:
        decorated = tool(
            spec.name, spec.description, spec.input_schema.model_json_schema()
        )(_make_handler(spec, request_id))
        sdk_tools.append(decorated)
    return sdk_tools


async def run_sdk_agent(
    *,
    goal: str,
    request_id: str,
    specs: list[ToolSpec],
    model: str,
    system_prompt: str,
    max_turns: int = 8,
    cost_cap_usd: float = 0.5,
    price: Price | None = None,
) -> SdkAgentResult:
    """SDK on Bedrock で goal を回す（ライブ; Node CLI + Bedrock 資格情報が必要）。

    ガードレールは SDK ネイティブ: `max_turns`（反復上限）と `max_budget_usd`（コスト上限）。
    6-bis ログは AssistantMessage.usage を呼び出し毎に記録。
    """
    server = create_sdk_mcp_server(
        "teamagent", tools=build_skill_tools(specs, request_id)
    )
    options = ClaudeAgentOptions(
        mcp_servers={"teamagent": server},
        allowed_tools=[f"mcp__teamagent__{s.name}" for s in specs],
        tools=[],  # 組み込みツールは無効化（Skill だけ使わせる）
        system_prompt=system_prompt,
        model=model,
        max_turns=max_turns,
        max_budget_usd=cost_cap_usd,
        permission_mode="bypassPermissions",  # ヘッドレス（承認プロンプト無し）
    )

    result = SdkAgentResult(answer="")
    # SDK はストリーミングで同一メッセージ(同一 message_id)を部分→完成と複数回 yield する。
    # last-wins で完成版だけ残す（first-wins だと部分版を拾い取りこぼす。実機で確認済）。
    by_id: dict[str, AssistantMessage] = {}
    no_id: list[AssistantMessage] = []
    final_text: str | None = None
    async for message in query(prompt=goal, options=options):
        if isinstance(message, AssistantMessage):
            if message.message_id is not None:
                by_id[message.message_id] = message  # 同一idは最後（完成版）で上書き
            else:
                no_id.append(message)
        elif isinstance(message, ResultMessage):
            result.session_total_cost_usd = message.total_cost_usd
            # 最終回答は ResultMessage.result が正規（合成テキストはここに入る）.
            if message.result:
                final_text = message.result

    # 完成版メッセージだけを 6-bis ログ＋コスト集計（呼び出し毎、request_id 付き）.
    answer_parts: list[str] = []
    for message in (*by_id.values(), *no_id):
        result.num_turns += 1
        if message.usage:
            rec = usage_to_record(
                message.usage, model=message.model, request_id=request_id, price=price
            )
            log_cost_record(rec)
            result.cost_records.append(rec)
            result.total_cost_usd += rec.cost_usd
        for block in message.content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                answer_parts.append(text)

    # 最終回答: ResultMessage.result を優先、無ければ assistant テキストを連結.
    result.answer = (final_text or "\n".join(answer_parts)).strip()
    logger.info(
        "agent_result",
        request_id=request_id,
        session_total_cost_usd=result.session_total_cost_usd,
        num_turns=result.num_turns,
        estimated_cost_usd=round(result.total_cost_usd, 6),
    )

    return result


__all__ = [
    "CostRecord",
    "Price",
    "SdkAgentResult",
    "build_skill_tools",
    "log_cost_record",
    "run_sdk_agent",
    "usage_to_record",
]
