"""VideoAnalysis Skill (動画分析) の入出力 Pydantic スキーマ。

競合 PR 動画 (YouTube/Shorts) を Gemini 2.5 Flash で分析し、構成・テロップ・
フック・尺・CTA を抽出して提案書に転記できる形にする。仕様: 実装計画 §7.2 Skill ④。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class VideoAnalysisInput(BaseModel):
    """VideoAnalysis Skill の入力。"""

    url: str = Field(
        min_length=1, max_length=500, description="分析対象の動画 URL (YouTube/Shorts)"
    )
    focus: str | None = Field(
        default=None,
        max_length=200,
        description="分析の着眼点 (任意。例: 『フックとCTAを重点的に』)",
    )


class VideoAnalysisOutput(BaseModel):
    """VideoAnalysis Skill の出力。"""

    url: str
    analysis: str = Field(
        description="競合動画の構造分析 (構成/フック/テロップ/尺/CTA/転記ポイント)"
    )
    model_id: str = Field(description="使用した Gemini モデル")
    total_cost_usd: float = Field(ge=0.0, description="この分析の概算コスト")
    report_url: str | None = Field(
        default=None,
        description=(
            "HTMLレポートの配信URL（USE_HTML_REPORTS 有効時のみ）。"
            "**このURLは書き換えず、そのまま利用者へ提示すること**"
        ),
    )
