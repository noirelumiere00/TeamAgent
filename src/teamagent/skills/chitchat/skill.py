"""Chitchat Skill 本体（挨拶・お礼・雑談・能力質問への会話応答）。

RAG/rerank/embedder を使わず Bedrock(Haiku) を 1 回呼ぶだけの軽量 Skill。
検索すべき本題は intent.py の task-first ガードで除外済みのため、ここには雑談だけが来る。
3 層分離: Skill 層。生成は adapters/bedrock_client.py 経由。prompt はファイル。
"""

from __future__ import annotations

import os
from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.chitchat.schema import ChitchatInput, ChitchatOutput

logger = structlog.get_logger(__name__)

_FALLBACK = "こんにちは。営業支援エージェントです。案件の質問や資料検索などお手伝いできます。"


@register
class ChitchatSkill(BaseSkill[ChitchatInput, ChitchatOutput]):
    """挨拶・お礼・雑談・能力質問に会話AIとして応答する Skill（検索しない）。"""

    name: ClassVar[str] = "chitchat"
    description: ClassVar[str] = (
        "挨拶・お礼・雑談・能力質問に、検索せず社内アシスタントとして自然に応答する"
    )
    input_schema: ClassVar[type[BaseModel]] = ChitchatInput
    output_schema: ClassVar[type[BaseModel]] = ChitchatOutput

    def __init__(
        self,
        bedrock: BedrockClient | None = None,
        *,
        prompt_version: str = "v1",
        max_tokens: int = 256,
        temperature: float = 0.3,
    ) -> None:
        self._bedrock = bedrock  # 遅延生成（認証未設定でも import/register は通す）
        self._prompt_version = prompt_version
        self._max_tokens = max_tokens
        self._temperature = temperature

    def _client(self) -> BedrockClient:
        if self._bedrock is None:
            # 会話は軽量モデルで十分。検索系の Sonnet とは別に Haiku を使う。
            region = os.environ.get("AWS_REGION", "us-east-1")
            model_id = os.environ.get(
                "BEDROCK_HAIKU_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
            )
            self._bedrock = BedrockClient(region=region, model_id=model_id)
        return self._bedrock

    def run(self, input: ChitchatInput, ctx: SkillContext) -> ChitchatOutput:
        log = ctx.bind_logger(self.name)
        try:
            system = load_prompt("chitchat", self._prompt_version, "system")
            resp = self._client().converse(
                messages=[{"role": "user", "content": [{"text": input.message}]}],
                request_id=ctx.request_id,
                system=system,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                cache_system=True,
            )
            reply = resp.text.strip() or _FALLBACK
            cost = resp.usage.cost_usd
        except Exception as e:  # 会話失敗でユーザーを止めない（定型で返す）
            logger.warning("chitchat_failed", request_id=ctx.request_id, error=type(e).__name__)
            return ChitchatOutput(reply=_FALLBACK, total_cost_usd=0.0)
        log.info("chitchat_done", cost_usd=cost, out_tokens=resp.usage.output_tokens)
        return ChitchatOutput(reply=reply, total_cost_usd=cost)
