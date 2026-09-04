"""search_surface_check Skill 本体（Skill層）: カタログ③ 検索面チェック。

トポロジ判断:
- TikTok面 = tiktok_acquire 成果物（S3の posts.normalized.json・rank_display=検索面表示順の
  忠実記録）を読むのが正。3KW以上はこの経路必須（MCP同期300s天井の保護）。
  1〜2KWの即席チェックだけ adapters.tiktok_scraper.search_tiktok の直スクレイプを許す。
- IG面 = Apify Actor 直（ActorはApifyインフラで走る＝クラウドIP遮断が構造的に消える）。
  sessionid+Puppeteer経路(vseo-analytics-web)は日本の住宅IP前提なので採らない。
- 勢力図分類 = Bedrock（KW×媒体ごとに1コール・失敗は unknown で縮退）。
- クライアント在圏判定 = @handle 決定的マッチ（LLMに任せない）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.apify_client import ApifyClient, ApifyError
from teamagent.adapters.cost_guard import CostGuard, CostLimitExceededError
from teamagent.media.contracts import TIKTOK_N_PER_KW_MAX
from teamagent.prompts.loader import load_prompt
from teamagent.skills._shared.rollout import ROLLOUT_DENIED_MESSAGE, rollout_allowed
from teamagent.skills._shared.text_safety import sanitize_llm_text
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.search_surface_check.report import render_surface_report
from teamagent.skills.search_surface_check.schema import (
    MAX_DIRECT_KEYWORDS,
    KwSurface,
    SearchSurfaceCheckInput,
    SearchSurfaceCheckOutput,
    SurfacePost,
)

logger = structlog.get_logger(__name__)

_ALLOWLIST_ENV = "SEARCH_SURFACE_ALLOWED_EMAILS"
_VALID_CATEGORIES = {"news", "gourmet", "ugc", "brand_official", "influencer", "other"}


def _normalize_handle(h: str) -> str:
    return h.strip().lstrip("@").lower()


def _parse_json_block(text: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


@register
class SearchSurfaceCheckSkill(BaseSkill[SearchSurfaceCheckInput, SearchSurfaceCheckOutput]):
    """③ 検索面チェック: TikTok×IGで「誰が上位に出てるか」の勢力図と在圏判定。"""

    name: ClassVar[str] = "search_surface_check"
    description: ClassVar[str] = (
        "検索KW群のTikTok/Instagramの検索面（誰のどんな投稿が上位に出るか）を取得し、"
        "面の勢力図（ニュース/グルメ/一般/公式/インフルエンサーの割合）とクライアント動画の"
        "在圏判定つきの媒体比較レポート(HTML署名URL)を作る。"
        "TikTok面は3KW以上なら必ず先に tiktok_acquire(videos_per_kw=0) を実行し、"
        "acquire_job_id を渡すこと（1〜2KWの即席チェックのみ直接取得可）。"
        "動画の中身分析は video_algorithm、X(Twitter)の声集めは x_voice_search。"
    )
    input_schema: ClassVar[type[BaseModel]] = SearchSurfaceCheckInput
    output_schema: ClassVar[type[BaseModel]] = SearchSurfaceCheckOutput

    def __init__(
        self,
        apify: ApifyClient | None = None,
        bedrock: Any | None = None,
        publisher: Any | None = None,
        tiktok_source_factory: Any | None = None,
        tiktok_search_fn: Any | None = None,
    ) -> None:
        self._apify = apify
        self._bedrock = bedrock
        self._publisher = publisher
        # callable(job_id, caller_audit_hash) -> TikTokS3Source
        self._tiktok_source_factory = tiktok_source_factory
        self._tiktok_search_fn = tiktok_search_fn  # 直スクレイプ経路（テスト注入用）

    # ---- 依存の遅延生成 -------------------------------------------------------

    def _get_apify(self) -> ApifyClient:
        if self._apify is None:
            self._apify = ApifyClient.from_env(ledger=CostGuard.from_env())
        return self._apify

    def _get_bedrock(self) -> Any:
        if self._bedrock is None:
            from teamagent.adapters.bedrock_client import BedrockClient

            self._bedrock = BedrockClient.from_env()
        return self._bedrock

    def _publish_html(self, html: str, *, request_id: str, query: str) -> str | None:
        try:
            path = os.path.join(tempfile.mkdtemp(prefix="surface_"), f"{uuid.uuid4().hex}.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            logger.warning("surface_report_write_failed", request_id=request_id)
            return None
        if self._publisher is not None:
            url = self._publisher(path, request_id=request_id, query=query)
            return str(url) if url else None
        if not os.environ.get("VSEO_REPORT_BUCKET"):
            return None
        from teamagent.adapters.report_publish import publish_html_file_result
        from teamagent.skills._shared.report_delivery import delivery_url

        result = publish_html_file_result(path, request_id=request_id, query=query)
        if result is None:
            return None
        # 配信URLの判断は全 skill 共通のチョークポイントへ（openclaw が presigned のクエリを
        # 落として壊す事象は本 skill のレポートでも同じく起きるため x_research と同じ扱いにする）。
        return delivery_url(result, request_id=request_id)

    # ---- TikTok面 --------------------------------------------------------------

    def _tiktok_from_s3(
        self,
        job_id: str,
        audit_principal_hash: str,
        keywords: list[str],
        max_per_kw: int,
    ) -> dict[str, list[SurfacePost]]:
        """acquire 成果物（rank_display順・再ソート禁止）から KW ごとの面を組む。"""
        if self._tiktok_source_factory is not None:
            source = self._tiktok_source_factory(job_id, audit_principal_hash)
        else:
            from teamagent.adapters.tiktok_s3_source import TikTokS3Source

            source = TikTokS3Source(job_id, audit_principal_hash=audit_principal_hash)
        by_kw: dict[str, list[SurfacePost]] = {kw: [] for kw in keywords}
        for p in source.posts():
            kw = str(p.get("kw", ""))
            if kw not in by_kw or len(by_kw[kw]) >= max_per_kw:
                continue
            by_kw[kw].append(
                SurfacePost(
                    platform="tiktok",
                    keyword=kw,
                    rank=int(p.get("rank_display", len(by_kw[kw]) + 1)),
                    url=str(p.get("url", "")),
                    author=str(p.get("account_id") or p.get("account_name") or ""),
                    author_followers=int(p.get("followers", 0) or 0),
                    desc=str(p.get("title", "")),
                    play_count=int(p.get("plays", 0) or 0),
                    like_count=int(p.get("likes", 0) or 0),
                    comment_count=int(p.get("comments", 0) or 0),
                )
            )
        return by_kw

    def _tiktok_direct(
        self, keywords: list[str], max_per_kw: int, request_id: str
    ) -> dict[str, list[SurfacePost]]:
        """1〜2KWの即席経路（bot プロセス内スクレイプ）。

        ``max_videos`` は dispatcher Lambda の n_per_kw 上限（``TIKTOK_N_PER_KW_MAX``）を
        超えると ``search_tiktok`` 側で fail-fast する。入力スキーマでも同じ上限を課して
        いるが、プログラムから直接 skill を組んだ場合の保険として二重に clamp する。
        """
        search = self._tiktok_search_fn
        if search is None:
            from teamagent.adapters.tiktok_scraper import search_tiktok

            search = search_tiktok
        max_videos = min(max_per_kw, TIKTOK_N_PER_KW_MAX)
        by_kw: dict[str, list[SurfacePost]] = {}
        for kw in keywords:
            result = search(kw, max_videos=max_videos, request_id=request_id)
            posts: list[SurfacePost] = []
            for i, v in enumerate(result.videos, 1):
                author = getattr(getattr(v, "author", None), "unique_id", "") or ""
                followers = int(getattr(getattr(v, "author", None), "follower_count", 0) or 0)
                posts.append(
                    SurfacePost(
                        platform="tiktok",
                        keyword=kw,
                        rank=i,  # 検索面の取得順＝表示順（再ソートしない）
                        url=str(getattr(v, "url", "")),
                        author=author,
                        author_followers=followers,
                        desc=str(getattr(v, "desc", ""))[:80],
                        play_count=int(getattr(v, "play_count", 0) or 0),
                        like_count=int(getattr(v, "digg_count", 0) or 0),
                        comment_count=int(getattr(v, "comment_count", 0) or 0),
                    )
                )
            by_kw[kw] = posts
        return by_kw

    # ---- IG面 -------------------------------------------------------------------

    def _ig_surface(
        self,
        keywords: list[str],
        *,
        surface: str,
        limit: int,
        request_id: str,
        user: str,
        warnings: list[str],
    ) -> tuple[dict[str, list[SurfacePost]], float]:
        """IG面（Apify）。shortcode dedup＋出現頻度=面の定着度で序列化。"""
        cost = 0.0
        by_kw: dict[str, list[SurfacePost]] = {}

        def _one(kw: str) -> tuple[str, list[Any], float]:
            posts, c = self._get_apify().ig_search(
                kw, limit=limit, surface=surface, request_id=request_id, user_email=user
            )
            return kw, posts, c

        with ThreadPoolExecutor(max_workers=min(4, len(keywords))) as ex:
            for fut in [ex.submit(_one, kw) for kw in keywords]:
                try:
                    kw, ig_posts, c = fut.result()
                except CostLimitExceededError:
                    raise
                except ApifyError as e:
                    warnings.append(f"IG面の取得に失敗: {str(e)[:80]}")
                    continue
                cost += c
                # dedup + 出現回数カウント（IGは同じ人気リールが繰り返し出る仕様）
                agg: dict[str, dict[str, Any]] = {}
                for p in ig_posts:
                    key = p.shortcode or p.url
                    if key in agg:
                        agg[key]["appearances"] += 1
                    else:
                        agg[key] = {"post": p, "appearances": 1}
                ranked = sorted(
                    agg.values(),
                    key=lambda a: (
                        a["appearances"],
                        a["post"].view_count or a["post"].like_count,
                    ),
                    reverse=True,
                )
                by_kw[kw] = [
                    SurfacePost(
                        platform="instagram",
                        keyword=kw,
                        rank=i,
                        appearances=a["appearances"],
                        url=a["post"].url,
                        author=a["post"].author,
                        desc=a["post"].caption[:80],
                        play_count=a["post"].view_count,
                        like_count=a["post"].like_count,
                        comment_count=a["post"].comment_count,
                        thumb_url=a["post"].thumb_url,
                    )
                    for i, a in enumerate(ranked, 1)
                ]
        return by_kw, cost

    # ---- 分類・判定 ---------------------------------------------------------------

    def _classify(
        self,
        keyword: str,
        platform: str,
        posts: list[SurfacePost],
        request_id: str,
        warnings: list[str],
    ) -> tuple[list[SurfacePost], float]:
        """Bedrock 1コールで面の投稿者タイプを分類（失敗は unknown 縮退）。"""
        if not posts:
            return posts, 0.0
        posts_json = json.dumps(
            [
                {
                    "id": str(i),
                    "account": p.author,
                    "text": p.desc,
                    "followers": p.author_followers,
                }
                for i, p in enumerate(posts)
            ],
            ensure_ascii=False,
        )
        try:
            prompt = load_prompt("search_surface_check", "v1", "classify").format(
                keyword=keyword, platform=platform, posts_json=posts_json
            )
            resp = self._get_bedrock().converse(
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                request_id=request_id,
                max_tokens=2048,
            )
            parsed = _parse_json_block(resp.text) or {}
            categories = parsed.get("categories") or {}
            out: list[SurfacePost] = []
            for i, p in enumerate(posts):
                cat = str(categories.get(str(i), "unknown"))
                out.append(
                    p.model_copy(
                        update={"category": cat if cat in _VALID_CATEGORIES else "unknown"}
                    )
                )
            return out, float(resp.usage.cost_usd)
        except Exception as e:
            warnings.append(f"{platform}『{keyword}』の勢力図分類をスキップしました")
            logger.warning(
                "surface_classify_failed",
                request_id=request_id,
                keyword=keyword,
                error=type(e).__name__,
            )
            return posts, 0.0

    def _mark_client(
        self, posts: list[SurfacePost], client_accounts: list[str]
    ) -> list[SurfacePost]:
        handles = {_normalize_handle(a) for a in client_accounts if a.strip()}
        if not handles:
            return posts
        return [
            p.model_copy(update={"is_client": _normalize_handle(p.author) in handles})
            for p in posts
        ]

    @staticmethod
    def _ratio(posts: list[SurfacePost]) -> dict[str, float]:
        if not posts:
            return {}
        counts: dict[str, int] = {}
        for p in posts:
            counts[p.category] = counts.get(p.category, 0) + 1
        return {k: round(v / len(posts), 3) for k, v in counts.items()}

    # ---- 本体 ---------------------------------------------------------------------

    def run(self, input: SearchSurfaceCheckInput, ctx: SkillContext) -> SearchSurfaceCheckOutput:
        log = ctx.bind_logger(self.name)
        user = str(ctx.metadata.get("user_email") or ctx.user_id or "")
        from teamagent.adapters.tiktok_s3_source import media_audit_principal_hash

        audit_hash = media_audit_principal_hash(user)
        if not rollout_allowed(_ALLOWLIST_ENV, user):
            return SearchSurfaceCheckOutput(
                keywords=input.keywords, slack_summary=ROLLOUT_DENIED_MESSAGE
            )
        warnings: list[str] = []
        total_cost = 0.0
        start = time.monotonic()

        want_tiktok = "tiktok" in input.platforms
        want_ig = "instagram" in input.platforms
        if (
            want_tiktok
            and input.acquire_job_id is None
            and len(input.keywords) > MAX_DIRECT_KEYWORDS
        ):
            return SearchSurfaceCheckOutput(
                keywords=input.keywords,
                slack_summary=(
                    f"{len(input.keywords)}KWのTikTok面チェックは、先に "
                    f"tiktok_acquire(keywords={input.keywords}, videos_per_kw=0) を実行し、"
                    "完了後に acquire_job_id を渡してください"
                    f"（直接取得は{MAX_DIRECT_KEYWORDS}KWまで）。"
                ),
            )

        # 取得
        tiktok_by_kw: dict[str, list[SurfacePost]] = {}
        ig_by_kw: dict[str, list[SurfacePost]] = {}
        try:
            if want_tiktok:
                try:
                    if input.acquire_job_id:
                        tiktok_by_kw = self._tiktok_from_s3(
                            input.acquire_job_id,
                            audit_hash,
                            input.keywords,
                            input.max_posts_per_kw,
                        )
                    else:
                        tiktok_by_kw = self._tiktok_direct(
                            input.keywords, input.max_posts_per_kw, ctx.request_id
                        )
                except Exception as e:
                    warnings.append(f"TikTok面の取得に失敗: {type(e).__name__}")
                    log.warning("surface_tiktok_failed", error=type(e).__name__)
            if want_ig:
                surface = input.ig_surface or os.environ.get("IG_SURFACE_DEFAULT", "search")
                ig_by_kw, cost = self._ig_surface(
                    input.keywords,
                    surface=surface,
                    limit=max(input.max_posts_per_kw, 50),
                    request_id=ctx.request_id,
                    user=user,
                    warnings=warnings,
                )
                total_cost += cost
        except CostLimitExceededError as e:
            return SearchSurfaceCheckOutput(
                keywords=input.keywords, slack_summary=str(e), warnings=[str(e)]
            )

        # 分類（KW×媒体ごとに並列）+ 在圏判定 + 勢力図
        surfaces: list[KwSurface] = []
        classify_jobs: list[tuple[str, str, list[SurfacePost]]] = []
        for kw in input.keywords:
            if want_tiktok and tiktok_by_kw.get(kw):
                classify_jobs.append((kw, "tiktok", tiktok_by_kw[kw]))
            if want_ig and ig_by_kw.get(kw):
                classify_jobs.append((kw, "instagram", ig_by_kw[kw]))

        classified: dict[tuple[str, str], list[SurfacePost]] = {}
        if input.analyze and classify_jobs:
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = {
                    ex.submit(self._classify, kw, platform, posts, ctx.request_id, warnings): (
                        kw,
                        platform,
                    )
                    for kw, platform, posts in classify_jobs
                }
                for fut, key in futs.items():
                    posts, cost = fut.result()
                    classified[key] = posts
                    total_cost += cost
        else:
            classified = {(kw, pf): posts for kw, pf, posts in classify_jobs}

        for (kw, platform), posts in classified.items():
            posts = self._mark_client(posts, input.client_accounts)
            surfaces.append(
                KwSurface(
                    keyword=kw,
                    platform=platform,
                    posts=posts,
                    category_ratio=self._ratio(posts) if input.analyze else {},
                    client_ranks=[p.rank for p in posts if p.is_client],
                )
            )
        surfaces.sort(key=lambda s: (input.keywords.index(s.keyword), s.platform))

        if not surfaces:
            return SearchSurfaceCheckOutput(
                keywords=input.keywords,
                warnings=warnings,
                total_cost_usd=round(total_cost, 4),
                slack_summary="検索面のデータを取得できませんでした。",
            )

        # 媒体比較サマリ（Bedrock 1コール・失敗は空で縮退）
        comparison = ""
        if input.analyze:
            try:
                digest = {f"{s.keyword}/{s.platform}": s.category_ratio for s in surfaces}
                resp = self._get_bedrock().converse(
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "text": (
                                        "以下はTikTok/Instagramの検索面のカテゴリ構成比です。"
                                        "媒体ごとの面の性格の違いを、提案論点になる形で"
                                        "2-4文のプレーンテキストで要約してください"
                                        "（Markdown記法禁止・データにない断定禁止）。\n"
                                        + json.dumps(digest, ensure_ascii=False)
                                    )
                                }
                            ],
                        }
                    ],
                    request_id=ctx.request_id,
                    max_tokens=512,
                )
                comparison = resp.text.strip()
                total_cost += resp.usage.cost_usd
            except Exception:
                log.warning("surface_comparison_failed")

        html = render_surface_report(
            keywords=input.keywords,
            surfaces=surfaces,
            comparison_summary=comparison,
            client_name=input.client_name,
        )
        report_url = self._publish_html(
            html, request_id=ctx.request_id, query="・".join(input.keywords)
        )
        out = SearchSurfaceCheckOutput(
            keywords=input.keywords,
            surfaces=surfaces,
            comparison_summary=comparison,
            report_url=report_url,
            total_cost_usd=round(total_cost, 4),
            warnings=warnings,
        )
        out.slack_summary = self._slack_summary(out, input)
        log.info(
            "search_surface_check_done",
            kw=len(input.keywords),
            surfaces=len(surfaces),
            cost_usd=out.total_cost_usd,
            latency_s=round(time.monotonic() - start, 1),
        )
        return out

    def _slack_summary(self, out: SearchSurfaceCheckOutput, input: SearchSurfaceCheckInput) -> str:
        lines = [f"🗺️ *検索面チェック* 完了（{'・'.join(out.keywords)}）"]
        if out.comparison_summary:
            lines.append(sanitize_llm_text(out.comparison_summary))
        client_hits = [s for s in out.surfaces if s.client_ranks]
        if input.client_accounts:
            if client_hits:
                hits = "、".join(
                    f"{s.keyword}/{s.platform}({len(s.client_ranks)}件)" for s in client_hits
                )
                lines.append(f"⭐ クライアント在圏: {hits}")
            else:
                lines.append("⭐ クライアント投稿はどの面にも出ていません")
        if out.report_url:
            lines.append(f"📄 媒体比較レポート（7日有効）: {out.report_url}")
        if out.warnings:
            lines.append("⚠️ " + " / ".join(out.warnings))
        lines.append(f"_概算 ${out.total_cost_usd:.4f}_")
        return "\n".join(lines)
