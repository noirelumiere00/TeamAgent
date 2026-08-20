"""ClientKarte Skill の入出力 Pydantic スキーマ。

クライアント単位の「カルテ」= 提案履歴・温度感推移・次アクションを時系列で束ねる。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClientKarteInput(BaseModel):
    """ClientKarte Skill の入力。"""

    client_name: str = Field(min_length=1, max_length=100, description="対象クライアント名")
    limit: int = Field(default=20, ge=1, le=50, description="束ねる FB イベントの上限")
    project_name: str | None = Field(
        default=None,
        max_length=100,
        description=(
            "案件名（分かる場合）。指定すると関連資料をこの案件名に一致するものへ絞り、"
            "0 件なら同じ顧客の別案件の資料を代わりに提案する"
        ),
    )


class KarteEvent(BaseModel):
    """カルテ上の 1 イベント (= 1 営業 FB)。時系列の 1 点。"""

    chunk_id: int
    occurred_at: str | None = Field(default=None, description="FB の日時 (modified_at)")
    deal_phase: str | None = Field(default=None, description="案件フェーズ")
    bant_score: str | None = Field(default=None, description="BANT 評価")
    channel_type: str | None = Field(default=None, description="代理店/直販")
    next_action: str | None = Field(default=None, description="次アクション")
    summary: str = Field(description="FB 本文の冒頭抜粋")
    url: str | None = Field(
        default=None,
        description=(
            "この FB の出典（元 Slack スレッド）を開く permalink。"
            "workspace が未設定など組み立てられない場合は None（推測した URL は入れない）"
        ),
    )


class ClientKarteOutput(BaseModel):
    """ClientKarte Skill の出力。"""

    client_name: str
    answer: str = Field(description="提案履歴・温度感推移・推奨アクションの合成サマリ")
    events: list[KarteEvent] = Field(default_factory=list, description="時系列イベント一覧")
    event_count: int = Field(ge=0, description="束ねた FB イベント数")
    document_count: int = Field(
        default=0,
        ge=0,
        description=(
            "この顧客について取得できた関連資料の件数（= answer 末尾の一覧の総数）。"
            "実ファイルを送った件数ではない（それは attached_count）。"
            "提案分岐（案件一致 0 件）では 1 件も同梱していなくてもここは総数が入る"
        ),
    )
    attached_count: int = Field(
        default=0, ge=0, description="実ファイルを Slack へ添付できた資料の件数"
    )
    total_cost_usd: float = Field(ge=0.0, description="この実行の概算コスト")
