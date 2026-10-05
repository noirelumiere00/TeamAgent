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

# 投稿者のタイプ。打ち手に直結する粒度で切る（公式=自社運用・メディア=媒体タイアップ・
# 報道=広報露出・クリエイター=専門家起用・インフルエンサー=著名人起用・一般=UGC 喚起）。
# 旧版の gourmet（食の専門投稿者）は食品以外の KW で意味を失うため creator に統合した。
CategoryLabel = Literal[
    "brand_official", "media", "news", "creator", "influencer", "ugc", "other", "unknown"
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
    competitor_accounts: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="比較する競合のアカウント（@handle・最大5）。面に出ている順位を数える",
    )
    max_videos: Literal[5, 10] | None = Field(
        default=None,
        description="動画の中身を分析する本数（5 か 10。未指定=5）。全 KW の上位から選ぶ",
    )
    confirmed: bool = Field(
        default=False,
        description=(
            "依頼者が KW・分析本数・競合の確認に答えた後なら true。false だと（対象の人には）"
            "取得の前に確認文だけを返す"
        ),
    )


class SurfacePost(BaseModel):
    platform: Literal["tiktok", "instagram"]
    keyword: str
    rank: int = Field(description="TikTok=検索面表示順(rank_display) / IG=出現頻度×エンゲージ序列")
    appearances: int = Field(default=1, description="IGの重複出現回数（面の定着度）")
    url: str = ""
    author: str = Field(default="", description="@handle（TikTok=uniqueId / IG=username）")
    author_name: str = Field(default="", description="表示名（TikTok=nickname。IGは空）")
    author_followers: int = 0
    desc: str = ""
    hashtags: list[str] = Field(default_factory=list)
    play_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    share_count: int = 0
    save_count: int = 0
    posted_at: int = Field(default=0, description="投稿日時（epoch 秒。0=不明）")
    duration_sec: int = Field(default=0, description="動画の尺（秒。0=不明）")
    thumb_url: str = ""
    category: CategoryLabel = "unknown"
    is_client: bool = False
    is_competitor: bool = Field(default=False, description="比較する競合のアカウントの投稿")
    kw_ranks: list[str] = Field(
        default_factory=list,
        description=(
            "全 KW から選んだ動画の、KW ごとの表示順位（例「KW 2位」）。"
            "動画分析の対象に選んだときだけ入る"
        ),
    )
    mentions_client: bool = Field(
        default=False,
        description="本文・タグにクライアント名が出る（クライアント以外の投稿を含む）",
    )
    is_pr: bool = Field(default=False, description="#PR 等の広告表記あり")


class CategoryStat(BaseModel):
    category: str
    count: int
    count_share: float = Field(description="本数の割合 0-1")
    play_share: float = Field(description="再生数合計に占める割合 0-1")
    median_plays: int


class HolderStat(BaseModel):
    """同じアカウントが複数の枠を持っている（面の常連）。"""

    author: str
    author_name: str = ""
    category: str = "unknown"
    ranks: list[int]


class TierStat(BaseModel):
    tier: str
    count: int
    play_share: float


class SaveLeader(BaseModel):
    rank: int
    author: str
    save_rate_pct: float
    plays: int


class TagStat(BaseModel):
    tag: str
    count: int


class SurfaceFacts(BaseModel):
    """面の構造を決定的に数えた結果（LLM を通さない）。結論の根拠はここの数字だけ。"""

    n: int
    unique_authors: int
    categories: list[CategoryStat] = Field(default_factory=list)
    holders: list[HolderStat] = Field(default_factory=list)
    tiers: list[TierStat] = Field(default_factory=list)
    small_in_top10: int | None = Field(
        default=None, description="上位10本のうちフォロワー1万人未満のアカウントの本数"
    )
    top10_n: int = 0
    median_plays: int = 0
    reach_ratio_median: float | None = Field(
        default=None, description="再生数÷フォロワー数の中央値（1超=フォロワー外に届いている）"
    )
    most_played_rank: int | None = None
    rank_play_rho: float | None = Field(
        default=None, description="順位と再生数の順位相関（8本以上のときだけ）"
    )
    median_save_rate_pct: float | None = None
    save_leaders: list[SaveLeader] = Field(default_factory=list)
    median_age_days: int | None = None
    recent_90d: int | None = None
    median_duration_sec: int | None = None
    kw_in_text: int | None = Field(
        default=None, description="本文かタグに検索KWの語をすべて含む投稿の本数"
    )
    top_tags: list[TagStat] = Field(default_factory=list)
    pr_ranks: list[int] = Field(default_factory=list)
    client_ranks: list[int] = Field(default_factory=list)
    mention_ranks: list[int] = Field(default_factory=list)


class ConclusionPoint(BaseModel):
    text: str
    ranks: list[int] = Field(default_factory=list, description="根拠にした投稿の順位")


class SurfaceConclusion(BaseModel):
    """面の読み（LLM）。数字は SurfaceFacts と投稿一覧にあるものだけ（コードで検査）。"""

    headline: str
    winning: ConclusionPoint | None = None
    gap: ConclusionPoint | None = None
    actions: list[ConclusionPoint] = Field(default_factory=list)
    angles: list[ConclusionPoint] = Field(
        default_factory=list, description="上位に共通する切り口（2本以上に出ているものだけ）"
    )
    generated_by: Literal["llm", "rule"] = "llm"


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
    competitor_ranks: list[int] = Field(
        default_factory=list, description="競合アカウントの投稿の面内順位（空=面に出ていない）"
    )
    facts: SurfaceFacts | None = None
    conclusion: SurfaceConclusion | None = None


class SearchSurfaceCheckOutput(BaseModel):
    status: Literal["ok", "needs_input"] = Field(
        default="ok",
        description="needs_input=取得の前の確認（slack_summary の確認文をそのまま返す）",
    )
    keywords: list[str] = Field(default_factory=list)
    surfaces: list[KwSurface] = Field(default_factory=list)
    comparison_summary: str = Field(
        default="", description="KW×媒体ごとの結論（1行ずつ）。分析しなかったときは空"
    )
    report_url: str | None = Field(default=None, description="媒体比較HTML署名URL（7日）")
    slack_summary: str = ""
    total_cost_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)
    measured_epoch: int = Field(
        default=0,
        description="実測の時刻（epoch 秒・0=未実測）。2 段目がレポートを作り直すときに使う",
    )
    followup_note: str = Field(
        default="",
        description=(
            "2 段目（動画の中身）の予告など、mcp が slack_summary に足した 1 行。"
            "直接投稿の Block Kit で同じ行を出すために持つ（Aico へは返さない）"
        ),
    )
    deferred: bool = Field(
        default=False,
        description=(
            "検索上位と動画の中身を全部そろえてから 1 通で届けるため、この応答では結果を出さない"
            "（slack_summary は作成中の 1 行。mcp が後で投稿する・Aico へは返さない）"
        ),
    )
    tiktok_source: Literal["", "direct", "acquire_job"] = Field(
        default="",
        description=(
            "TikTok 面の取り方。direct=この依頼で検索した・acquire_job=事前の tiktok_acquire の"
            "成果物を読んだ（この依頼では検索し直していない）・空=TikTok 面なし/不明。"
            "直接投稿で「依頼のたびに検索し直した値」と書いてよいかの判断に使う"
            "（Aico へは返さない）"
        ),
    )


# ── 2 段目: 上位の動画の中身（video_algorithm の分析エンジンで見る） ──────────────


class LabelCount(BaseModel):
    label: str
    count: int


class VideoDigest(BaseModel):
    """上位 N 本の動画の中身を決定的に数えた結果（LLM を通さない）。

    集計の分母は「動画を見て分析できた本数」（watched）。サムネだけの分析（動画を取得できず
    静止画 1 枚で見たもの）はテロップ・構成・音を判定できないので、集計に入れない。
    """

    keyword: str
    requested: int = Field(description="分析しようとした本数（quota で丸める前）")
    reserved: int = Field(description="月間上限から確保できた本数")
    watched: int = Field(description="動画を見て分析できた本数（集計の分母）")
    watched_ranks: list[int] = Field(default_factory=list)
    cover_only_ranks: list[int] = Field(default_factory=list)
    failed_ranks: list[int] = Field(default_factory=list)
    hook_types: list[LabelCount] = Field(default_factory=list)
    opening_telop: int = 0
    telop_kw: int = 0
    spoken_kw: int = 0
    median_duration_sec: float | None = None
    median_cut_count: float | None = None
    pacing: list[LabelCount] = Field(default_factory=list)
    cta: int = 0
    cta_types: list[LabelCount] = Field(default_factory=list)
    narration: int = 0
    trending_sound: int = 0
    median_coherence: float | None = None
    save_top_ranks: list[int] = Field(
        default_factory=list, description="保存率の高い 2 本の順位（動画を見て分析できたものから）"
    )
    save_top_common: list[str] = Field(
        default_factory=list, description="その 2 本に共通すること（相関の断定はしない）"
    )


class VideoDigestConclusion(BaseModel):
    """動画の中身から見た勝ち筋（LLM）。数字は集計と 1 本ずつの一覧にあるものだけ。"""

    headline: str
    winning: ConclusionPoint | None = None
    save_reason: ConclusionPoint | None = None
    generated_by: Literal["llm", "rule"] = "llm"
    grounded: bool = Field(
        default=False,
        description="数字と順位を入力と照合した（常に許す数なし）。True のときだけ照合済みと書く",
    )


class FollowupVideo(BaseModel):
    """2 段目の 1 本（直接投稿の「1本ずつ」の行に使う）。数えた値だけ（LLM の文は持たない）。"""

    rank: int
    author: str = ""
    url: str = ""
    state: Literal["watched", "cover_only", "failed"] = "failed"
    hook: str = Field(default="", description="フックの和名（hook_label）。動画を見たときだけ")
    opening_telop: bool = False
    telop_kw: bool = False
    spoken_kw: bool = False
    duration_sec: int = 0
    cut_count: int | None = None
    pacing: str = Field(default="", description="テンポの和名（不明は空）")
    has_cta: bool = False
    narration: bool = False


class SurfaceVideoFollowupOutput(BaseModel):
    """2 段目の結果（会話へ追記する文面と、章を足したレポート）。"""

    keyword: str
    status: Literal["ok", "quota_exhausted", "all_failed", "no_videos"]
    digest: VideoDigest | None = None
    conclusion: VideoDigestConclusion | None = None
    report_url: str | None = None
    slack_text: str = ""
    total_cost_usd: float = 0.0
    videos: list[FollowupVideo] = Field(
        default_factory=list, description="分析した上位 N 本（順位順・直接投稿の 1 本 1 行）"
    )
    measured_epoch: int = Field(
        default=0, description="1 段目の実測の時刻（同じ検索回の続きと示す）"
    )
    across_keywords: bool = Field(
        default=False,
        description="全 KW から選んだ（N位＝総合順位。KW ごとの表示順位はレポートに）",
    )
