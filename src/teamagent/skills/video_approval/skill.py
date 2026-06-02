"""VideoApproval Skill 本体 (動画一次FB審査)。

編集者納品の動画を、案件のオリエンと照合して一次FBを生成する。

動画取得経路 (URL で分岐):
- Google Drive URL: drive_video.download_drive_video → bytes → Gemini inline
- YouTube/Shorts: Gemini が file_uri で直接取得
- TikTok/Instagram 等: video_download (yt-dlp) → bytes → Gemini inline

Gemini に「動画 + オリエン (4観点の正解条件)」を渡し、構造化FB (合否 + 指摘) を得る。
LLM 出力の JSON ブロックを防御的にパースし、失敗してもFB本文は必ず返す (fail-safe)。

3 層分離: Skill 層。動画取得は adapters/ (drive_video / video_download)、
分析は adapters/gemini_client.py 経由。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.gemini_client import GeminiClient
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.video_approval.schema import (
    ApprovalIssue,
    VideoApprovalInput,
    VideoApprovalOutput,
)

logger = structlog.get_logger(__name__)

_YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE)
_DRIVE_RE = re.compile(r"(drive|docs)\.google\.com", re.IGNORECASE)
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)

# 動画取得関数の型 (テスト差し替え用)
Downloader = Callable[[str], tuple[bytes, str]]


@register
class VideoApprovalSkill(BaseSkill[VideoApprovalInput, VideoApprovalOutput]):
    """動画をオリエンと照合して一次FBを生成する Skill。"""

    name: ClassVar[str] = "video_approval"
    description: ClassVar[str] = (
        "編集者納品の動画をオリエン(必須要素/NG/テロップ/尺仕様)と照合し一次FBを生成"
    )
    input_schema: ClassVar[type[BaseModel]] = VideoApprovalInput
    output_schema: ClassVar[type[BaseModel]] = VideoApprovalOutput

    def __init__(
        self,
        gemini: GeminiClient | None = None,
        *,
        prompt_version: str = "v1",
        drive_downloader: Downloader | None = None,
        video_downloader: Downloader | None = None,
        proxy_fn: Callable[[bytes, str], tuple[bytes, str]] | None = None,
        max_download_mb: int = 300,
    ) -> None:
        self._gemini = gemini
        self._prompt_version = prompt_version
        self._drive_downloader = drive_downloader
        self._video_downloader = video_downloader
        self._proxy_fn = proxy_fn
        # 納品動画は短尺でも高画質で 25〜50MB あるため、まず大きめに DL してから
        # proxy(ffmpeg)で Gemini inline 上限以下へ縮める。max は OOM 防御の絶対上限。
        self._max_download_mb = max_download_mb

    def _client(self) -> GeminiClient:
        if self._gemini is None:
            self._gemini = GeminiClient.from_env()
        return self._gemini

    def _download_drive(self, url: str, request_id: str) -> tuple[bytes, str]:
        if self._drive_downloader is not None:
            return self._drive_downloader(url)
        from teamagent.adapters.drive_video import download_drive_video

        return download_drive_video(url, request_id=request_id, max_mb=self._max_download_mb)

    def _download_other(self, url: str, request_id: str) -> tuple[bytes, str]:
        if self._video_downloader is not None:
            return self._video_downloader(url)
        from teamagent.adapters.video_download import download_video

        return download_video(url, request_id=request_id)

    def _ensure_under_limit(self, data: bytes, mime: str, request_id: str) -> tuple[bytes, str]:
        """Gemini inline 上限を超える動画は ffmpeg proxy で縮める（テスト差し替え可）。"""
        if self._proxy_fn is not None:
            return self._proxy_fn(data, mime)
        from teamagent.adapters.video_proxy import ensure_under_limit

        return ensure_under_limit(data, mime, request_id=request_id)

    def run(self, input: VideoApprovalInput, ctx: SkillContext) -> VideoApprovalOutput:
        log = ctx.bind_logger(self.name)
        url = input.video_url or ""
        is_youtube = bool(_YOUTUBE_RE.search(url))
        is_drive = bool(_DRIVE_RE.search(url))
        log.info(
            "video_approval_start",
            source="youtube" if is_youtube else ("drive" if is_drive else "download"),
            has_url=bool(url),
        )

        system = load_prompt("video_approval", self._prompt_version, "system")
        user_prompt = (
            "# 案件オリエン（審査の正解条件）\n"
            f"{input.orientation.to_prompt_block()}\n\n"
            "上記オリエンに照らして、この動画を4観点で審査し、"
            "システム指示のフォーマットで一次FBを作成してください。"
        )

        if not url:
            return VideoApprovalOutput(
                verdict="確認要",
                summary="動画 URL が指定されていません。",
                feedback_text="動画 URL が見つかりませんでした。URL をご確認ください。",
            )

        if is_youtube:
            resp = self._client().analyze_video_url(
                url=url, prompt=user_prompt, request_id=ctx.request_id, system=system
            )
        else:
            data, mime = (
                self._download_drive(url, ctx.request_id)
                if is_drive
                else self._download_other(url, ctx.request_id)
            )
            # Gemini inline 上限(~20MB)超なら ffmpeg proxy で縮めてから渡す
            data, mime = self._ensure_under_limit(data, mime, ctx.request_id)
            resp = self._client().analyze_video_bytes(
                data=data,
                mime_type=mime,
                prompt=user_prompt,
                request_id=ctx.request_id,
                system=system,
            )

        out = self._parse(resp.text, resp.cost_usd, resp.model_id)
        log.info(
            "video_approval_done",
            verdict=out.verdict,
            issues=len(out.issues),
            cost_usd=out.total_cost_usd,
        )
        return out

    def _parse(self, text: str, cost: float, model_id: str) -> VideoApprovalOutput:
        """LLM 出力 (FB本文 + JSONブロック) を構造化する。fail-safe。"""
        data: dict[str, Any] = {}
        m = _JSON_BLOCK_RE.search(text)
        if m:
            try:
                parsed = json.loads(m.group(1))
                if isinstance(parsed, dict):
                    data = parsed
            except (json.JSONDecodeError, ValueError):
                data = {}

        feedback_text = _JSON_BLOCK_RE.sub("", text).strip() or text.strip()

        issues: list[ApprovalIssue] = []
        raw_issues = data.get("issues")
        if isinstance(raw_issues, list):
            for it in raw_issues:
                if not isinstance(it, dict):
                    continue
                issues.append(
                    ApprovalIssue(
                        category=str(it.get("category", "確認要")),
                        severity=str(it.get("severity", "suggestion")),
                        timecode=_clean(it.get("timecode")),
                        detail=str(it.get("detail", "")),
                        fix=_clean(it.get("fix")),
                    )
                )

        verdict = _clean(data.get("verdict")) or ("OK" if not issues else "要修正")
        summary = _clean(data.get("summary")) or feedback_text[:60]
        return VideoApprovalOutput(
            verdict=verdict,
            summary=summary,
            issues=issues,
            feedback_text=feedback_text,
            model_id=model_id,
            total_cost_usd=cost,
        )


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("null", "none", "n/a", "—", "-"):
        return None
    return s
