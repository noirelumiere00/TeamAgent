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

from teamagent.adapters.retry import RetryPolicy, call_with_retry

logger = structlog.get_logger(__name__)


def _env_int(name: str, default: int) -> int:
    """env を int として読む（空・不正値は default）。"""
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# Vertex(Gemini) の一過性エラー（リトライ可）を判定するマーカー。google-genai は版で例外型が
# 揺れるため、型ではなく code 属性 / メッセージで安全側に分類する。
_VERTEX_RETRYABLE_MARKERS = (
    "resourceexhausted",
    "resource exhausted",
    "rate limit",
    "quota",
    "unavailable",
    "service unavailable",
    "deadline",
    "timeout",
    "timed out",
    "internal error",
)


def _is_retryable_vertex(exc: BaseException) -> bool:
    """Gemini の一過性エラー（429/レート/quota/503/500/timeout）のみ True。

    URL 取得不能（Cannot fetch content / ROBOTED）等の恒久エラーはリトライしない（即上げ）。
    """
    msg = str(exc).lower()
    if "cannot fetch content" in msg or "roboted" in msg:
        return False
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 503):
        return True
    return any(marker in msg for marker in _VERTEX_RETRYABLE_MARKERS)


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
    """Gemini 2.5 Flash の薄ラッパー。2 つの認証経路をサポートする。

    1. **Vertex AI** (仕様 §7.2 の指定・推奨): 既存 GCP プロジェクト経由。
       `GEMINI_USE_VERTEX=true` + `GEMINI_VERTEX_PROJECT` で有効化。認証は ADC
       (GOOGLE_APPLICATION_CREDENTIALS のサービスアカウント等)。社内の
       AI Studio 制限を回避でき、Drive/Gmail と同じ GCP の土俵に揃う。
    2. **AI Studio API キー**: `GEMINI_API_KEY` 環境変数 (個人向け、制限環境では不可)。
    """

    def __init__(
        self,
        api_key: str | None = None,
        model_id: str = "gemini-2.5-flash",
        *,
        use_vertex: bool = False,
        project: str | None = None,
        location: str = "us-central1",
    ) -> None:
        self.api_key = api_key
        self.model_id = model_id
        self.use_vertex = use_vertex
        self.project = project
        self.location = location
        # 遅延 import：google-genai は heavy & 一部環境で SSL 問題が出るため
        self._client: Any | None = None

    @classmethod
    def from_env(cls) -> GeminiClient:
        """環境変数から認証経路とモデルを読む。Vertex を優先する。"""
        model_id = os.environ.get("GEMINI_MODEL_ID", "gemini-2.5-flash")

        use_vertex = os.environ.get("GEMINI_USE_VERTEX", "false").lower() in ("1", "true", "yes")
        if use_vertex:
            project = os.environ.get("GEMINI_VERTEX_PROJECT") or os.environ.get(
                "GOOGLE_CLOUD_PROJECT"
            )
            if not project:
                raise RuntimeError(
                    "GEMINI_USE_VERTEX=true ですが GEMINI_VERTEX_PROJECT (GCP プロジェクト ID) "
                    "が未設定です。Vertex AI を有効化した GCP プロジェクト ID を設定してください"
                )
            location = os.environ.get("GEMINI_VERTEX_LOCATION", "us-central1")
            return cls(model_id=model_id, use_vertex=True, project=project, location=location)

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key or api_key.startswith("AIzaSyxxxxx"):
            raise RuntimeError(
                "Gemini の認証が未設定です。次のいずれかを設定してください: "
                "(1) GEMINI_USE_VERTEX=true + GEMINI_VERTEX_PROJECT (推奨・社内 GCP 経由)、"
                "(2) GEMINI_API_KEY (Google AI Studio 発行)"
            )
        return cls(api_key=api_key, model_id=model_id)

    def _ensure_client(self) -> Any:
        """google-genai クライアントを遅延初期化 (Vertex / API キーを切替)。"""
        if self._client is None:
            from google import genai

            if self.use_vertex:
                self._client = genai.Client(
                    vertexai=True, project=self.project, location=self.location
                )
            else:
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
        """YouTube / YouTube Shorts の動画 URL を file_uri で Gemini に分析させる。

        **Gemini が file_uri で直接取得できるのは YouTube 系のみ**。TikTok/Instagram は
        URL_ROBOTED で拒否されるので analyze_video_bytes (yt-dlp DL) を使う。
        著作権ガード: 動画はダウンロードせず file_uri (stream URL) で渡す。
        """
        from google.genai import types

        parts = [
            types.Part(file_data=types.FileData(file_uri=url, mime_type="video/*")),
            types.Part(text=prompt),
        ]
        return self._generate_video(parts, request_id, system=system)

    def analyze_video_bytes(
        self,
        data: bytes,
        mime_type: str,
        prompt: str,
        request_id: str,
        *,
        system: str | None = None,
    ) -> GeminiResponse:
        """ダウンロード済みの動画 bytes を inline で Gemini に分析させる。

        TikTok/Instagram など file_uri で取得できない動画用 (yt-dlp で取得した bytes)。
        Vertex/Gemini の inline 上限に収まるサイズ前提 (~20MB)。
        """
        from google.genai import types

        parts = [
            types.Part.from_bytes(data=data, mime_type=mime_type),
            types.Part(text=prompt),
        ]
        return self._generate_video(parts, request_id, system=system)

    def generate_text(
        self, prompt: str, request_id: str, *, system: str | None = None
    ) -> GeminiResponse:
        """テキストのみの生成 (複数動画分析の横断まとめ等)。動画 part は含めない。"""
        from google.genai import types

        return self._generate_video([types.Part(text=prompt)], request_id, system=system)

    def _generate_video(
        self, parts: list[Any], request_id: str, *, system: str | None
    ) -> GeminiResponse:
        """動画/テキスト part を generate_content に投げ GeminiResponse に整形する共通処理。"""
        from google.genai import types

        client = self._ensure_client()
        start = time.perf_counter()
        contents = [types.Content(role="user", parts=parts)]
        config = types.GenerateContentConfig(system_instruction=system) if system else None

        try:
            # 一過性エラー（429/レート/quota/503/500/timeout）は指数バックオフで自動リトライ。
            # Bedrock 側と非対称だった堅牢性を揃える。恒久エラー（URL取得不能等）は即上げ。
            response = call_with_retry(
                lambda: client.models.generate_content(
                    model=self.model_id,
                    contents=contents,
                    config=config,
                ),
                is_retryable=_is_retryable_vertex,
                policy=RetryPolicy(
                    max_attempts=_env_int("GEMINI_RETRY_MAX_ATTEMPTS", 3),
                    base_delay_s=0.6,
                    max_delay_s=8.0,
                ),
                on_retry=lambda n, d, e: logger.warning(
                    "gemini_retry",
                    request_id=request_id,
                    attempt=n,
                    delay_s=round(d, 2),
                    error=type(e).__name__,
                ),
            )
        except Exception as e:
            # 生 URL/プロンプトはログに残さない (CLAUDE.md 6-bis)
            logger.exception("gemini_generate_failed", request_id=request_id)
            msg = str(e)
            # file_uri を直接クロールできない URL (TikTok/IG、robots 拒否 YouTube 等)。
            # 設定不良ではなく URL 側の制約なので専用マーカーで上げ案内に変換させる。
            if "Cannot fetch content" in msg or "ROBOTED" in msg:
                raise RuntimeError(
                    "VIDEO_URL_NOT_FETCHABLE: この動画URLは直接取得できませんでした"
                ) from e
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
