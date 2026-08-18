"""web_research Skill 本体 — 公開Webの市場リサーチ（read-only）。

経路: Slack の自由文（「◯◯の市場規模を調べて」）→ OpenClaw → SOUL 指示で本ツール →
mcp 境界内で Gemini の **Google 検索グラウンディング** を 1 回呼び、要約と
groundingMetadata を受ける → サーバが出典を番号付きで決定的に整形して返す。

⚠️ 死守ライン:
  G1 本人限定（ctx.metadata.user_email→fail-closed）＋ WEB_RESEARCH_ALLOWED_EMAILS 段階公開。
  G2 検索結果は「不信データ」。要約 LLM は mcp 境界内で回し、生ページ本文を OpenClaw の
     エージェントに一切見せない（返すのは要約＋出典のタイトル/URL だけ）。
  G3 **出典は LLM 出力から作らない**。groundingMetadata から render.build_sources が機械的に
     組む。LLM 本文中の URL は sanitize_summary が伏字化して採用しない。
  G4 グラウンディング失敗（groundingMetadata 無し / 有効な出典ゼロ）は fail-closed。
     「それらしい要約」を出典なしで返さない（ハルシネーションを検索結果と誤認させない）。
  G5 read-only: Web ページへの直 fetch も書込 API も無い（取得は Google 側で完結）。

3 層分離: 本ファイルは Skill 層。google-genai は触らず adapters/gemini_client.py 経由
（CLAUDE.md 6-bis）。
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.gemini_client import GeminiClient
from teamagent.skills._shared.rollout import ROLLOUT_DENIED_MESSAGE, rollout_allowed
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.web_research.prompts import SYSTEM_PROMPT, build_user_prompt
from teamagent.skills.web_research.render import (
    NOT_GROUNDED_MESSAGE,
    SEARCH_FAILED_MESSAGE,
    SUMMARY_MAX_LEN,
    build_message,
    build_sources,
)
from teamagent.skills.web_research.sanitize import sanitize_query, sanitize_summary
from teamagent.skills.web_research.schema import (
    WebResearchInput,
    WebResearchOutput,
)

logger = structlog.get_logger(__name__)

_ALLOWLIST_ENV = "WEB_RESEARCH_ALLOWED_EMAILS"
_JST = _dt.timezone(_dt.timedelta(hours=9))
# 既定 60s: 実効の律速は MCP 300s 天井ではなく openclaw のターン制限（実測 ~181s）。
# 検索＋要約は 1 往復なので、その内側に十分な余裕を持って収める。
_DEFAULT_DEADLINE_S = 60


def _deadline_s() -> float:
    raw = os.environ.get("WEB_RESEARCH_DEADLINE_S", "").strip()
    if not raw:
        return float(_DEFAULT_DEADLINE_S)
    try:
        return max(10.0, float(raw))
    except ValueError:
        return float(_DEFAULT_DEADLINE_S)


@register
class WebResearchSkill(BaseSkill[WebResearchInput, WebResearchOutput]):
    """公開Webを検索して要約＋出典を返す Skill（read-only・社内データには触らない）。"""

    name: ClassVar[str] = "web_research"
    description: ClassVar[str] = (
        "公開Webを Google 検索して、市場規模・トレンド・競合の一般情報・製品仕様・"
        "ニュースなどを日本語要約＋出典URL付きで返す読み取り専用ツール。"
        "**社内資料・案件・顧客情報の照会には絶対に使わない**（それらは search / clientkarte）。"
        "X（旧Twitter）の生活者の声は x_voice_search、TikTok/Instagram の検索面は "
        "search_surface_check を使う。"
        "⚠️ クエリは外部の検索サービスへ送信されるため、社外秘の文言・顧客名・案件名を"
        "そのまま query に入れないこと。"
        "呼び出し時は arguments に `_user_context: {slack_user_id: '<依頼した本人のuser_id>'}` を"
        "必ず含める（本人解決鍵）。"
    )
    input_schema: ClassVar[type[BaseModel]] = WebResearchInput
    output_schema: ClassVar[type[BaseModel]] = WebResearchOutput

    def __init__(
        self,
        gemini: Any | None = None,
        *,
        now_factory: Any | None = None,
    ) -> None:
        self._gemini = gemini
        # テストで「今」を固定するための注入口（recency_days の after: 日付に効く）。
        self._now_factory = now_factory or (lambda: _dt.datetime.now(tz=_JST))

    def _client(self) -> Any:
        if self._gemini is None:
            self._gemini = GeminiClient.from_env()
        return self._gemini

    def run(self, input: WebResearchInput, ctx: SkillContext) -> WebResearchOutput:
        log = ctx.bind_logger(self.name)

        # ① G1: 本人限定（fail-closed）。MCP 外殻が slack_user_id→email を解決して注入。
        requester = str(ctx.metadata.get("user_email", "") or "").strip()
        if not requester:
            raise PermissionError("web_research は本人 user_email が必須です")

        # ② 段階公開 allowlist（空=全員許可）。
        if not rollout_allowed(_ALLOWLIST_ENV, requester):
            log.info("web_research_rollout_denied")
            return WebResearchOutput(error="rollout_denied", message=ROLLOUT_DENIED_MESSAGE)

        query = sanitize_query(input.query)
        if not query:
            log.info("web_research_empty_query")
            return WebResearchOutput(error="not_grounded", message=NOT_GROUNDED_MESSAGE)

        now = self._now_factory()
        today = (now.astimezone(_JST) if now.tzinfo else now.replace(tzinfo=_JST)).date()
        prompt = build_user_prompt(
            query,
            max_results=input.max_results,
            recency_days=input.recency_days,
            today=today,
        )

        # ③ 検索＋要約は mcp 境界内の 1 往復。生ページ本文はここから外へ出さない。
        try:
            response = self._client().generate_with_google_search(
                prompt,
                ctx.request_id,
                system=SYSTEM_PROMPT,
                timeout_s=_deadline_s(),
            )
        except Exception as e:
            log.warning("web_research_search_failed", err=type(e).__name__)
            return WebResearchOutput(
                query=query, error="search_failed", message=SEARCH_FAILED_MESSAGE
            )

        # ④ G3/G4: 出典はサーバが groundingMetadata から機械的に組む。
        #    グラウンディングが取れていなければ fail-closed（要約だけを返さない）。
        sources = (
            build_sources(response.sources, response.supports, limit=input.max_results)
            if response.grounded
            else []
        )
        summary = sanitize_summary(response.text, max_len=SUMMARY_MAX_LEN)
        if not sources or not summary:
            log.info(
                "web_research_not_grounded",
                grounded=response.grounded,
                source_count=len(sources),
                summary_len=len(summary),
            )
            return WebResearchOutput(
                query=query, error="not_grounded", message=NOT_GROUNDED_MESSAGE
            )

        log.info(
            "web_research_done",
            source_count=len(sources),
            summary_len=len(summary),
            search_queries=len(response.search_queries),
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
        )  # クエリ本文・要約本文・ページ本文はログに出さない
        return WebResearchOutput(
            query=query,
            sources=sources,
            message=build_message(query, summary, sources),
        )
