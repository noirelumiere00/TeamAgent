"""proposal_deck Skill の入出力 Pydantic スキーマ。

Agent（orchestrator）が search / proposal_draft / clientkarte / mail_constraints で集めた
研究素材を `research_material` に渡し、商材情報とあわせて FMT v2 95 placeholder を埋める。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from teamagent.skills.proposal_deck.confidentiality import contains_forbidden_term
from teamagent.skills.proposal_deck.contract import (
    PROPOSAL_BUILDER_REQUIRED_AUXILIARY,
    PROPOSAL_BUILDER_TEMPLATE_PROFILE,
    VALID_IDS,
    EvidenceImage,
)

_AUXILIARY_PLACEHOLDER_KEY = re.compile(r"PB-[A-Z0-9_-]+")
_MAX_AUXILIARY_PLACEHOLDER_CHARS = 20_000


class ProposalDeckInput(BaseModel):
    """proposal_deck Skill の入力。"""

    product_name: str = Field(
        min_length=1, max_length=200, description="商材・サービス名（{1} タイトル等の起点）"
    )
    goal: str = Field(min_length=1, max_length=1000, description="提案の目的（{2} 等）")
    target_persona: str = Field(min_length=1, max_length=1000, description="ターゲット像（{4} 等）")
    deadline: str = Field(default="", max_length=100, description="施策時期/締切（{5}）")
    urls: list[str] = Field(default_factory=list, description="商材/LP URL")
    research_material: str = Field(
        default="",
        max_length=40000,
        description=(
            "過去事例/Slack/Mail/Web から収集した研究素材（Agent が投入する自然文/箇条書き）"
        ),
    )
    evidence_images: dict[int, list[EvidenceImage]] = Field(
        default_factory=dict,
        description=(
            "同一requestのproposal_campaignフィーダが取得した証拠画像。"
            "Composerには生成させず決定論的に後付けする。"
        ),
    )
    posting_start_date: date | None = Field(
        default=None,
        description="投稿開始日 D（ISO date）。スケジュール/ガント生成を行わない場合は未指定。",
    )
    auxiliary_placeholders: dict[str, str] = Field(
        default_factory=dict,
        description=("統合FMTの補助枠。キーは PB-[A-Z0-9_-]+、値は非空、値の合計は最大20000文字。"),
    )
    derived_auxiliary_placeholders: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Composer本文から決定論的に派生する統合FMT補助枠。"
            "キーはPB-*、値は参照する95枠ID。LLMへは渡さない。"
        ),
    )
    enforce_provenance: bool = Field(
        default=False,
        description=(
            "True の場合、citationの入力証拠URL完全一致と定量主張の同一ID citationを機械検証。"
        ),
    )
    quantitative_evidence: dict[str, list[str]] | None = Field(
        default=None,
        description=(
            "定量表現→同一入力オブジェクトの根拠URL。proposal-builderが決定論的に生成し、"
            "LLMの数値創作と別オブジェクトURLによる根拠ロンダリングを検査する。"
        ),
    )
    forbidden_output_terms: list[str] = Field(
        default_factory=list,
        max_length=10,
        description=(
            "Composer本文/citationへ出してはいけない文字列。守秘商材名をbuilderが設定し、"
            "一致したIDを自己修復へ戻す。"
        ),
    )
    forced_skipped_ids: list[int] = Field(
        default_factory=list,
        description=(
            "上流の欠損が判明している枠。LLM出力後に本文/citationを破棄し、"
            "決定論的に『要確認（データ未検出）』へ置換する。"
        ),
    )
    publish_artifact: bool | None = Field(
        default=None,
        description=(
            "成果物のS3 publish制御。True=publish、False=抑止、None=従来どおりenvで決定。"
        ),
    )
    template_profile: Literal["base", "proposal-builder-v1"] = Field(
        default="base",
        description=(
            "base は従来FMT、proposal-builder-v1 は95数値枠・事例/アカウント枠・"
            "D相対日付枠の実在を描画前に厳格検証する。"
        ),
    )
    template_path: str | None = Field(
        default=None,
        description="FMT テンプレ .pptx パス（既定: 環境変数 TEAMAGENT_FMT_TEMPLATE）",
    )
    out_dir: str | None = Field(
        default=None,
        description="出力ディレクトリ（既定: 環境変数 TEAMAGENT_DECK_OUT_DIR or /tmp）",
    )
    max_repair: int = Field(
        default=4, ge=0, le=8, description="ComposerOutput 検証失敗時の自己修復回数"
    )
    emit_pdf: bool = Field(
        default=False,
        description=(
            "PPTX に加えて PDF コンパニオンも生成する（weasyprint で HTML→PDF）。"
            "env USE_PROPOSAL_DECK_PDF=1 でも有効化。既定 OFF。"
        ),
    )

    @field_validator("auxiliary_placeholders")
    @classmethod
    def _validate_auxiliary_placeholders(cls, value: dict[str, str]) -> dict[str, str]:
        for key, text in value.items():
            if _AUXILIARY_PLACEHOLDER_KEY.fullmatch(key) is None:
                raise ValueError(
                    f"invalid auxiliary placeholder key {key!r}; expected PB-[A-Z0-9_-]+"
                )
            if not text.strip():
                raise ValueError(f"auxiliary placeholder {key!r} must not be empty")
        total_chars = sum(len(text) for text in value.values())
        if total_chars > _MAX_AUXILIARY_PLACEHOLDER_CHARS:
            raise ValueError(
                f"auxiliary placeholder values exceed {_MAX_AUXILIARY_PLACEHOLDER_CHARS} characters"
            )
        return value

    @field_validator("evidence_images")
    @classmethod
    def _validate_evidence_images(
        cls, value: dict[int, list[EvidenceImage]]
    ) -> dict[int, list[EvidenceImage]]:
        invalid = set(value) - VALID_IDS
        if invalid:
            raise ValueError(f"evidence_images reference invalid ids: {sorted(invalid)}")
        for placeholder_id, images in value.items():
            if any(image.placeholder_id != placeholder_id for image in images):
                raise ValueError(
                    f"evidence_images key {placeholder_id} does not match image.placeholder_id"
                )
        return value

    @field_validator("derived_auxiliary_placeholders")
    @classmethod
    def _validate_derived_auxiliary_placeholders(cls, value: dict[str, int]) -> dict[str, int]:
        for key, placeholder_id in value.items():
            if _AUXILIARY_PLACEHOLDER_KEY.fullmatch(key) is None:
                raise ValueError(
                    f"invalid derived auxiliary placeholder key {key!r}; expected PB-[A-Z0-9_-]+"
                )
            if placeholder_id not in VALID_IDS:
                raise ValueError(
                    f"derived auxiliary placeholder {key!r} references invalid "
                    f"placeholder ID {placeholder_id}"
                )
        return value

    @field_validator("quantitative_evidence")
    @classmethod
    def _validate_quantitative_evidence(
        cls, value: dict[str, list[str]] | None
    ) -> dict[str, list[str]] | None:
        if value is None:
            return None
        if len(value) > 500:
            raise ValueError("quantitative_evidence exceeds 500 claims")
        total_chars = 0
        for claim, urls in value.items():
            if not claim.strip() or len(claim) > 200 or not urls or len(urls) > 80:
                raise ValueError("quantitative_evidence entry is invalid")
            total_chars += len(claim) + sum(len(url) for url in urls)
        if total_chars > 100_000:
            raise ValueError("quantitative_evidence exceeds 100000 characters")
        return value

    @field_validator("forbidden_output_terms")
    @classmethod
    def _validate_forbidden_output_terms(cls, value: list[str]) -> list[str]:
        if any(not term.strip() or len(term) > 200 for term in value):
            raise ValueError("forbidden_output_terms entries must contain 1-200 characters")
        return list(dict.fromkeys(value))

    @field_validator("forced_skipped_ids")
    @classmethod
    def _validate_forced_skipped_ids(cls, value: list[int]) -> list[int]:
        invalid = set(value) - VALID_IDS
        if invalid:
            raise ValueError(f"forced_skipped_ids contains invalid IDs: {sorted(invalid)}")
        return sorted(set(value))

    @model_validator(mode="after")
    def _deterministic_outputs_do_not_contain_forbidden_terms(
        self,
    ) -> ProposalDeckInput:
        explicit_keys = set(self.auxiliary_placeholders)
        derived_keys = set(self.derived_auxiliary_placeholders)
        overlap = explicit_keys & derived_keys
        if overlap:
            raise ValueError(
                f"auxiliary placeholders cannot be both explicit and derived: {sorted(overlap)}"
            )
        if self.template_profile == PROPOSAL_BUILDER_TEMPLATE_PROFILE:
            supplied_keys = explicit_keys | derived_keys
            if supplied_keys != PROPOSAL_BUILDER_REQUIRED_AUXILIARY:
                raise ValueError(
                    "proposal-builder auxiliary keys must exactly match the "
                    f"integrated template contract: {sorted(supplied_keys)}"
                )
        leaking_keys = sorted(
            key
            for key, text in self.auxiliary_placeholders.items()
            if contains_forbidden_term(text, self.forbidden_output_terms)
        )
        if leaking_keys:
            raise ValueError(
                "forbidden_output_terms remain in deterministic auxiliary "
                f"placeholders: {leaking_keys}"
            )
        return self


class ProposalDeckOutput(BaseModel):
    """proposal_deck Skill の出力（生入力は含めない＝CLAUDE.md 6-bis）。"""

    pptx_path: str = Field(description="生成された .pptx のパス（コンテナ内ローカル）")
    pptx_url: str | None = Field(
        default=None,
        description=(
            "Slack から開ける署名付き URL（既定: 非公開 S3 presigned 7 日）。"
            "USE_PROPOSAL_DECK_PUBLISH=1（権威ゲート）+ VSEO_REPORT_BUCKET "
            "設定時のみ非 None。publish_artifact=True でもゲート OFF なら公開しない。"
        ),
    )
    version_id: str = Field(
        default="",
        description="この生成物の版 ID（再生成ごとに一意。版管理/差し替え追跡の anchor）。",
    )
    pdf_path: str | None = Field(
        default=None, description="生成された PDF コンパニオンのパス（emit_pdf 時のみ非 None）。"
    )
    pdf_url: str | None = Field(
        default=None,
        description=(
            "PDF の署名付き URL（emit_pdf かつ USE_PROPOSAL_DECK_PUBLISH=1 "
            "の権威ゲート ON 時のみ非 None）。"
        ),
    )
    filled_count: int = Field(ge=0, description="埋めた placeholder 数")
    skipped_count: int = Field(ge=0, description="要確認（データ未検出）の placeholder 数")
    coverage_ratio: float = Field(ge=0.0, le=1.0, description="埋め率（filled / 95）")
    skipped_ids: list[int] = Field(default_factory=list, description="要確認の placeholder ID")
    total_cost_usd: float = Field(ge=0.0, description="この実行の概算コスト")
