"""proposal_deck Skill の入出力 Pydantic スキーマ。

Agent（orchestrator）が search / proposal_draft / clientkarte / mail_constraints で集めた
研究素材を `research_material` に渡し、商材情報とあわせて FMT v2 95 placeholder を埋める。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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


class ProposalDeckOutput(BaseModel):
    """proposal_deck Skill の出力（生入力は含めない＝CLAUDE.md 6-bis）。"""

    pptx_path: str = Field(description="生成された .pptx のパス（コンテナ内ローカル）")
    pptx_url: str | None = Field(
        default=None,
        description=(
            "Slack から開ける署名付き URL（既定: 非公開 S3 presigned 7 日）。"
            "USE_PROPOSAL_DECK_PUBLISH=1 + VSEO_REPORT_BUCKET 設定時のみ非 None。"
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
        description="PDF の署名付き URL（emit_pdf + USE_PROPOSAL_DECK_PUBLISH=1 時のみ非 None）。",
    )
    filled_count: int = Field(ge=0, description="埋めた placeholder 数")
    skipped_count: int = Field(ge=0, description="要確認（データ未検出）の placeholder 数")
    coverage_ratio: float = Field(ge=0.0, le=1.0, description="埋め率（filled / 95）")
    skipped_ids: list[int] = Field(default_factory=list, description="要確認の placeholder ID")
    total_cost_usd: float = Field(ge=0.0, description="この実行の概算コスト")
