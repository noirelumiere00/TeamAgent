"""VideoAnalysis Skill 本体 (動画分析、仕様: 実装計画 §7.2 Skill ④)。

競合 PR 動画を Gemini 2.5 Flash で分析し、構成・フック・テロップ・尺・CTA を
抽出して提案に転記できる形で返す。

取得経路は URL で分岐:
- **YouTube / Shorts**: Gemini が file_uri で直接取得 (DL 不要・低コスト)。
- **TikTok / Instagram 等**: file_uri 不可 (URL_ROBOTED) のため yt-dlp で一時 DL
  → bytes を inline で Gemini に渡す → 即破棄 (著作権・ToS 配慮、仕様 S11.6)。

3 層分離: Skill 層。Gemini は adapters/gemini_client.py、DL は
adapters/video_download.py 経由。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.gemini_client import GeminiClient
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.video.schema import VideoAnalysisInput, VideoAnalysisOutput

logger = structlog.get_logger(__name__)

# Gemini が file_uri で直接取得できるドメイン (YouTube 系)
_YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE)

# download_video の型 (テストで差し替え可能にするため Callable で持つ)
Downloader = Callable[[str], tuple[bytes, str]]


@register
class VideoAnalysisSkill(BaseSkill[VideoAnalysisInput, VideoAnalysisOutput]):
    """競合動画を Gemini で構造分析する Skill。"""

    name: ClassVar[str] = "video_analysis"
    description: ClassVar[str] = "競合 PR 動画(YouTube/Shorts/TikTok/IG)の構成・フック・CTA を分析"
    input_schema: ClassVar[type[BaseModel]] = VideoAnalysisInput
    output_schema: ClassVar[type[BaseModel]] = VideoAnalysisOutput

    def __init__(
        self,
        gemini: GeminiClient | None = None,
        *,
        prompt_version: str = "v1",
        downloader: Downloader | None = None,
    ) -> None:
        # gemini は遅延生成 (認証未設定環境でも import / 登録は通す)
        self._gemini = gemini
        self._prompt_version = prompt_version
        self._downloader = downloader

    def _client(self) -> GeminiClient:
        if self._gemini is None:
            self._gemini = GeminiClient.from_env()
        return self._gemini

    def _download(self, url: str, request_id: str) -> tuple[bytes, str]:
        if self._downloader is not None:
            return self._downloader(url)
        from teamagent.adapters.video_download import download_video

        return download_video(url, request_id=request_id)

    def run(self, input: VideoAnalysisInput, ctx: SkillContext) -> VideoAnalysisOutput:
        log = ctx.bind_logger(self.name)
        # §N: SSRF 早期拒否（YouTube file_uri 経路含む全入口を DL/Gemini 前にコスト0で弾く）。
        from teamagent.adapters.url_guard import UrlGuardError, validate_scrape_url

        try:
            # check_dns=False＝安価/非ネットワーク（解決先IPの最終検査は download_video backstop）。
            validate_scrape_url(input.url, request_id=ctx.request_id, check_dns=False)
        except UrlGuardError as e:
            raise RuntimeError(f"VIDEO_URL_BLOCKED: {e}") from e
        is_youtube = bool(_YOUTUBE_RE.search(input.url))
        log.info(
            "video_analysis_start",
            url_len=len(input.url),
            has_focus=input.focus is not None,
            source="youtube" if is_youtube else "download",
        )

        system = load_prompt("video", self._prompt_version, "system")
        user_prompt = "この動画を、システム指示のフォーマットに従って構造分析してください。"
        if input.focus:
            user_prompt += f"\n特に次の観点を重視: {input.focus}"

        if is_youtube:
            # YouTube/Shorts: file_uri で直接 (DL 不要)
            resp = self._client().analyze_video_url(
                url=input.url,
                prompt=user_prompt,
                request_id=ctx.request_id,
                system=system,
            )
        else:
            # TikTok/IG 等: yt-dlp で一時 DL → inline bytes (取得後は adapter 内で破棄)
            data, mime = self._download(input.url, ctx.request_id)
            resp = self._client().analyze_video_bytes(
                data=data,
                mime_type=mime,
                prompt=user_prompt,
                request_id=ctx.request_id,
                system=system,
            )

        log.info("video_analysis_done", cost_usd=resp.cost_usd, model_id=resp.model_id)
        return VideoAnalysisOutput(
            url=input.url,
            analysis=resp.text or "（分析結果が空でした。動画 URL を確認してください）",
            model_id=resp.model_id,
            total_cost_usd=resp.cost_usd,
        )

    def analyze_bytes(
        self,
        data: bytes,
        mime_type: str,
        ctx: SkillContext,
        *,
        focus: str | None = None,
        label: str = "uploaded",
    ) -> VideoAnalysisOutput:
        """Slack 等にアップロードされた動画 bytes を直接分析する (URL 不要)。"""
        log = ctx.bind_logger(self.name)
        log.info("video_analysis_start", source="bytes", size_kb=len(data) // 1024)

        system = load_prompt("video", self._prompt_version, "system")
        user_prompt = "この動画を、システム指示のフォーマットに従って構造分析してください。"
        if focus:
            user_prompt += f"\n特に次の観点を重視: {focus}"

        resp = self._client().analyze_video_bytes(
            data=data,
            mime_type=mime_type,
            prompt=user_prompt,
            request_id=ctx.request_id,
            system=system,
        )
        log.info("video_analysis_done", cost_usd=resp.cost_usd, model_id=resp.model_id)
        return VideoAnalysisOutput(
            url=label,
            analysis=resp.text or "（分析結果が空でした）",
            model_id=resp.model_id,
            total_cost_usd=resp.cost_usd,
        )

    def synthesize_batch(self, analyses: list[str], ctx: SkillContext) -> tuple[str, float]:
        """複数動画の個別分析を横断まとめに合成する。(text, cost) を返す。"""
        if not analyses:
            return ("分析できた動画がありませんでした。", 0.0)
        if len(analyses) == 1:
            return (analyses[0], 0.0)

        system = load_prompt("video", self._prompt_version, "batch_synthesis")
        joined = "\n\n".join(f"## 動画 {i + 1} の分析\n{a}" for i, a in enumerate(analyses))
        prompt = (
            f"# {len(analyses)} 本のショート動画の個別分析\n{joined}\n\n"
            "上記を横断して、フォーマットに従ってまとめてください。"
        )
        resp = self._client().generate_text(prompt, ctx.request_id, system=system)
        return resp.text or "（まとめ生成に失敗しました）", resp.cost_usd
