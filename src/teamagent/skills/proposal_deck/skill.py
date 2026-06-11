"""ProposalDeck Skill 本体 — 商材情報 + 研究素材 → FMT v2 95 placeholder → .pptx 生成。

3 層分離: Skill 層。生成は BedrockClient.converse 経由（Sonnet 4.6）、レンダリングは
python-pptx（renderer）。teamagent_consulting で AWS Bedrock 実走まで実証した Composer を
本番 Skill 化したもの。研究素材（過去事例/Slack/Mail/Web）は Agent が他 Skill で集めて渡す。

ComposerOutput の検証（網羅・文字数・citations）違反は、そのエラー文を次ターンに渡して
self-repair（最大 input.max_repair 回）。converse は tool を使わず、モデルに JSON のみを
出力させて parse する（コードフェンス/前後文は _extract_json が許容）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel, ValidationError

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.proposal_deck.contract import ComposerOutput
from teamagent.skills.proposal_deck.renderer import render_deck
from teamagent.skills.proposal_deck.schema import ProposalDeckInput, ProposalDeckOutput

logger = structlog.get_logger(__name__)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
_SAFE_NAME = re.compile(r"[^\w\-]+", re.UNICODE)


def _extract_json(text: str) -> str:
    """converse のテキストから JSON オブジェクトを取り出す（コードフェンス/前後文許容）。"""
    fenced = _JSON_FENCE.search(text)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


@register
class ProposalDeckSkill(BaseSkill[ProposalDeckInput, ProposalDeckOutput]):
    """商材情報と研究素材から提案書 FMT(95 項目)を埋めて .pptx を生成する Skill。"""

    name: ClassVar[str] = "proposal_deck"
    description: ClassVar[str] = (
        "商材情報と研究素材（過去事例/Slack/Mail）から提案書FMT v2の95項目を埋めてPPTXを生成する"
    )
    input_schema: ClassVar[type[BaseModel]] = ProposalDeckInput
    output_schema: ClassVar[type[BaseModel]] = ProposalDeckOutput

    def __init__(
        self,
        bedrock: BedrockClient | None = None,
        *,
        prompt_version: str = "v1",
        max_tokens: int = 16000,
    ) -> None:
        self._bedrock = bedrock or BedrockClient.from_env()
        self._prompt_version = prompt_version
        self._max_tokens = max_tokens

    def run(self, input: ProposalDeckInput, ctx: SkillContext) -> ProposalDeckOutput:
        log = ctx.bind_logger(self.name)
        log.info(
            "proposal_deck_start",
            product=input.product_name,
            research_len=len(input.research_material),
            max_repair=input.max_repair,
        )

        composer_out, cost_usd = self._compose(input, ctx)

        template = self._resolve_template(input)
        out_dir = Path(input.out_dir or os.environ.get("TEAMAGENT_DECK_OUT_DIR", "/tmp"))
        safe = _SAFE_NAME.sub("_", input.product_name).strip("_") or "deck"
        out_path = out_dir / f"{ctx.request_id}_{safe}.pptx"
        rendered = render_deck(composer_out, template, out_path)

        skipped_ids = sorted(s.id for s in composer_out.skipped_placeholders)
        log.info(
            "proposal_deck_done",
            pptx=str(rendered),
            filled=len(composer_out.placeholders),
            skipped=len(skipped_ids),
            cost_usd=cost_usd,
        )
        return ProposalDeckOutput(
            pptx_path=str(rendered),
            filled_count=len(composer_out.placeholders),
            skipped_count=len(skipped_ids),
            coverage_ratio=composer_out.coverage_ratio,
            skipped_ids=skipped_ids,
            total_cost_usd=cost_usd,
        )

    def _resolve_template(self, input: ProposalDeckInput) -> Path:
        raw = input.template_path or os.environ.get("TEAMAGENT_FMT_TEMPLATE")
        if not raw:
            raise ValueError(
                "FMT テンプレ未指定: input.template_path か "
                "env TEAMAGENT_FMT_TEMPLATE を設定してください"
            )
        path = Path(raw)
        if not path.exists():
            raise FileNotFoundError(f"FMT テンプレが見つかりません: {path}")
        return path

    def _compose(self, input: ProposalDeckInput, ctx: SkillContext) -> tuple[ComposerOutput, float]:
        system = load_prompt("proposal_deck", self._prompt_version, "system")
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [{"text": self._build_user_message(input)}]}
        ]
        total_cost = 0.0
        last_error: str | None = None
        for attempt in range(input.max_repair + 1):
            resp = self._bedrock.converse(
                messages=messages,
                request_id=ctx.request_id,
                system=system,
                cache_system=True,
                max_tokens=self._max_tokens,
            )
            total_cost += resp.usage.cost_usd
            try:
                data = json.loads(_extract_json(resp.text))
                return ComposerOutput.model_validate(data), total_cost
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)
                if attempt >= input.max_repair:
                    break
                messages.append({"role": "assistant", "content": [{"text": resp.text[:4000]}]})
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "text": (
                                    "前回の出力が ComposerOutput スキーマに"
                                    f"合いませんでした: {last_error}\n"
                                    "指摘の ID/文字数/網羅の不足を直し、"
                                    "JSON のみ（前後の説明文なし）で再送してください。"
                                )
                            }
                        ],
                    }
                )
        raise ValueError(
            f"proposal_deck compose failed after {input.max_repair + 1} attempts: {last_error}"
        )

    @staticmethod
    def _build_user_message(input: ProposalDeckInput) -> str:
        urls = "\n".join(f"- {u}" for u in input.urls) or "（なし）"
        research = input.research_material.strip() or (
            "（研究素材なし。商材情報と一般知識で埋め、出典が要る箇所は skipped_placeholders に"
            "『要確認（データ未検出）』で積むこと）"
        )
        return (
            "# 商材情報\n"
            f"- 商材・サービス名: {input.product_name}\n"
            f"- 目的: {input.goal}\n"
            f"- ターゲット: {input.target_persona}\n"
            f"- 施策時期/締切: {input.deadline or '未定'}\n"
            f"- 公式/LP URL:\n{urls}\n\n"
            "# 研究素材（過去事例 / Slack / Mail / Web から収集）\n"
            f"{research}\n\n"
            "上記をもとに、FMT v2 の 95 placeholder を埋めた ComposerOutput を "
            "JSON のみで出力してください。"
        )
