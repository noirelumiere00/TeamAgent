"""tiktok_comment_mining の I/O スキーマ（Pydantic v2）。

「バズ動画のコメント欄は無料のグループインタビュー」— 反応の分類と生活者の語彙を
構造化して返す。上限は課金直結（$0.001/コメント）なので schema 入口で clamp する。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CommentBucket(BaseModel):
    """反応分類1カテゴリ（カタログ⑤の分類軸）。"""

    category: str = Field(
        description="推薦/売切れ嘆き/口コミ検証/ツッコミ・ネタ/質問/願望・欲しい/批判/その他"
    )
    count: int = 0
    examples: list[str] = Field(
        default_factory=list, description="代表コメント（原文のまま・いいね順）"
    )


class VideoCommentInsight(BaseModel):
    video_url: str
    total_comments: int = 0
    buckets: list[CommentBucket] = Field(default_factory=list)
    consumer_vocabulary: list[str] = Field(
        default_factory=list, description="生活者の語彙（商品の呼び名・比喩・定型文）"
    )
    common_questions: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    desires: list[str] = Field(default_factory=list)
    purchase_signals: list[str] = Field(default_factory=list)
    overall_sentiment: str = ""
    key_themes: list[str] = Field(default_factory=list)
    source: str = Field(default="", description="取得経路（chromium | apify）")


class CommentMiningInput(BaseModel):
    video_urls: list[str] = Field(min_length=1, max_length=3, description="TikTok動画URL（1〜3本）")
    max_comments_per_video: int = Field(default=200, ge=20, le=500)
    classify: bool = Field(default=True, description="Bedrockでの反応分類を行うか")
    client_name: str | None = Field(default=None, description="クライアント名（文脈補助・任意）")
    # 第二弾フック: tiktok_acquire の include.comments 実装後に
    # acquire_job_id 入力を追加してowner-boundバルク読み込みに対応する。


class CommentMiningOutput(BaseModel):
    videos: list[VideoCommentInsight] = Field(default_factory=list)
    cross_vocabulary: list[str] = Field(
        default_factory=list, description="複数動画横断で頻出する生活者語彙"
    )
    report_url: str | None = None
    slack_summary: str = ""
    scraped_comments: int = 0
    total_cost_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)
