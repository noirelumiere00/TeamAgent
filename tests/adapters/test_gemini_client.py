"""GeminiClient の単体テスト (API は呼ばない、純ロジック + from_env のみ)。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import (
    DEFAULT_LOCATION,
    DEFAULT_MODEL_ID,
    GeminiClient,
    _estimate_cost,
    _is_retryable_vertex,
    resolve_location,
)


class _CodedError(Exception):
    """code 属性つきの疑似 Vertex エラー（google-genai の例外を模す）。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


_MISSING = object()


def _fake_client(response: Any) -> GeminiClient:
    fake = MagicMock()
    fake.models.generate_content.return_value = response
    return GeminiClient(api_key="test-key", client=fake)


def _video_response(thoughts: int | None | object = _MISSING) -> SimpleNamespace:
    usage_fields: dict[str, object] = {
        "prompt_token_count": 400,
        "candidates_token_count": 297,
    }
    if thoughts is not _MISSING:
        usage_fields["thoughts_token_count"] = thoughts
    return SimpleNamespace(
        text="analysis",
        usage_metadata=SimpleNamespace(**usage_fields),
    )


def _grounded_response(thoughts: int | None | object = _MISSING) -> dict[str, Any]:
    usage: dict[str, object] = {
        "promptTokenCount": 400,
        "candidatesTokenCount": 297,
    }
    if thoughts is not _MISSING:
        usage["thoughtsTokenCount"] = thoughts
    return {
        "text": "grounded analysis",
        "candidates": [
            {
                "groundingMetadata": {
                    "groundingChunks": [{"web": {"title": "source", "uri": "https://example.com"}}]
                }
            }
        ],
        "usageMetadata": usage,
    }


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_CodedError("rate limit exceeded", code=429), True),
        (_CodedError("Service Unavailable", code=503), True),
        (_CodedError("internal", code=500), True),
        (Exception("429 ResourceExhausted: quota"), True),
        (Exception("503 UNAVAILABLE"), True),
        (Exception("deadline exceeded"), True),
        (Exception("request timed out"), True),
        # 恒久エラー（URL 側制約）→ リトライしない
        (RuntimeError("Cannot fetch content from the provided URL"), False),
        (Exception("ROBOTED"), False),
        # 設定不良など一般エラー → リトライしない
        (_CodedError("invalid argument", code=400), False),
        (ValueError("bad config"), False),
    ],
)
def test_is_retryable_vertex(exc: BaseException, expected: bool) -> None:
    assert _is_retryable_vertex(exc) is expected


def test_estimate_cost_flash() -> None:
    # gemini-2.5-flash: in $0.30 / out $2.50 per 1M tokens（2026-09 の公式 pricing）
    cost = _estimate_cost("gemini-2.5-flash", 1_000_000, 1_000_000)
    assert cost == pytest.approx(2.80)


def test_estimate_cost_default_model_is_priced() -> None:
    """既定モデル（2.5 Flash 廃止後の後継）が価格表に載っていること。載っていないと費用が 0 で記録される。"""
    # gemini-3.5-flash: in $1.50 / out $9.00 per 1M tokens
    cost = _estimate_cost(DEFAULT_MODEL_ID, 1_000_000, 1_000_000)
    assert cost == pytest.approx(10.50)


def test_estimate_cost_unknown_model_is_zero() -> None:
    assert _estimate_cost("unknown-model", 1000, 1000) == 0.0


def test_video_cost_includes_thinking_tokens() -> None:
    pytest.importorskip("google.genai")
    result = _fake_client(_video_response(thoughts=1000))._generate_video(
        [], "req-video", system=None
    )

    assert result.output_tokens == 297
    assert result.thoughts_tokens == 1000
    assert result.cost_usd == pytest.approx(0.012273)


@pytest.mark.parametrize("thoughts", [_MISSING, None], ids=["missing", "none"])
def test_video_cost_treats_missing_thinking_tokens_as_zero(thoughts: object) -> None:
    pytest.importorskip("google.genai")
    result = _fake_client(_video_response(thoughts))._generate_video([], "req-video", system=None)

    assert result.output_tokens == 297
    assert result.thoughts_tokens == 0
    assert result.cost_usd == pytest.approx(0.003273)


def test_grounded_cost_includes_thinking_tokens_and_search_surcharge() -> None:
    pytest.importorskip("google.genai")
    result = _fake_client(_grounded_response(thoughts=1000)).generate_with_google_search(
        "prompt", "req-grounded"
    )

    assert result.output_tokens == 297
    assert result.thoughts_tokens == 1000
    assert result.cost_usd == pytest.approx(0.047273)


@pytest.mark.parametrize("thoughts", [_MISSING, None], ids=["missing", "none"])
def test_grounded_cost_treats_missing_thinking_tokens_as_zero(thoughts: object) -> None:
    pytest.importorskip("google.genai")
    result = _fake_client(_grounded_response(thoughts)).generate_with_google_search(
        "prompt", "req-grounded"
    )

    assert result.output_tokens == 297
    assert result.thoughts_tokens == 0
    assert result.cost_usd == pytest.approx(0.038273)


def test_from_env_raises_without_any_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_USE_VERTEX", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI"):
        GeminiClient.from_env()


def test_from_env_raises_on_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_USE_VERTEX", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyxxxxx_placeholder")
    with pytest.raises(RuntimeError, match="GEMINI"):
        GeminiClient.from_env()


def test_from_env_ok_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_USE_VERTEX", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaReal-looking-key-1234567890")
    monkeypatch.setenv("GEMINI_MODEL_ID", "gemini-2.5-pro")
    client = GeminiClient.from_env()
    assert client.use_vertex is False
    assert client.model_id == "gemini-2.5-pro"
    assert client.api_key is not None and client.api_key.startswith("AIza")


def test_from_env_vertex_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """GEMINI_USE_VERTEX=true + project で Vertex モードになる (API キー不要)。"""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_USE_VERTEX", "true")
    monkeypatch.setenv("GEMINI_VERTEX_PROJECT", "teamagent-gcp")
    monkeypatch.setenv("GEMINI_VERTEX_LOCATION", "asia-northeast1")
    monkeypatch.setenv("GEMINI_MODEL_ID", "gemini-2.5-flash")  # 2.5 系は regional をそのまま使う
    client = GeminiClient.from_env()
    assert client.use_vertex is True
    assert client.project == "teamagent-gcp"
    assert client.location == "asia-northeast1"
    assert client.api_key is None


def test_from_env_defaults_to_3_5_flash_lite_on_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """GEMINI_MODEL_ID / GEMINI_VERTEX_LOCATION 未指定なら 3.5 Flash ＋ global（2.5 Flash 廃止対応・09-24 裁定）。"""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL_ID", raising=False)
    monkeypatch.delenv("GEMINI_VERTEX_LOCATION", raising=False)
    monkeypatch.setenv("GEMINI_USE_VERTEX", "true")
    monkeypatch.setenv("GEMINI_VERTEX_PROJECT", "teamagent-gcp")
    client = GeminiClient.from_env()
    assert client.model_id == DEFAULT_MODEL_ID == "gemini-3.5-flash"
    assert client.location == DEFAULT_LOCATION == "global"


def test_resolve_location_forces_global_for_gemini_3(monkeypatch: pytest.MonkeyPatch) -> None:
    """3 系に regional（TD env の us-central1 が残った状態）を渡しても global に読み替える。2.5 系は触らない。"""
    assert resolve_location("gemini-3.5-flash-lite", "us-central1") == "global"
    assert resolve_location("gemini-3.8-flash", "asia-northeast1") == "global"
    assert resolve_location("gemini-3.5-flash-lite", "global") == "global"
    assert resolve_location("gemini-2.5-flash", "us-central1") == "us-central1"
    assert resolve_location("gemini-2.5-flash", "") == DEFAULT_LOCATION
    monkeypatch.setenv("GEMINI_USE_VERTEX", "true")
    monkeypatch.setenv("GEMINI_VERTEX_PROJECT", "teamagent-gcp")
    monkeypatch.setenv("GEMINI_VERTEX_LOCATION", "us-central1")
    monkeypatch.setenv("GEMINI_MODEL_ID", "gemini-3.5-flash-lite")
    assert GeminiClient.from_env().location == "global"


def test_from_env_vertex_requires_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_VERTEX_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GEMINI_USE_VERTEX", "true")
    with pytest.raises(RuntimeError, match="GEMINI_VERTEX_PROJECT"):
        GeminiClient.from_env()


def test_analyze_video_unfetchable_url_maps_to_marker() -> None:
    """TikTok 等のクロール不可 URL は VIDEO_URL_NOT_FETCHABLE マーカーで上がる。"""
    pytest.importorskip("google.genai")  # CI に未導入なら skip (ローカルでは実行)
    client = GeminiClient(api_key="AIzaReal-key-123")
    fake = MagicMock()
    fake.models.generate_content.side_effect = Exception(
        "400 INVALID_ARGUMENT. Cannot fetch content from the provided URL. "
        "Status: URL_ROBOTED-ROBOTED_DENIED"
    )
    client._client = fake  # _ensure_client はこれを返す
    with pytest.raises(RuntimeError, match="VIDEO_URL_NOT_FETCHABLE"):
        client.analyze_video_url("https://www.tiktok.com/@x/video/1", "p", "req-1")


def test_analyze_video_other_error_generic_message() -> None:
    """その他のエラーは汎用メッセージ (マーカー無し)。"""
    pytest.importorskip("google.genai")  # CI に未導入なら skip
    client = GeminiClient(api_key="AIzaReal-key-123")
    fake = MagicMock()
    fake.models.generate_content.side_effect = Exception("500 internal")
    client._client = fake
    with pytest.raises(RuntimeError, match="動画分析に失敗"):
        client.analyze_video_url("https://youtube.com/shorts/x", "p", "req-2")
