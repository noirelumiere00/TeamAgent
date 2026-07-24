"""ClientKarte Skill 本体。

あるクライアントの営業 FB を時系列で束ね、提案履歴・温度感推移・推奨ネクスト
アクションを 1 枚のカルテに合成する。営業の「あの会社、今どうなってる？」に即答する。

3 層分離: 本ファイルは Skill 層。psycopg / boto3 は触らず adapters/ 経由。
"""

from __future__ import annotations

import os
import re
from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.adapters.pgvector_client import PgVectorClient, SearchHit
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.clientkarte.schema import (
    ClientKarteInput,
    ClientKarteOutput,
    KarteEvent,
)

logger = structlog.get_logger(__name__)

_DEFAULT_EVENT_SUMMARY_MAX_CHARS = 160
_DEFAULT_SYNTHESIS_BODY_MAX_CHARS = 200
_TRUNCATION_MARKER = "…"
_SENTENCE_BOUNDARY_RE = re.compile(r"(?:[。！？!?]+[」』）】”’\"']*|[.]+(?=\s|$)|\n+)")


def _env_positive_int(name: str, default: int) -> int:
    """正の整数 env を読む。不正値は安全な既定値へ戻す。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _truncate_at_sentence_boundary(text: str, max_chars: int) -> str:
    """上限内の最後の文境界で切り、後続があることを ``…`` で示す。

    単一文が上限を超える場合だけは、上限を守るため文字境界へフォールバックする。
    """
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    if max_chars <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:max_chars]

    # 上限ちょうどの文末も完全な一文として残す。文末が marker 分の余白を使い切る場合は、
    # 文中切断して marker を付けるより、marker なしで文境界を優先する。
    capped = cleaned[:max_chars]
    matches = list(_SENTENCE_BOUNDARY_RE.finditer(capped))
    if matches:
        clipped = capped[: matches[-1].end()].rstrip()
        if len(clipped) + len(_TRUNCATION_MARKER) <= max_chars:
            return f"{clipped}{_TRUNCATION_MARKER}"
        return clipped

    budget = max_chars - len(_TRUNCATION_MARKER)
    return f"{cleaned[:budget].rstrip()}{_TRUNCATION_MARKER}"


@register
class ClientKarteSkill(BaseSkill[ClientKarteInput, ClientKarteOutput]):
    """クライアント単位の時系列カルテを合成する Skill。"""

    name: ClassVar[str] = "clientkarte"
    description: ClassVar[str] = "指定クライアントの提案履歴・温度感・次アクションを時系列で束ねる"
    input_schema: ClassVar[type[BaseModel]] = ClientKarteInput
    output_schema: ClassVar[type[BaseModel]] = ClientKarteOutput

    def __init__(
        self,
        bedrock: BedrockClient | None = None,
        pgvector: PgVectorClient | None = None,
        *,
        prompt_version: str = "v1",
        summary_max_tokens: int = 900,
        event_summary_max_chars: int | None = None,
        synthesis_body_max_chars: int | None = None,
        app_role: str | None = "teamagent_app",
    ) -> None:
        self._bedrock = bedrock or BedrockClient.from_env()
        self._pgvector = pgvector or PgVectorClient.from_env()
        self._prompt_version = prompt_version
        self._summary_max_tokens = summary_max_tokens
        # 数値の既定値は従来の 160/200 字を維持し、運用で品質比較できるよう env 化する。
        self._event_summary_max_chars = max(
            1,
            event_summary_max_chars
            if event_summary_max_chars is not None
            else _env_positive_int(
                "CLIENTKARTE_EVENT_SUMMARY_MAX_CHARS",
                _DEFAULT_EVENT_SUMMARY_MAX_CHARS,
            ),
        )
        self._synthesis_body_max_chars = max(
            1,
            synthesis_body_max_chars
            if synthesis_body_max_chars is not None
            else _env_positive_int(
                "CLIENTKARTE_SYNTHESIS_BODY_MAX_CHARS",
                _DEFAULT_SYNTHESIS_BODY_MAX_CHARS,
            ),
        )
        self._app_role = app_role

    def run(self, input: ClientKarteInput, ctx: SkillContext) -> ClientKarteOutput:
        log = ctx.bind_logger(self.name)
        log.info("clientkarte_start", client_name=input.client_name, limit=input.limit)

        user_email = ctx.metadata.get("user_email")
        user_groups_raw = ctx.metadata.get("user_groups")
        user_groups = list(user_groups_raw) if isinstance(user_groups_raw, (list, tuple)) else None
        user_role = ctx.metadata.get("user_role")

        with self._pgvector.connection(
            app_role=self._app_role,
            user_email=user_email,
            user_groups=user_groups,
            user_role=user_role,
        ) as conn:
            hits = self._pgvector.list_client_timeline_recent(
                conn=conn,
                client_name=input.client_name,
                limit=input.limit,
                request_id=ctx.request_id,
            )

        events = [self._to_event(h) for h in hits]
        answer, cost_usd = self._synthesize(input.client_name, hits, ctx.request_id)

        log.info("clientkarte_done", event_count=len(events), cost_usd=cost_usd)
        return ClientKarteOutput(
            client_name=input.client_name,
            answer=answer,
            events=events,
            event_count=len(events),
            total_cost_usd=cost_usd,
        )

    def _to_event(self, hit: SearchHit) -> KarteEvent:
        meta = hit.metadata or {}
        return KarteEvent(
            chunk_id=hit.chunk_id,
            occurred_at=meta.get("occurred_at"),
            deal_phase=meta.get("deal_phase"),
            bant_score=meta.get("bant_score"),
            channel_type=meta.get("channel_type"),
            next_action=meta.get("next_action"),
            summary=_truncate_at_sentence_boundary(
                hit.content,
                self._event_summary_max_chars,
            ),
        )

    def _synthesize(
        self, client_name: str, hits: list[SearchHit], request_id: str
    ) -> tuple[str, float]:
        """時系列 FB を Bedrock でカルテに合成する。FB が無ければ Bedrock を呼ばない。"""
        if not hits:
            return (f"「{client_name}」の営業 FB 記録が見つかりませんでした。", 0.0)

        system = load_prompt("clientkarte", self._prompt_version, "system")

        lines: list[str] = []
        for h in hits:
            m = h.metadata or {}
            head = (
                f"[chunk_id: {h.chunk_id}] {m.get('occurred_at', '日付不明')} "
                f"/ フェーズ={m.get('deal_phase', '-')} / BANT={m.get('bant_score', '-')} "
                f"/ チャネル={m.get('channel_type', '-')}"
            )
            extras: list[str] = []
            for label, key in (
                ("顧客反応", "client_reaction"),
                ("ポジ", "positive_reaction"),
                ("ネガ", "negative_reaction"),
                ("次アクション", "next_action"),
                ("提案メニュー", "proposed_menu"),
                ("共有メモ", "shared_memo"),
            ):
                if m.get(key):
                    extras.append(f"{label}: {m[key]}")
            # 構造化メタを本文より前に独立表示し、位置依存の本文抜粋より優先して合成させる。
            extra_block = ("\n  構造化メタ: " + " / ".join(extras)) if extras else ""
            body = _truncate_at_sentence_boundary(
                h.content,
                self._synthesis_body_max_chars,
            )
            lines.append(f"{head}{extra_block}\n  補足本文（構造化メタにない情報の補完）: {body}")

        timeline_block = "\n\n".join(lines)
        user_message = (
            f"# 対象クライアント\n{client_name}\n\n"
            f"# 時系列の営業 FB（古い順、{len(hits)} 件）\n{timeline_block}\n\n"
            "上記を、フォーマットに従ってクライアント・カルテに束ねてください。"
        )

        resp = self._bedrock.converse(
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            request_id=request_id,
            system=system,
            cache_system=True,
            max_tokens=self._summary_max_tokens,
        )
        return resp.text, resp.usage.cost_usd
