"""oauth_connect Skill の I/O スキーマ（Pydantic v2）。

本人専用の Google / Slack 連携（OAuth 認可）リンクを返す。生トークンは扱わない
（URL を本人が開いて許可 → connect-web の /oauth2/callback・/slack/oauth/callback が
token を保存）。**既に連携済みのサービスはリンクを出さない**（未連携のものだけ案内）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class OAuthConnectInput(BaseModel):
    """oauth_connect の入力。

    対象は常に「呼び出した本人」（user_email は SkillContext.metadata から取得）なので
    入力パラメータは持たない。OpenClaw からは引数なしで呼べる。
    """


class OAuthConnectOutput(BaseModel):
    """oauth_connect の出力。未連携サービスの認可 URL と案内文を返す。"""

    url: str | None = Field(
        default=None,
        description=(
            "本人メールで HMAC 署名済みの Google OAuth 認可 URL。"
            "既に Google 連携済みなら None（リンクを出さない）"
        ),
    )
    slack_url: str | None = Field(
        default=None,
        description=(
            "本人メールで署名済みの Slack OAuth(user_scope) 認可 URL。"
            "既に Slack 連携済み / SLACK_OAUTH_REDIRECT_URI 未設定なら None"
        ),
    )
    user_email_masked: str = Field(description="連携対象メール（マスク表示・監査/表示用）")
    message: str = Field(
        description="Slack にそのまま出せる案内文（未連携サービスの URL のみを含む）"
    )
