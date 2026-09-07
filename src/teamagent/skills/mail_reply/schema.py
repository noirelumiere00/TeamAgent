"""mail_reply Skill の I/O スキーマ（Pydantic v2）。

⚠️ 生メール本文・生 messageId は戻り値/ログに出さない（G3）。draft_body は AI 生成文なので返す。
to_display（返信先）は本人の取引相手＝本人にだけ Slack 表示する（ログではマスク）。

## スレッドの取り違え防止（2026-09-07 本番実測）

同じ話題（例: 日本教育財団）のスレッドが複数あるとき、従来は「検索で最新の 1 通」に
返信下書きを作っていた。利用者が件名【…】や差出人（ベクトル徳野）で指したのに別スレッド
（石川さん・クオラス経由）へ下書きが出来た。対策:

* 手がかり（``subject_contains`` / ``from_contains`` / ``received_after``）で候補を絞る。
* 絞っても 2 件以上なら **下書きを作らず** ``ambiguous_threads`` を返して番号で選んでもらう。
  選ばれたら ``thread_id`` を指定して呼び直す。
* 誤って作った下書きは ``discard_draft_id`` で **Aico 自身が削除**してから作り直せる
  （削除できるのは TeamAgent 製の下書きだけ・送信は一切しない）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MailReplyInput(BaseModel):
    """返信ドラフト生成の入力。G5: 対象メールは client_name か target_message_id で必ず絞る。

    ``client_name`` の required を外したのは、**返信先が既に確定している呼び出し**
    （``target_message_id`` 指定＝一覧から本人が選んだ 1 件）で、顧客名を捏造して
    埋める以外に呼びようが無かったため。空のまま ``target_message_id`` 無しで呼べば、
    従来どおり client_name_guard が受信箱を 1 度も叩かずに案内文へ落とす
    （ただし ``subject_contains`` / ``from_contains`` のどちらかがあれば、それを鍵に検索する）。
    """

    client_name: str = Field(
        default="",
        max_length=100,
        description=(
            "返信対象を探すクライアント/案件名（会社名・案件名）。"
            "target_message_id / thread_id で対象を明示するときは空でよい"
        ),
    )
    instructions: str | None = Field(
        default=None, max_length=500, description="返信の方針・盛り込みたい点（任意・トーン等）"
    )
    lookback_days: int = Field(default=30, ge=1, le=90, description="対象メールを探す期間（日）")
    target_message_id: str | None = Field(
        default=None, description="返信対象メールを明示指定する場合の messageId（任意）"
    )
    subject_contains: str = Field(
        default="",
        max_length=200,
        description=(
            "利用者が件名で指したときの手がかり。【…】や「…の件」の中身をそのまま入れる"
            "（例:「日本教育財団様_PR関連のご提案について」）。件名に含まれる語で候補を絞る"
        ),
    )
    from_contains: str = Field(
        default="",
        max_length=200,
        description=(
            "利用者が差出人で指したときの手がかり。氏名（姓）かメールアドレスの一部"
            "（例:「徳野」「tokuno@」）。会社名は client_name に入れる"
        ),
    )
    received_after: str = Field(
        default="",
        pattern=r"^(\d{4}-\d{2}-\d{2})?$",
        description="この日付以降に届いたメールに限る（YYYY-MM-DD・任意）",
    )
    thread_id: str = Field(
        default="",
        max_length=64,
        description=(
            "ambiguous_threads から利用者が番号で選んだ候補の thread_id を**そのまま**入れる"
            "（自分で作らない・利用者には見せない）。指定時は検索せずそのスレッドに作る"
        ),
    )
    discard_draft_id: str = Field(
        default="",
        max_length=128,
        description=(
            "利用者が「それじゃない」と言ったとき、直前の結果の gmail_draft_id を入れる。"
            "その下書きを削除してから正しいスレッドで作り直す（削除できるのは本ツールが"
            "作った下書きだけ・送信はしない）"
        ),
    )


class ReplyThreadCandidate(BaseModel):
    """返信先の候補スレッド 1 件（本人にだけ表示・本文は Gmail の抜粋を 1 行マスク済み）。"""

    number: int = Field(ge=1, description="一覧の番号（利用者にはこの番号で選んでもらう）")
    thread_id: str = Field(
        description="選ばれたら thread_id 引数へそのまま渡す（利用者には見せない）"
    )
    subject: str = Field(default="", description="件名（マスク・短縮済み）")
    from_display: str = Field(default="", description="差出人（表示名またはアドレス・マスク済み）")
    received_at: str = Field(default="", description="最後に届いた日時（JST）")
    preview: str = Field(default="", description="冒頭 1 行（Gmail の抜粋・マスク・短縮済み）")


class MailReplyOutput(BaseModel):
    """返信ドラフト結果。送信はしない（Gmail 下書き保存のみ）。"""

    client_name: str
    created: bool = Field(description="Gmail 下書きを作成できたか")
    to_display: str = Field(
        default="",
        description="返信先アドレス（本人確認用。結果は本人へ ephemeral 配信され他者に出さない）",
    )
    draft_subject: str = Field(default="", description="生成した下書きの件名")
    draft_body: str = Field(
        default="", description="生成した下書き本文（AI 生成・本人がGmailで確認→送信）"
    )
    gmail_draft_id: str = Field(
        default="",
        description=(
            "作成された Gmail 下書きの ID。利用者が「それじゃない」と言ったら"
            "次の呼び出しの discard_draft_id に入れる（利用者には見せない）"
        ),
    )
    thread_id: str = Field(
        default="", description="下書きを作ったスレッドの ID（内部値・利用者には見せない）"
    )
    open_url: str = Field(
        default="",
        description=(
            "その下書きを Gmail で開くリンク（スレッド直リンク）。"
            "**リンクは原文のまま本人へ併記すること**"
        ),
    )
    note: str = Field(default="", description="但し書き（送信していない 等）")
    error: str = Field(
        default="",
        description=(
            "決定論コード。'ambiguous_threads'＝候補が複数で**下書きは作っていない**"
            "（ambiguous_threads を番号付きで見せて選んでもらい、thread_id 指定で呼び直す）"
        ),
    )
    ambiguous_threads: list[ReplyThreadCandidate] = Field(
        default_factory=list,
        description="候補スレッド（最大 3 件・表示順）。空でなければ下書きは未作成",
    )
    discarded_draft_id: str = Field(
        default="", description="discard_draft_id で実際に削除した下書きの ID（削除できた時のみ）"
    )
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="この生成の概算コスト")
