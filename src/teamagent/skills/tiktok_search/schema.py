"""TikTokSearch Skill の入出力 Pydantic スキーマ。

「TikTokで新宿 ランチ 検索して」「#新宿 で調べて」のような指示で、TikTok を
検索 → 上位動画のメタ (再生数/いいね/作者/ハッシュタグ等) を取得 → Gemini で
横断分析して、ベクトルの企画・制作に効くインサイトを返す。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TikTokSearchInput(BaseModel):
    """TikTokSearch Skill の入力。"""

    query: str = Field(
        min_length=1,
        max_length=100,
        description="検索語 (例: '新宿 ランチ')。hashtag のときは '#' 抜きの語 (例: '新宿')",
    )
    search_type: str = Field(
        default="keyword",
        description="keyword | hashtag。hashtag が空振りしたら keyword に自動フォールバック",
    )
    max_videos: int = Field(
        default=10, ge=1, le=30, description="取得する上位動画の本数 (既定 10)"
    )
    analyze: bool = Field(
        default=True, description="取得後に Gemini で横断分析するか (false ならデータのみ)"
    )


class TikTokVideoOut(BaseModel):
    """検索で取れた 1 本の動画 (Slack 表示・後段分析用)。"""

    rank: int
    url: str
    author: str  # @uniqueId
    author_followers: int
    desc: str
    play_count: int
    digg_count: int  # いいね
    comment_count: int
    share_count: int
    collect_count: int  # 保存
    engagement_rate: float
    duration: int
    hashtags: list[str] = Field(default_factory=list)


class TikTokSearchOutput(BaseModel):
    """TikTokSearch Skill の出力。"""

    query: str
    search_type: str  # keyword | hashtag | keyword(fallback)
    count: int
    videos: list[TikTokVideoOut] = Field(default_factory=list)
    analysis: str | None = Field(
        default=None, description="Gemini による上位動画の横断分析 (analyze=true のとき)"
    )
    model_id: str | None = None
    total_cost_usd: float = Field(default=0.0, ge=0.0)
