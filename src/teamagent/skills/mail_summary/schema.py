"""mail_summary Skill の I/O スキーマ（Pydantic v2）。

⚠️ 戻り値に生メール本文・生 From・生 messageId を含めないこと（G3）。要約は LLM 生成文、
件名は DLP マスク後・短縮、相手はマスク。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MailSummaryInput(BaseModel):
    """本人受信箱の要約入力。

    G5（無差別走査の禁止）は「client_name が必須」ではなく **client_name_guard の
    意味検査**で守る。min_length=1 を外したのは、外側ルーターに「顧客が分からない時は
    空で呼ぶ」という**正直な選択肢**を与えるため。required のままだと
    ``client_name="今日のメール"`` のような依頼文の断片が必ず詰められ、Gmail の完全一致
    フレーズ検索が 0 件になって「連携が壊れた」ように見えていた（P0-2 の実測症状）。
    空・断片のいずれも run() 冒頭のガードが受信箱を 1 回も叩かずに案内文へ落とす。
    """

    client_name: str = Field(
        default="",
        max_length=100,
        examples=["花王", "アサヒ飲料", "森ビル"],
        description=(
            "対象クライアント/案件名。**顧客名・案件名だけ**を入れる。"
            "『今日のメール』『返信必要』『今週の空き時間』のような"
            "依頼文の断片・期間・状態は入れない。"
            "顧客が特定できない依頼では**空のまま**呼ぶ（サーバが正しい案内を返す）。"
        ),
    )
    lookback_days: int = Field(default=14, ge=1, le=90, description="遡る期間（日）")
    max_messages: int = Field(
        default=15, ge=1, le=40, description="走査する最大メール数（コスト/レイテンシ上限）"
    )


class MailHighlight(BaseModel):
    """要約に含める受信メール 1 件のメタ（DLP マスク後・本文は含まない）。"""

    counterpart_masked: str = Field(description="相手アドレスのマスク表示")
    subject_scrubbed: str = Field(default="", max_length=80, description="件名（マスク後・短縮）")
    occurred_at: str | None = Field(default=None, description="受信日時（ISO・JST +09:00・判明時）")
    occurred_at_display: str | None = Field(
        default=None,
        description=(
            "受信日時のJST表示（例 08/13(木) 19:00）。"
            "表示にはこの文字列をそのまま使い、ISO から時刻・曜日を再計算しないこと"
        ),
    )


class MailSummaryOutput(BaseModel):
    """要約結果。生本文・生件名・生 From は一切含まない。"""

    client_name: str
    summary: str = Field(default="", description="LLM による横断要約（DLP マスク後の本文に基づく）")
    highlights: list[MailHighlight] = Field(default_factory=list)
    scanned_count: int = Field(ge=0, description="走査したメール数")
    inbox_owner_masked: str = Field(default="", description="参照した受信箱（マスク）")
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="この要約の概算コスト")
    error: str = Field(
        default="",
        description=(
            "決定論コード（空文字＝正常に結果あり）。"
            "'client_name_structural'=client_name が依頼文の断片 / "
            "'client_name_missing'=client_name が空 / "
            "'not_connected'=Google 未連携 / "
            "'reauth_needed'=認証情報の再取得が必要（失効・スコープ不足を含む） / "
            "'gmail_api_failed'=受信箱の検索に失敗（**0 件という意味ではない**） / "
            "'no_hits'=検索したが 0 件 / 'bulk_only'=ヒットしたが全件一斉配信で要約対象外"
        ),
    )
    message: str = Field(
        default="",
        description=(
            "利用者へそのまま出せる案内文（error が空でない時のみ。正常時は空文字）。"
            "**この文言をそのまま伝えること**。0 件の理由を推測して補わない"
        ),
    )
    connection: str = Field(
        default="",
        description=(
            "メール連携の状態。'live'=実際に受信箱を検索した（0 件でも連携は正常）/ "
            "'ok'=連携は解決済みだが検索していない（client_name ガードで停止）/ "
            "空文字=連携できていない（error=not_connected / reauth_needed）。"
            "**'live' や 'ok' が入っているのに『連携が未完了かもしれません』と言わないこと**"
        ),
    )
