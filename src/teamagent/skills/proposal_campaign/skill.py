"""ProposalCampaign Skill 本体 — KW群 → 並列で実物サムネ → evidence_images（任意で PPTX）。

OC は本スキルを 1 回呼ぶだけ（並列検索は内部 ThreadPool に閉じる＝外殻に重い多段を背負わせない）。
bare ComposerOutput は作らない（95枠網羅 validator で落ちる）ため
evidence_images を一次成果物として返す。PPTX 描画は caller 提供の
95枠 ComposerOutput(.json) があるときだけ render_deck(enable_images=True)。
生入力はログに出さない（CLAUDE.md 6-bis）。
"""

from __future__ import annotations

import os
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import ClassVar

import structlog
from pydantic import BaseModel, ValidationError

from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.proposal_campaign.adapters import (
    Fetcher,
    Normalizer,
    Searcher,
    default_fetcher,
    default_normalizer,
    default_searcher,
)
from teamagent.skills.proposal_campaign.feeder import (
    assign_placeholder_ids,
    build_evidence_images,
    fetch_one,
    resolve_keywords,
)
from teamagent.skills.proposal_campaign.schema import (
    EvidenceImage,
    KWThumbnailResult,
    ProposalCampaignInput,
    ProposalCampaignOutput,
)
from teamagent.skills.proposal_deck.contract import ComposerOutput
from teamagent.skills.proposal_deck.renderer import render_deck

logger = structlog.get_logger(__name__)


@register
class ProposalCampaignSkill(BaseSkill[ProposalCampaignInput, ProposalCampaignOutput]):
    """KW群から実物サムネ証拠を集め、{58-92}枠の evidence_images を組む（任意で PPTX 描画）。"""

    name: ClassVar[str] = "proposal_campaign"
    description: ClassVar[str] = (
        "KW群からTikTok1位の実物サムネを並列取得し、提案FMTの{58-92}枠に貼る証拠画像(evidence_images)を組む"
    )
    input_schema: ClassVar[type[BaseModel]] = ProposalCampaignInput
    output_schema: ClassVar[type[BaseModel]] = ProposalCampaignOutput

    def __init__(
        self,
        *,
        searcher: Searcher | None = None,
        fetcher: Fetcher | None = None,
        normalizer: Normalizer | None = None,
        max_workers: int = 3,
    ) -> None:
        self._searcher: Searcher = searcher or default_searcher
        self._fetcher: Fetcher = fetcher or default_fetcher
        self._normalizer: Normalizer = normalizer or default_normalizer
        self._max_workers = max_workers

    def run(self, input: ProposalCampaignInput, ctx: SkillContext) -> ProposalCampaignOutput:
        log = ctx.bind_logger(self.name)
        version_id = f"v-{uuid.uuid4().hex[:12]}"

        keywords = resolve_keywords(
            keywords=input.keywords,
            gemini_dr_json_path=input.gemini_dr_json_path,
            composer_output_json_path=input.composer_output_json_path,
        )[: input.max_keywords]
        if not keywords:
            log.warning("proposal_campaign_no_keywords")
            return ProposalCampaignOutput(version_id=version_id)

        pids = assign_placeholder_ids(keywords)
        fallback_bytes = self._load_fallback(input.fallback_image_path)
        cache_dir = (
            Path(input.image_cache_dir)
            if input.image_cache_dir
            else Path(tempfile.mkdtemp(prefix=f"campaign_{ctx.request_id}_"))
        )
        log.info(
            "proposal_campaign_start",
            n_keywords=len(keywords),
            has_fallback=fallback_bytes is not None,
            enable_pptx=input.enable_pptx_render,
        )

        pairs = list(zip(keywords, pids, strict=True))

        def _do(pair: tuple[str, int]) -> tuple[KWThumbnailResult, EvidenceImage | None]:
            keyword, placeholder_id = pair
            return fetch_one(
                keyword=keyword,
                placeholder_id=placeholder_id,
                searcher=self._searcher,
                fetcher=self._fetcher,
                normalizer=self._normalizer,
                fallback_bytes=fallback_bytes,
                cache_dir=cache_dir,
                request_id=ctx.request_id,
            )

        workers = max(1, min(self._max_workers, len(pairs)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            rows = list(ex.map(_do, pairs))

        results = [row[0] for row in rows]
        evidences = [row[1] for row in rows if row[1] is not None]
        evidence_images = build_evidence_images(evidences)

        success = sum(1 for r in results if r.source == "tiktok_1st")
        fallback = sum(1 for r in results if r.source == "fallback")
        errors = sum(1 for r in results if r.source == "error")
        total = len(results)

        pptx_path: str | None = None
        composer_json = input.composer_output_json_path
        if input.enable_pptx_render and composer_json:
            pptx_path = self._render(
                composer_json_path=composer_json,
                template_path=input.template_path,
                out_dir=input.out_dir,
                evidence_images=evidence_images,
                ctx=ctx,
            )

        log.info(
            "proposal_campaign_done",
            total=total,
            success=success,
            fallback=fallback,
            errors=errors,
            placed=len(evidences),
            pptx=bool(pptx_path),
        )
        return ProposalCampaignOutput(
            evidence_images=evidence_images,
            results=results,
            pptx_path=pptx_path,
            version_id=version_id,
            total_keywords=total,
            success_count=success,
            fallback_count=fallback,
            error_count=errors,
            coverage_ratio=(success + fallback) / total if total else 0.0,
        )

    @staticmethod
    def _load_fallback(path: str | None) -> bytes | None:
        if not path:
            return None
        try:
            return Path(path).read_bytes() or None
        except OSError:
            return None

    def _render(
        self,
        *,
        composer_json_path: str,
        template_path: str | None,
        out_dir: str | None,
        evidence_images: dict[int, list[EvidenceImage]],
        ctx: SkillContext,
    ) -> str | None:
        """95枠 ComposerOutput(.json) ＋ テンプレがある時だけ PPTX を描画（graceful）。"""
        log = ctx.bind_logger(self.name)
        raw_tpl = template_path or os.environ.get("TEAMAGENT_FMT_TEMPLATE")
        if not raw_tpl or not Path(raw_tpl).exists():
            log.warning("proposal_campaign_pptx_skip_no_template")
            return None
        try:
            text = Path(composer_json_path).read_text(encoding="utf-8")
            composer = ComposerOutput.model_validate_json(text).model_copy(
                update={"evidence_images": evidence_images}
            )
        except (OSError, ValidationError, ValueError):
            log.warning("proposal_campaign_pptx_skip_bad_composer")
            return None
        out_base = Path(out_dir or tempfile.gettempdir())
        out_path = out_base / f"{ctx.request_id}_campaign.pptx"
        from teamagent.adapters.media_job import MediaJobClient

        if MediaJobClient.is_configured():
            staged_images: list[tuple[int, int, bytes, str]] = []
            for placeholder_id, images in evidence_images.items():
                for image in images:
                    if not image.image_path:
                        continue
                    try:
                        body = Path(image.image_path).read_bytes()
                    except OSError:
                        continue
                    mime = "image/png" if body.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
                    staged_images.append((placeholder_id, image.rank, body, mime))
            pptx = MediaJobClient().render_proposal_pptx(
                Path(raw_tpl).read_bytes(),
                composer.model_dump_json().encode("utf-8"),
                request_fingerprint=f"{ctx.request_id}:campaign-pptx",
                evidence_images=staged_images,
                fail_if_missing=False,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(pptx)
            rendered = out_path
        else:
            rendered = render_deck(
                composer, Path(raw_tpl), out_path, fail_if_missing=False, enable_images=True
            )
        return str(rendered)
