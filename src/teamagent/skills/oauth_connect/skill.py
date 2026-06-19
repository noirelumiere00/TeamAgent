"""oauth_connect Skill 本体。

ユーザーが「連携」「Google連携」「接続」等と言ったとき、本人メールに紐づく
個別の OAuth 認可 URL を返す。実トークンの取得・保存は connect-web の
/oauth2/callback が担う（本 Skill は URL を生成して返すだけ）。

セキュリティ:
  - 対象は常に「呼び出した本人」。user_email は SkillContext.metadata から取得し、
    無ければ fail-closed（他人分の URL を作らない）。MCP 境界の hybrid identity が
    slack_user_id → 本人 user_email を解決して metadata に載せている前提。
  - URL の state は本人 email で HMAC 署名済み（make_state）。万一他人がリンクを
    開いて別 Google を認可しても、callback は state の email を key に保存するため
    「あなた専用」である旨を message に明記して誤共有を促さない。
"""

from __future__ import annotations

import os
from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.google_oauth_flow import OAuthConsentFlow
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.oauth_connect.schema import OAuthConnectInput, OAuthConnectOutput

logger = structlog.get_logger(__name__)


def _mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1] if local else ''}***@{domain}"


@register
class OAuthConnectSkill(BaseSkill[OAuthConnectInput, OAuthConnectOutput]):
    """本人専用の Google 連携リンクを発行する Skill（per-user・URL のみ）。"""

    name: ClassVar[str] = "oauth_connect"
    description: ClassVar[str] = (
        "ユーザーが自分の Google アカウントを連携（認可）するための"
        "本人専用リンクを発行する。『連携』『連携したい』『Google連携』『接続』『connect』"
        "等を言われたら呼ぶ。対象は常に話しかけている本人で、引数は不要"
        "（本人は MCP 境界が解決する）。返した URL を本人に提示すること。"
    )
    input_schema: ClassVar[type[BaseModel]] = OAuthConnectInput
    output_schema: ClassVar[type[BaseModel]] = OAuthConnectOutput

    def run(self, _input: OAuthConnectInput, ctx: SkillContext) -> OAuthConnectOutput:
        log = ctx.bind_logger(self.name)

        # G1: 本人 user_email 必須（fail-closed・他人分の URL を作らない）。
        requester = ctx.metadata.get("user_email")
        if not requester or not isinstance(requester, str):
            raise PermissionError("oauth_connect は本人 user_email が必須です（本人専用リンク）")
        requester = requester.strip()
        if not requester:
            raise PermissionError("本人 user_email が必須です（空不可・fail-closed）")

        redirect = os.environ.get("OAUTH_REDIRECT_URI", "").strip()
        if not redirect:
            raise ValueError("OAUTH_REDIRECT_URI が未設定です（connect-web の公開 callback URL）")

        try:
            url, _state = OAuthConsentFlow(redirect_uri=redirect).authorization_url(requester)
        except Exception as e:
            log.warning("oauth_connect_url_failed", error=type(e).__name__)
            raise ValueError(
                "連携リンクの生成に失敗しました（管理者へ: OAuth 系 env をご確認ください）"
            ) from e

        masked = _mask_email(requester)
        message = (
            f"👋 *{requester}* の Google を連携します（1回だけ・所要1分）。\n"
            "下のリンクは *あなた専用* です（他の人と共有しないでください）。\n"
            "開いて、表示される権限（メールの読み取り・下書き作成、カレンダー等）を"
            "*許可* してください:\n"
            f"{url}\n\n"
            "「✅ 連携が完了しました」が出れば成功です。あとは話しかけるだけ。"
        )
        log.info("oauth_connect_url_issued", user_email_masked=masked)
        return OAuthConnectOutput(url=url, user_email_masked=masked, message=message)
