"""Python Anthropic Bedrock client の 6-bis コストログをオフライン検証.

ライブ Bedrock 抜きで、per-call usage から 6-bis の cost/token を取り出せることを確認する。
"""

from __future__ import annotations

from anthropic.types import Usage

from teamagent.orchestrator.sdk_runner import Price, usage_to_record


def test_anthropic_usage_carries_per_call_usage_and_maps_to_6bis() -> None:
    usage = Usage(
        input_tokens=1000,
        output_tokens=200,
        cache_read_input_tokens=500,
        cache_creation_input_tokens=100,
    )

    rec = usage_to_record(
        usage.model_dump(exclude_none=True),
        model="jp.anthropic.claude-sonnet-4-6",
        request_id="req-xyz",
    )
    # 6-bis が要求する粒度の全フィールドが取れる
    assert rec.request_id == "req-xyz"
    assert rec.model == "jp.anthropic.claude-sonnet-4-6"
    assert rec.input_tokens == 1000
    assert rec.output_tokens == 200
    assert rec.cache_read_tokens == 500
    assert rec.cache_creation_tokens == 100
    assert rec.cost_usd > 0.0


def test_cost_calculation_matches_price_table() -> None:
    rec = usage_to_record(
        {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        model="m",
        request_id="r",
        price=Price(),  # input $3/Mtok, output $15/Mtok
    )
    assert abs(rec.cost_usd - 18.0) < 1e-6


def test_missing_usage_keys_default_to_zero() -> None:
    rec = usage_to_record({"input_tokens": 10}, model="m", request_id="r")
    assert rec.input_tokens == 10
    assert rec.output_tokens == 0
    assert rec.cache_read_tokens == 0
    assert rec.cache_creation_tokens == 0
