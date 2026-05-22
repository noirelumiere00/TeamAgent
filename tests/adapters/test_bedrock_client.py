"""BedrockClient のユニットテスト。

boto3 を直接モックして、Converse の usage / cost / latency が
ConverseResponse に正しくマップされることを確認する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from teamagent.adapters.bedrock_client import BedrockClient, _estimate_cost


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
