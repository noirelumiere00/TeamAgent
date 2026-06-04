"""方式B: Claude Agent SDK on Bedrock でオーケストレーターを回す統合.

既存 Skill を `@tool` で in-process ツール化し、SDK 自身のエージェントループに委ねる
（自前 loop.py は使わない＝SDKがループの主体）。6-bis 準拠のため、SDK が返す
`AssistantMessage.usage`（**呼び出し毎**のトークン）から cost/token を記録する。

Phase 0（堅牢化）を反映:
  - RLS: SkillContext に user_id / metadata(user_email 等) を伝播。require_rls で fail-closed。
  - エラー/予算可観測性: ResultMessage の is_error/subtype 等を読み stopped_reason に反映。
  - 無限ループ殺し: 同一ツール×同一入力の連続呼び出しを上限で拒否。
  - イベントループ非阻害: 同期 Skill を run_in_executor + per-tool timeout で実行。
  - 失敗は構造化エラーとして LLM に返す（ループを落とさない）。

⚠️ 実行要件（ライブ）:
  - 同梱 Node CLI（SDK が spawn）→ Node 24 系
  - Bedrock: env `CLAUDE_CODE_USE_BEDROCK=1` + AWS 資格情報 + `AWS_REGION`
    + inference profile ID（`model` 引数）。本番 Bot と同じ Bedrock を指す。

オフライン検証可能な部分: `usage_to_record()` / `classify_result()` / `_make_handler()` の
ガード（RLS伝播・繰返し拒否・timeout・例外→構造化エラー）。tests/orchestrator が検証する。
"""

from __future__ import annotations

import asyncio
import json
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

from .faithfulness import extract_chunk_ids_from_tool_json
from .loop import OrchestratorError
from .tools import ToolSpec

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Price:
    """Bedrock の概算単価（USD / 1M tokens）。PoC 既定値（Sonnet 4.6 想定）。

    cache_read は入力の 0.1x、cache_write(=creation) は 1.25x（6-bis-5 / Bedrock 仕様）。
    **概算**。正確な実コストは ResultMessage.total_cost_usd / model_usage（SDK集計）を正とする。
    実値は config 化して差し替える。
    """

    input_per_mtok: float = 3.0
    output_per_mtok: float = 15.0
    cache_read_per_mtok: float = 0.30
    cache_write_per_mtok: float = 3.75


@dataclass(frozen=True)
class CostRecord:
    """1 Bedrock 呼び出し分の 6-bis ログレコード（トークンは実測、cost は概算）。"""

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


def classify_result(*, subtype: str, is_error: bool) -> str:
    """ResultMessage の subtype/is_error → stopped_reason（純関数, テスト可）.

    例: success→"final" / error_max_turns / error_max_budget_usd / その他 subtype をそのまま返す。
    """
    if is_error or (subtype and subtype != "success"):
        return subtype or "error"
    return "final"


@dataclass
class SdkAgentResult:
    answer: str
    cost_records: list[CostRecord] = field(default_factory=list)
    total_cost_usd: float = 0.0  # 自前概算（参考）
    session_total_cost_usd: float | None = None  # ResultMessage 由来（SDK実コスト=正）
    model_usage: dict[str, Any] | None = None  # モデル別実集計（cost較正用）
    num_turns: int = 0
    stopped_reason: str = "final"
    is_error: bool = False
    api_error_status: int | None = None
    errors: list[str] = field(default_factory=list)
    permission_denials: list[Any] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)  # 実際に呼ばれたツール名（順序つき）
    # ツールが返した chunk_id（最終回答の引用が捏造でないかの忠実性照合用）
    available_chunk_ids: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.stopped_reason == "final" and not self.is_error


def _make_handler(
    spec: ToolSpec,
    *,
    request_id: str,
    user_id: str | None,
    ctx_metadata: dict[str, Any],
    call_counts: dict[str, int],
    tool_timeout_s: float,
    max_same_call: int = 2,
    tool_calls: list[str] | None = None,
    available_chunk_ids: list[int] | None = None,
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Skill を SDK ツールハンドラへ変換（Phase 0 ガード込み）.

    - RLS: SkillContext に request_id / user_id / metadata を伝播。
    - 無限ループ殺し: 同一ツール×同一入力が max_same_call を超えたら構造化エラーを返す。
    - 非阻害: 同期 skill.run を run_in_executor + timeout で実行（Slack ループを塞がない）。
    - 失敗（入力不正/タイムアウト/例外）は raise せず is_error の構造化結果で返す。
    """

    calls = tool_calls if tool_calls is not None else []
    chunk_sink = available_chunk_ids if available_chunk_ids is not None else []

    def _err(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        key = f"{spec.name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
        call_counts[key] = call_counts.get(key, 0) + 1
        if call_counts[key] > max_same_call:
            logger.warning(
                "tool_repeat_blocked",
                request_id=request_id,
                skill=spec.name,
                count=call_counts[key],
            )
            return _err(
                f"ツール {spec.name} は同一入力で既に {max_same_call} 回呼ばれています。"
                "別の手法を試すか、得られた情報で最終回答をまとめてください。"
            )

        try:
            skill_input = spec.input_schema(**args)
        except Exception as e:
            logger.warning(
                "tool_input_invalid",
                request_id=request_id,
                skill=spec.name,
                error=type(e).__name__,
            )
            return _err(f"入力がスキーマに合いません（{type(e).__name__}）")

        calls.append(spec.name)  # 有効な入力で呼ばれた＝エージェントのツール選択を記録（評価用）
        ctx = SkillContext(request_id=request_id, user_id=user_id, metadata=dict(ctx_metadata))
        loop = asyncio.get_running_loop()
        try:
            output = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: spec.instantiate().run(skill_input, ctx)),
                timeout=tool_timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "tool_timeout", request_id=request_id, skill=spec.name, timeout_s=tool_timeout_s
            )
            return _err(f"ツール {spec.name} が {tool_timeout_s}s でタイムアウトしました。")
        except Exception as e:
            logger.warning(
                "tool_error", request_id=request_id, skill=spec.name, error=type(e).__name__
            )
            return _err(f"ツール {spec.name} 実行エラー（{type(e).__name__}）。別手段を検討して。")

        # Skill 内部が Bedrock を叩いた場合のコストも 6-bis ログ（取りこぼし防止）.
        skill_cost = float(getattr(output, "total_cost_usd", 0.0) or 0.0)
        if skill_cost:
            logger.info("skill_cost", request_id=request_id, skill=spec.name, cost_usd=skill_cost)
        result_text = output.model_dump_json()
        # 忠実性照合用: ツールが返した chunk_id を蓄積（最終回答の引用が捏造でないか後で検証）.
        chunk_sink.extend(extract_chunk_ids_from_tool_json(result_text))
        return {"content": [{"type": "text", "text": result_text}]}

    return handler


def build_skill_tools(
    specs: list[ToolSpec],
    *,
    request_id: str,
    user_id: str | None = None,
    ctx_metadata: dict[str, Any] | None = None,
    tool_timeout_s: float = 60.0,
    tool_calls: list[str] | None = None,
    available_chunk_ids: list[int] | None = None,
) -> list[Any]:
    """ToolSpec 群 → SDK MCP ツール群（Pydantic input_schema を JSON Schema で渡す）.

    call_counts を全ツールで共有し、同一入力の繰返し呼び出しを run 単位で抑制する。
    tool_calls / available_chunk_ids（任意）を渡すと、全ツールで共有して呼び出し順・
    返却 chunk_id を記録する（評価・忠実性照合用）。
    """
    call_counts: dict[str, int] = {}
    meta = ctx_metadata or {}
    sdk_tools: list[Any] = []
    for spec in specs:
        handler = _make_handler(
            spec,
            request_id=request_id,
            user_id=user_id,
            ctx_metadata=meta,
            call_counts=call_counts,
            tool_timeout_s=tool_timeout_s,
            tool_calls=tool_calls,
            available_chunk_ids=available_chunk_ids,
        )
        sdk_tools.append(
            tool(spec.name, spec.description, spec.input_schema.model_json_schema())(handler)
        )
    return sdk_tools


async def run_sdk_agent(
    *,
    goal: str,
    request_id: str,
    specs: list[ToolSpec],
    model: str,
    system_prompt: str,
    user_id: str | None = None,
    ctx_metadata: dict[str, Any] | None = None,
    require_rls: bool = False,
    max_turns: int = 8,
    cost_cap_usd: float = 0.5,
    tool_timeout_s: float = 60.0,
    price: Price | None = None,
) -> SdkAgentResult:
    """SDK on Bedrock で goal を回す（ライブ; Node CLI + Bedrock 資格情報が必要）。

    ガードレールは SDK ネイティブ: `max_turns`（反復上限）と `max_budget_usd`（コスト上限）。
    require_rls=True の場合、ctx_metadata に user_email が無ければ fail-closed（越権防止）。
    """
    meta = ctx_metadata or {}
    if require_rls and not meta.get("user_email"):
        raise OrchestratorError(
            "RLS required but ctx_metadata['user_email'] is missing (fail-closed)"
        )

    tool_calls: list[str] = []
    available_chunk_ids: list[int] = []
    server = create_sdk_mcp_server(
        "teamagent",
        tools=build_skill_tools(
            specs,
            request_id=request_id,
            user_id=user_id,
            ctx_metadata=meta,
            tool_timeout_s=tool_timeout_s,
            tool_calls=tool_calls,
            available_chunk_ids=available_chunk_ids,
        ),
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
        # SDK 隔離モード: filesystem settings を一切読まない（setting_sources=None だと
        # cwd の CLAUDE.md / .claude/settings.json を自動文脈化し、古い「DBは空かも」等の
        # 記述にLLMが引っ張られツール結果を無視→ハルシ/暈す原因になる）。
        # オーケストレーターは system_prompt + MCPツール + goal だけを文脈にする。
        setting_sources=[],
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
            # エラー/予算/拒否の可観測性（黙って劣化回答を返さない）.
            result.session_total_cost_usd = message.total_cost_usd
            result.model_usage = message.model_usage
            result.is_error = bool(message.is_error)
            result.api_error_status = message.api_error_status
            result.errors = list(message.errors or [])
            result.permission_denials = list(message.permission_denials or [])
            result.stopped_reason = classify_result(
                subtype=message.subtype, is_error=bool(message.is_error)
            )
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
    result.tool_calls = tool_calls
    result.available_chunk_ids = list(dict.fromkeys(available_chunk_ids))  # 重複除去
    if result.is_error and not result.answer:
        result.answer = f"(エージェント未完了: stopped_reason={result.stopped_reason})"
    logger.info(
        "agent_result",
        request_id=request_id,
        stopped_reason=result.stopped_reason,
        is_error=result.is_error,
        session_total_cost_usd=result.session_total_cost_usd,
        num_turns=result.num_turns,
        estimated_cost_usd=round(result.total_cost_usd, 6),
        tool_calls=result.tool_calls,
    )

    return result


__all__ = [
    "CostRecord",
    "Price",
    "SdkAgentResult",
    "build_skill_tools",
    "classify_result",
    "log_cost_record",
    "run_sdk_agent",
    "usage_to_record",
]
