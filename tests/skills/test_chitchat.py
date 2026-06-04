"""ChitchatSkill の単体テスト（Bedrock をモックし、検索せず会話応答することを検証）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from teamagent.skills.base import SkillContext
from teamagent.skills.chitchat.schema import ChitchatInput
from teamagent.skills.chitchat.skill import ChitchatSkill


def _resp(text: str, cost: float = 0.0002) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        usage=SimpleNamespace(cost_usd=cost, output_tokens=40),
        model_id="haiku",
        latency_ms=900,
        stop_reason="end_turn",
    )


def test_chitchat_returns_conversational_reply() -> None:
    bedrock = MagicMock()
    bedrock.converse.return_value = _resp("こんにちは！お手伝いできます。")
    out = ChitchatSkill(bedrock=bedrock).run(
        ChitchatInput(message="こんにちは"), SkillContext(request_id="t")
    )
    assert "こんにちは" in out.reply
    assert out.total_cost_usd == 0.0002
    # 検索/RAG を呼ばず Bedrock を 1 回だけ叩く
    assert bedrock.converse.call_count == 1
    _, kwargs = bedrock.converse.call_args
    assert kwargs.get("cache_system") is True  # system は固定 → cache
    assert kwargs.get("system")  # load_prompt 由来の非空 system プロンプト


def test_chitchat_falls_back_on_error() -> None:
    bedrock = MagicMock()
    bedrock.converse.side_effect = RuntimeError("bedrock down")
    out = ChitchatSkill(bedrock=bedrock).run(
        ChitchatInput(message="やあ"), SkillContext(request_id="t")
    )
    assert out.reply  # 失敗してもユーザーを止めず定型で返す
    assert out.total_cost_usd == 0.0
