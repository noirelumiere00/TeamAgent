"""web_research Skill の I/O スキーマ（Pydantic v2）。

read-only（公開Webの検索と要約のみ・書込 API も直 fetch も無い）。message は LLM が
そのまま返す決定的日本語文で、**出典の番号・URL はサーバが groundingMetadata から機械的に
組んだもの**。エージェントに出典を再構成させない（URL ねつ造事故の構造的な封じ込め）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WebResearchInput(BaseModel):
    """公開Webの市場リサーチ依頼。"""

    query: str = Field(
        min_length=1,
        max_length=200,
        description="調べたいこと（例『2026年 国内 ショート動画広告 市場規模』）。"
        "社内資料・案件名・顧客名は入れない（外部検索サービスへ送信されるため）。",
    )
    max_results: int = Field(
        default=5, ge=1, le=8, description="message に載せる出典の最大数（1〜8・既定5）"
    )
    recency_days: int = Field(
        default=0,
        ge=0,
        le=365,
        description="直近何日の情報を優先するか（0〜365・既定0＝期間指定なし）。"
        "Google 検索の after: 演算子でサーバがクエリに機械付与するベストエフォート。",
    )


class WebSource(BaseModel):
    """出典 1 件（すべてサーバが groundingMetadata から機械的に組む）。"""

    index: int = Field(ge=1, description="message 中の [n] と対応する 1 始まりの通し番号")
    title: str = Field(default="", description="ページタイトル（無害化済み・URL は含まない）")
    url: str = Field(default="", description="https のみ・検証済みの出典 URL")
    domain: str = Field(default="", description="url から機械的に導いたホスト名")


class WebResearchOutput(BaseModel):
    """Web リサーチ結果（読み取りのみ）。"""

    query: str = Field(default="", description="実際に検索へ渡したクエリ（無害化済み）")
    sources: list[WebSource] = Field(
        default_factory=list, description="番号付き出典（message の [n] と一致）"
    )
    error: str = Field(
        default="",
        description="失敗種別（not_grounded/search_failed/rollout_denied 等・無ければ空）",
    )
    message: str = Field(
        default="",
        description="LLM がそのまま返す決定的日本語文（要約＋番号付き出典）。"
        "言い換え・出典の付け替え・URL の再構成をしないこと。",
    )
