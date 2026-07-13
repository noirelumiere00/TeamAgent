"""x_voice_search / x_needs_mining / x_buzz_measure(+status) の I/O スキーマ（Pydantic v2）。

営業の頼み方（カタログP.9）から逆算した入出力。件数・期間の上限は LLM の暴走入力を
スキーマ入口で構造的に弾く（コストガードの第一段）。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

_MAX_PERIOD_DAYS = 62  # ④の取得期間上限（1日ずつ分割取得するためコスト直結）


class XPostCard(BaseModel):
    """①②④共通の1投稿（納品カード）。text は原文のまま＝要約・改変禁止。"""

    post_id: str
    url: str
    author_handle: str = ""
    author_note: str = Field(default="", description="属性メモ（美容系/一般 等・LLM推定）")
    text: str
    like_count: int = 0
    retweet_count: int = 0
    created_at: str = ""
    verified: bool = Field(default=False, description="xtracto による投稿ID単位の実在検証済みか")
    verify_note: str = Field(default="", description="未検証時の注記（例: 要再確認: 取得不可）")


# ---- ① 世の中の声集め ---------------------------------------------------------


class XVoiceSearchInput(BaseModel):
    """「◯◯がXでどう言われてるか、投稿の実物つきで集めて」。"""

    product_name: str = Field(description="商材/ブランド名（レポート表題・文脈判定用）")
    queries: list[str] = Field(
        min_length=1,
        max_length=6,
        description="検索クエリ群（同義語・文脈違いをAIが商材から立案。例:『白湯』『アサヒ 白湯』）",
    )
    search_type: Literal["top", "latest"] = "top"
    results_per_query: int = Field(default=20, ge=5, le=30)
    max_selected: int = Field(default=15, ge=3, le=30, description="カード化する厳選件数")


class XVoiceSearchOutput(BaseModel):
    product_name: str
    posts: list[XPostCard] = Field(default_factory=list)
    searched: int = 0
    selected: int = 0
    verified_count: int = 0
    unverified_count: int = 0
    noise_note: str = Field(default="", description="多義性ノイズ等の気づき（例: 白湯=飲料以外）")
    report_url: str | None = Field(default=None, description="HTMLカード集の署名URL（7日）")
    slack_summary: str = ""
    total_cost_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)


# ---- ② ニーズ発掘 -------------------------------------------------------------


class NeedCluster(BaseModel):
    label: str = Field(description="ニーズ分類名（例: 価格/品揃え/時間帯/サービスの穴）")
    insight: str = Field(description="提案の切り口になるインサイト仮説（1-2文）")
    post_ids: list[str] = Field(default_factory=list)


class XNeedsMiningInput(BaseModel):
    """「◯◯業界の生活者の不満とか欲求をXから拾ってきて」。"""

    theme: str = Field(description="対象領域（例: コンビニ、白湯、宅配クリーニング）")
    emotion_words: list[str] = Field(
        default=["めんどくさい", "売ってほしい", "高い", "困る"],
        min_length=1,
        max_length=8,
        description="掛け合わせる感情/本音ワード",
    )
    min_faves: int = Field(default=5, ge=0, description="いいね数下限（ノイズ足切り）")
    results_per_query: int = Field(default=15, ge=5, le=30)
    max_selected: int = Field(default=12, ge=5, le=30)


class XNeedsMiningOutput(BaseModel):
    theme: str
    posts: list[XPostCard] = Field(default_factory=list)
    clusters: list[NeedCluster] = Field(default_factory=list)
    hypothesis_summary: str = Field(default="", description="全体のインサイト仮説")
    report_url: str | None = None
    slack_summary: str = ""
    total_cost_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)


# ---- ④ 効果測定（非同期） -----------------------------------------------------


class XBuzzMeasureInput(BaseModel):
    """「キャンペーン期間の前後で、Xの発話がどう変わったかレポートにして」（submitのみ）。"""

    keyword: str = Field(description="発話量を測る検索語（例: セブンイレブン 新商品）")
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    campaign_date: str | None = Field(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="施策日（前後比較の境界。日別グラフに縦線表示）",
    )
    max_items_per_day: int = Field(default=100, ge=10, le=200)
    min_faves: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _validate_period(self) -> XBuzzMeasureInput:
        start = _dt.date.fromisoformat(self.start_date)
        end = _dt.date.fromisoformat(self.end_date)
        if end < start:
            raise ValueError("end_date は start_date 以降にしてください")
        if (end - start).days + 1 > _MAX_PERIOD_DAYS:
            raise ValueError(f"期間は最大{_MAX_PERIOD_DAYS}日です（1日ずつ分割取得のため）")
        if self.campaign_date is not None:
            camp = _dt.date.fromisoformat(self.campaign_date)
            if not (start <= camp <= end):
                raise ValueError("campaign_date は期間内の日付にしてください")
        return self


class XBuzzMeasureOutput(BaseModel):
    job_id: str
    status: str = Field(description="queued 等。実取得は日数分かかる(非同期)")
    poll_after_s: int = Field(default=90, description="この秒数後に x_buzz_measure_status を呼ぶ目安")
    message: str = ""


class XBuzzMeasureStatusInput(BaseModel):
    job_id: str = Field(description="x_buzz_measure が返した job_id")


class XBuzzMeasureStatusOutput(BaseModel):
    job_id: str
    status: str = Field(description="queued | running | done | failed | unknown")
    progress: dict[str, Any] | None = Field(default=None, description="{days_done, days_total}")
    daily_counts: list[dict[str, Any]] = Field(
        default_factory=list, description="[{date, count}]（done時）"
    )
    top_posts: list[XPostCard] = Field(default_factory=list, description="バズ投稿TOP（全文）")
    spike_analysis: str = Field(default="", description="山が立った日の中身分析（done時）")
    report_url: str | None = None
    s3_prefix: str | None = None
    error_code: str | None = None
    message: str = ""
    total_cost_usd: float = 0.0
