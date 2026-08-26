"""digest_ack の入出力スキーマ。

入力は署名トークン 1 本だけ。個別確認・一括確認・取り消しの区別はトークンの
署名済み payload が持つので、呼び出し側（LLM）が種別を決められる引数は置かない
（種別を引数にすると、LLM が「取り消し」を「確認済み」へ読み替える経路ができる）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DigestAckInput(BaseModel):
    ack_token: str = Field(
        default="",
        max_length=2000,
        description=(
            "押されたボタンの value（HMAC 署名トークン）。**そのまま渡す**こと。"
            "中身を解釈・要約・再構成してはいけない（生 ID は入っていない）"
        ),
    )


class DigestAckOutput(BaseModel):
    acked: int = Field(default=0, ge=0, description="確認済みにした件数")
    unacked: int = Field(default=0, ge=0, description="取り消した件数")
    undo_token: str = Field(
        default="",
        max_length=2000,
        description=(
            "取り消し用の署名トークン（ack 成功時のみ非空・有効 1 時間）。"
            "押下直後の ephemeral に『↩︎ 取り消す』ボタンとして載せる"
        ),
    )
    error: str = Field(
        default="",
        max_length=32,
        description='"" / "no_input" / "expired" / "store_failed"',
    )
    message: str = Field(default="", max_length=200, description="本人へ返す案内文")
