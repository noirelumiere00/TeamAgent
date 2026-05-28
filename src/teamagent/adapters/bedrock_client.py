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


@dataclass(frozen=True)
class RerankResult:
    """rerank() の 1 件の結果。元の sources での index と relevance score。"""

    index: int  # 元 sources リストでの位置 (0-based)
    relevance_score: float  # 0.0 〜 1.0、1.0 に近いほど関連性高い


@dataclass(frozen=True)
class RerankResponse:
    """rerank() の返り値。relevance_score 降順で並べた results を返す。"""

    results: list[RerankResult]
    model_arn: str
    latency_ms: int
    query_count: int  # コスト計算用 (Bedrock Rerank の課金単位)


# Cohere Rerank v3.5 の料金 (2026/5 時点): $2.00 / 1,000 queries (1 query ≦ 100 docs)
# 出典: https://aws.amazon.com/bedrock/pricing/
_RERANK_COST_PER_QUERY: dict[str, float] = {
    "cohere.rerank-v3-5": 0.002,  # $2 / 1000
    "amazon.rerank-v1": 0.001,  # $1 / 1000 (参考、現在未採用)
}


def _estimate_rerank_cost(model_arn: str, query_count: int) -> float:
    """Bedrock Rerank の課金は queries 数ベース (各 query は最大 100 docs)。

    1 query 内 docs 数による課金差はないため、queries 数だけで計算可能。
    """
    for prefix, price in _RERANK_COST_PER_QUERY.items():
        if prefix in model_arn:
            return round(query_count * price, 6)
    return 0.0


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
        rerank_client: Any | None = None,
        rerank_model_arn: str | None = None,
    ) -> None:
        self.region = region
        self.model_id = model_id
        self._client = client or boto3.client("bedrock-runtime", region_name=region)
        # Day 8 (2026-05-28) Sprint 4-A: Cohere Rerank v3.5 サポート。
        # `bedrock-agent-runtime` は `bedrock-runtime` (Converse 用) とは別クライアント。
        self._rerank_client = rerank_client or boto3.client(
            "bedrock-agent-runtime", region_name=region
        )
        # ap-northeast-1 で Cohere Rerank v3.5 が In-Region 提供されている。
        # 出典: https://docs.aws.amazon.com/bedrock/latest/userguide/rerank-supported.html
        self.rerank_model_arn = rerank_model_arn or (
            f"arn:aws:bedrock:{region}::foundation-model/cohere.rerank-v3-5:0"
        )

    @classmethod
    def from_env(cls) -> BedrockClient:
        """環境変数から BedrockClient を構築する。

        必須: AWS_REGION, BEDROCK_MODEL_ID
        オプション: BEDROCK_RERANK_MODEL_ARN (省略時は ap-northeast-1 の Cohere v3.5)
        """
        region = os.environ.get("AWS_REGION", "ap-northeast-1")
        model_id = os.environ.get("BEDROCK_MODEL_ID", "jp.anthropic.claude-sonnet-4-6")
        rerank_arn = os.environ.get("BEDROCK_RERANK_MODEL_ARN")
        return cls(region=region, model_id=model_id, rerank_model_arn=rerank_arn)

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

    def rerank(
        self,
        query: str,
        documents: list[str],
        request_id: str,
        *,
        top_n: int | None = None,
    ) -> RerankResponse:
        """Bedrock Agent Runtime Rerank API (Cohere Rerank v3.5) で文書を再ランクする。

        Day 8 (2026-05-28) Sprint 4-A 追加。dense retrieval の固有名詞弱点を補強する。
        Anthropic Contextual Retrieval 公式ベンチで失敗率 5.7% → 1.9% (-67%) を実現する
        中核機能 (https://www.anthropic.com/news/contextual-retrieval)。

        Args:
            query: ユーザークエリ
            documents: 元のチャンク内容のリスト (top_k=30 程度を渡し、top-N に絞る用途)
            request_id: トレース ID
            top_n: 返す上位件数。None なら全件 (relevance score でソート済)

        Returns:
            RerankResponse: results は relevance_score 降順、index は元 documents の位置

        Cost: $2 / 1000 queries (1 query につき最大 100 docs、それ以上は要分割)

        Raises:
            ValueError: documents が空 or 1001 件以上
            botocore.exceptions.ClientError: Bedrock API エラー (上位でハンドル)
        """
        if not documents:
            raise ValueError("rerank: documents が空です")
        if len(documents) > 1000:
            # API spec の上限 (https://docs.aws.amazon.com/bedrock/latest/APIReference/
            # API_agent-runtime_Rerank.html#bedrock-agent-runtime_Rerank-request-sources)
            raise ValueError(f"rerank: documents は最大 1000 件 (got {len(documents)})")

        number_of_results = top_n if top_n is not None else len(documents)

        request_body: dict[str, Any] = {
            "queries": [{"type": "TEXT", "textQuery": {"text": query}}],
            "rerankingConfiguration": {
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": self.rerank_model_arn},
                    "numberOfResults": number_of_results,
                },
            },
            "sources": [
                {
                    "type": "INLINE",
                    "inlineDocumentSource": {
                        "type": "TEXT",
                        "textDocument": {"text": doc},
                    },
                }
                for doc in documents
            ],
        }

        start = time.perf_counter()
        resp = self._rerank_client.rerank(**request_body)
        latency_ms = int((time.perf_counter() - start) * 1000)

        results_raw = resp.get("results", []) or []
        results: list[RerankResult] = []
        for r in results_raw:
            results.append(
                RerankResult(
                    index=int(r.get("index", 0)),
                    relevance_score=float(r.get("relevanceScore", 0.0)),
                )
            )

        # Bedrock Rerank の課金は queries 数 (この実装では常に 1 query)
        cost_usd = _estimate_rerank_cost(self.rerank_model_arn, query_count=1)

        logger.info(
            "bedrock_rerank",
            request_id=request_id,
            model_arn=self.rerank_model_arn,
            input_docs=len(documents),
            returned_results=len(results),
            top_score=results[0].relevance_score if results else None,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )

        return RerankResponse(
            results=results,
            model_arn=self.rerank_model_arn,
            latency_ms=latency_ms,
            query_count=1,
        )
