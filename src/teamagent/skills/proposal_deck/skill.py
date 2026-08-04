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
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel, ValidationError

from teamagent.adapters.bedrock_client import BedrockClient
from teamagent.prompts.loader import load_prompt
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.proposal_deck.confidentiality import contains_forbidden_term
from teamagent.skills.proposal_deck.contract import ComposerOutput, SkippedPlaceholder
from teamagent.skills.proposal_deck.provenance import (
    ProvenanceValidationError,
    validate_composer_provenance,
)
from teamagent.skills.proposal_deck.schema import ProposalDeckInput, ProposalDeckOutput

logger = structlog.get_logger(__name__)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
_SAFE_NAME = re.compile(r"[^\w\-]+", re.UNICODE)


def _envflag(name: str) -> bool:
    return os.environ.get(name, "false").lower() in ("1", "true", "yes")


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
        self._temporary_output_dirs: set[Path] = set()
        self._temporary_output_lock = threading.Lock()

    def run(self, input: ProposalDeckInput, ctx: SkillContext) -> ProposalDeckOutput:
        log = ctx.bind_logger(self.name)
        log.info(
            "proposal_deck_start",
            product=input.product_name,
            research_len=len(input.research_material),
            max_repair=input.max_repair,
        )

        composer_out, cost_usd = self._compose(input, ctx)

        # 版 ID: 再生成ごとに一意。版管理/差し替え追跡の anchor（提案#12,#19）。
        version_id = f"v-{uuid.uuid4().hex[:12]}"
        template = self._resolve_template(input)
        # 出力先: 明示指定 > env > OS の一時ディレクトリ（Linux コンテナでは /tmp）。
        # tempfile.gettempdir() で TMPDIR を尊重しハードコード /tmp（bandit B108）を避ける。
        safe = _SAFE_NAME.sub("_", input.product_name).strip("_") or "deck"
        configured_out_dir = input.out_dir or os.environ.get("TEAMAGENT_DECK_OUT_DIR")
        if configured_out_dir:
            out_dir = Path(configured_out_dir)
            temporary_output = False
        else:
            safe_request = _SAFE_NAME.sub("_", ctx.request_id).strip("_")[:64] or "request"
            out_dir = Path(tempfile.mkdtemp(prefix=f"teamagent-deck-{safe_request}-"))
            temporary_output = True
            with self._temporary_output_lock:
                self._temporary_output_dirs.add(out_dir)
        try:
            out_path = out_dir / f"{ctx.request_id}_{safe}.pptx"
            rendered = self._render_pptx(
                composer_out,
                template,
                out_path,
                request_id=ctx.request_id,
            )

            pptx_url = self._publish_if_enabled(
                str(rendered),
                input.product_name,
                ctx.request_id,
                kind="pptx",
                publish_artifact=input.publish_artifact,
            )
            pdf_path, pdf_url = self._emit_pdf_if_enabled(
                composer_out, input, ctx, version_id=version_id, out_dir=out_dir, safe=safe
            )

            skipped_ids = sorted(s.id for s in composer_out.skipped_placeholders)
            log.info(
                "proposal_deck_done",
                pptx=str(rendered),
                version_id=version_id,
                filled=len(composer_out.placeholders),
                skipped=len(skipped_ids),
                cost_usd=cost_usd,
                published=bool(pptx_url),
                pdf=bool(pdf_path),
            )
            return ProposalDeckOutput(
                pptx_path=str(rendered),
                pptx_url=pptx_url,
                version_id=version_id,
                pdf_path=pdf_path,
                pdf_url=pdf_url,
                filled_count=len(composer_out.placeholders),
                skipped_count=len(skipped_ids),
                coverage_ratio=composer_out.coverage_ratio,
                skipped_ids=skipped_ids,
                total_cost_usd=cost_usd,
            )
        except Exception:
            if temporary_output:
                self._remove_temporary_output_dir(out_dir)
            raise

    def _remove_temporary_output_dir(self, path: Path) -> None:
        with self._temporary_output_lock:
            owned = path in self._temporary_output_dirs
        if not owned:
            return
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        with self._temporary_output_lock:
            self._temporary_output_dirs.discard(path)

    def cleanup_output(self, output: ProposalDeckOutput) -> None:
        """Remove request-scoped PPTX/PDF after the runtime serializes them."""

        self._remove_temporary_output_dir(Path(output.pptx_path).parent)

    @staticmethod
    def _render_pptx(
        composer_out: ComposerOutput,
        template: Path,
        out_path: Path,
        *,
        request_id: str,
    ) -> Path:
        """media job設定時はworkerへ委譲し、ローカル開発だけ従来rendererを使う。"""
        from teamagent.adapters.media_job import MediaJobClient

        if not MediaJobClient.is_configured() and MediaJobClient.local_runtime_enabled():
            from teamagent.skills.proposal_deck.renderer import render_deck

            return render_deck(composer_out, template, out_path)
        MediaJobClient.require_configured()
        pptx = MediaJobClient().render_proposal_pptx(
            template.read_bytes(),
            composer_out.model_dump_json().encode("utf-8"),
            request_fingerprint=f"{request_id}:proposal-pptx",
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(pptx)
        return out_path

    def _emit_pdf_if_enabled(
        self,
        composer_out: ComposerOutput,
        input: ProposalDeckInput,
        ctx: SkillContext,
        *,
        version_id: str,
        out_dir: Path,
        safe: str,
    ) -> tuple[str | None, str | None]:
        """emit_pdf 有効時に PDF を生成し、失敗は明示的に呼び出し元へ返す。"""
        if not (input.emit_pdf or _envflag("USE_PROPOSAL_DECK_PDF")):
            return None, None
        from teamagent.adapters.media_job import MediaJobClient, MediaJobError

        try:
            pdf_out = out_dir / f"{ctx.request_id}_{safe}.pdf"
            if MediaJobClient.is_configured():
                from teamagent.skills.proposal_deck.pdf_export import build_proposal_html

                rendered = MediaJobClient().html_to_pdf(
                    build_proposal_html(
                        composer_out,
                        product_name=input.product_name,
                        version_id=version_id,
                    ),
                    request_fingerprint=f"{ctx.request_id}:proposal-pdf",
                )
                pdf_out.parent.mkdir(parents=True, exist_ok=True)
                pdf_out.write_bytes(rendered)
                rendered_pdf = pdf_out
            elif MediaJobClient.local_runtime_enabled():
                from teamagent.skills.proposal_deck.pdf_export import render_proposal_pdf

                rendered_pdf = render_proposal_pdf(
                    composer_out,
                    product_name=input.product_name,
                    version_id=version_id,
                    out_path=pdf_out,
                )
            else:
                raise MediaJobError("MEDIA_JOB_NOT_CONFIGURED")
        except MediaJobError:
            logger.exception("proposal_deck_pdf_failed", product=input.product_name)
            raise
        except Exception:
            logger.exception("proposal_deck_pdf_failed", product=input.product_name)
            raise
        pdf_url = self._publish_if_enabled(
            str(rendered_pdf),
            input.product_name,
            ctx.request_id,
            kind="pdf",
            publish_artifact=input.publish_artifact,
        )
        return str(rendered_pdf), pdf_url

    @staticmethod
    def _publish_if_enabled(
        path: str,
        product_name: str,
        request_id: str,
        *,
        kind: str = "pptx",
        publish_artifact: bool | None = None,
    ) -> str | None:
        """USE_PROPOSAL_DECK_PUBLISH is the authoritative publish gate.

        ``USE_PROPOSAL_DECK_PUBLISH`` が公開の権威ゲート。OFF なら
        ``publish_artifact=True`` でも公開しない（入力はツール呼び出し経由で
        外部から到達するため、env ゲートをバイパスできると S3 公開+presigned
        URL 発行をインジェクションで強制できてしまう＝レビュー MED）。
        ``publish_artifact=False`` はゲート ON でも個別に抑止できる。
        S3 認証や VSEO_REPORT_BUCKET 未設定なら publish_file 側が None を返すため、失敗しても
        skill 全体は成功扱い（Slack に URL は出ないだけ）。
        """
        if not _envflag("USE_PROPOSAL_DECK_PUBLISH"):
            return None
        if publish_artifact is False:
            return None
        try:
            from teamagent.adapters.report_publish import publish_pdf_file, publish_pptx_file

            publisher = publish_pdf_file if kind == "pdf" else publish_pptx_file
            return publisher(path, request_id=request_id, query=product_name)
        except Exception:
            logger.exception("proposal_deck_publish_failed", path=path, kind=kind)
            return None

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
                composer_out = ComposerOutput.model_validate(data)
                forced_skips = set(input.forced_skipped_ids)
                placeholders = {
                    placeholder_id: text
                    for placeholder_id, text in composer_out.placeholders.items()
                    if placeholder_id not in forced_skips
                }
                citations = {
                    placeholder_id: values
                    for placeholder_id, values in composer_out.citations_per_placeholder.items()
                    if placeholder_id not in forced_skips
                }
                skipped = [
                    item
                    for item in composer_out.skipped_placeholders
                    if item.id not in forced_skips
                ]
                skipped.extend(
                    SkippedPlaceholder(
                        id=placeholder_id,
                        reason=("要確認（データ未検出）: proposal-builderの依存入力がありません"),
                    )
                    for placeholder_id in sorted(forced_skips)
                )
                resolved_auxiliary = dict(input.auxiliary_placeholders)
                for key, placeholder_id in input.derived_auxiliary_placeholders.items():
                    resolved_auxiliary[key] = placeholders.get(
                        placeholder_id,
                        "要確認（データ未検出）",
                    )
                # LLMには95枠本文だけを生成させる。事例・アカウント・D起点日付と
                # template profile は検証済みの決定論的入力を後付けし、モデルに改変させない。
                composer_out = ComposerOutput.model_validate(
                    {
                        **composer_out.model_dump(mode="python"),
                        "placeholders": placeholders,
                        "citations_per_placeholder": citations,
                        "skipped_placeholders": skipped,
                        "auxiliary_placeholders": resolved_auxiliary,
                        "posting_start_date": input.posting_start_date,
                        "template_profile": input.template_profile,
                    }
                )
                forbidden_ids = sorted(
                    placeholder_id
                    for placeholder_id, text in composer_out.placeholders.items()
                    if contains_forbidden_term(text, input.forbidden_output_terms)
                )
                forbidden_citation_ids = sorted(
                    placeholder_id
                    for placeholder_id, citations in composer_out.citations_per_placeholder.items()
                    if any(
                        contains_forbidden_term(citation, input.forbidden_output_terms)
                        for citation in citations
                    )
                )
                forbidden_skip_ids = sorted(
                    item.id
                    for item in composer_out.skipped_placeholders
                    if contains_forbidden_term(item.reason, input.forbidden_output_terms)
                )
                forbidden_evidence_ids = sorted(
                    placeholder_id
                    for placeholder_id, images in composer_out.evidence_images.items()
                    if any(
                        contains_forbidden_term(
                            " ".join(
                                value
                                for value in (
                                    image.keyword,
                                    image.source_url,
                                    image.image_path,
                                    image.video_url,
                                )
                                if value
                            ),
                            input.forbidden_output_terms,
                        )
                        for image in images
                    )
                )
                if (
                    forbidden_ids
                    or forbidden_citation_ids
                    or forbidden_skip_ids
                    or forbidden_evidence_ids
                ):
                    raise ProvenanceValidationError(
                        [
                            "confidential term remains in placeholder IDs "
                            f"{forbidden_ids}, citation IDs {forbidden_citation_ids}, "
                            f"skip IDs {forbidden_skip_ids}, or evidence IDs "
                            f"{forbidden_evidence_ids}"
                        ]
                    )
                if input.enforce_provenance:
                    validate_composer_provenance(
                        composer_out,
                        input_urls=input.urls,
                        research_material=input.research_material,
                        quantitative_evidence=input.quantitative_evidence,
                    )
                return composer_out, total_cost
            except (
                json.JSONDecodeError,
                ValidationError,
                ProvenanceValidationError,
            ) as exc:
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
                                    "前回の出力が ComposerOutput スキーマまたは"
                                    f"根拠検証に合いませんでした: {last_error}\n"
                                    "指摘の ID/文字数/網羅/citation の不足を直し、"
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
