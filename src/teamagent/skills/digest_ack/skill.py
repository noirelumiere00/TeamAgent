"""digest_ack Skill 本体 — 朝ダイジェストの項目を「確認済み」にする（翌朝以降隠す）。

入口はボタン押下のみ。朝ダイジェストの「☑️ 確認済みにする」/「☑️ 全部確認した」/
押下直後の ephemeral に出る「↩︎ 取り消す」の 3 つが、いずれも同じ ``action_id``
（``digest_ack``）で本 Skill に来る。どれなのかは **署名済みトークンの ``typ``** が持つ。

⚠️ 死守ライン:
  G1 本人限定（``user_email`` 必須・fail-closed）。他人の確認状態は触れない
    （DB 側も RLS で本人行に束縛。アプリのバグだけが唯一の防壁にならないようにしている）。
  トークン検証: 署名・所有者照合・失効・形式を ``decode_ack_token`` が担保（fail-closed）。
    壊れたトークンを「たぶんこれだろう」で救済しない。
  G3 生の thread_id / channel_id は元々トークンに載っていない（載るのはハッシュ済み
    item_key）。``message`` にもログにも **件数以外は出さない**。
  黙って成功と言わない: DB 書込が 0 件なら成功文言を返さない。ここで嘘をつくと
    「押したのに翌朝また出てくる」を利用者が自力で診断できなくなる。
  外部送信ゼロ: Google API も Slack API も呼ばない。触るのは自分の ``digest_ack`` 行だけ。
"""

from __future__ import annotations

from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.skills._shared.user_context import USER_CONTEXT_RULE
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.digest_ack.schema import DigestAckInput, DigestAckOutput
from teamagent.skills.morning_digest.ack_token import decode_ack_token, encode_unack_token

logger = structlog.get_logger(__name__)

_ERR_MSG: dict[str, str] = {
    "no_input": "確認する項目が分かりませんでした。最新の朝ダイジェストから操作してください。",
    "expired": "このボタンは無効です（期限切れ/不正）。最新のダイジェストから操作してください。",
    "store_failed": "確認済みにできませんでした。時間をおいて、もう一度お試しください。",
    "undo_failed": "取り消せませんでした。時間をおいて、もう一度お試しください。",
}
#: 「新着が来たらまた出す」は仕様の要。押した人がここで安心して押せるかが分かれ目なので、
#: 毎回この一文を添える（「消えた」と誤解されると次から押されなくなる）。
_ACK_SUFFIX = "新しい返信が来たら、また表示します。"
_UNDO_MSG = "↩︎ 取り消しました。次回の朝ダイジェストにまた表示されます。"


@register
class DigestAckSkill(BaseSkill[DigestAckInput, DigestAckOutput]):
    """朝ダイジェストの項目を確認済みにする / その取り消しを行う Skill。"""

    name: ClassVar[str] = "digest_ack"
    description: ClassVar[str] = (
        "朝ダイジェストの項目を『確認済み』にして、翌朝以降のダイジェストから隠すツール。"
        "Slack の interaction で action='digest_ack' を受け取ったら、その value（署名トークン）を "
        "ack_token に**そのまま**渡して呼ぶ。個別の『☑️ 確認済みにする』・『☑️ 全部確認した』・"
        "『↩︎ 取り消す』はすべてこの action_id で来るが、どの操作かはトークン側が持っているので、"
        "呼び出し側は種別を判断しなくてよい（value を書き換えたり作り直したりしないこと）。"
        "隠れるのは『確認済みかつ、その後スレッドに新着が無い』間だけで、新しい返信が来れば"
        "翌朝また表示される。確認済みの記録は 30 日で自動的に消える。"
        "メールの送信・下書き作成・カレンダー登録は一切しない（引数が存在しない）。"
        + USER_CONTEXT_RULE
    )
    input_schema: ClassVar[type[BaseModel]] = DigestAckInput
    output_schema: ClassVar[type[BaseModel]] = DigestAckOutput

    def __init__(self, store: Any | None = None) -> None:
        # store は差し込み可（テスト用）。既定は遅延生成＝import 時に DB を触らない。
        self._store = store

    def _get_store(self) -> Any:
        if self._store is None:
            from teamagent.adapters.digest_ack_store import DigestAckStore

            self._store = DigestAckStore()
        return self._store

    def run(self, input: DigestAckInput, ctx: SkillContext) -> DigestAckOutput:
        log = ctx.bind_logger(self.name)

        # G1: 本人限定（fail-closed）。morning_digest / mail_draft と同じ形。
        requester = ctx.metadata.get("user_email")
        if not requester or not isinstance(requester, str):
            raise PermissionError("digest_ack は本人 user_email が必須です（本人の確認状態限定）")
        requester = requester.strip()
        if not requester:
            raise PermissionError("本人 user_email が必須です（空不可・fail-closed）")

        token = (input.ack_token or "").strip()
        if not token:
            log.info("digest_ack_no_input")
            return DigestAckOutput(error="no_input", message=_ERR_MSG["no_input"])

        payload = decode_ack_token(token, requester)
        if payload is None:
            # token 値は出さない（署名済みとはいえログに残す理由が無い）。
            log.info("digest_ack_invalid_token")
            return DigestAckOutput(error="expired", message=_ERR_MSG["expired"])

        store = self._get_store()

        if payload.kind == "unack":
            n = store.unack(requester, payload.items, request_id=ctx.request_id)
            if n <= 0:
                log.warning("digest_ack_unack_failed", requested=len(payload.items))
                return DigestAckOutput(error="store_failed", message=_ERR_MSG["undo_failed"])
            log.info("digest_ack_done", kind="unack", count=n)
            return DigestAckOutput(unacked=n, message=_UNDO_MSG)

        n = store.ack(requester, payload.items, request_id=ctx.request_id)
        if n <= 0:
            log.warning("digest_ack_store_failed", requested=len(payload.items))
            return DigestAckOutput(error="store_failed", message=_ERR_MSG["store_failed"])

        # 取り消し導線（裁定: 押下直後の ephemeral に出す）。発行できなくても ack 自体は
        # 成立しているので、成功として返す（取り消しリンクが無いだけ）。
        undo_token = ""
        try:
            undo_token = encode_unack_token(payload.items, requester) or ""
        except Exception:
            log.warning("digest_ack_undo_token_failed")

        message = (
            f"☑️ 確認済みにしました。{_ACK_SUFFIX}"
            if n == 1
            else f"☑️ {n} 件を確認済みにしました。{_ACK_SUFFIX}"
        )
        log.info("digest_ack_done", kind=payload.kind, count=n)
        return DigestAckOutput(acked=n, undo_token=undo_token, message=message)
