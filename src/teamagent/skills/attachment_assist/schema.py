"""attachment_assist Skill の I/O スキーマ（Pydantic v2）。

⚠️ **入力に channel / user / file_id / URL を持たせない**。読む対象は
「署名済み caller claim 由来の会話（``ctx.metadata`` の channel_id / thread_ts）」に
添付されたファイルだけで、LLM 申告で対象を移動できないことを **スキーマの形**で保証する。

例外は ``permalink``（Slack 投稿リンク・``ATTACHMENT_PERMALINK_ENABLED`` が ON のときだけ有効）。
リンク先の投稿は **依頼者本人の xoxp** で読むので、本人が見られない投稿へは移動できない
（Slack API が本人の可視範囲を強制する）。OFF のときは何も読まずに ``permalink_disabled``
（会話内の別の添付をリンク先と取り違えて要約しないため）。permalink が空なら従来どおり。

``message`` は LLM がそのまま返す決定的日本語文（言い換え・再要約をさせない）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# mode 一覧（skill 側の分岐と 1 箇所で対応させる）。
ATTACHMENT_MODES = ("summary", "revise", "minutes", "aggregate", "translate")

AttachmentMode = Literal["summary", "revise", "minutes", "aggregate", "translate"]


class AttachmentAssistInput(BaseModel):
    """会話に添付されたファイルの読取・加工の入力。

    対象ファイルは「いま話しているスレッド / チャンネル」の添付から選ぶ。
    ファイル ID・URL・チャンネル ID を **受け付けない**（会話外は構造的に読めない）。
    唯一の例外 ``permalink`` は本人の xoxp で読む（本人の可視範囲を超えない）。
    """

    mode: AttachmentMode = Field(
        default="summary",
        description=(
            "処理の種類。summary=要約 / revise=修正案 / minutes=議事録フォーマット化 / "
            "aggregate=集計の概観 / translate=英訳。"
        ),
    )
    instruction: str = Field(
        default="",
        max_length=1000,
        description="依頼者の具体的な要望（例『先方向けに3行で』『敬体に直して』）。省略可。",
    )
    file_name: str = Field(
        default="",
        max_length=255,
        description=(
            "対象ファイル名（会話に複数添付があるときの指定用・部分一致可）。"
            "省略時は会話内の最新の対応ファイルを使う。"
        ),
    )
    permalink: str = Field(
        default="",
        max_length=500,
        description=(
            "Slack 投稿のリンク（https://<ws>.slack.com/archives/…）の先の**添付ファイル**を"
            "読む・添付してと頼まれたときだけ渡す（スレッドの要約は slack_summary）。"
        ),
    )


class AttachmentAssistOutput(BaseModel):
    """読取・加工の結果（テキスト返答のみ。ファイル生成・配信は行わない）。"""

    file_name: str = Field(default="", description="実際に処理したファイル名")
    kind: str = Field(default="", description="判定した種別（pdf/docx/pptx/xlsx/text）")
    pages: int = Field(default=0, ge=0, description="抽出できたページ/スライド/シート数")
    chars: int = Field(default=0, ge=0, description="LLM へ渡した本文の文字数（cap 後）")
    truncated: bool = Field(
        default=False, description="入力上限で本文を途中までしか処理していないなら True"
    )
    mode: str = Field(default="", description="実行した mode")
    other_files: list[str] = Field(
        default_factory=list,
        description="同じ会話にあった他の対応ファイル名（file_name で指定し直せる）",
    )
    error: str = Field(
        default="",
        description=(
            "失敗種別（no_attachment/external_file/too_large/unsupported_type/"
            "download_failed/extract_failed/empty_text/llm_failed/no_conversation/"
            "bad_permalink/other_workspace/not_found/read_failed/not_connected/"
            "bot_cannot_fetch/permalink_disabled・"
            "無ければ空）"
        ),
    )
    message: str = Field(
        default="",
        description="LLM がそのまま返す決定的日本語文（言い換え・再要約・追記をしないこと）",
    )
    attached: bool = Field(
        default=False,
        description="投稿リンク経路で元ファイルをこの会話に添付し直せたら True",
    )
    total_cost_usd: float = Field(default=0.0, ge=0.0, description="Bedrock 実費")
