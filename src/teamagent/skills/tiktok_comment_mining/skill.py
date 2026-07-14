"""tiktok_comment_mining Skill 本体（Skill層）: カタログ⑤ コメント欄マイニング。

取得は既存資産を再利用（新規スクレイパは作らない）:
  一次 = adapters.tiktok_scraper.get_tiktok_comments（chromium・コメントAPI傍受・実装済み）
  縮退 = apify_client.tiktok_comments（clockworks/tiktok-comments-scraper・$0.001/件）
  ※ tiktok検索が deploy_log 2026-07-06 で確立した chromium→Apify 縮退と同型。
分析は Bedrock（移植元 tiktok-research-agent/analyzer/comment_analyzer.py の分類軸を
カタログ⑤の反応分類[推薦/売切れ嘆き/口コミ検証/…]で拡張。Gemini直呼びは使わない）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.apify_client import ApifyClient, ApifyError
from teamagent.adapters.cost_guard import CostGuard, CostLimitExceededError
from teamagent.prompts.loader import load_prompt
from teamagent.skills._shared.rollout import ROLLOUT_DENIED_MESSAGE, rollout_allowed
from teamagent.skills._shared.text_safety import sanitize_llm_text
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.tiktok_comment_mining.report import render_comment_report
from teamagent.skills.tiktok_comment_mining.schema import (
    CommentBucket,
    CommentMiningInput,
    CommentMiningOutput,
    VideoCommentInsight,
)

logger = structlog.get_logger(__name__)

_ALLOWLIST_ENV = "COMMENT_MINING_ALLOWED_EMAILS"
_APIFY_UNIT_USD = 0.001  # clockworks の概算単価/コメント


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


def _str_list(v: Any, limit: int = 20) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x) for x in v if str(x).strip()][:limit]


@register
class TikTokCommentMiningSkill(BaseSkill[CommentMiningInput, CommentMiningOutput]):
    """⑤ コメント欄マイニング: バズ動画のコメントを分類し生活者の語彙を抽出する。"""

    name: ClassVar[str] = "tiktok_comment_mining"
    description: ClassVar[str] = (
        "TikTok動画URL(1〜3本)のコメント欄を取得し、反応の分類（推薦/売切れ嘆き/口コミ検証/"
        "ツッコミ/質問/願望/批判）と生活者の語彙（広告文言の元ネタ）をレポート(HTML署名URL)に"
        "する。バズ動画のコメント欄=無料のグループインタビュー。新商品ローンチ後の初速の"
        "生活者反応レポートに使う。動画の中身(映像)分析は video_algorithm、"
        "KW検索面の勢力図は search_surface_check。"
    )
    input_schema: ClassVar[type[BaseModel]] = CommentMiningInput
    output_schema: ClassVar[type[BaseModel]] = CommentMiningOutput

    def __init__(
        self,
        apify: ApifyClient | None = None,
        bedrock: Any | None = None,
        publisher: Any | None = None,
        comments_fn: Any | None = None,
        persister: Any | None = None,
    ) -> None:
        self._apify = apify
        self._bedrock = bedrock
        self._publisher = publisher
        self._comments_fn = comments_fn  # chromium 一次経路（テスト注入用）
        self._persister = persister  # ResearchPersister（Part1・None なら永続化 no-op）

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
            path = os.path.join(tempfile.mkdtemp(prefix="comments_"), f"{uuid.uuid4().hex}.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            logger.warning("comment_report_write_failed", request_id=request_id)
            return None
        if self._publisher is not None:
            url = self._publisher(path, request_id=request_id, query=query)
            return str(url) if url else None
        if not os.environ.get("VSEO_REPORT_BUCKET"):
            return None
        from teamagent.adapters.report_publish import publish_html_file

        return publish_html_file(path, request_id=request_id, query=query)

    # ---- 取得（chromium一次 → Apify縮退） ---------------------------------------

    def _fetch_comments(
        self,
        video_url: str,
        max_comments: int,
        *,
        request_id: str,
        user: str,
        warnings: list[str],
    ) -> tuple[list[dict[str, Any]], str, float]:
        """コメント取得。戻り値: (comments[{text,likes,author}], source, cost)。"""
        fetch = self._comments_fn
        if fetch is None:
            from teamagent.adapters.tiktok_scraper import get_tiktok_comments

            fetch = get_tiktok_comments
        try:
            result = fetch(video_url, max_comments=max_comments, request_id=request_id)
            comments = [
                {"text": c.text, "likes": c.likes, "author": c.author} for c in result.comments
            ]
            if comments:
                return comments, "chromium", 0.0
        except Exception as e:
            logger.info(
                "comment_chromium_fallback",
                request_id=request_id,
                error=type(e).__name__,
            )
        # 縮退: Apify clockworks（クラウドIP遮断/0件時）
        try:
            comments, cost = self._get_apify().tiktok_comments(
                video_url, max_comments=max_comments, request_id=request_id, user_email=user
            )
            return comments, "apify", cost
        except CostLimitExceededError:
            raise
        except ApifyError as e:
            warnings.append(f"コメント取得に失敗: {str(e)[:80]}")
            return [], "none", 0.0

    # ---- 分析（Bedrock） ---------------------------------------------------------

    def _analyze(
        self,
        video_url: str,
        comments: list[dict[str, Any]],
        client_name: str | None,
        *,
        request_id: str,
        warnings: list[str],
    ) -> tuple[VideoCommentInsight, float]:
        insight = VideoCommentInsight(video_url=video_url, total_comments=len(comments))
        if not comments:
            return insight, 0.0
        indexed_comments = [(f"c{i}", c) for i, c in enumerate(comments) if str(c["text"]).strip()]
        comments_by_id = {cid: c for cid, c in indexed_comments}
        comments_text = "\n".join(
            f"[{cid}] {c['text']} (いいね: {c['likes']})" for cid, c in indexed_comments
        )
        try:
            prompt = load_prompt("tiktok_comment_mining", "v1", "classify").format(
                video_url=video_url,
                client_context=client_name or "指定なし",
                comments_text=comments_text[:24000],
            )
            resp = self._get_bedrock().converse(
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                request_id=request_id,
                max_tokens=3072,
            )
            parsed = _parse_json_block(resp.text)
            if parsed is None:
                warnings.append("コメント分類の解析に失敗しました（件数のみ）")
                return insight, float(resp.usage.cost_usd)
            buckets: list[CommentBucket] = []
            for b in parsed.get("buckets") or []:
                if not isinstance(b, dict):
                    continue
                # LLMには原文を書き直させずIDだけ選ばせる。存在するIDをコード側で
                # 原コメントへ戻すため、創作・改変された「代表コメント」は混入できない。
                example_ids = _str_list(b.get("example_ids"), limit=3)
                selected = [comments_by_id[cid] for cid in example_ids if cid in comments_by_id]
                selected.sort(key=lambda c: int(c.get("likes", 0) or 0), reverse=True)
                buckets.append(
                    CommentBucket(
                        category=str(b.get("category") or "その他"),
                        count=max(0, int(b.get("count", 0) or 0)),
                        examples=[str(c["text"]) for c in selected],
                    )
                )
            insight = VideoCommentInsight(
                video_url=video_url,
                total_comments=len(comments),
                buckets=buckets,
                consumer_vocabulary=_str_list(parsed.get("consumer_vocabulary")),
                common_questions=_str_list(parsed.get("common_questions")),
                pain_points=_str_list(parsed.get("pain_points")),
                desires=_str_list(parsed.get("desires")),
                purchase_signals=_str_list(parsed.get("purchase_signals")),
                overall_sentiment=str(parsed.get("overall_sentiment") or ""),
                key_themes=_str_list(parsed.get("key_themes"), limit=5),
            )
            return insight, float(resp.usage.cost_usd)
        except Exception as e:
            warnings.append("コメント分類をスキップしました（取得のみ）")
            logger.warning("comment_classify_failed", request_id=request_id, error=type(e).__name__)
            return insight, 0.0

    # ---- 本体 ---------------------------------------------------------------------

    def run(self, input: CommentMiningInput, ctx: SkillContext) -> CommentMiningOutput:
        log = ctx.bind_logger(self.name)
        user = str(ctx.metadata.get("user_email") or ctx.user_id or "")
        if not rollout_allowed(_ALLOWLIST_ENV, user):
            return CommentMiningOutput(slack_summary=ROLLOUT_DENIED_MESSAGE)
        warnings: list[str] = []
        total_cost = 0.0
        insights: list[VideoCommentInsight] = []
        scraped = 0

        # SSRF/allowlist 検証を両経路の手前で1か所実施する。chromium 経路の url_guard 例外を
        # _fetch_comments の except Exception が「縮退」と誤解釈し、拒否されたURLを Apify に
        # そのまま渡す穴を塞ぐ（self-review 指摘）。不正URLは即エラーで返す。
        from teamagent.adapters.url_guard import UrlGuardError, validate_scrape_url

        valid_urls: list[str] = []
        for url in input.video_urls:
            try:
                valid_urls.append(validate_scrape_url(url, request_id=ctx.request_id))
            except UrlGuardError:
                return CommentMiningOutput(
                    slack_summary=(
                        f"不正なURLが含まれています（TikTok動画URLを指定してください）: {url[:60]}"
                    ),
                    warnings=[f"URL検証に失敗: {url[:60]}"],
                )

        try:
            for url in valid_urls:
                comments, source, cost = self._fetch_comments(
                    url,
                    input.max_comments_per_video,
                    request_id=ctx.request_id,
                    user=user,
                    warnings=warnings,
                )
                total_cost += cost
                scraped += len(comments)
                if input.classify:
                    insight, llm_cost = self._analyze(
                        url,
                        comments,
                        input.client_name,
                        request_id=ctx.request_id,
                        warnings=warnings,
                    )
                    total_cost += llm_cost
                else:
                    insight = VideoCommentInsight(video_url=url, total_comments=len(comments))
                insight.source = source
                insights.append(insight)
        except CostLimitExceededError as e:
            return CommentMiningOutput(slack_summary=str(e), warnings=[str(e)])

        if scraped == 0:
            return CommentMiningOutput(
                videos=insights,
                warnings=warnings,
                total_cost_usd=round(total_cost, 4),
                slack_summary=(
                    "コメントを取得できませんでした（コメントが少ない動画では0件になる"
                    "場合があります）。"
                ),
            )

        # 複数動画横断の語彙（出現順を保ちつつ重複除去）
        seen: set[str] = set()
        cross_vocab: list[str] = []
        for ins in insights:
            for w in ins.consumer_vocabulary:
                if w not in seen:
                    seen.add(w)
                    cross_vocab.append(w)

        html = render_comment_report(
            videos=insights, cross_vocabulary=cross_vocab, client_name=input.client_name
        )
        report_url = self._publish_html(
            html, request_id=ctx.request_id, query=input.client_name or "comments"
        )
        out = CommentMiningOutput(
            videos=insights,
            cross_vocabulary=cross_vocab[:20],
            report_url=report_url,
            scraped_comments=scraped,
            total_cost_usd=round(total_cost, 4),
            warnings=warnings,
        )
        out.slack_summary = self._slack_summary(out)
        log.info(
            "tiktok_comment_mining_done",
            videos=len(insights),
            comments=scraped,
            cost_usd=out.total_cost_usd,
        )
        # Part1: コメント分析を永続記録（client_name を商材扱い・空なら persister 側で no-op）。
        if self._persister is not None and (input.client_name or "").strip():
            from teamagent.skills.x_research.persist_body import build_comment_summary_md

            self._persister.schedule(
                tool="tiktok_comment",
                product_name=input.client_name or "",
                title=f"{input.client_name} コメント欄マイニング",
                body_md=build_comment_summary_md(out, client_name=input.client_name or ""),
                owner_email=user,
                request_id=ctx.request_id,
                cls_solution="SNSコメント分析",
                cls_doc_type="コメント分析",
            )
        return out

    def _slack_summary(self, out: CommentMiningOutput) -> str:
        lines = [
            f"💬 *コメント欄マイニング* 完了"
            f"（{len(out.videos)}動画・{out.scraped_comments}コメント）"
        ]
        for ins in out.videos:
            if not ins.buckets:
                continue
            top = sorted(ins.buckets, key=lambda b: b.count, reverse=True)[:3]
            # category はLLM生成なので sanitize（他スキルと整合）。examples は原文コメント＝
            # 原文保証のため sanitize しない（x_voice の投稿本文と同じ扱い）。
            dist = "・".join(f"{sanitize_llm_text(b.category, max_len=20)}{b.count}" for b in top)
            lines.append(f"{ins.video_url}\n分布: {dist}（全{ins.total_comments}件）")
            ex = next((b.examples[0] for b in top if b.examples), None)
            if ex:
                lines.append(f"代表: {ex}")
        if out.cross_vocabulary:
            vocab = [sanitize_llm_text(w, max_len=30) for w in out.cross_vocabulary[:8]]
            lines.append("🗣️ 生活者の語彙: " + "、".join(vocab))
        if out.report_url:
            lines.append(f"📄 分類レポート（7日有効）: {out.report_url}")
        if out.warnings:
            lines.append("⚠️ " + " / ".join(out.warnings))
        lines.append(f"_概算 ${out.total_cost_usd:.4f}_")
        return "\n".join(lines)
