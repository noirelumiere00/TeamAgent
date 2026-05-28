"""BedrockClient のユニットテスト。

boto3 を直接モックして、Converse の usage / cost / latency が
ConverseResponse に正しくマップされることを確認する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.bedrock_client import BedrockClient, _estimate_cost


@pytest.fixture
def fake_bedrock_response_mock() -> dict[str, Any]:
    """Bedrock Converse のダミーレスポンス。"""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "こんにちは"}],
            }
        },
        "stopReason": "end_turn",
        "usage": {
            "inputTokens": 100,
            "outputTokens": 50,
            "cacheReadInputTokens": 10,
            "cacheWriteInputTokens": 0,
        },
    }


def _fake_bedrock_response() -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "こんにちは"}],
            }
        },
        "stopReason": "end_turn",
        "usage": {
            "inputTokens": 100,
            "outputTokens": 50,
            "cacheReadInputTokens": 10,
            "cacheWriteInputTokens": 0,
        },
    }


def test_converse_returns_text_and_usage() -> None:
    """Bedrock の Converse レスポンスを ConverseResponse にマップできること。"""
    mock_client = MagicMock()
    mock_client.converse.return_value = _fake_bedrock_response()

    client = BedrockClient(
        region="ap-northeast-1",
        model_id="jp.anthropic.claude-sonnet-4-6",
        client=mock_client,
    )
    resp = client.converse(
        messages=[{"role": "user", "content": [{"text": "test"}]}],
        request_id="req-test-1",
    )

    assert resp.text == "こんにちは"
    assert resp.usage.input_tokens == 100
    assert resp.usage.output_tokens == 50
    assert resp.usage.cache_read_input_tokens == 10
    assert resp.stop_reason == "end_turn"
    # cost: 100/1M * 3 + 50/1M * 15 = 0.0003 + 0.00075 = 0.00105 (sonnet-4-6 jp)
    assert resp.usage.cost_usd > 0
    assert resp.latency_ms >= 0


def test_converse_passes_system_prompt() -> None:
    """system が渡されたとき converse() の引数に正しく含まれること。"""
    mock_client = MagicMock()
    mock_client.converse.return_value = _fake_bedrock_response()
    client = BedrockClient(
        region="ap-northeast-1",
        model_id="jp.anthropic.claude-sonnet-4-6",
        client=mock_client,
    )

    client.converse(
        messages=[{"role": "user", "content": [{"text": "x"}]}],
        request_id="req-2",
        system="You are a helpful assistant.",
    )

    call_kwargs = mock_client.converse.call_args.kwargs
    assert call_kwargs["system"] == [{"text": "You are a helpful assistant."}]
    assert call_kwargs["inferenceConfig"]["temperature"] == 0.1


def test_estimate_cost_sonnet() -> None:
    """Sonnet 4.6 のコスト推算が正しいこと。"""
    # 1M input + 1M output = 3 + 15 = 18 USD
    cost = _estimate_cost("jp.anthropic.claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert cost == 18.0


def test_estimate_cost_unknown_model_returns_zero() -> None:
    """未知のモデルでも 0.0 を返して落ちないこと。"""
    cost = _estimate_cost("unknown-model", 100, 100)
    assert cost == 0.0


def test_estimate_cost_with_cache_read() -> None:
    """cache_read 分は input price × 0.1 で計算される（コスト削減確認）。

    Sonnet 4.6: input $3 / output $15 / cache_read $0.3
    入力 1M（うち 900K は cache_read）+ 出力 100K = 100K × $3/1M + 900K × $0.3/1M + 100K × $15/1M
    = 0.3 + 0.27 + 1.5 = 2.07 USD
    （cache 無しなら 1M × $3 + 100K × $15 = 3 + 1.5 = 4.5 USD なので約 54% 削減）
    """
    cost = _estimate_cost(
        "jp.anthropic.claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=900_000,
    )
    assert cost == pytest.approx(2.07, rel=0.01)


def test_converse_with_cache_system(fake_bedrock_response_mock: dict[str, Any]) -> None:
    """cache_system=True で system に cachePoint が含まれること。"""
    from unittest.mock import MagicMock

    mock_client = MagicMock()
    mock_client.converse.return_value = fake_bedrock_response_mock
    client = BedrockClient(
        region="ap-northeast-1",
        model_id="jp.anthropic.claude-sonnet-4-6",
        client=mock_client,
    )

    client.converse(
        messages=[{"role": "user", "content": [{"text": "x"}]}],
        request_id="req-3",
        system="cached system prompt",
        cache_system=True,
    )
    call_kwargs = mock_client.converse.call_args.kwargs
    assert call_kwargs["system"] == [
        {"text": "cached system prompt"},
        {"cachePoint": {"type": "default"}},
    ]


def test_converse_without_cache_system(fake_bedrock_response_mock: dict[str, Any]) -> None:
    """cache_system=False（デフォルト）で cachePoint が含まれないこと。"""
    from unittest.mock import MagicMock

    mock_client = MagicMock()
    mock_client.converse.return_value = fake_bedrock_response_mock
    client = BedrockClient(
        region="ap-northeast-1",
        model_id="jp.anthropic.claude-sonnet-4-6",
        client=mock_client,
    )

    client.converse(
        messages=[{"role": "user", "content": [{"text": "x"}]}],
        request_id="req-4",
        system="plain system",
    )
    call_kwargs = mock_client.converse.call_args.kwargs
    assert call_kwargs["system"] == [{"text": "plain system"}]


# ==================================================================
# Day 8 (2026-05-28) Sprint 4-A: Cohere Rerank v3.5 サポート
# ==================================================================
def test_rerank_calls_bedrock_agent_runtime_with_correct_schema() -> None:
    """rerank() が bedrock-agent-runtime.rerank に正しい schema で呼び出すこと。"""
    from unittest.mock import MagicMock

    mock_rerank_client = MagicMock()
    mock_rerank_client.rerank.return_value = {
        "results": [
            {"index": 2, "relevanceScore": 0.95},
            {"index": 0, "relevanceScore": 0.72},
            {"index": 1, "relevanceScore": 0.40},
        ]
    }
    client = BedrockClient(
        region="ap-northeast-1",
        model_id="jp.anthropic.claude-sonnet-4-6",
        client=MagicMock(),
        rerank_client=mock_rerank_client,
    )

    resp = client.rerank(
        query="日本ガイシ ケイパ",
        documents=["doc 1 about A", "doc 2 about B", "doc 3 about 日本ガイシ ケイパ"],
        request_id="req-rerank-1",
        top_n=2,
    )

    # API call kwargs を検証
    call_kwargs = mock_rerank_client.rerank.call_args.kwargs
    assert call_kwargs["queries"] == [{"type": "TEXT", "textQuery": {"text": "日本ガイシ ケイパ"}}]
    assert call_kwargs["rerankingConfiguration"]["type"] == "BEDROCK_RERANKING_MODEL"
    assert (
        call_kwargs["rerankingConfiguration"]["bedrockRerankingConfiguration"][
            "modelConfiguration"
        ]["modelArn"]
        == "arn:aws:bedrock:ap-northeast-1::foundation-model/cohere.rerank-v3-5:0"
    )
    assert (
        call_kwargs["rerankingConfiguration"]["bedrockRerankingConfiguration"]["numberOfResults"]
        == 2
    )
    assert len(call_kwargs["sources"]) == 3
    assert (
        call_kwargs["sources"][0]["inlineDocumentSource"]["textDocument"]["text"] == "doc 1 about A"
    )

    # Response 検証: relevance_score 降順、index は元 documents の位置
    assert len(resp.results) == 3
    assert resp.results[0].index == 2
    assert resp.results[0].relevance_score == 0.95
    assert resp.results[1].index == 0
    assert resp.query_count == 1


def test_rerank_empty_documents_raises() -> None:
    """documents=[] は ValueError。"""
    from unittest.mock import MagicMock

    client = BedrockClient(
        region="ap-northeast-1",
        model_id="x",
        client=MagicMock(),
        rerank_client=MagicMock(),
    )
    with pytest.raises(ValueError, match="documents が空"):
        client.rerank(query="x", documents=[], request_id="req")


def test_rerank_too_many_documents_raises() -> None:
    """documents > 1000 は ValueError (API spec の上限)。"""
    from unittest.mock import MagicMock

    client = BedrockClient(
        region="ap-northeast-1",
        model_id="x",
        client=MagicMock(),
        rerank_client=MagicMock(),
    )
    with pytest.raises(ValueError, match="最大 1000 件"):
        client.rerank(query="x", documents=["d"] * 1001, request_id="req")


def test_rerank_cost_estimation_uses_query_count() -> None:
    """Cohere Rerank v3.5 のコストは $2/1000 queries (1 query 固定 = $0.002)。"""

    from teamagent.adapters.bedrock_client import _estimate_rerank_cost

    arn = "arn:aws:bedrock:ap-northeast-1::foundation-model/cohere.rerank-v3-5:0"
    assert _estimate_rerank_cost(arn, query_count=1) == 0.002
    assert _estimate_rerank_cost(arn, query_count=10) == 0.02
    # 未知の model は 0
    assert _estimate_rerank_cost("arn:aws:bedrock:::foundation-model/unknown", query_count=1) == 0.0
