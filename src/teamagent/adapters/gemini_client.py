"""Gemini クライアント（動画分析 Skill 用）。

CLAUDE.md 6-bis Adapter 層。Skill から google-genai を直接呼ばない。
Gemini 2.5 Flash の動画分析機能を使う想定。

Sprint 11（Phase 4-d）で本格運用予定。Sprint 1 では雛形のみ。

Usage:
    client = GeminiClient.from_env()
    result = client.analyze_video_url(
        url="https://www.tiktok.com/@xxx/video/123",
        prompt="この動画のフックと CTA を分析して",
        request_id="req-1",
    )
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GeminiResponse:
    """Gemini API 呼び出しの返り値。"""

    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model_id: str
    latency_ms: int


# Gemini 2.5 Flash 料金（2026/5 時点、USD per 1M tokens）
# https://ai.google.dev/pricing
_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.5-pro": (1.25, 5.00),
}


def _estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """ざっくりコスト推算。"""
    price = _PRICE_TABLE.get(model_id, (0.0, 0.0))
    return round(
        input_tokens / 1_000_000 * price[0] + output_tokens / 1_000_000 * price[1],
        6,
    )


class GeminiClient:
    """Gemini 2.5 Flash の薄ラッパー。

    Sprint 11 で動画分析 Skill から呼ばれる。
    API キーは GEMINI_API_KEY 環境変数経由（Google AI Studio で発行）。
    """

    def __init__(self, api_key: str, model_id: str = "gemini-2.5-flash") -> None:
        self.api_key = api_key
        self.model_id = model_id
        # 遅延 import：google-genai は heavy & 一部環境で SSL 問題が出るため
        self._client: Any | None = None

    @classmethod
    def from_env(cls) -> GeminiClient:
        """環境変数から API キーとモデルを読む。"""
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key or api_key.startswith("AIzaSyxxxxx"):
            raise RuntimeError(
                "GEMINI_API_KEY が未設定です。"
                "Google AI Studio でキーを発行して .env に設定してください"
            )
        model_id = os.environ.get("GEMINI_MODEL_ID", "gemini-2.5-flash")
        return cls(api_key=api_key, model_id=model_id)

    def _ensure_client(self) -> Any:
        """google-genai クライアントを遅延初期化。"""
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def analyze_video_url(
        self,
        url: str,
        prompt: str,
        request_id: str,
        *,
        system: str | None = None,
    ) -> GeminiResponse:
        """YouTube / YouTube Shorts などの動画 URL を Gemini に分析させる。

        Args:
            url: 動画の公開 URL。**Gemini が file_uri で直接取得できるのは YouTube 系**。
                TikTok / Instagram は yt-dlp での取得 (別タスク) が必要。
            prompt: 分析依頼の自然文プロンプト
            request_id: トレース ID
            system: system instruction (任意、分析フォーマット指定用)

        Returns:
            GeminiResponse（テキスト本文 + usage / cost / latency）

        Notes:
            著作権ガード: 動画はダウンロードせず file_uri (stream URL) で渡す。
            google-genai SDK の generate_content に file_data part を含める。
        """
        from google.genai import types

        client = self._ensure_client()
        start = time.perf_counter()

        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part(file_data=types.FileData(file_uri=url, mime_type="video/*")),
                    types.Part(text=prompt),
                ],
            )
        ]
        config = types.GenerateContentConfig(system_instruction=system) if system else None

        try:
            response = client.models.generate_content(
                model=self.model_id,
                contents=contents,
                config=config,
            )
        except Exception as e:
            # 生 URL/プロンプトはログに残さない (CLAUDE.md 6-bis)
            logger.exception("gemini_generate_failed", request_id=request_id)
            raise RuntimeError(f"Gemini 動画分析に失敗しました: {type(e).__name__}") from e

        latency_ms = int((time.perf_counter() - start) * 1000)
        text = response.text or ""
        usage = getattr(response, "usage_metadata", None)
        input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        cost_usd = _estimate_cost(self.model_id, input_tokens, output_tokens)

        logger.info(
            "gemini_analyze_video",
            request_id=request_id,
            model_id=self.model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            text_len=len(text),
        )
        return GeminiResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            model_id=self.model_id,
            latency_ms=latency_ms,
        )

    def health_check(self) -> bool:
        """API キーが有効かどうかを軽量に確認する。

        実装は Sprint 11 で。今は単に from_env() が通れば True とする雑な版。
        """
        try:
            self._ensure_client()
        except Exception as e:
            logger.warning("gemini_health_check_failed", error=str(e))
            return False
        return True
