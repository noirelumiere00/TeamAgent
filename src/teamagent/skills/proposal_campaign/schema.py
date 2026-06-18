"""proposal_campaign の I/O スキーマ（Pydantic v2）。

EvidenceImage は proposal_deck の契約を再利用（再定義しない）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from teamagent.skills.proposal_deck.contract import EvidenceImage

__all__ = [
    "EvidenceImage",
    "KWThumbnailResult",
    "ProposalCampaignInput",
    "ProposalCampaignOutput",
]


class ProposalCampaignInput(BaseModel):
    """KW群 → 実物サムネ → evidence_images（任意で PPTX 描画）の入力。"""

    keywords: list[str] = Field(default_factory=list, description="検索KW（直接指定・最優先）")
    gemini_dr_json_path: str | None = Field(
        default=None, description="GeminiDRJSON(.json) から KW を抽出"
    )
    composer_output_json_path: str | None = Field(
        default=None,
        description="ComposerOutput(.json)。enable_pptx_render 時の描画元 兼 {58-92}KW抽出元",
    )
    max_keywords: int = Field(default=6, ge=1, le=20, description="検索する KW の上限")
    fallback_image_path: str | None = Field(
        default=None, description="検索/取得失敗時に使う代替画像（ローカル）"
    )
    image_cache_dir: str | None = Field(
        default=None, description="取得サムネの保存先（未指定は一時ディレクトリ）"
    )
    template_path: str | None = Field(
        default=None, description="PPTX 描画テンプレ（未指定は env TEAMAGENT_FMT_TEMPLATE）"
    )
    out_dir: str | None = Field(default=None, description="PPTX 出力先（未指定は一時ディレクトリ）")
    enable_pptx_render: bool = Field(
        default=False, description="composer_output_json_path 提供時に PPTX を描画する"
    )
    max_workers: int = Field(default=3, ge=1, le=8, description="並列検索のワーカ数")


class KWThumbnailResult(BaseModel):
    """1 KW の取得結果（監査用）。生成物には載せず監査ログ/出力に残す。"""

    keyword: str
    placeholder_id: int
    rank: int
    success: bool
    source: Literal["tiktok_1st", "fallback", "error"]
    video_url: str | None = None
    cover_url: str | None = None
    image_path: str | None = None
    error: str | None = None


class ProposalCampaignOutput(BaseModel):
    """evidence_images（一次成果物）＋ 監査結果 ＋ 任意の PPTX。"""

    evidence_images: dict[int, list[EvidenceImage]] = Field(default_factory=dict)
    results: list[KWThumbnailResult] = Field(default_factory=list)
    pptx_path: str | None = None
    pptx_url: str | None = None
    version_id: str = ""
    total_keywords: int = 0
    success_count: int = 0
    fallback_count: int = 0
    error_count: int = 0
    coverage_ratio: float = 0.0
