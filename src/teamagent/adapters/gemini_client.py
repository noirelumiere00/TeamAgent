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
            from google import genai  # type: ignore

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def analyze_video_url(
        self,
        url: str,
        prompt: str,
        request_id: str,
    ) -> GeminiResponse:
        """YouTube / TikTok / Instagram などの動画 URL を Gemini に分析させる。

        Args:
            url: 動画の公開 URL（YouTube / TikTok / Instagram 等）
            prompt: 分析依頼の自然文プロンプト
            request_id: トレース ID

        Returns:
            GeminiResponse（テキスト本文 + usage / cost）

        Notes:
            Sprint 11 で実装する想定。今は構造だけ用意。
            実装時は client.models.generate_content() を使い、
            contents に {"file_data": {"file_uri": url, "mime_type": "video/*"}}
            を含める。
        """
        client = self._ensure_client()
        time.perf_counter()

        # 実装はここから（Sprint 11）。雛形では NotImplementedError を返す。
        # response = client.models.generate_content(
        #     model=self.model_id,
        #     contents=[
        #         {
        #             "role": "user",
        #             "parts": [
        #                 {"file_data": {"file_uri": url, "mime_type": "video/*"}},
        #                 {"text": prompt},
        #             ],
        #         }
        #     ],
        # )
        # text = response.text
        # usage = response.usage_metadata
        # ...
        del client  # 未使用警告抑制
        raise NotImplementedError(
            "Sprint 11（Phase 4-d）で実装予定。"
            "現在は雛形のみで、API 呼び出しコードはコメントアウト状態。"
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
