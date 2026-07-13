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

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from teamagent.adapters.retry import RetryPolicy, call_with_retry

logger = structlog.get_logger(__name__)


# Bedrock で「一過性（リトライ可）」と判断するエラーコード。
# スロットリングと一時的なサーバ/接続エラーのみ。ValidationException や
# AccessDeniedException 等の恒久エラーはリトライしても無駄なので含めない。
# 除外の意図（公式エラー表と突合済み）:
#   - ServiceQuotaExceededException は「上限超過」で一過性でない → 非対象。
#   - ModelStreamErrorException / ModelErrorException は ConverseStream 専用。本実装は
#     非ストリーミングの converse()/rerank() のみなので対象外。
# コード名が将来変わっても HTTP status (429/5xx) の二段構えで拾える。
_RETRYABLE_ERROR_CODES: frozenset[str] = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "ThrottledException",
        "TooManyRequestsException",
        "RequestThrottledException",
        "ServiceUnavailableException",
        "ServiceUnavailable",
        "InternalServerException",
        "ModelTimeoutException",
        "ModelNotReadyException",
    }
)
_RETRYABLE_HTTP_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def _is_bedrock_retryable(exc: BaseException) -> bool:
    """Bedrock 呼び出しの例外が「一過性＝リトライすべき」かを判定する。

    - ``ClientError``: error code か HTTP status で throttling / 5xx を判定。
    - ``BotoCoreError``: 接続断・読み取りタイムアウト等の一時的ネットワーク障害（リトライ可）。
    - それ以外（ValidationException 等の恒久エラーや想定外）: リトライしない。
    """
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {}) or {}
        code = err.get("Code")
        if code in _RETRYABLE_ERROR_CODES:
            return True
        status = (exc.response.get("ResponseMetadata", {}) or {}).get("HTTPStatusCode")
        return isinstance(status, int) and status in _RETRYABLE_HTTP_STATUS
    # ReadTimeout / Connect / EndpointConnection 等は BotoCoreError 配下（一時的）
    return isinstance(exc, BotoCoreError)


def _env_int(name: str, default: int) -> int:
    """env を int として読む（空・不正値は default）。"""
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """env を float として読む（空・不正値は default）。"""
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


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
class EmbedResponse:
    """embed_texts() の返り値。1024 次元ベクトルのリストと推算コストを持つ。"""

    embeddings: list[list[float]]
    model_id: str
    latency_ms: int
    cost_usd: float


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


# Cohere Embed multilingual v3 の料金 (2026/5 時点): $0.10 / 1M input tokens。
# 出典: https://aws.amazon.com/bedrock/pricing/
# トークン数は Bedrock InvokeModel レスポンスから取れないため、文字数を 4 文字/token と
# 概算する（コスト「推算」用途で十分・課金実体ではない）。
_EMBED_COST_PER_MILLION_TOKENS: dict[str, float] = {
    "cohere.embed-multilingual-v3": 0.10,
    "cohere.embed-english-v3": 0.10,
}
_EMBED_CHARS_PER_TOKEN = 4


def _estimate_embed_cost(model_id: str, total_chars: int) -> float:
    """Cohere Embed のコストを文字数から概算する（4 文字 ≒ 1 token・推算用途）。"""
    for prefix, price in _EMBED_COST_PER_MILLION_TOKENS.items():
        if model_id.startswith(prefix):
            est_tokens = total_chars / _EMBED_CHARS_PER_TOKEN
            return round(est_tokens / 1_000_000 * price, 8)
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
        embed_model_id: str | None = None,
        retry_policy: RetryPolicy | None = None,
        read_timeout: int = 120,
    ) -> None:
        self.region = region
        self.model_id = model_id
        # Cohere Embed multilingual v3（InvokeModel・bedrock-runtime 側）。
        # rerank と同じく ap-northeast-1 で In-Region 提供される基盤モデル。
        self.embed_model_id = embed_model_id or "cohere.embed-multilingual-v3"
        # リトライは本クラスの call_with_retry が一元管理する。botocore 内部リトライは
        # total_max_attempts=1（=初回のみ・リトライ無し）に固定し、自前リトライとの二重化で
        # 待ち時間が掛け算になるのを防ぐ。
        #   ⚠️ Config では `max_attempts` は「リトライ回数(初回を含まない)」の意味になり、
        #      max_attempts=1 を指定しても解決値は total_max_attempts=2（初回+1リトライ）になる
        #      （実機 botocore 1.43 で確認）。総試行1回にしたいので必ず total_max_attempts を使う。
        # 併せて read/connect タイムアウトと TCP keepalive を明示する。
        #   - 既定 read 60s だと長い生成で早期に切れる事があるため 120s。
        #   - tcp_keepalive: VPC/NAT/NLB の固定 350s アイドルで接続が無言切断され、再利用時に
        #     70s+ の cold-start/接続リセットになるのを防ぐ（AWS Bedrock 公式推奨）。
        #     OS 側も net.ipv4.tcp_keepalive_time<350 を設定すること（デプロイ runbook 参照）。
        self._retry_policy = retry_policy or RetryPolicy()
        boto_config = Config(
            retries={"total_max_attempts": 1, "mode": "standard"},
            connect_timeout=10,
            read_timeout=read_timeout,
            tcp_keepalive=True,
        )
        self._client = client or boto3.client(
            "bedrock-runtime", region_name=region, config=boto_config
        )
        # Day 8 (2026-05-28) Sprint 4-A: Cohere Rerank v3.5 サポート。
        # `bedrock-agent-runtime` は `bedrock-runtime` (Converse 用) とは別クライアント。
        self._rerank_client = rerank_client or boto3.client(
            "bedrock-agent-runtime", region_name=region, config=boto_config
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
        # コスト方針(2026-06-29): env 未設定時の既定は Haiku。以前の既定 Sonnet は
        # 「BEDROCK_MODEL_ID を注入し忘れたタスク」が silent に Sonnet 課金へ落ちる事故源だった
        # （2026-07-13 実測: 週次 ingest 分類が 573回/週 Sonnet 落ち＝CloudTrail で確定）。
        # 高品質が要る呼び出しは env で明示的に Sonnet を指定する（暗黙昇格の禁止）。
        model_id = os.environ.get("BEDROCK_MODEL_ID", "jp.anthropic.claude-haiku-4-5-20251001-v1:0")
        rerank_arn = os.environ.get("BEDROCK_RERANK_MODEL_ARN")
        embed_model_id = os.environ.get("COHERE_EMBED_MODEL_ID", "cohere.embed-multilingual-v3")
        # 任意で env からバックオフを上書き（既定: 5回 / base 0.5s / cap 20s）。
        policy = RetryPolicy(
            max_attempts=_env_int("BEDROCK_MAX_ATTEMPTS", 5),
            base_delay_s=_env_float("BEDROCK_RETRY_BASE_S", 0.5),
            max_delay_s=_env_float("BEDROCK_RETRY_MAX_S", 20.0),
        )
        return cls(
            region=region,
            model_id=model_id,
            rerank_model_arn=rerank_arn,
            embed_model_id=embed_model_id,
            retry_policy=policy,
            # 既定 120s。proposal_deck 等の長い生成（16k tokens）は
            # BEDROCK_READ_TIMEOUT で延長する。
            read_timeout=_env_int("BEDROCK_READ_TIMEOUT", 120),
        )

    def _make_retry_logger(
        self, event: str, request_id: str
    ) -> Callable[[int, float, BaseException], None]:
        """``call_with_retry`` の on_retry フック。リトライを構造化ログに warning で残す。

        スロットリングの頻度は「同時実行を絞るべきか / 上限緩和申請が要るか」の一次データ。
        管理画面のコスト・混雑可視化でも集計対象になる。
        """

        def _log(attempt: int, delay_s: float, exc: BaseException) -> None:
            if isinstance(exc, ClientError):
                code = str((exc.response.get("Error", {}) or {}).get("Code", "unknown"))
            else:
                code = type(exc).__name__
            logger.warning(
                event,
                request_id=request_id,
                attempt=attempt,
                backoff_s=round(delay_s, 3),
                error_code=code,
            )

        return _log

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
        resp = call_with_retry(
            lambda: self._client.converse(**kwargs),
            is_retryable=_is_bedrock_retryable,
            policy=self._retry_policy,
            on_retry=self._make_retry_logger("bedrock_converse_retry", request_id),
        )
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
        resp = call_with_retry(
            lambda: self._rerank_client.rerank(**request_body),
            is_retryable=_is_bedrock_retryable,
            policy=self._retry_policy,
            on_retry=self._make_retry_logger("bedrock_rerank_retry", request_id),
        )
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

    # Cohere Embed v3 の 1 リクエスト最大件数。
    # 出典: https://docs.cohere.com/reference/embed（texts は最大 96 件）。
    _EMBED_MAX_BATCH = 96

    def _invoke_embed_with_retry(self, body: str, request_id: str) -> dict[str, Any]:
        """InvokeModel(Cohere Embed) を 1 回分リトライ付きで呼ぶ（embed_texts のループ補助）。

        ループ変数をクロージャに束縛しないようメソッドに切り出す（B023 回避・型推論明示）。
        """
        return call_with_retry(
            lambda: self._client.invoke_model(modelId=self.embed_model_id, body=body),
            is_retryable=_is_bedrock_retryable,
            policy=self._retry_policy,
            on_retry=self._make_retry_logger("bedrock_embed_retry", request_id),
        )

    def embed_texts(
        self,
        texts: list[str],
        request_id: str,
        *,
        input_type: str,
    ) -> EmbedResponse:
        """Cohere Embed multilingual v3 で複数テキストを 1024 次元ベクトル化する。

        rerank() と同じ基盤（call_with_retry / コスト推算 / 構造化ログ）を踏襲する。
        Converse 用と同じ ``bedrock-runtime`` クライアント（self._client）の InvokeModel を
        使う（rerank だけが bedrock-agent-runtime 別クライアント）。

        Args:
            texts: 埋め込む素のテキスト（プレフィックス処理は呼び側 Embedder の責務）。
            request_id: トレース ID。
            input_type: ``"search_query"``（検索クエリ）/ ``"search_document"``（取り込み資料）。
                Cohere v3 の非対称埋め込み。e5 の "query: "/"passage: " に相当。

        Returns:
            EmbedResponse: embeddings は texts と同順・各 1024 次元（L2 正規化済）。

        Cohere v3 は正規化済みベクトルを返す（vector_cosine_ops 索引をそのまま流用可能）。
        96 件を超える texts は自動的に分割して複数回 InvokeModel する。

        Raises:
            ValueError: texts が空、または input_type が不正。
            botocore.exceptions.ClientError: Bedrock API エラー（上位でハンドル）。
        """
        if not texts:
            raise ValueError("embed_texts: texts が空です")
        if input_type not in ("search_query", "search_document"):
            raise ValueError(
                f"embed_texts: input_type は search_query / search_document のみ (got {input_type})"
            )

        all_embeddings: list[list[float]] = []
        total_chars = 0
        start = time.perf_counter()
        for i in range(0, len(texts), self._EMBED_MAX_BATCH):
            batch = texts[i : i + self._EMBED_MAX_BATCH]
            total_chars += sum(len(t) for t in batch)
            body = json.dumps(
                {
                    "texts": batch,
                    "input_type": input_type,
                    # 上限超過テキストは末尾を切る（埋め込み失敗で全体を止めない）。
                    "truncate": "END",
                }
            )
            # ループ変数 body をクロージャに束縛せず（B023 回避）、専用メソッドへ渡して
            # リトライする（call_with_retry は同一反復内で同期実行）。
            resp = self._invoke_embed_with_retry(body, request_id)
            payload = json.loads(resp["body"].read())
            for vec in payload.get("embeddings", []) or []:
                all_embeddings.append([float(x) for x in vec])
        latency_ms = int((time.perf_counter() - start) * 1000)

        cost_usd = _estimate_embed_cost(self.embed_model_id, total_chars)
        logger.info(
            "bedrock_embed",
            request_id=request_id,
            model_id=self.embed_model_id,
            input_type=input_type,
            input_texts=len(texts),
            returned=len(all_embeddings),
            dim=len(all_embeddings[0]) if all_embeddings else 0,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )
        return EmbedResponse(
            embeddings=all_embeddings,
            model_id=self.embed_model_id,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
        )
