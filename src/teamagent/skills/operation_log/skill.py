"""OperationLog Skill 本体 (営業活動ログ自動生成、仕様: 実装計画 §7.3 Skill ⑥)。

Slack スレッドの営業会話を読み、CRM に転記できる構造化ログ
(deal_phase / action / next_step / BANT) + 人が読めるログ本文を生成する。

入力経路は 2 つ:
- Slack スレッド: channel_id + thread_ts → SlackChannelIngestClient で取得・整形
- 直接テキスト: conversation_text をそのまま使う (テスト/他経路用)

3 層分離: Skill 層。Slack 取得は adapters/slack_channel_ingest_client.py、
生成は adapters/bedrock_client.py 経由。構造化フィールドは LLM 出力の JSON
ブロックを防御的にパースする (失敗してもログ本文は返す)。
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.operation_log.schema import (
    BantAssessment,
    OperationLogInput,
    OperationLogOutput,
)

logger = structlog.get_logger(__name__)

# LLM 出力から ```json ... ``` ブロックを取り出す (無ければ全体を JSON とみなす)
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@register
class OperationLogSkill(BaseSkill[OperationLogInput, OperationLogOutput]):
    """営業会話 → CRM 構造化ログを生成する Skill。"""

    name: ClassVar[str] = "operation_log"
    description: ClassVar[str] = (
        "Slack スレッドの営業会話を CRM 転記用の活動ログ"
        "(フェーズ/アクション/次ステップ/BANT)に構造化する"
    )
    input_schema: ClassVar[type[BaseModel]] = OperationLogInput
    output_schema: ClassVar[type[BaseModel]] = OperationLogOutput

    def __init__(
        self,
        bedrock: BedrockClient | None = None,
        slack_ingest: Any | None = None,
        *,
        prompt_version: str = "v1",
        summary_max_tokens: int = 900,
    ) -> None:
        # bedrock / slack は遅延生成 (認証未設定環境でも import / 登録は通す)
        self._bedrock = bedrock
        self._slack_ingest = slack_ingest
        self._prompt_version = prompt_version
        self._summary_max_tokens = summary_max_tokens

    def _bedrock_client(self) -> BedrockClient:
        if self._bedrock is None:
            self._bedrock = BedrockClient.from_env()
        return self._bedrock

    def _slack_client(self) -> Any:
        if self._slack_ingest is None:
            from teamagent.adapters.slack_channel_ingest_client import (
                SlackChannelIngestClient,
            )

            self._slack_ingest = SlackChannelIngestClient.from_env()
        return self._slack_ingest

    def run(self, input: OperationLogInput, ctx: SkillContext) -> OperationLogOutput:
        log = ctx.bind_logger(self.name)

        conversation, msg_count = self._resolve_conversation(input, ctx.request_id)
        log.info("operation_log_start", source_message_count=msg_count)

        if not conversation.strip():
            return OperationLogOutput(
                log_entry="ログ化できる会話が見つかりませんでした。",
                source_message_count=0,
            )

        text, cost = self._generate(conversation, ctx.request_id)
        out = self._parse_output(text, cost, msg_count)
        log.info(
            "operation_log_done",
            deal_phase=out.deal_phase,
            cost_usd=out.total_cost_usd,
        )
        return out

    def _resolve_conversation(self, input: OperationLogInput, request_id: str) -> tuple[str, int]:
        """入力からログ化対象の会話テキストと元メッセージ数を得る。"""
        if input.conversation_text:
            # 直接テキスト経路: 行数を概算メッセージ数とする
            text = input.conversation_text
            return text, text.count("\n") + 1

        if input.channel_id and input.thread_ts:
            from teamagent.adapters.slack_channel_ingest_client import (
                format_thread_as_document,
            )

            batch = self._slack_client().list_thread_replies(
                channel_id=input.channel_id,
                thread_ts=input.thread_ts,
                request_id=request_id,
            )
            msgs = list(batch.messages)
            if not msgs:
                return "", 0
            parent, replies = msgs[0], msgs[1:]
            return format_thread_as_document(parent, replies), len(msgs)

        return "", 0

    def _generate(self, conversation: str, request_id: str) -> tuple[str, float]:
        system = load_prompt("operation_log", self._prompt_version, "system")
        user_message = (
            "# 営業会話（Slack スレッド）\n"
            f"{conversation}\n\n"
            "上記を、システム指示のフォーマットに従って CRM 営業活動ログに変換してください。"
        )
        resp = self._bedrock_client().converse(
            messages=[{"role": "user", "content": [{"text": user_message}]}],
            request_id=request_id,
            system=system,
            cache_system=True,
            max_tokens=self._summary_max_tokens,
        )
        return resp.text, resp.usage.cost_usd

    def _parse_output(self, text: str, cost: float, msg_count: int) -> OperationLogOutput:
        """LLM 出力 (ログ本文 + JSON ブロック) を構造化フィールドに分解する。

        JSON ブロックがあれば構造化フィールドを埋め、本文は JSON を除いた残り。
        パース失敗でもログ本文 (text 全体) は必ず返す (fail-safe)。
        """
        data: dict[str, Any] = {}
        m = _JSON_BLOCK_RE.search(text)
        if m:
            try:
                parsed = json.loads(m.group(1))
                if isinstance(parsed, dict):
                    data = parsed
            except (json.JSONDecodeError, ValueError):
                data = {}

        # ログ本文 = JSON ブロックを取り除いた残り (無ければ全文)
        log_entry = _JSON_BLOCK_RE.sub("", text).strip() or text.strip()

        raw_bant = data.get("bant")
        bant_raw: dict[str, Any] = raw_bant if isinstance(raw_bant, dict) else {}
        bant = BantAssessment(
            budget=_clean(bant_raw.get("budget")),
            authority=_clean(bant_raw.get("authority")),
            need=_clean(bant_raw.get("need")),
            timeline=_clean(bant_raw.get("timeline")),
        )
        return OperationLogOutput(
            log_entry=log_entry,
            deal_phase=_clean(data.get("deal_phase")),
            action=_clean(data.get("action")),
            next_step=_clean(data.get("next_step")),
            bant=bant,
            source_message_count=msg_count,
            total_cost_usd=cost,
        )


def _clean(v: Any) -> str | None:
    """LLM が "null"/"不明"/空文字で返す欠損を None に正規化する。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("null", "none", "n/a", "不明", "—", "-"):
        return None
    return s
