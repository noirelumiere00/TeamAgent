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
        description="検索語（hashtagは#なし）。",
    )
    search_type: str = Field(
        default="keyword",
        description="keyword/hashtag（空振り時はkeyword）。",
    )
    max_videos: int = Field(default=10, ge=1, le=30)
    analyze: bool = Field(default=True)
    outputs: list[str] = Field(
        default_factory=lambda: ["html"],
        description=(
            "htmlは常時生成、pptxはパワポ、framesは該当箇所・動画表示を本人が求めた時だけ"
            "追加（待機・課金あり）。"
        ),
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
    cover_url: str = ""  # サムネイル (HTML レポート用)


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
    report_url: str | None = Field(
        default=None,
        description=(
            "HTMLレポートの配信URL（USE_HTML_REPORTS 有効時のみ）。"
            "**このURLは書き換えず、そのまま利用者へ提示すること**"
        ),
    )
    report_pptx_url: str | None = Field(
        default=None,
        description=(
            "PPTX の配信URL（outputs に 'pptx' を含めたときのみ）。"
            "**このURLは書き換えず、そのまま利用者へ提示すること**"
        ),
    )
