"""TikTokSearch Skill 本体。

「TikTokで新宿 ランチ 検索して」「#新宿 で調べて」のような指示で:
  1. TikTok を検索 (Puppeteer 実ブラウザ + 内部 API 傍受) → 上位動画のメタを取得
  2. Gemini で上位動画のメタを横断分析 (伸びている勝ちパターン/フックの型/推奨アクション)
  3. データ + 分析を返す

取得経路は adapters/tiktok_scraper.py (Node subprocess)、分析は adapters/gemini_client.py。
3 層分離: Skill 層。

Apify 等の課金 SaaS を使わず、ローカル Chrome で完結する (仕様: 検索ツール拡張)。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.gemini_client import GeminiClient
from teamagent.adapters.tiktok_scraper import TikTokSearchResult, search_tiktok
from teamagent.prompts.loader import load_prompt
from teamagent.skills._html.thumbs import rehost_many
from teamagent.skills._shared.report_html import html_reports_enabled, publish_report
from teamagent.skills._shared.report_pptx import publish_pptx
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.tiktok_search.report import build_report
from teamagent.skills.tiktok_search.schema import (
    TikTokSearchInput,
    TikTokSearchOutput,
    TikTokVideoOut,
)

logger = structlog.get_logger(__name__)

# search_tiktok の型 (テストで差し替え可能にするため Callable で持つ)
Searcher = Callable[..., TikTokSearchResult]


def _format_videos_for_prompt(videos: list[TikTokVideoOut]) -> str:
    """上位動画メタを Gemini 分析用のテキストに整形する。"""
    lines: list[str] = []
    for v in videos:
        tags = " ".join(f"#{t}" for t in v.hashtags[:8])
        lines.append(
            f"[{v.rank}] @{v.author} (フォロワー{v.author_followers:,})\n"
            f"  再生{v.play_count:,} / いいね{v.digg_count:,} / コメント{v.comment_count:,} / "
            f"シェア{v.share_count:,} / 保存{v.collect_count:,} / "
            f"エンゲージ率{v.engagement_rate:.1%} / 尺{v.duration}s\n"
            f"  説明: {v.desc[:180]}\n"
            f"  タグ: {tags}"
        )
    return "\n\n".join(lines)


@register
class TikTokSearchSkill(BaseSkill[TikTokSearchInput, TikTokSearchOutput]):
    """TikTok をキーワード/ハッシュタグ検索し、上位動画を取得・横断分析する Skill。"""

    name: ClassVar[str] = "tiktok_search"
    description: ClassVar[str] = (
        "TikTok単体をキーワード/ハッシュタグで検索し、上位動画のデータ(再生数/いいね/作者/"
        "ハッシュタグ等)を**今すぐ即時に**取得して Gemini で横断分析する"
        "（上位リストがすぐ欲しい時）。"
        "動画本体(mp4)のDLや分析素材の大量取得を伴う非同期ジョブは tiktok_acquire、"
        "TikTok×Instagramの検索面の勢力図・媒体比較・自社アカウントの在圏判定は "
        "search_surface_check、5本横断で冒頭フック/テロップの勝ち筋をタイムライン化するのは "
        "video_algorithm。"
    )
    input_schema: ClassVar[type[BaseModel]] = TikTokSearchInput
    output_schema: ClassVar[type[BaseModel]] = TikTokSearchOutput

    def __init__(
        self,
        gemini: GeminiClient | None = None,
        *,
        prompt_version: str = "v1",
        searcher: Searcher | None = None,
    ) -> None:
        self._gemini = gemini
        self._prompt_version = prompt_version
        # searcher 差し替え可 (テストで実ブラウザを起動しないため)
        self._searcher = searcher or search_tiktok

    def _client(self) -> GeminiClient:
        if self._gemini is None:
            self._gemini = GeminiClient.from_env()
        return self._gemini

    def run(self, input: TikTokSearchInput, ctx: SkillContext) -> TikTokSearchOutput:
        log = ctx.bind_logger(self.name)
        log.info(
            "tiktok_search_skill_start",
            search_type=input.search_type,
            max_videos=input.max_videos,
            analyze=input.analyze,
            query_len=len(input.query),
        )

        # 1. 検索 (Node subprocess)。失敗は adapter が TikTokScrapeError で上げる。
        result = self._searcher(
            input.query,
            search_type=input.search_type,
            max_videos=input.max_videos,
            request_id=ctx.request_id,
        )

        videos = [
            TikTokVideoOut(
                rank=i + 1,
                url=v.url,
                author=v.author.unique_id,
                author_followers=v.author.follower_count,
                desc=v.desc,
                play_count=v.play_count,
                digg_count=v.digg_count,
                comment_count=v.comment_count,
                share_count=v.share_count,
                collect_count=v.collect_count,
                engagement_rate=v.engagement_rate,
                duration=v.duration,
                hashtags=list(v.hashtags),
                cover_url=v.cover_url,
            )
            for i, v in enumerate(result.videos)
        ]

        out = TikTokSearchOutput(
            query=result.query,
            search_type=result.search_type,
            count=len(videos),
            videos=videos,
        )

        # 2. Gemini 横断分析 (任意)
        if input.analyze and videos:
            analysis, cost, model_id = self._analyze(input.query, videos, ctx)
            out.analysis = analysis
            out.total_cost_usd = cost
            out.model_id = model_id

        # 3. HTML レポート発行（フラグ OFF なら None＝現行どおり構造化結果だけを返す）
        # サムネは CDN の署名URLが数日で切れるため、自社S3へ再ホストしてから貼る（thumbs.py）。
        # I/O はここで済ませ、詰め替え（build_report）は純粋関数のままにする。
        thumbs = (
            rehost_many([v.cover_url for v in out.videos], request_id=ctx.request_id)
            if html_reports_enabled(self.name)
            else {}
        )
        report = build_report(out, thumbs=thumbs)
        out.report_url = publish_report(
            report,
            tool=self.name,
            request_id=ctx.request_id,
            query=input.query,
        )
        # PPTX は明示要求時のみ（media worker 同期実行で数十秒かかるため既定では作らない）。
        if "pptx" in [o.strip().lower() for o in input.outputs]:
            out.report_pptx_url = publish_pptx(report, tool=self.name, request_id=ctx.request_id)

        log.info(
            "tiktok_search_skill_done",
            count=out.count,
            cost_usd=out.total_cost_usd,
            has_report=bool(out.report_url),
        )
        return out

    def _analyze(
        self, query: str, videos: list[TikTokVideoOut], ctx: SkillContext
    ) -> tuple[str, float, str]:
        """上位動画メタを Gemini で横断分析する。(text, cost, model_id) を返す。"""
        system = load_prompt("tiktok_search", self._prompt_version, "system")
        body = _format_videos_for_prompt(videos)
        prompt = (
            f"# 検索「{query}」の上位 {len(videos)} 本（TikTok 検索結果メタデータ）\n\n"
            f"{body}\n\n"
            "上記のメタデータを横断して、システム指示のフォーマットに従って"
            "「この界隈で何が伸びているか」と「自社制作への示唆」をまとめてください。"
        )
        resp = self._client().generate_text(prompt, ctx.request_id, system=system)
        return (
            resp.text or "（分析結果が空でした。データのみ参照してください）",
            resp.cost_usd,
            resp.model_id,
        )
