"""VideoAnalysis Skill 本体 (動画分析、仕様: 実装計画 §7.2 Skill ④)。

競合 PR 動画 (YouTube/Shorts) を Gemini 2.5 Flash で分析し、構成・フック・
テロップ・尺・CTA を抽出して提案に転記できる形で返す。

3 層分離: Skill 層。Gemini API は adapters/gemini_client.py 経由。
GEMINI_API_KEY が必要 (Google AI Studio 発行、仕様 S11.1/11.2 = 👤 タスク)。
"""

from __future__ import annotations

from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.gemini_client import GeminiClient
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.video.schema import VideoAnalysisInput, VideoAnalysisOutput

logger = structlog.get_logger(__name__)


@register
class VideoAnalysisSkill(BaseSkill[VideoAnalysisInput, VideoAnalysisOutput]):
    """競合動画を Gemini で構造分析する Skill。"""

    name: ClassVar[str] = "video_analysis"
    description: ClassVar[str] = "競合 PR 動画(YouTube/Shorts)の構成・フック・テロップ・CTA を分析"
    input_schema: ClassVar[type[BaseModel]] = VideoAnalysisInput
    output_schema: ClassVar[type[BaseModel]] = VideoAnalysisOutput

    def __init__(
        self,
        gemini: GeminiClient | None = None,
        *,
        prompt_version: str = "v1",
    ) -> None:
        # gemini は遅延生成 (GEMINI_API_KEY 未設定環境でも import / 登録は通す)
        self._gemini = gemini
        self._prompt_version = prompt_version

    def _client(self) -> GeminiClient:
        if self._gemini is None:
            self._gemini = GeminiClient.from_env()
        return self._gemini

    def run(self, input: VideoAnalysisInput, ctx: SkillContext) -> VideoAnalysisOutput:
        log = ctx.bind_logger(self.name)
        log.info("video_analysis_start", url_len=len(input.url), has_focus=input.focus is not None)

        system = load_prompt("video", self._prompt_version, "system")
        user_prompt = "この動画を、システム指示のフォーマットに従って構造分析してください。"
        if input.focus:
            user_prompt += f"\n特に次の観点を重視: {input.focus}"

        resp = self._client().analyze_video_url(
            url=input.url,
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
