"""mail_draft Skill の I/O スキーマ（Pydantic v2）。

⚠️ draft_token は HMAC 署名トークン（生 thread_id を含まない）。open_url は本人の Gmail
リンク（本人へ ephemeral 返す用）。生本文・生 messageId は戻り値/ログに出さない（G3）。

入口は 2 つある（どちらも「本人が明示的に選んだ 1 件」に対してだけ下書きを作る）:
  1. ``draft_token``: 朝ダイジェストの「✏️ 下書きを作成」ボタン押下（署名トークン）
  2. ``selection``: mail_followup が出した候補一覧に対する本人の返事（「1番で」等）
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MailDraftInput(BaseModel):
    """下書き作成の入力。ボタン押下（draft_token）か一覧からの選択（selection）のどちらか。"""

    draft_token: str = Field(
        default="",
        max_length=400,
        description="『下書きを作成』ボタンの value（HMAC署名トークン・生thread_id非露出）",
    )
    selection: str = Field(
        default="",
        max_length=200,
        description=(
            "mail_followup の候補一覧に対する**利用者の返事をそのまま**入れる"
            "（例『1番で』『1と3、丁寧めで』『電通の件』）。"
            "**要約・番号への言い換えをしないこと**（曖昧さの判定はサーバが行う）"
        ),
    )
    candidate_refs: list[str] = Field(
        default_factory=list,
        max_length=10,
        description=(
            "直前に提示した候補の evidence_ref を**表示順のまま**並べる"
            "（mail_followup の items[].evidence_ref をそのままコピーする）。"
            "**番号（『1番で』等）で選ばれたときは必須**。"
            "これが無いと番号がどの件を指すか確認できないため、"
            "下書きを作らずに候補を出し直す（error='ambiguous_selection'）"
        ),
    )
    instructions: str | None = Field(
        default=None,
        max_length=500,
        description="下書きへの指示（トーン・盛り込みたい点）。利用者が言った範囲だけを入れる",
    )
    lookback_days: int = Field(
        default=14,
        ge=1,
        le=90,
        description=(
            "候補一覧を作ったときと**同じ遡り日数**。"
            "mail_followup の戻り値 lookback_days（＝実際に遡った日数）があれば"
            "**その値をそのまま渡す**。窓が狭いと一覧に出ていた件が見つからず"
            "『見つからなくなっていました』と事実と異なる説明になる"
        ),
    )
    idle_days: int | None = Field(
        default=None,
        ge=1,
        le=90,
        description=(
            "候補一覧を作ったときと同じ放置日数の下限（mail_followup に idle_days を"
            "渡したときは**同じ値を渡す**）。一覧と選択で条件がずれると番号が別の件を指す"
        ),
    )


class DraftedMail(BaseModel):
    """作成した下書き 1 件（本人にだけ ephemeral 表示する素材）。"""

    label: str = Field(
        default="", description="どの候補に対する下書きか（差出人「件名」・放置日数）"
    )
    to_display: str = Field(default="", description="返信先アドレス（本人確認用）")
    subject: str = Field(default="", description="下書きの件名")
    body: str = Field(default="", description="下書き本文（AI 生成・本人が確認して送信する）")
    gmail_draft_id: str = Field(default="", description="作成された Gmail 下書きの ID")
    open_url: str = Field(default="", description="Gmail で開くリンク（原文のまま併記する）")


class MailDraftOutput(BaseModel):
    """下書き作成結果（送信はしない＝Gmail 下書き保存のみ）。"""

    created: bool = Field(default=False, description="新規に下書きを作成できたか")
    already: bool = Field(default=False, description="既に下書きがあった（冪等スキップ）")
    error: str = Field(
        default="",
        description=(
            "失敗種別（無ければ空）。"
            "'ambiguous_selection'=どれを指しているか確定できなかった"
            "（**下書きは作っていない**。message をそのまま出し、次は本出力の "
            "candidate_refs を渡して選び直してもらう）/ "
            "'vanished_selection'=選ばれた番号の件が受信箱で見つからなくなっていた"
            "（**別の件へ繰り上げていない**）/ "
            "'no_selection'=draft_token も selection も無い / "
            "'no_candidates'=候補が無い / "
            "expired/quota/not_connected/reauth_needed/gmail_api_failed 等"
        ),
    )
    candidate_refs: list[str] = Field(
        default_factory=list,
        description=(
            "聞き返し（ambiguous_selection）で出し直した一覧に対応する evidence_ref。"
            "次に mail_draft を呼ぶときは**これをそのまま candidate_refs に渡す**"
            "（番号の指し先を message の一覧と一致させるため）"
        ),
    )
    open_url: str = Field(
        default="", description="Gmail でその案件スレッド/下書きを開くリンク（本人確認用）"
    )
    drafts: list[DraftedMail] = Field(
        default_factory=list,
        description=(
            "作成した下書き（selection 経路。本文とリンクを**そのまま**本人に見せる）。"
            "ボタン押下経路では空"
        ),
    )
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="この生成の概算コスト")
    message: str = Field(default="", description="本人へ返す案内文（成功/失敗）")
