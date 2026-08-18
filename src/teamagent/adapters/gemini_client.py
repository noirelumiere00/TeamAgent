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


@dataclass(frozen=True)
class GroundingSource:
    """groundingMetadata.groundingChunks[i].web を素の値へ落としたもの。

    **index は groundingChunks の添字と 1:1 で保たれる**（web 以外の chunk は uri="" の
    プレースホルダとして残す）。groundingSupports[].groundingChunkIndices が
    この添字を指すため、間引くと参照がずれる。
    """

    title: str
    uri: str
    domain: str


@dataclass(frozen=True)
class GroundingSupport:
    """groundingMetadata.groundingSupports[i]（本文断片 ↔ 出典 chunk の対応）。"""

    text: str
    source_indices: tuple[int, ...]


@dataclass(frozen=True)
class GeminiGroundedResponse:
    """Google 検索グラウンディング付き生成の返り値。

    grounded=False は「検索の裏付けが取れていない」＝呼び出し側は fail-closed にする。
    """

    text: str
    sources: tuple[GroundingSource, ...]
    supports: tuple[GroundingSupport, ...]
    search_queries: tuple[str, ...]
    grounded: bool
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model_id: str
    latency_ms: int


def _pick(obj: Any, *names: str) -> Any:
    """SDK オブジェクト（snake_case 属性）と生 JSON（camelCase キー）の両方から値を取る。

    google-genai は版によって pydantic オブジェクトを返したり、REST の生 dict を
    そのまま持ち回ったりする。どちらでも壊れないように両対応で読む。
    """
    for name in names:
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _parse_grounding(
    candidate: Any,
) -> tuple[tuple[GroundingSource, ...], tuple[GroundingSupport, ...], tuple[str, ...]]:
    """candidate.groundingMetadata から出典・対応・検索クエリを取り出す。

    metadata が無い / chunks が空なら全て空で返す（＝呼び出し側が grounded=False と判定）。
    """
    meta = _pick(candidate, "grounding_metadata", "groundingMetadata")
    if meta is None:
        return ((), (), ())

    sources: list[GroundingSource] = []
    for chunk in _as_list(_pick(meta, "grounding_chunks", "groundingChunks")):
        web = _pick(chunk, "web")
        if web is None:
            # retrievedContext / maps 等。添字を保つためプレースホルダを置く。
            sources.append(GroundingSource(title="", uri="", domain=""))
            continue
        sources.append(
            GroundingSource(
                title=str(_pick(web, "title") or ""),
                uri=str(_pick(web, "uri") or ""),
                domain=str(_pick(web, "domain") or ""),
            )
        )

    supports: list[GroundingSupport] = []
    for support in _as_list(_pick(meta, "grounding_supports", "groundingSupports")):
        segment = _pick(support, "segment")
        indices = _as_list(_pick(support, "grounding_chunk_indices", "groundingChunkIndices"))
        clean: list[int] = []
        for raw in indices:
            try:
                idx = int(raw)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(sources):
                clean.append(idx)
        supports.append(
            GroundingSupport(
                text=str(_pick(segment, "text") or "") if segment is not None else "",
                source_indices=tuple(clean),
            )
        )

    queries = tuple(
        str(q) for q in _as_list(_pick(meta, "web_search_queries", "webSearchQueries")) if q
    )
    return (tuple(sources), tuple(supports), queries)


# Gemini 2.5 Flash 料金（2026/5 時点、USD per 1M tokens）
# https://ai.google.dev/pricing
_PRICE_TABLE: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.5-pro": (1.25, 5.00),
}


# Google 検索グラウンディングは token 課金と別に「1 grounded prompt」単位で課金される
# （公表 $35 / 1,000 prompt・無料枠を超えた分）。token 換算だけだと実費を桁で過小報告する
# ので、grounded 呼び出しのコストにはこの定額を足す。⚠️料金改定時は要更新。
_GROUNDING_REQUEST_USD = 0.035
# grounded 呼び出しのリトライ上限（動画分析の 3 とは別値。理由は
# generate_with_google_search の docstring）。
_GROUNDED_RETRY_ATTEMPTS = 2


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
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self.model_id = model_id
        self.use_vertex = use_vertex
        self.project = project
        self.location = location
        # 遅延 import：google-genai は heavy & 一部環境で SSL 問題が出るため
        # client を渡すと遅延生成をスキップする（テストのフェイク注入口）。
        self._client: Any | None = client

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

    def generate_with_google_search(
        self,
        prompt: str,
        request_id: str,
        *,
        system: str | None = None,
        timeout_s: float | None = None,
    ) -> GeminiGroundedResponse:
        """Google 検索グラウンディングを有効にして生成する（web_research Skill 用）。

        CLAUDE.md 6-bis: Skill 側は google-genai を import しない。検索ツールの有効化も
        groundingMetadata の解釈もここ（Adapter 層）に閉じ、Skill には素の dataclass を返す。

        ⚠️ Google 検索ツールは structured output（responseSchema / JSON mime type）と併用
        できない。要約は自由記述で受け、**出典は必ず groundingMetadata から機械的に組む**
        （LLM 本文中の URL は呼び出し側で採用しないこと）。

        grounded=False は「検索の裏付けが無い応答」＝呼び出し側は fail-closed にする。

        timeout_s は **1 回の HTTP 試行あたり** の上限。リトライは既定 2 回までに絞ってあり
        （_GROUNDED_RETRY_ATTEMPTS）、最悪でも timeout_s×2＋バックオフで頭打ちになる。
        動画分析と同じ 3 回にすると 3×deadline で OpenClaw のターン制限（実測 ~181s）を
        突き抜け、ターンごと応答全損する。
        """
        from google.genai import types

        client = self._ensure_client()
        config_kwargs: dict[str, Any] = {
            "tools": [types.Tool(google_search=types.GoogleSearch())],
        }
        if system:
            config_kwargs["system_instruction"] = system
        if timeout_s is not None and timeout_s > 0:
            # google-genai の timeout はミリ秒。OpenClaw のターン制限の内側で必ず返すため。
            config_kwargs["http_options"] = types.HttpOptions(timeout=int(timeout_s * 1000))
        config = types.GenerateContentConfig(**config_kwargs)
        contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]

        start = time.perf_counter()
        try:
            response = call_with_retry(
                lambda: client.models.generate_content(
                    model=self.model_id,
                    contents=contents,
                    config=config,
                ),
                is_retryable=_is_retryable_vertex,
                policy=RetryPolicy(
                    max_attempts=_env_int(
                        "GEMINI_GROUNDED_RETRY_MAX_ATTEMPTS", _GROUNDED_RETRY_ATTEMPTS
                    ),
                    base_delay_s=0.6,
                    max_delay_s=4.0,
                ),
                on_retry=lambda n, d, e: logger.warning(
                    "gemini_grounded_retry",
                    request_id=request_id,
                    attempt=n,
                    delay_s=round(d, 2),
                    error=type(e).__name__,
                ),
            )
        except Exception as e:
            # 生プロンプト（＝利用者のクエリ）はログに残さない (CLAUDE.md 6-bis)
            logger.exception("gemini_grounded_generate_failed", request_id=request_id)
            raise RuntimeError(f"Gemini Web 検索に失敗しました: {type(e).__name__}") from e

        latency_ms = int((time.perf_counter() - start) * 1000)
        candidates = _as_list(_pick(response, "candidates"))
        candidate = candidates[0] if candidates else None
        sources, supports, queries = (
            _parse_grounding(candidate) if candidate is not None else ((), (), ())
        )
        text = str(_pick(response, "text") or "")
        usage = _pick(response, "usage_metadata", "usageMetadata")
        input_tokens = int(_pick(usage, "prompt_token_count", "promptTokenCount") or 0)
        output_tokens = int(_pick(usage, "candidates_token_count", "candidatesTokenCount") or 0)
        grounded = any(s.uri for s in sources)
        cost_usd = round(
            _estimate_cost(self.model_id, input_tokens, output_tokens)
            + (_GROUNDING_REQUEST_USD if grounded else 0.0),
            6,
        )

        logger.info(
            "gemini_google_search",
            request_id=request_id,
            model_id=self.model_id,
            grounded=grounded,
            source_count=sum(1 for s in sources if s.uri),
            support_count=len(supports),
            query_count=len(queries),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            text_len=len(text),
        )  # 検索クエリ本文・ページ本文はログに出さない
        return GeminiGroundedResponse(
            text=text,
            sources=sources,
            supports=supports,
            search_queries=queries,
            grounded=grounded,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            model_id=self.model_id,
            latency_ms=latency_ms,
        )

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
