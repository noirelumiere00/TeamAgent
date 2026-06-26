"""mail_draft Skill の I/O スキーマ（Pydantic v2）。

⚠️ draft_token は HMAC 署名トークン（生 thread_id を含まない）。open_url は本人の Gmail
リンク（本人へ ephemeral 返す用）。生本文・生 messageId は戻り値/ログに出さない（G3）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MailDraftInput(BaseModel):
    """『下書きを作成』ボタン押下の入力。value(署名トークン)を渡す。"""

    draft_token: str = Field(
        min_length=1,
        max_length=400,
        description="『下書きを作成』ボタンの value（HMAC署名トークン・生thread_id非露出）",
    )


class MailDraftOutput(BaseModel):
    """下書き作成結果（送信はしない＝Gmail 下書き保存のみ）。"""

    created: bool = Field(default=False, description="新規に下書きを作成できたか")
    already: bool = Field(default=False, description="既に下書きがあった（冪等スキップ）")
    error: str = Field(
        default="", description="失敗種別（expired/quota/not_connected 等・無ければ空）"
    )
    open_url: str = Field(
        default="", description="Gmail でその案件スレッド/下書きを開くリンク（本人確認用）"
    )
    message: str = Field(default="", description="本人へ返す案内文（成功/失敗）")
