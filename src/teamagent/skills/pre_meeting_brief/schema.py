"""pre_meeting_brief の I/O スキーマ（Pydantic v2）。

⚠️ 情報最小化の契約（テストで固定）:
  - **生 ``description`` のフィールドを作らない**（作った瞬間に第三者の自由文が
    ログ・Slack・Scheduler へ乗る経路ができる）
  - 参加者は **ドメインのみ**（ローカル部を持たない）
  - 表示用 ``*_display`` には必ず ``*_scrubbed`` の対を置く（``morning_digest/schema.py``
    の既存規約と同じ。ログ・監査には scrubbed 側だけを出す）
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PreMeetingBriefInput(BaseModel):
    """事例ブリーフの入力。

    定期便（朝ダイジェスト）は ``relative_day="today"`` で呼ぶ。on-demand の
    「◯◯の事例出して」はカレンダーを介さず ``client`` 直指定で引ける（取りこぼし・
    誤爆の自力回復口）。
    """

    relative_day: str = Field(
        default="today",
        max_length=10,
        description='対象日: "today" / "tomorrow" / "YYYY-MM-DD"',
    )
    client: str | None = Field(
        default=None,
        max_length=80,
        description="企業名の直接指定（カレンダーを介さない引き当て）。未指定なら当日予定から",
    )
    max_meetings: int = Field(default=5, ge=1, le=12, description="ブリーフに載せる MTG 上限")
    max_cases: int = Field(default=3, ge=1, le=5, description="1 MTG あたりの事例上限")


class CaseRef(BaseModel):
    """引き当てた事例 1 件（マスター表の構造化列だけから組む）。

    ⚠️ pptx の先頭 chunk を使わないこと（chunk 0 は表紙スライドのテキストで、効果は
    中盤にある）。``effect_display`` が空なら固定文言に落とす＝本文を混入させない。
    """

    company_display: str = Field(default="", max_length=60, description="事例の企業名（表示）")
    company_scrubbed: str = Field(default="", max_length=60, description="同（マスク・ログ用）")
    product_display: str = Field(default="", max_length=60, description="商材/施策名（表示）")
    product_scrubbed: str = Field(default="", max_length=60, description="同（マスク・ログ用）")
    industry_display: str = Field(default="", max_length=40, description="業種（表示）")
    effect_display: str = Field(default="", max_length=160, description="効果の一文（表示）")
    effect_scrubbed: str = Field(default="", max_length=160, description="同（マスク・ログ用）")
    owner_display: str = Field(default="", max_length=40, description="社内担当（表示）")
    external_use: str = Field(
        default="unknown", max_length=8, description="対外利用可否: ok / ng / unknown"
    )
    external_use_note: str = Field(
        default="", max_length=60, description="注記の表示文言（スクラブ済み）"
    )
    source_title: str = Field(default="", max_length=120, description="出典資料名（出典節用）")
    source_uri: str = Field(
        default="",
        max_length=600,
        description="出典 URL。**必ず source_uri の実値**（文字列連結で作らない）",
    )
    match_stage: int = Field(
        default=0, ge=0, le=4, description="1=完全一致 2=部分一致 3=同業種AND商材 4=同業種のみ"
    )
    client_group: str = Field(
        default="", max_length=40, description="複数クライアント時の系統（[○○系] の中身）"
    )
    same_client: bool = Field(default=False, description="MTG の取引先と同一クライアントか")


class PreMeetingBriefItem(BaseModel):
    """社外 MTG 1 件ぶんのブリーフ。"""

    start_at: str | None = Field(default=None, description="開始（ISO）")
    end_at: str | None = Field(default=None, description="終了（ISO）")
    title_display: str = Field(default="", max_length=120, description="予定名（表示・無害化済み）")
    title_scrubbed: str = Field(default="", max_length=120, description="同（マスク・ログ用）")
    verdict: str = Field(default="external", max_length=12, description="external / uncertain")
    clients_display: list[str] = Field(
        default_factory=list, description="取引先（表示・最大2社・無害化済み）"
    )
    clients_scrubbed: list[str] = Field(default_factory=list, description="同（マスク・ログ用）")
    client_industries: list[str] = Field(
        default_factory=list, description="取引先の業種（clients_display と同じ並び・不明は空文字）"
    )
    agency_display: str = Field(
        default="", max_length=60, description="代理店（担当者名まで・表示・無害化済み）"
    )
    agency_scrubbed: str = Field(default="", max_length=60, description="同（マスク・ログ用）")
    attendee_domains: list[str] = Field(
        default_factory=list, max_length=10, description="参加者ドメイン（ローカル部は持たない）"
    )
    cases: list[CaseRef] = Field(default_factory=list, description="引き当てた事例")
    no_exact_note: str = Field(
        default="",
        max_length=160,
        description="完全一致が無いときの ※行（黙って同業種事例だけ出さないため）",
    )


class PreMeetingBriefOutput(BaseModel):
    """事例ブリーフの結果。"""

    items: list[PreMeetingBriefItem] = Field(default_factory=list)
    date: str = Field(default="", max_length=10, description="対象日（JST・YYYY-MM-DD）")
    scanned: bool = Field(
        default=False,
        description=(
            "実際に走査できたか。False は「見ていない」であって「社外MTGが 0 件だった」"
            "ではない（描画側はこれを見ずに『社外MTGなし』と書いてはいけない）"
        ),
    )
    corpus_available: bool = Field(
        default=False,
        description=(
            "事例集（case_corpus）が金庫に取り込まれているか。False のときは"
            "**節そのものを出さない**（「できません」を毎朝配信しない）"
        ),
    )
    external_count: int = Field(default=0, ge=0, description="社外と判定した件数")
    uncertain_count: int = Field(default=0, ge=0, description="社外か要確認と判定した件数")
    source_lines: list[str] = Field(
        default_factory=list, max_length=6, description="— 出典 — 節に出す行（重複排除済み）"
    )
    message: str = Field(
        default="",
        max_length=200,
        description="on-demand 経路でそのまま返す短文（面ガードで伏せたときの定型文もここ）",
    )
    errors: list[str] = Field(default_factory=list, description="部分失敗の構造化メッセージ")


__all__ = [
    "CaseRef",
    "PreMeetingBriefInput",
    "PreMeetingBriefItem",
    "PreMeetingBriefOutput",
]
