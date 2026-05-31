"""GeminiClient の単体テスト (API は呼ばない、純ロジック + from_env のみ)。"""

from __future__ import annotations

import pytest

from teamagent.adapters.gemini_client import GeminiClient, _estimate_cost


def test_estimate_cost_flash() -> None:
    # gemini-2.5-flash: in $0.15 / out $0.60 per 1M tokens
    cost = _estimate_cost("gemini-2.5-flash", 1_000_000, 1_000_000)
    assert cost == pytest.approx(0.75)


def test_estimate_cost_unknown_model_is_zero() -> None:
    assert _estimate_cost("unknown-model", 1000, 1000) == 0.0


def test_from_env_raises_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiClient.from_env()


def test_from_env_raises_on_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyxxxxx_placeholder")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiClient.from_env()


def test_from_env_ok_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaReal-looking-key-1234567890")
    monkeypatch.setenv("GEMINI_MODEL_ID", "gemini-2.5-pro")
    client = GeminiClient.from_env()
    assert client.model_id == "gemini-2.5-pro"
    assert client.api_key.startswith("AIza")
