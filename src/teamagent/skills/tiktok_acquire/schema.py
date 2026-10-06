"""tiktok_acquire / tiktok_acquire_status の I/O スキーマ（Pydantic v2）。

submit は重い取得を SQS に投函するだけ（即return）。実体は使い捨て Fargate。
status は DynamoDB を読み、done なら S3 成果物を署名URL化して返す。
大きい mp4 は **インライン禁止＝S3参照(s3_key)+署名URL(url)** の2系統で返す。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class TikTokAcquireInput(BaseModel):
    """取得リクエスト（AIが商材からKWを立案して発行）。"""

    keywords: list[str] = Field(min_length=1, max_length=10, description="検索キーワード群(1〜10)")
    search_type: Literal["keyword", "hashtag"] = Field(
        default="keyword",
        description="keyword検索、またはhashtag検索（空振り時はkeywordへフォールバック）",
    )
    n_per_kw: int = Field(
        default=10,
        ge=1,
        le=30,
        description=(
            "各KWの取得本数(最大30)。1回の実行時間に収まらない組み合わせは断らずに"
            "組み直して取得する（動画なしは指標だけの取得・動画ありはKWごとに分割）"
        ),
    )
    videos_per_kw: int = Field(
        default=2,
        ge=0,
        le=10,
        description="各KWで動画本体(mp4)を保存する上位本数。0=指標だけ（検索面チェック用）",
    )
    sort: Literal["display", "save_rate", "recent"] = Field(
        default="display",
        description="動画DLの選抜軸(display=検索上位/save_rate=保存率上位/recent=新着)。指標の並びは検索順固定。",
    )
    client_name: str | None = Field(
        default=None, description="クライアント名(任意・config.jsonに反映)"
    )
    industry: str | None = Field(default=None, description="業種(任意)")
    competitors: list[str] = Field(default_factory=list, description="競合名(任意・SoV分析用)")


class TikTokAcquireJob(BaseModel):
    """分割したときの1ジョブ分（どの KW をどの job_id で取ったか）。"""

    job_id: str
    keywords: list[str]
    n_per_kw: int
    videos_per_kw: int
    artifact_mode: Literal["metadata_only", "full"] = Field(
        description="full=指標+サムネ+動画 / metadata_only=表示順と指標だけ"
    )


class TikTokAcquireOutput(BaseModel):
    job_id: str = Field(description="最初のジョブの job_id（1ジョブならこれだけ）")
    status: str = Field(description="queued 等。実取得は数分かかる(非同期)")
    poll_after_s: int = Field(
        default=75, description="この秒数後に tiktok_acquire_status を呼ぶ目安"
    )
    message: str
    job_ids: list[str] = Field(
        default_factory=list,
        description="投函した全ジョブの job_id。複数あるときは job_id ごとに status を照会する",
    )
    jobs: list[TikTokAcquireJob] = Field(
        default_factory=list,
        description="ジョブごとの KW と本数（KW 単位で後工程に渡す job_id の対応表）",
    )
    adjustments: list[str] = Field(
        default_factory=list,
        description="1回の実行時間に収めるため要求から変えた点（利用者にそのまま伝える）",
    )


class TikTokAcquireStatusInput(BaseModel):
    job_id: str = Field(description="tiktok_acquire が返した job_id")


class TikTokAcquireStatusOutput(BaseModel):
    job_id: str
    status: str = Field(description="queued | running | done | failed | unknown")
    progress: dict[str, Any] | None = None
    counts: dict[str, Any] | None = None
    s3_prefix: str | None = None
    posts_json_url: str | None = Field(
        default=None,
        description="posts.normalized.json の短期署名URL（status再照会で再発行）",
    )
    config_json_url: str | None = None
    manifest_url: str | None = Field(default=None, description="videos/manifest.json の署名URL")
    videos: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "各動画 {pid,kw,downloaded,s3_key(機械用),url(人向け),thumb_url,tiktok_url}。"
            "二段構え（USE_TIKTOK_APIFY_FALLBACK=1）時は acquired_via=worker|apify で出所を明示"
        ),
    )
    error_code: str | None = None
    warnings: list[str] = Field(default_factory=list)
    shortfalls: list[dict[str, Any]] = Field(default_factory=list)
    message: str = ""
