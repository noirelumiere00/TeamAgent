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
    strict_industry: bool = Field(
        default=False,
        description=(
            "業界フィルタの厳密度。"
            "False (soft, 既定): industry=指定値 OR industry IS NULL を許容。"
            "Router の auto-detect で Slack docs (industry メタ無し) が全件除外されるのを防ぐ。"
            "True (strict): 厳密一致。ユーザーが明示的に業界を指定したスラッシュコマンド等で使う。"
        ),
    )
    filter_client: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "取引先/案件名フィルタ（部分一致 ILIKE）。"
            "cls_project（全資料に付く取引先）を主に "
            "client_name（FB）/ title の OR グループへ照合。"
            "表記ゆれ（例: 日本ガイシ↔NGK）を部分一致で吸収する。"
        ),
    )
    filter_doc_type: str | None = Field(
        default=None,
        max_length=50,
        description=(
            "資料種別フィルタ（cls_doc_type 等価）。"
            "提案書 / 議事録 / 報告書 / 価格表 / 契約 のいずれか。"
            "fail-open 再検索でも外れない sticky フィルタとして配線する"
            "（クエリ自動抽出 extract_knowledge_filters より優先）。"
        ),
    )
    filter_solution: str | None = Field(
        default=None,
        max_length=50,
        description=(
            "施策/ソリューション種別フィルタ（cls_solution 等価）。"
            "SNS運用 / 動画広告 / インフルエンサー / SEO / Web制作 / 広告運用 / イベント 等。"
            "fail-open 再検索でも外れない sticky フィルタとして配線する。"
        ),
    )
    filter_budget: str | None = Field(
        default=None,
        description=(
            "予算バンドフィルタ（cls_budget 等価・〜100万 / 100〜500万 / 500万〜）。"
            "fail-open 再検索でも外れない sticky フィルタとして配線する。"
        ),
    )
    include_unknown_budget: bool = Field(
        default=False,
        description=(
            "予算フィルタ時に cls_budget='不明' も含めるか（soft 化）。"
            "False (既定, strict): 指定バンドのみ。True: 指定バンド OR '不明' を許容する。"
        ),
    )
    sort_budget_near: str | None = Field(
        default=None,
        description=(
            "この予算バンドに近い順で取得後ソート（〜100万 / 100〜500万 / 500万〜）。"
            "絞らず並べ替えのみ。SEARCH_BUDGET_SORT が有効なときだけ発火する。"
        ),
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
    client_name: str | None = Field(
        default=None,
        description="クライアント名（営業 FB の構造化メタ。Slack FB hit で設定）",
    )
    deal_phase: str | None = Field(
        default=None,
        description="案件フェーズ（ヒアリング/提案/受注 等。営業 FB の構造化メタ）",
    )
    bant_score: str | None = Field(
        default=None,
        description="BANT 評価（A/B/C 等。営業 FB の構造化メタ）",
    )
    channel_type: str | None = Field(
        default=None,
        description="チャネル種別（代理店/直販 等。営業 FB の構造化メタ）",
    )
    title: str | None = Field(
        default=None,
        description="資料タイトル（Drive ファイル名 / Slack スレッド見出し等。新スキーマ）",
    )
    project: str | None = Field(
        default=None,
        description="案件名/取引先（ナレッジ自動分類 cls_project）",
    )
    industry: str | None = Field(
        default=None,
        description="業界（ナレッジ自動分類 cls_industry）",
    )
    doc_type: str | None = Field(
        default=None,
        description="資料種別（提案書/議事録/報告書 等・自動分類 cls_doc_type）",
    )
    budget: str | None = Field(
        default=None,
        description="予算バンド（〜100万/100〜500万/500万〜/不明・自動分類 cls_budget）",
    )
    is_low_confidence: bool = Field(
        default=False,
        description="低信頼ヒット（fallback しきい値で救出された borderline）。配信は控える",
    )


class SearchOutput(BaseModel):
    """検索 Skill の出力。"""

    answer: str = Field(description="Claude による要約（引用付き）")
    hits: list[SearchHitOut] = Field(default_factory=list, description="検索ヒット一覧")
    total_cost_usd: float = Field(ge=0.0, description="この検索実行の概算コスト")
