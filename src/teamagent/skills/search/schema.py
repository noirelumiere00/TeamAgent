"""検索 Skill の入出力 Pydantic スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchInput(BaseModel):
    """検索 Skill の入力。"""

    query: str = Field(min_length=1, max_length=1000, description="自然文クエリ")
    top_k: int = Field(default=5, ge=1, le=50, description="返す上位件数")
    filter_industry: str | None = Field(
        default=None,
        max_length=100,
        description="業界フィルタ（メタデータ JSONB 経由）",
    )


class SearchHitOut(BaseModel):
    """検索結果の1ヒット。

    引用フォーマット強化（Sprint 2 / 2.8）：
    - `source` は表示用のフォールバック文字列（後方互換）
    - `file_name` / `page_num` を別フィールドで持ち、Block Kit で構造化表示する
    - `score` は cosine 類似度（0.0〜1.0）
    """

    chunk_id: int
    content: str
    score: float = Field(ge=0.0, le=1.0, description="cosine 類似度（1.0 に近いほど類似）")
    source: str | None = Field(
        default=None,
        description=(
            "表示用フォールバック（例：'a.pdf (p.3)'）。"
            "新規実装では file_name / page_num を優先する"
        ),
    )
    file_name: str | None = Field(
        default=None,
        description="元 PDF のファイル名（Block Kit で太字表示）",
    )
    page_num: int | None = Field(
        default=None,
        ge=1,
        description="元 PDF のページ番号（1 始まり）",
    )
    drive_url: str | None = Field(
        default=None,
        description="Google Drive 等の正本 URL。営業がクリックして元 PDF を開く",
    )
    source_uri: str | None = Field(
        default=None,
        description=(
            "元データの URI（新スキーマ）。'slack://CHANNEL_ID/THREAD_TS' / 'gdrive://FILE_ID' 等"
        ),
    )
    source_type: str | None = Field(
        default=None,
        description="ソース種別（'slack' / 'pdf' / 'gdrive' 等）。新スキーマ用",
    )
    channel_name: str | None = Field(
        default=None,
        description="Slack チャネル名（source_type='slack' の場合に設定）",
    )


class SearchOutput(BaseModel):
    """検索 Skill の出力。"""

    answer: str = Field(description="Claude による要約（引用付き）")
    hits: list[SearchHitOut] = Field(default_factory=list, description="検索ヒット一覧")
    total_cost_usd: float = Field(ge=0.0, description="この検索実行の概算コスト")
