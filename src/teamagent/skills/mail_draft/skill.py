"""mail_draft Skill 本体 — 朝ダイジェストの「✏️ 下書きを作成」ボタン押下を処理する。

経路: Slack のボタン押下 → OpenClaw(socket) が system event としてエージェントへ転送 →
SOUL 指示でエージェントが本ツールを呼ぶ（value=署名トークンを draft_token に渡す）。
本ツールは HMAC 署名トークンを検証し、その案件スレッドの **Reply-All 返信下書き**を
本人の Gmail に作成（送信しない）、Gmail で開くリンクを返す。

旧 Python Bolt worker（slack_bot.py の @app.action("mail_draft")）と同じ判断ロジックを、
OpenClaw から呼べる MCP ツールとして再構成したもの（生成本体は morning_digest skill を再利用）。

⚠️ 死守ライン:
  G1 本人受信箱限定（user_email→token, fail-closed）。未連携は error で案内。
  トークン検証: 署名・所有者照合・失効を decode_draft_token が担保（fail-closed）。
  G4' 書込は drafts.create のみ（送信は denylist 物理封鎖）。連打/コスト対策に 1人10件/日。
  G3 生 thread_id/件名/本文は value・戻り値・ログに出さない（token と open_url のみ）。
"""

from __future__ import annotations

import datetime as _dt
from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.oauth_token_store import TokenStore
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.mail_draft.schema import MailDraftInput, MailDraftOutput
from teamagent.skills.morning_digest.draft_token import decode_draft_token

logger = structlog.get_logger(__name__)

# generate_draft_for_thread の error → 本人向け案内文（ephemeral）。
_ERR_MSG: dict[str, str] = {
    "expired": "このボタンは無効です（期限切れ/不正）。最新のダイジェストから操作してください。",
    "quota": "本日の下書き作成上限（10件/日）に達しました。明日また利用できます。",
    "not_connected": "下書き作成には Google の連携が必要です"
    "（@AiLa に『連携』と話しかけて許可してください）。",
    "reauth_needed": "下書き作成には Google の再連携が必要です"
    "（下書き作成権限を許可してください）。",
    "not_addressed": "このスレッドはご本人宛（To）ではないため下書きは作成しません。",
    "thread_gone": "対象のスレッドが見つかりませんでした。",
    "thread_error": "スレッドの取得に失敗しました。時間をおいて再度お試しください。",
    "invalid_thread": "ボタンの情報が不正です。最新のダイジェストから操作してください。",
    "not_draftable": "下書きを作成できませんでした（返信先不明/一斉送信 等）。",
}
_OK_MSG = (
    "✅ 返信下書きを作成しました（未送信・Slackでは送信しません）。"
    "Gmail で確認して送信してください。"
)
_ALREADY_MSG = "✏️ この案件は既に下書きがあります。Gmail で確認してください。"


@register
class MailDraftSkill(BaseSkill[MailDraftInput, MailDraftOutput]):
    """『下書きを作成』押下→当該スレッドへ Reply-All 下書きを作る Skill（送信は人間）。"""

    name: ClassVar[str] = "mail_draft"
    description: ClassVar[str] = (
        "朝ダイジェストの『✏️ 下書きを作成』ボタン押下を処理するツール。"
        "Slack の interaction で action='mail_draft' を受け取ったら、その value（署名トークン）を "
        "draft_token に渡して呼ぶ。本人受信箱の当該スレッドへ Reply-All の返信下書きを作成し"
        "（送信はしない）、Gmail で開くリンク(open_url)と案内文(message)を返す。"
        "呼び出し時は arguments に `_user_context: {slack_user_id: '<押した本人のuser_id>'}` を"
        "必ず含める（本人解決鍵）。"
    )
    input_schema: ClassVar[type[BaseModel]] = MailDraftInput
    output_schema: ClassVar[type[BaseModel]] = MailDraftOutput

    _QUOTA_LIMIT: ClassVar[int] = 10

    def __init__(self, token_store: TokenStore | None = None) -> None:
        self._token_store = token_store
        # 1人1日あたりの上限カウンタ（MCP プロセス常駐の in-memory）。
        self._counts: dict[str, tuple[str, int]] = {}

    def run(self, input: MailDraftInput, ctx: SkillContext) -> MailDraftOutput:
        log = ctx.bind_logger(self.name)

        # G1: 本人受信箱限定（fail-closed）。MCP 外殻が slack_user_id→email を解決して注入。
        requester = str(ctx.metadata.get("user_email", "") or "").strip()
        if not requester:
            raise PermissionError("mail_draft は本人 user_email が必須です（本人受信箱限定）")

        # トークン検証（署名・所有者・失効）。生 thread_id はここで初めて復元（ログには出さない）。
        thread_id = decode_draft_token(input.draft_token, requester)
        if not thread_id:
            log.info("mail_draft_invalid_token")  # token 値・thread_id は出さない
            return MailDraftOutput(created=False, error="expired", message=_ERR_MSG["expired"])

        if not self._quota_ok(requester):
            log.info("mail_draft_quota_exceeded")
            return MailDraftOutput(created=False, error="quota", message=_ERR_MSG["quota"])

        # 生成本体は morning_digest skill を再利用（全文取得→anchor→Reply-All→drafts.create）。
        from teamagent.skills.morning_digest.skill import MorningDigestSkill

        skill = MorningDigestSkill(token_store=self._token_store)
        res = skill.generate_draft_for_thread(thread_id, requester, ctx)
        open_url = str(res.get("thread_url", "") or "")
        err = res.get("error")

        if res.get("created"):
            self._quota_consume(requester)
            log.info("mail_draft_created", cost_usd=float(res.get("cost_usd", 0.0) or 0.0))
            return MailDraftOutput(created=True, open_url=open_url, message=_OK_MSG)
        if res.get("already"):
            log.info("mail_draft_already")
            return MailDraftOutput(
                created=False, already=True, open_url=open_url, message=_ALREADY_MSG
            )

        key = str(err or "not_draftable")
        log.info("mail_draft_failed", err=key)  # 種別のみ（本文・宛先は出さない）
        return MailDraftOutput(
            created=False,
            error=key,
            open_url=open_url,
            message=_ERR_MSG.get(key, _ERR_MSG["not_draftable"]),
        )

    # ── 1人1日あたりの上限（連打/コスト対策・in-memory）─────────────────────
    def _quota_ok(self, email: str) -> bool:
        today = _dt.date.today().isoformat()
        day, n = self._counts.get(email, (today, 0))
        return today != day or n < self._QUOTA_LIMIT

    def _quota_consume(self, email: str) -> None:
        today = _dt.date.today().isoformat()
        day, n = self._counts.get(email, (today, 0))
        self._counts[email] = (today, (n + 1) if today == day else 1)
