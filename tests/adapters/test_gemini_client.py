"""GeminiClient の単体テスト (API は呼ばない、純ロジック + from_env のみ)。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gemini_client import (
    GeminiClient,
    _estimate_cost,
    _is_retryable_vertex,
)


class _CodedError(Exception):
    """code 属性つきの疑似 Vertex エラー（google-genai の例外を模す）。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


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
    # gemini-2.5-flash: in $0.15 / out $0.60 per 1M tokens
    cost = _estimate_cost("gemini-2.5-flash", 1_000_000, 1_000_000)
    assert cost == pytest.approx(0.75)


def test_estimate_cost_unknown_model_is_zero() -> None:
    assert _estimate_cost("unknown-model", 1000, 1000) == 0.0


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
    client = GeminiClient.from_env()
    assert client.use_vertex is True
    assert client.project == "teamagent-gcp"
    assert client.location == "asia-northeast1"
    assert client.api_key is None


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
