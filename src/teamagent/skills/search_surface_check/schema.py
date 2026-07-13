"""search_surface_check の I/O スキーマ（Pydantic v2）。

「『セブン』『ファミマ』…で検索したとき、TikTokとインスタで誰が上位に出てるか」に
面の勢力図（カテゴリ比率）とクライアント在圏判定つきで答える。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 直スクレイプ(tiktok_search経路)を許すKW数の上限。これを超えたら tiktok_acquire 経由必須
# （MCP同期300s天井の保護。descriptionにも明記して二重に強制する）。
MAX_DIRECT_KEYWORDS = 2

CategoryLabel = Literal[
    "news", "gourmet", "ugc", "brand_official", "influencer", "other", "unknown"
]


class SearchSurfaceCheckInput(BaseModel):
    keywords: list[str] = Field(min_length=1, max_length=5, description="検索KW群(1〜5)")
    platforms: list[Literal["tiktok", "instagram"]] = Field(
        default=["tiktok", "instagram"], min_length=1
    )
    client_name: str | None = Field(default=None, description="クライアント名（在圏判定の補助）")
    client_accounts: list[str] = Field(
        default_factory=list, description="クライアントのアカウント（@handle。TikTok/IG両方可）"
    )
    acquire_s3_prefix: str | None = Field(
        default=None,
        description=(
            "tiktok_acquire 成果物の s3_prefix。3KW以上のTikTok面はこの経路が必須"
            "（先に tiktok_acquire(videos_per_kw=0) を実行して渡す）"
        ),
    )
    max_posts_per_kw: int = Field(default=30, ge=5, le=50)
    ig_surface: Literal["search", "hashtag"] | None = Field(
        default=None,
        description="IG面の取得方式（未指定=環境既定。search=検索面/hashtag=タグ面）",
    )
    analyze: bool = Field(default=True, description="Bedrockでの勢力図分類を行うか")


class SurfacePost(BaseModel):
    platform: Literal["tiktok", "instagram"]
    keyword: str
    rank: int = Field(description="TikTok=検索面表示順(rank_display) / IG=出現頻度×エンゲージ序列")
    appearances: int = Field(default=1, description="IGの重複出現回数（面の定着度）")
    url: str = ""
    author: str = ""
    author_followers: int = 0
    desc: str = ""
    play_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    thumb_url: str = ""
    category: CategoryLabel = "unknown"
    is_client: bool = False


class KwSurface(BaseModel):
    keyword: str
    platform: str
    posts: list[SurfacePost] = Field(default_factory=list)
    category_ratio: dict[str, float] = Field(
        default_factory=dict, description="面の勢力図（カテゴリ→割合0-1）"
    )
    client_ranks: list[int] = Field(
        default_factory=list, description="クライアント投稿の面内順位（空=面に出ていない）"
    )


class SearchSurfaceCheckOutput(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    surfaces: list[KwSurface] = Field(default_factory=list)
    comparison_summary: str = Field(default="", description="TikTok×IG面の性格比較（2-4文）")
    report_url: str | None = Field(default=None, description="媒体比較HTML署名URL（7日）")
    slack_summary: str = ""
    total_cost_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)
