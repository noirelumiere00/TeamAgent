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
