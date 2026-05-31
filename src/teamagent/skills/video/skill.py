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
