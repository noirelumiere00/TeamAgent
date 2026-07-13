"""X(Twitter)リサーチ Skill 群（Skill層）: ①x_voice_search ②x_needs_mining ④x_buzz_measure。

設計:
- ①②は同期（Apify actor はApifyインフラで実行・MCPからはREST）。内部デッドライン
  X_SYNC_DEADLINE_S(既定240s) で MCP 300s 天井の内側に収め、超過時は縮退
  （未検証分を「要再確認」で返す＝タイムアウト全損を構造的に排除）。
- ④は非同期（A′トポロジ）。submit は SQS 投函のみ即return、実取得は使い捨てFargateの
  軽量ワーカー（teamagent.workers.x_buzz_job）。status が done 初回にSonnet山分析+HTMLを
  生成し DynamoDB にキャッシュする。
- 納品する投稿は必ず xtracto 実在検証を通す（検証不能は「要再確認」明記・黙って捨てない）。
- コストは apify_client(CostGuard) が check/record。予算超過は実行前に止まる（fail-close）。
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

from teamagent.adapters.apify_client import ApifyClient, ApifyError, XPost
from teamagent.adapters.cost_guard import CostGuard, CostLimitExceeded
from teamagent.adapters.x_task_store import XTaskStore, new_job_id
from teamagent.prompts.loader import load_prompt
from teamagent.skills._shared.rollout import ROLLOUT_DENIED_MESSAGE, rollout_allowed
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.x_research.report import (
    render_buzz_report,
    render_needs_report,
    render_voice_cards,
)
from teamagent.skills.x_research.schema import (
    NeedCluster,
    XBuzzMeasureInput,
    XBuzzMeasureOutput,
    XBuzzMeasureStatusInput,
    XBuzzMeasureStatusOutput,
    XNeedsMiningInput,
    XNeedsMiningOutput,
    XPostCard,
    XVoiceSearchInput,
    XVoiceSearchOutput,
)

logger = structlog.get_logger(__name__)

_ALLOWLIST_ENV = "X_RESEARCH_ALLOWED_EMAILS"  # 段階公開（空=全員許可）
_MAX_PARALLEL_QUERIES = 4


def _deadline_s() -> int:
    try:
        return max(60, int(os.environ.get("X_SYNC_DEADLINE_S", "240")))
    except ValueError:
        return 240


def _user_of(ctx: SkillContext) -> str:
    return str(ctx.metadata.get("user_email") or ctx.user_id or "")


def _parse_json_block(text: str) -> dict[str, Any] | None:
    """LLM出力から最初のJSONオブジェクトを寛容に取り出す（フェンス除去）。"""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _posts_for_prompt(posts: list[XPost], limit_chars: int = 280) -> str:
    return json.dumps(
        [
            {
                "post_id": p.post_id,
                "author": p.author_handle,
                "text": p.text[:limit_chars],
                "likes": p.like_count,
            }
            for p in posts
        ],
        ensure_ascii=False,
    )


def _dedup(posts: list[XPost]) -> list[XPost]:
    seen: set[str] = set()
    out: list[XPost] = []
    for p in posts:
        key = p.post_id or p.url
        if key and key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _analysis_bedrock() -> Any:
    """分析用 Bedrock クライアント。X_ANALYSIS_MODEL_ID があれば明示注入（暗黙昇格禁止）。"""
    from teamagent.adapters.bedrock_client import BedrockClient

    model_id = os.environ.get("X_ANALYSIS_MODEL_ID", "").strip()
    if not model_id:
        return BedrockClient.from_env()
    return BedrockClient(
        region=os.environ.get("AWS_REGION", "ap-northeast-1"), model_id=model_id
    )


class _XSyncBase:
    """①②共通の部品（Apify/Bedrock/publisher の遅延生成と注入可能化）。"""

    def __init__(
        self,
        apify: ApifyClient | None = None,
        bedrock: Any | None = None,
        analysis_bedrock: Any | None = None,
        publisher: Any | None = None,
    ) -> None:
        self._apify = apify
        self._bedrock = bedrock  # ノイズ除去（既定 Haiku）
        self._analysis = analysis_bedrock  # 分類/山分析（X_ANALYSIS_MODEL_ID）
        self._publisher = publisher  # callable(path, request_id, query) -> url | None

    def _get_apify(self) -> ApifyClient:
        if self._apify is None:
            self._apify = ApifyClient.from_env(ledger=CostGuard.from_env())
        return self._apify

    def _get_bedrock(self) -> Any:
        if self._bedrock is None:
            from teamagent.adapters.bedrock_client import BedrockClient

            self._bedrock = BedrockClient.from_env()
        return self._bedrock

    def _get_analysis(self) -> Any:
        if self._analysis is None:
            self._analysis = _analysis_bedrock()
        return self._analysis

    def _publish_html(self, html: str, *, request_id: str, query: str) -> str | None:
        try:
            path = os.path.join(tempfile.mkdtemp(prefix="x_research_"), f"{uuid.uuid4().hex}.html")
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            logger.warning("x_research_report_write_failed", request_id=request_id)
            return None
        if self._publisher is not None:
            url = self._publisher(path, request_id=request_id, query=query)
            return str(url) if url else None
        if not os.environ.get("VSEO_REPORT_BUCKET"):
            return None
        from teamagent.adapters.report_publish import publish_html_file

        return publish_html_file(path, request_id=request_id, query=query)

    def _search_parallel(
        self,
        queries: list[str],
        *,
        count: int,
        search_type: str,
        remaining_s: int,
        request_id: str,
        user: str,
        warnings: list[str],
    ) -> tuple[list[XPost], float]:
        """クエリ群を並列検索（クエリ単位の失敗は警告に落として続行）。"""
        cost = 0.0
        results: list[XPost] = []

        def _one(q: str) -> tuple[list[XPost], float]:
            return self._get_apify().search_posts(
                q,
                count=count,
                search_type=search_type,
                deadline_s=remaining_s,
                request_id=request_id,
                user_email=user,
            )

        with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_QUERIES, len(queries))) as ex:
            futures = {ex.submit(_one, q): q for q in queries}
            for fut, q in futures.items():
                try:
                    posts, c = fut.result()
                    results.extend(posts)
                    cost += c
                except CostLimitExceeded:
                    raise
                except ApifyError as e:
                    warnings.append(f"クエリ『{q}』の検索に失敗: {str(e)[:80]}")
        return results, cost

    def _verify_selected(
        self,
        selected: list[XPost],
        *,
        author_notes: dict[str, str],
        remaining_s: int,
        request_id: str,
        user: str,
        warnings: list[str],
    ) -> tuple[list[XPostCard], float]:
        """厳選分だけ xtracto 実在検証し、カード化する（縮退時は全件 要再確認）。"""
        cost = 0.0
        verified_map: dict[str, XPost | None] = {}
        if remaining_s < 20:
            warnings.append("時間切れのため実在検証を省略しました（全件 要再確認）")
        else:
            try:
                verified_map, cost = self._get_apify().verify_posts(
                    [p.url for p in selected],
                    deadline_s=remaining_s,
                    request_id=request_id,
                    user_email=user,
                )
            except CostLimitExceeded:
                raise
            except ApifyError as e:
                warnings.append(f"実在検証に失敗（全件 要再確認扱い）: {str(e)[:80]}")
        cards: list[XPostCard] = []
        for p in selected:
            v = verified_map.get(p.url)
            cards.append(
                XPostCard(
                    post_id=p.post_id,
                    url=p.url,
                    author_handle=p.author_handle,
                    author_note=author_notes.get(p.post_id, ""),
                    text=(v.text if v is not None and v.text else p.text),
                    like_count=(v.like_count if v is not None and v.like_count else p.like_count),
                    retweet_count=p.retweet_count,
                    created_at=p.created_at,
                    verified=v is not None,
                    verify_note="" if v is not None else "要再確認: 再取得できませんでした",
                )
            )
        return cards, cost


@register
class XVoiceSearchSkill(_XSyncBase, BaseSkill[XVoiceSearchInput, XVoiceSearchOutput]):
    """① 世の中の声集め: 商材のX実在投稿を全文+いいね+URL付きカード集で返す。"""

    name: ClassVar[str] = "x_voice_search"
    description: ClassVar[str] = (
        "商材/ブランドがX(Twitter)で「世の中でどう言われてるか」を、実在する投稿の"
        "全文+いいね数+URL付きカード集(HTML署名URL)で集める。投稿ID単位で実在検証済み。"
        "提案書の『世の中の声』ページやオリエン直後の初動リサーチ用。"
        "感情ワードでの不満/欲求の発掘は x_needs_mining、期間指定の発話量測定は x_buzz_measure。"
    )
    input_schema: ClassVar[type[BaseModel]] = XVoiceSearchInput
    output_schema: ClassVar[type[BaseModel]] = XVoiceSearchOutput

    def run(self, input: XVoiceSearchInput, ctx: SkillContext) -> XVoiceSearchOutput:
        log = ctx.bind_logger(self.name)
        user = _user_of(ctx)
        if not rollout_allowed(_ALLOWLIST_ENV, user):
            return XVoiceSearchOutput(
                product_name=input.product_name, slack_summary=ROLLOUT_DENIED_MESSAGE
            )
        start = time.monotonic()
        warnings: list[str] = []
        total_cost = 0.0

        def remaining() -> int:
            return max(10, int(_deadline_s() - (time.monotonic() - start)))

        try:
            raw, cost = self._search_parallel(
                input.queries,
                count=input.results_per_query,
                search_type=input.search_type,
                remaining_s=remaining(),
                request_id=ctx.request_id,
                user=user,
                warnings=warnings,
            )
            total_cost += cost
            posts = _dedup(raw)
            if not posts:
                return XVoiceSearchOutput(
                    product_name=input.product_name,
                    warnings=warnings,
                    total_cost_usd=round(total_cost, 4),
                    slack_summary=(
                        f"「{input.product_name}」のX投稿は0件でした"
                        "（クエリを変えて再実行してみてください）。"
                    ),
                )

            # ノイズ除去+属性メモ（Haiku 1回・失敗は全残し=fail-open）
            keep_ids = {p.post_id for p in posts}
            author_notes: dict[str, str] = {}
            noise_note = ""
            try:
                prompt = load_prompt("x_research", "v1", "noise_filter").format(
                    product_name=input.product_name, posts_json=_posts_for_prompt(posts)
                )
                resp = self._get_bedrock().converse(
                    messages=[{"role": "user", "content": [{"text": prompt}]}],
                    request_id=ctx.request_id,
                    max_tokens=2048,
                )
                total_cost += resp.usage.cost_usd
                parsed = _parse_json_block(resp.text)
                if parsed and isinstance(parsed.get("keep"), list):
                    llm_keep = {str(i) for i in parsed["keep"]}
                    if llm_keep & keep_ids:
                        keep_ids = llm_keep & keep_ids
                    notes = parsed.get("author_notes")
                    if isinstance(notes, dict):
                        author_notes = {str(k): str(v) for k, v in notes.items()}
                    noise_note = str(parsed.get("noise_note") or "")
            except Exception as e:
                warnings.append("ノイズ除去をスキップしました（全件を候補に含めます）")
                log.warning("x_voice_noise_filter_failed", error=type(e).__name__)

            kept = [p for p in posts if p.post_id in keep_ids]
            selected = sorted(kept, key=lambda p: p.like_count, reverse=True)[: input.max_selected]
            cards, cost = self._verify_selected(
                selected,
                author_notes=author_notes,
                remaining_s=remaining(),
                request_id=ctx.request_id,
                user=user,
                warnings=warnings,
            )
            total_cost += cost
        except CostLimitExceeded as e:
            return XVoiceSearchOutput(
                product_name=input.product_name, slack_summary=str(e), warnings=[str(e)]
            )
        except ApifyError as e:
            return XVoiceSearchOutput(
                product_name=input.product_name,
                slack_summary=f"X検索に失敗しました: {e}",
                warnings=[str(e)],
            )

        html = render_voice_cards(
            product_name=input.product_name,
            posts=cards,
            noise_note=noise_note,
            searched=len(posts),
        )
        report_url = self._publish_html(
            html, request_id=ctx.request_id, query=input.product_name
        )
        verified_count = sum(1 for c in cards if c.verified)
        out = XVoiceSearchOutput(
            product_name=input.product_name,
            posts=cards,
            searched=len(posts),
            selected=len(cards),
            verified_count=verified_count,
            unverified_count=len(cards) - verified_count,
            noise_note=noise_note,
            report_url=report_url,
            total_cost_usd=round(total_cost, 4),
            warnings=warnings,
        )
        out.slack_summary = self._slack_summary(out)
        log.info(
            "x_voice_search_done",
            searched=out.searched,
            selected=out.selected,
            verified=out.verified_count,
            cost_usd=out.total_cost_usd,
        )
        return out

    def _slack_summary(self, out: XVoiceSearchOutput) -> str:
        lines = [
            f"🗣️ *Xの声集め* 完了「{out.product_name}」"
            f"（取得{out.searched}件 → 厳選{out.selected}件・実在検証済み{out.verified_count}件）"
        ]
        for i, p in enumerate(out.posts[:3], 1):
            mark = "" if p.verified else "（⚠️要再確認）"
            lines.append(f"{i}. @{p.author_handle} ❤️{p.like_count:,}{mark}\n{p.text}")
        if out.noise_note:
            lines.append(f"🔎 {out.noise_note}")
        if out.report_url:
            lines.append(f"📄 カード集・全{out.selected}件（7日有効）: {out.report_url}")
        if out.warnings:
            lines.append("⚠️ " + " / ".join(out.warnings))
        lines.append(f"_概算 ${out.total_cost_usd:.4f}_")
        return "\n".join(lines)


@register
class XNeedsMiningSkill(_XSyncBase, BaseSkill[XNeedsMiningInput, XNeedsMiningOutput]):
    """② ニーズ発掘: 感情ワード掛け合わせでXから生活者の不満・欲求を発掘する。"""

    name: ClassVar[str] = "x_needs_mining"
    description: ClassVar[str] = (
        "業界/テーマの提案の種さがし。「◯◯ めんどくさい/売ってほしい/高い」等の感情ワード"
        "掛け合わせでX(Twitter)から生活者の不満・欲求の本音投稿を発掘し、ニーズ分類と"
        "インサイト仮説付きのレポート(HTML署名URL)にする。いいね数下限でノイズ足切り。"
        "商材名の evaluated な言及集めは x_voice_search、発話量の時系列測定は x_buzz_measure。"
    )
    input_schema: ClassVar[type[BaseModel]] = XNeedsMiningInput
    output_schema: ClassVar[type[BaseModel]] = XNeedsMiningOutput

    def run(self, input: XNeedsMiningInput, ctx: SkillContext) -> XNeedsMiningOutput:
        log = ctx.bind_logger(self.name)
        user = _user_of(ctx)
        if not rollout_allowed(_ALLOWLIST_ENV, user):
            return XNeedsMiningOutput(theme=input.theme, slack_summary=ROLLOUT_DENIED_MESSAGE)
        start = time.monotonic()
        warnings: list[str] = []
        total_cost = 0.0

        def remaining() -> int:
            return max(10, int(_deadline_s() - (time.monotonic() - start)))

        queries = [f"{input.theme} {w}" for w in input.emotion_words]
        try:
            raw, cost = self._search_parallel(
                queries,
                count=input.results_per_query,
                search_type="top",
                remaining_s=remaining(),
                request_id=ctx.request_id,
                user=user,
                warnings=warnings,
            )
            total_cost += cost
            posts = [p for p in _dedup(raw) if p.like_count >= input.min_faves]
            if not posts:
                return XNeedsMiningOutput(
                    theme=input.theme,
                    warnings=warnings,
                    total_cost_usd=round(total_cost, 4),
                    slack_summary=(
                        f"「{input.theme}」×感情ワードで いいね{input.min_faves}以上の投稿は"
                        "0件でした（min_faves を下げるかワードを変えてみてください）。"
                    ),
                )

            # 分類+インサイト仮説（Sonnet 1回・失敗は分類なしで投稿だけ納品=縮退）
            clusters: list[NeedCluster] = []
            hypothesis = ""
            try:
                prompt = load_prompt("x_research", "v1", "needs").format(
                    theme=input.theme, posts_json=_posts_for_prompt(posts)
                )
                resp = self._get_analysis().converse(
                    messages=[{"role": "user", "content": [{"text": prompt}]}],
                    request_id=ctx.request_id,
                    max_tokens=3072,
                )
                total_cost += resp.usage.cost_usd
                parsed = _parse_json_block(resp.text)
                if parsed:
                    ids_available = {p.post_id for p in posts}
                    for c in parsed.get("clusters") or []:
                        if not isinstance(c, dict):
                            continue
                        pids = [
                            str(i) for i in (c.get("post_ids") or []) if str(i) in ids_available
                        ]
                        clusters.append(
                            NeedCluster(
                                label=str(c.get("label") or "分類"),
                                insight=str(c.get("insight") or ""),
                                post_ids=pids,
                            )
                        )
                    hypothesis = str(parsed.get("hypothesis_summary") or "")
            except Exception as e:
                warnings.append("ニーズ分類をスキップしました（投稿のみ納品）")
                log.warning("x_needs_classify_failed", error=type(e).__name__)

            clustered_ids = {pid for c in clusters for pid in c.post_ids}
            pool = [p for p in posts if p.post_id in clustered_ids] or posts
            selected = sorted(pool, key=lambda p: p.like_count, reverse=True)[: input.max_selected]
            cards, cost = self._verify_selected(
                selected,
                author_notes={},
                remaining_s=remaining(),
                request_id=ctx.request_id,
                user=user,
                warnings=warnings,
            )
            total_cost += cost
            selected_ids = {c.post_id for c in cards}
            clusters = [
                NeedCluster(
                    label=c.label,
                    insight=c.insight,
                    post_ids=[pid for pid in c.post_ids if pid in selected_ids],
                )
                for c in clusters
            ]
        except CostLimitExceeded as e:
            return XNeedsMiningOutput(theme=input.theme, slack_summary=str(e), warnings=[str(e)])
        except ApifyError as e:
            return XNeedsMiningOutput(
                theme=input.theme,
                slack_summary=f"X検索に失敗しました: {e}",
                warnings=[str(e)],
            )

        html = render_needs_report(
            theme=input.theme,
            clusters=clusters,
            posts=cards,
            hypothesis_summary=hypothesis,
            searched=len(posts),
        )
        report_url = self._publish_html(html, request_id=ctx.request_id, query=input.theme)
        out = XNeedsMiningOutput(
            theme=input.theme,
            posts=cards,
            clusters=clusters,
            hypothesis_summary=hypothesis,
            report_url=report_url,
            total_cost_usd=round(total_cost, 4),
            warnings=warnings,
        )
        out.slack_summary = self._slack_summary(out)
        log.info(
            "x_needs_mining_done",
            selected=len(cards),
            clusters=len(clusters),
            cost_usd=out.total_cost_usd,
        )
        return out

    def _slack_summary(self, out: XNeedsMiningOutput) -> str:
        lines = [f"💡 *ニーズ発掘* 完了「{out.theme}」（厳選{len(out.posts)}件・{len(out.clusters)}分類）"]
        if out.hypothesis_summary:
            lines.append(out.hypothesis_summary)
        top = max(out.posts, key=lambda p: p.like_count, default=None)
        if top is not None:
            lines.append(f"最大共感: @{top.author_handle} ❤️{top.like_count:,}\n{top.text}")
        if out.report_url:
            lines.append(f"📄 分類レポート（7日有効）: {out.report_url}")
        if out.warnings:
            lines.append("⚠️ " + " / ".join(out.warnings))
        lines.append(f"_概算 ${out.total_cost_usd:.4f}_")
        return "\n".join(lines)


@register
class XBuzzMeasureSkill(BaseSkill[XBuzzMeasureInput, XBuzzMeasureOutput]):
    """④ 効果測定: 期間指定のX発話量取得ジョブを投函する（非同期・即return）。"""

    name: ClassVar[str] = "x_buzz_measure"
    description: ClassVar[str] = (
        "キャンペーン前後でX(Twitter)の発話量がどう変わったかを測る非同期ジョブを開始する。"
        "期間(最大62日)を1日ずつ分割取得し、日別発話数グラフ+山の日の中身分析+バズ投稿TOP"
        "全文のレポートを作る。即座にjob_idを返すので、数分後に x_buzz_measure_status を呼ぶ。"
        "今すぐ見る単発のX検索は x_voice_search / x_needs_mining を使う。"
    )
    input_schema: ClassVar[type[BaseModel]] = XBuzzMeasureInput
    output_schema: ClassVar[type[BaseModel]] = XBuzzMeasureOutput

    def __init__(self, store: XTaskStore | None = None) -> None:
        self._store = store or XTaskStore()

    def run(self, input: XBuzzMeasureInput, ctx: SkillContext) -> XBuzzMeasureOutput:
        log = ctx.bind_logger(self.name)
        user = _user_of(ctx)
        if not rollout_allowed(_ALLOWLIST_ENV, user):
            return XBuzzMeasureOutput(
                job_id="", status="denied", poll_after_s=0, message=ROLLOUT_DENIED_MESSAGE
            )
        job_id = new_job_id()
        spec = {
            "job_id": job_id,
            "keyword": input.keyword,
            "start_date": input.start_date,
            "end_date": input.end_date,
            "campaign_date": input.campaign_date,
            "max_items_per_day": input.max_items_per_day,
            "min_faves": input.min_faves,
            "s3_prefix": f"x-research/{job_id}/",
            "requested_by": user or "unknown",
            "request_id": ctx.request_id,
        }
        ok = self._store.submit(spec)
        log.info("x_buzz_submitted", job_id=job_id, ok=ok, keyword=input.keyword)
        if not ok:
            return XBuzzMeasureOutput(
                job_id=job_id,
                status="failed",
                poll_after_s=0,
                message="ジョブの投函に失敗しました(設定/権限を確認してください)。",
            )
        return XBuzzMeasureOutput(
            job_id=job_id,
            status="queued",
            poll_after_s=90,
            message=(
                f"発話量の取得を開始しました（{input.start_date}〜{input.end_date}・"
                f"日数分の分割取得のため数分かかります）。job_id={job_id}"
            ),
        )


@register
class XBuzzMeasureStatusSkill(_XSyncBase, BaseSkill[XBuzzMeasureStatusInput, XBuzzMeasureStatusOutput]):
    """④ 効果測定の状態照会。done 初回に山分析+HTMLレポートを生成しキャッシュする。"""

    name: ClassVar[str] = "x_buzz_measure_status"
    description: ClassVar[str] = (
        "x_buzz_measure が返した job_id の進行状況を照会する。done なら日別発話数・"
        "バズ投稿TOP全文・山の日の中身分析・レポート署名URLを返す。"
    )
    input_schema: ClassVar[type[BaseModel]] = XBuzzMeasureStatusInput
    output_schema: ClassVar[type[BaseModel]] = XBuzzMeasureStatusOutput

    def __init__(
        self,
        store: XTaskStore | None = None,
        analysis_bedrock: Any | None = None,
        publisher: Any | None = None,
    ) -> None:
        super().__init__(analysis_bedrock=analysis_bedrock, publisher=publisher)
        self._store = store or XTaskStore()

    def run(self, input: XBuzzMeasureStatusInput, ctx: SkillContext) -> XBuzzMeasureStatusOutput:
        log = ctx.bind_logger(self.name)
        st = self._store.get_status(input.job_id)
        if st is None:
            return XBuzzMeasureStatusOutput(
                job_id=input.job_id, status="unknown", message="そのjob_idは見つかりません。"
            )
        status = str(st.get("status", "unknown"))
        log.info("x_buzz_status", job_id=input.job_id, status=status)
        if status != "done":
            msg = {
                "queued": "順番待ちです。少し待って再度照会してください。",
                "running": "取得中です（1日ずつ分割取得しています）。",
                "failed": f"失敗しました: {st.get('error_code') or ''}".strip(),
            }.get(status, "状態不明です。")
            return XBuzzMeasureStatusOutput(
                job_id=input.job_id,
                status=status,
                progress=st.get("progress"),
                error_code=st.get("error_code"),
                message=msg,
            )

        s3_prefix = str(st.get("s3_prefix") or f"x-research/{input.job_id}/")
        results = self._store.read_results(s3_prefix) or {}
        daily = [d for d in results.get("daily_counts", []) if isinstance(d, dict)]
        top_posts = [
            XPostCard(
                post_id=str(p.get("post_id", "")),
                url=str(p.get("url", "")),
                author_handle=str(p.get("author_handle", "")),
                text=str(p.get("text", "")),
                like_count=int(p.get("like_count", 0) or 0),
                retweet_count=int(p.get("retweet_count", 0) or 0),
                created_at=str(p.get("created_at", "")),
                verified=True,  # 期間取得の実測スクレイプ＝取得時実在
            )
            for p in results.get("top_posts", [])
            if isinstance(p, dict)
        ]
        total_cost = float(st.get("total_cost_usd") or results.get("total_cost_usd") or 0.0)
        spec = results.get("spec") or {}

        report_url = st.get("report_url")
        spike = str(st.get("spike_analysis") or "")
        if not report_url and daily:
            # done 初回: 山分析（Sonnet）→ HTML → 発行 → キャッシュ
            if not spike:
                try:
                    spike_days = sorted(daily, key=lambda d: int(d.get("count", 0)), reverse=True)
                    sample = [
                        p for p in results.get("top_posts", []) if isinstance(p, dict)
                    ][:10]
                    prompt = load_prompt("x_research", "v1", "buzz").format(
                        keyword=str(spec.get("keyword", "")),
                        start_date=str(spec.get("start_date", "")),
                        end_date=str(spec.get("end_date", "")),
                        campaign_date=str(spec.get("campaign_date") or "指定なし"),
                        daily_counts_json=json.dumps(daily, ensure_ascii=False),
                        sample_posts_json=json.dumps(
                            [
                                {"text": str(p.get("text", ""))[:280], "likes": p.get("like_count")}
                                for p in sample
                            ],
                            ensure_ascii=False,
                        ),
                    )
                    resp = self._get_analysis().converse(
                        messages=[{"role": "user", "content": [{"text": prompt}]}],
                        request_id=ctx.request_id,
                        max_tokens=1024,
                    )
                    total_cost += resp.usage.cost_usd
                    spike = resp.text.strip()
                    _ = spike_days  # 参照はプロンプト内 daily に含まれる
                except Exception as e:
                    log.warning("x_buzz_spike_analysis_failed", error=type(e).__name__)
            html = render_buzz_report(
                keyword=str(spec.get("keyword", "")),
                start_date=str(spec.get("start_date", "")),
                end_date=str(spec.get("end_date", "")),
                campaign_date=spec.get("campaign_date"),
                daily_counts=daily,
                top_posts=top_posts,
                spike_analysis=spike,
            )
            report_url = self._publish_html(
                html, request_id=ctx.request_id, query=str(spec.get("keyword", ""))
            )
            if report_url:
                self._store.cache_report(
                    input.job_id, report_url=report_url, spike_analysis=spike
                )

        total = sum(int(d.get("count", 0) or 0) for d in daily)
        report_line = f"\n📄 レポート（7日有効）: {report_url}" if report_url else ""
        return XBuzzMeasureStatusOutput(
            job_id=input.job_id,
            status="done",
            progress=st.get("progress"),
            daily_counts=daily,
            top_posts=top_posts,
            spike_analysis=spike,
            report_url=report_url,
            s3_prefix=s3_prefix,
            total_cost_usd=round(total_cost, 4),
            message=(
                f"完了しました（総発話 {total:,}件・{len(daily)}日分）。{spike}{report_line}"
            ),
        )
