"""Bedrock Converse API 薄いラッパー。

3層分離の Adapter 層。Skill からは BedrockClient.converse() だけを呼ぶ。
boto3 への直叩きは禁止（CLAUDE.md 6-bis Don't）。

Usage:
    client = BedrockClient.from_env()
    resp = client.converse(
        messages=[{"role": "user", "content": [{"text": "hello"}]}],
        request_id="req-abc",
    )
    print(resp.text, resp.usage.cost_usd)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import boto3
import structlog

logger = structlog.get_logger(__name__)


# 2026/5 時点の東京リージョン on-demand 料金（USD / 1M tokens）
# 出典: https://aws.amazon.com/bedrock/pricing/
_PRICE_TABLE: dict[str, tuple[float, float]] = {
    # model_id_prefix: (input_per_million, output_per_million)
    "jp.anthropic.claude-sonnet-4-6": (3.0, 15.0),
    "jp.anthropic.claude-haiku-4-5": (1.0, 5.0),
    "us.anthropic.claude-sonnet-4-6": (3.0, 15.0),
    "us.anthropic.claude-haiku-4-5": (1.0, 5.0),
}


@dataclass(frozen=True)
class TokenUsage:
    """Bedrock の usage を表すデータクラス。"""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class ConverseResponse:
    """converse() の返り値。テキスト本体と usage を持つ。"""

    text: str
    usage: TokenUsage
    model_id: str
    latency_ms: int
    stop_reason: str


def _estimate_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """コスト推算（prompt caching 込み）。

    Anthropic caching の料金（Bedrock 経由）:
    - cache_read:  input price × 0.1
    - cache_write: input price × 1.25
    - input_tokens は cache 系を含む合計値で来るので、cache 分を差し引いて新規分だけ計算
    """
    for prefix, (price_in, price_out) in _PRICE_TABLE.items():
        if model_id.startswith(prefix):
            fresh_input = max(0, input_tokens - cache_read_tokens - cache_write_tokens)
            return round(
                fresh_input / 1_000_000 * price_in
                + cache_read_tokens / 1_000_000 * (price_in * 0.1)
                + cache_write_tokens / 1_000_000 * (price_in * 1.25)
                + output_tokens / 1_000_000 * price_out,
                6,
            )
    return 0.0


class BedrockClient:
    """Bedrock Converse の薄いラッパー。

    Skill 層からは boto3 を直接見せない。コスト・レイテンシ・usage を
    構造化ログに出力する責務もここに集約する。
    """

    def __init__(
        self,
        region: str,
        model_id: str,
        client: Any | None = None,
    ) -> None:
        self.region = region
        self.model_id = model_id
        self._client = client or boto3.client("bedrock-runtime", region_name=region)

    @classmethod
    def from_env(cls) -> BedrockClient:
        """環境変数から BedrockClient を構築する。

        必須: AWS_REGION, BEDROCK_MODEL_ID
        """
        region = os.environ.get("AWS_REGION", "ap-northeast-1")
        model_id = os.environ.get("BEDROCK_MODEL_ID", "jp.anthropic.claude-sonnet-4-6")
        return cls(region=region, model_id=model_id)

    def converse(
        self,
        messages: list[dict[str, Any]],
        request_id: str,
        system: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        cache_system: bool = False,
    ) -> ConverseResponse:
        """Bedrock Converse API を呼ぶ。

        Args:
            messages: [{"role": "user", "content": [{"text": "..."}]}, ...]
            request_id: トレース ID（構造化ログに伝播）
            system: System プロンプト
            temperature: 0.1 推奨（CLAUDE.md 6 ハルシネーション抑制）
            max_tokens: 上限トークン
            cache_system: True で system プロンプト末尾に cachePoint を入れる。
                同じ system prompt を頻繁に呼ぶ場合（検索 Skill 等）に
                input cost を 1/10 に削減する。Anthropic prompt caching を活用。

        Returns:
            ConverseResponse(text, usage, model_id, latency_ms, stop_reason)
        """
        kwargs: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": messages,
            "inferenceConfig": {
                "temperature": temperature,
                "maxTokens": max_tokens,
            },
        }
        if system is not None:
            system_blocks: list[dict[str, Any]] = [{"text": system}]
            if cache_system:
                # cachePoint は同じ system 文字列を 2 回目以降の呼び出しで
                # cache_read として再利用させる（コスト 1/10）
                system_blocks.append({"cachePoint": {"type": "default"}})
            kwargs["system"] = system_blocks

        start = time.perf_counter()
        resp = self._client.converse(**kwargs)
        latency_ms = int((time.perf_counter() - start) * 1000)

        usage_raw = resp.get("usage", {})
        input_tokens = int(usage_raw.get("inputTokens", 0))
        output_tokens = int(usage_raw.get("outputTokens", 0))
        cache_read = int(usage_raw.get("cacheReadInputTokens", 0))
        cache_create = int(usage_raw.get("cacheWriteInputTokens", 0))

        cost_usd = _estimate_cost(
            self.model_id,
            input_tokens,
            output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_create,
        )
        usage = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_create,
            cost_usd=cost_usd,
        )

        text = self._extract_text(resp)
        stop_reason: str = resp.get("stopReason", "unknown")

        logger.info(
            "bedrock_converse",
            request_id=request_id,
            model_id=self.model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_create,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            stop_reason=stop_reason,
        )

        return ConverseResponse(
            text=text,
            usage=usage,
            model_id=self.model_id,
            latency_ms=latency_ms,
            stop_reason=stop_reason,
        )

    @staticmethod
    def _extract_text(resp: dict[str, Any]) -> str:
        """Converse のレスポンスからテキストを取り出す。"""
        output = resp.get("output", {})
        message = output.get("message", {})
        contents = message.get("content", [])
        for block in contents:
            text = block.get("text")
            if text:
                return str(text)
        return ""
