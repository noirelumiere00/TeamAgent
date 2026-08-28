"""omiyage_report（お土産資料 便1）の入出力 Pydantic スキーマ。

設計方針:
- submit の入力は **全て任意**。不足はサーバ側 preflight が決定論で検出し、
  ``needs_input`` として不足リスト + 補完候補 + 回答欄を返す（2ラリー設計）。
  required な自由文字列を作らないことで、外側ルーターの値捏造ハザードも避ける。
- status の job_id は ``^omy_[0-9a-f]{32}$`` の pattern で束縛し、proposal_builder
  （``pb_`` プレフィクス）の job と **スキーマ境界で** 分離する。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

OMIYAGE_JOB_ID_PATTERN = r"^omy_[0-9a-f]{32}$"

MissingField = Literal["brand", "competitors", "keywords"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _clean_names(values: list[str], *, limit: int) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = " ".join(str(value).split())
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    if len(cleaned) > limit:
        raise ValueError(f"at most {limit} entries are allowed")
    return cleaned


class OmiyageReportSubmitInput(_StrictModel):
    """お土産資料ジョブの投入入力（不足があれば needs_input で返る）。"""

    brand: str = Field(
        default="",
        max_length=200,
        description="対象ブランド名（例: エムキュア）。不明なら空のまま呼ぶ",
    )
    competitors: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="競合ブランド名（1〜4社）。不明なら空のまま呼ぶ",
    )
    keywords: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="一般検索キーワード（1〜3語・例: ヘアケア）。不明なら空のまま呼ぶ",
    )
    official_tiktok_account: str = Field(
        default="",
        max_length=200,
        description="対象ブランドの公式TikTokアカウント（URLまたは@ハンドル・任意）",
    )

    @field_validator("brand", "official_tiktok_account")
    @classmethod
    def _squash_whitespace(cls, value: str) -> str:
        return " ".join(str(value).split())

    @field_validator("competitors")
    @classmethod
    def _clean_competitors(cls, value: list[str]) -> list[str]:
        return _clean_names(value, limit=4)

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, value: list[str]) -> list[str]:
        return _clean_names(value, limit=3)


class OmiyageSuggestion(_StrictModel):
    """カルテ・金庫から決定論に引けた補完候補（提案であり確定値ではない）。"""

    field: MissingField
    candidates: list[str] = Field(default_factory=list, max_length=8)
    source: str = ""


class OmiyageReportSubmitOutput(_StrictModel):
    """受付結果。needs_input / busy のときはジョブを作らない。

    ``busy`` = 同時実行の上限に達していて受け付けなかった（失敗ではなく順番待ち）。
    ``retry_after_seconds`` を置いてから同じ入力で再 submit すればよい。
    """

    status: Literal["queued", "needs_input", "busy", "failed"]
    job_id: str = ""
    retry_after_seconds: int = Field(default=0, ge=0)
    missing: list[MissingField] = Field(default_factory=list)
    suggestions: list[OmiyageSuggestion] = Field(default_factory=list)
    message: str


class OmiyageReportStatusInput(_StrictModel):
    job_id: str = Field(pattern=OMIYAGE_JOB_ID_PATTERN)


class OmiyageAxisSummary(_StrictModel):
    """1検索軸の取得サマリ（資料の透明性欄と同じ内容）。"""

    role: Literal["general", "brand", "competitor"]
    label: str
    query: str
    requested: int = Field(ge=0)
    fetched: int = Field(ge=0)
    failed: bool = False
    failure_code: str = ""


class OmiyageVideoAnalysisSummary(_StrictModel):
    """動画解析の実施状況とコスト（ジョブ記録に残す・監査JSONの要約）。"""

    executed: bool = False
    requested: int = Field(default=0, ge=0)
    analyzed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    skip_reason: str = ""
    cost_usd_estimate: float = Field(default=0.0, ge=0)
    cost_cap_usd: float = Field(default=0.0, ge=0)
    model_id: str = ""


class OmiyageReportResult(_StrictModel):
    """job 完了時に store へ保存する結果（小さく保つ・生データは持たない）。"""

    status: Literal["ready", "partial"]
    message: str
    summary_lines: list[str] = Field(min_length=1, max_length=5)
    next_step: str
    slack_delivered: bool = False
    delivery_target: Literal["thread", "dm", "none"] = "none"
    axes: list[OmiyageAxisSummary] = Field(default_factory=list)
    pptx_filename: str = ""
    spec_version: str = ""
    video_analysis: OmiyageVideoAnalysisSummary = Field(default_factory=OmiyageVideoAnalysisSummary)
    deck_plan_s3_uri: str = ""
    audit_s3_uri: str = ""


class OmiyageReportStatusOutput(_StrictModel):
    job_id: str
    status: Literal["queued", "running", "done", "failed"]
    retry_after_seconds: int = Field(default=0, ge=0)
    report_status: Literal["ready", "partial"] | None = None
    result_message: str = ""
    summary_lines: list[str] = Field(default_factory=list)
    next_step: str = ""
    slack_delivered: bool = False
    delivery_target: Literal["thread", "dm", "none"] = "none"
    axes: list[OmiyageAxisSummary] = Field(default_factory=list)
    video_analysis: OmiyageVideoAnalysisSummary = Field(default_factory=OmiyageVideoAnalysisSummary)
    deck_plan_s3_uri: str = ""
    audit_s3_uri: str = ""
    error_code: str | None = None
    message: str = ""


__all__ = [
    "OMIYAGE_JOB_ID_PATTERN",
    "MissingField",
    "OmiyageAxisSummary",
    "OmiyageReportResult",
    "OmiyageReportStatusInput",
    "OmiyageReportStatusOutput",
    "OmiyageReportSubmitInput",
    "OmiyageReportSubmitOutput",
    "OmiyageSuggestion",
    "OmiyageVideoAnalysisSummary",
]
