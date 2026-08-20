"""Python-only Anthropic Bedrock client でオーケストレーターを回す統合.

既存 Skill を Anthropic tool use に変換し、bounded な in-process loop で実行する。
各 ``messages.create`` 応答の usage から、6-bis 準拠の cost/token を記録する。

Phase 0（堅牢化）を反映:
  - RLS: SkillContext に user_id / metadata(user_email 等) を伝播。require_rls で fail-closed。
  - エラー/予算可観測性: API/stop_reason/概算予算超過を stopped_reason に反映。
  - 無限ループ殺し: 同一ツール×同一入力の連続呼び出しを上限で拒否。
  - イベントループ非阻害: 同期 Skill を run_in_executor + per-tool timeout で実行。
  - 失敗は構造化エラーとして LLM に返す（ループを落とさない）。

⚠️ 実行要件（ライブ）:
  - Bedrock AWS 資格情報 + ``AWS_REGION`` + inference profile ID（``model`` 引数）。
  - Node/Bun CLI や subprocess は不要。core image は Python client だけを含む。

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
from anthropic import AsyncAnthropicBedrock

from teamagent.skills.base import ORCHESTRATED_METADATA_KEY, SkillContext

from .faithfulness import extract_chunk_ids_from_tool_json
from .loop import OrchestratorError
from .tools import ToolSpec

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Price:
    """Bedrock の概算単価（USD / 1M tokens）。PoC 既定値（Sonnet 4.6 想定）。

    cache_read は入力の 0.1x、cache_write(=creation) は 1.25x（6-bis-5 / Bedrock 仕様）。
    **概算**。Bedrock の請求単価と差が出ないよう、実値は config 化して差し替える。
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
    """Anthropic 応答の ``usage``（呼び出し毎）→ 6-bis レコードに変換（純関数）。

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
    """subtype/is_error → stopped_reason（純関数, テスト可）.

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
    session_total_cost_usd: float | None = None  # 呼び出し毎の概算 cost 合計
    model_usage: dict[str, Any] | None = None  # セッションの token 実測集計
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
        # オーケストレーターの中間ステップである印を立てる。配信系スキルはこの印を見て
        # Slack へのファイル投下を止める（「調べるだけ」の呼び出しで資料を飛ばさない）。
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata={**ctx_metadata, ORCHESTRATED_METADATA_KEY: True},
        )
        skill = spec.instantiate()
        loop = asyncio.get_running_loop()
        try:
            output = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: skill.run(skill_input, ctx)),
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
        try:
            result_text = output.model_dump_json()
            # 忠実性照合用: ツールが返した chunk_id を蓄積（最終回答の引用が捏造でないか後で検証）.
            chunk_sink.extend(extract_chunk_ids_from_tool_json(result_text))
            return {"content": [{"type": "text", "text": result_text}]}
        finally:
            skill.cleanup_output(output)

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
) -> list[tuple[dict[str, Any], Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]]:
    """ToolSpec 群 → Anthropic tool 定義と in-process handler の組.

    call_counts を全ツールで共有し、同一入力の繰返し呼び出しを run 単位で抑制する。
    tool_calls / available_chunk_ids（任意）を渡すと、全ツールで共有して呼び出し順・
    返却 chunk_id を記録する（評価・忠実性照合用）。
    """
    call_counts: dict[str, int] = {}
    meta = ctx_metadata or {}
    bedrock_tools: list[
        tuple[dict[str, Any], Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]
    ] = []
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
        bedrock_tools.append(
            (
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": spec.input_schema.model_json_schema(),
                },
                handler,
            )
        )
    return bedrock_tools


def _usage_dict(usage: Any) -> dict[str, Any]:
    """Anthropic usage model をログ・集計用の plain dict にする。"""

    if hasattr(usage, "model_dump"):
        dumped = usage.model_dump(exclude_none=True)
        if isinstance(dumped, dict):
            return dumped
    return {
        key: getattr(usage, key, 0)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    }


def _tool_result_content(output: dict[str, Any]) -> list[dict[str, str]]:
    """既存 handler の MCP-compatible content を Anthropic tool_result に正規化。"""

    content = output.get("content")
    if not isinstance(content, list):
        return [{"type": "text", "text": json.dumps(output, ensure_ascii=False, default=str)}]
    normalized: list[dict[str, str]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if block.get("type") == "text" and isinstance(text, str):
            normalized.append({"type": "text", "text": text})
    if normalized:
        return normalized
    return [{"type": "text", "text": json.dumps(output, ensure_ascii=False, default=str)}]


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
    """Python Anthropic client on Bedrock で goal を回す。

    ``max_turns`` と ``cost_cap_usd`` を呼び出し境界で fail-closed に強制する。
    require_rls=True の場合、ctx_metadata に user_email が無ければ fail-closed（越権防止）。
    """
    meta = ctx_metadata or {}
    if require_rls and not meta.get("user_email"):
        raise OrchestratorError(
            "RLS required but ctx_metadata['user_email'] is missing (fail-closed)"
        )

    tool_calls: list[str] = []
    available_chunk_ids: list[int] = []
    if not 1 <= max_turns <= 32:
        raise OrchestratorError("max_turns must be between 1 and 32")
    if not 0 < cost_cap_usd <= 100:
        raise OrchestratorError("cost_cap_usd must be between 0 and 100")

    built_tools = build_skill_tools(
        specs,
        request_id=request_id,
        user_id=user_id,
        ctx_metadata=meta,
        tool_timeout_s=tool_timeout_s,
        tool_calls=tool_calls,
        available_chunk_ids=available_chunk_ids,
    )
    tool_defs = [definition for definition, _handler in built_tools]
    handlers = {definition["name"]: handler for definition, handler in built_tools}
    result = SdkAgentResult(answer="")
    messages: list[dict[str, Any]] = [{"role": "user", "content": goal}]
    answer_parts: list[str] = []
    usage_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    client = AsyncAnthropicBedrock()
    try:
        for turn in range(max_turns):
            if result.total_cost_usd >= cost_cap_usd:
                result.is_error = True
                result.stopped_reason = "error_max_budget_usd"
                break
            try:
                response = await client.messages.create(
                    model=model,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=messages,  # type: ignore[arg-type]
                    tools=tool_defs,  # type: ignore[arg-type]
                )
            except Exception as exc:
                result.is_error = True
                result.stopped_reason = "error_api"
                status_code = getattr(exc, "status_code", None)
                result.api_error_status = status_code if isinstance(status_code, int) else None
                result.errors.append(type(exc).__name__)
                logger.warning(
                    "agent_api_error",
                    request_id=request_id,
                    error=type(exc).__name__,
                    status_code=result.api_error_status,
                )
                break

            result.num_turns = turn + 1
            usage = _usage_dict(response.usage)
            rec = usage_to_record(
                usage,
                model=str(response.model or model),
                request_id=request_id,
                price=price,
            )
            log_cost_record(rec)
            result.cost_records.append(rec)
            result.total_cost_usd += rec.cost_usd
            for key in usage_totals:
                usage_totals[key] += int(usage.get(key, 0) or 0)

            response_content = [block.model_dump(exclude_none=True) for block in response.content]
            messages.append({"role": "assistant", "content": response_content})
            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type == "text" and isinstance(block.text, str):
                    answer_parts.append(block.text)
                    continue
                if block.type != "tool_use":
                    continue
                handler = handlers.get(block.name)
                if handler is None:
                    output: dict[str, Any] = {
                        "content": [
                            {
                                "type": "text",
                                "text": f"unknown tool: {block.name}",
                            }
                        ],
                        "is_error": True,
                    }
                else:
                    raw_input = block.input if isinstance(block.input, dict) else {}
                    output = await handler(raw_input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": _tool_result_content(output),
                        "is_error": bool(output.get("is_error", False)),
                    }
                )

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
                if result.total_cost_usd >= cost_cap_usd:
                    result.is_error = True
                    result.stopped_reason = "error_max_budget_usd"
                    break
                continue

            stop_reason = str(response.stop_reason or "")
            if stop_reason in {"end_turn", "stop_sequence"}:
                result.stopped_reason = "final"
            else:
                result.is_error = True
                result.stopped_reason = f"error_{stop_reason or 'incomplete'}"
            break
        else:
            result.is_error = True
            result.stopped_reason = "error_max_turns"
    finally:
        await client.close()

    result.answer = "\n".join(answer_parts).strip()
    result.tool_calls = tool_calls
    result.available_chunk_ids = list(dict.fromkeys(available_chunk_ids))  # 重複除去
    result.session_total_cost_usd = round(result.total_cost_usd, 6)
    result.model_usage = {model: usage_totals}
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
