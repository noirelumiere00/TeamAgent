"""search_surface_check の I/O スキーマ（Pydantic v2）。

「『セブン』『ファミマ』…で検索したとき、TikTokとインスタで誰が上位に出てるか」に
面の勢力図（カテゴリ比率）とクライアント在圏判定つきで答える。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from teamagent.media.contracts import TIKTOK_N_PER_KW_MAX

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
    acquire_job_id: str | None = Field(
        default=None,
        pattern=r"^(?:mj_[0-9a-f]{24}|tk_[0-9a-f]{12})$",
        description=(
            "tiktok_acquire が返した job_id。3KW以上のTikTok面はこの経路が必須"
            "（本人所有ジョブのimmutable成果物だけを読む）"
        ),
    )
    # 上限は TikTok 取得 dispatcher の n_per_kw 上限（TIKTOK_N_PER_KW_MAX=30）と同一。
    # 31〜50 を受理していた頃は _tiktok_direct → search_tiktok の fail-fast で
    # TIKTOK_MEDIA_JOB_FAILED: ValueError になり、面の取得が丸ごと落ちていた。
    max_posts_per_kw: int = Field(
        default=TIKTOK_N_PER_KW_MAX,
        ge=5,
        le=TIKTOK_N_PER_KW_MAX,
        description=(
            f"KWあたりの取得本数（5〜{TIKTOK_N_PER_KW_MAX}）。"
            "上限は TikTok 取得 dispatcher の n_per_kw 上限と同一"
        ),
    )
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
