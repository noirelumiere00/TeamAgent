"""schedule_propose Skill 本体 — 「🗓 日程候補を提案」ボタン押下を処理する（v0.3 Task4）。

経路: Slack のボタン押下 → OpenClaw(socket) or worker が受ける → 本ツールを呼ぶ
（value=署名トークンを schedule_token に渡す）。フロー:
  1. draft_token 形式の HMAC トークンを検証（署名・所有者・失効＝fail-closed）
  2. 本人カレンダーの freebusy から翌営業日以降の空き枠を計算（slot_finder）
  3. 候補日入りの **返信下書き（Reply-All・送信しない）** を Gmail に作成
     （既存 generate_draft_for_thread の body_override 経路＝LLM 不使用・コストゼロ）
  4. 各候補に **仮予定（tentative かつ transparent）** を本人カレンダーへ作成
     （transparent＝自分の freebusy を潰さない・招待は送らない・冪等 id で連打安全）

⚠️ 死守ライン:
  送信しない（drafts.create のみ）・相手に招待を送らない（adapter が物理封鎖）・
  G1 本人限定（token 所有者照合 + user_email fail-closed）・
  カレンダー書込権限が無い旧連携は **下書きのみ作成**（graceful degradation・再連携案内を併記）。
"""

from __future__ import annotations

import datetime as _dt
from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.gcalendar_client import DuplicateEventError, GCalendarClient
from teamagent.adapters.oauth_token_store import TokenStore
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.morning_digest.draft_token import decode_draft_token
from teamagent.skills.morning_digest.event_token import stable_event_id
from teamagent.skills.schedule_propose.schema import ScheduleProposeInput, ScheduleProposeOutput
from teamagent.skills.schedule_propose.slot_finder import (
    build_proposal_body,
    find_slots,
    format_candidates_ja,
)

logger = structlog.get_logger(__name__)

_JST = _dt.timezone(_dt.timedelta(hours=9))
_CAL_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"

_ERR_MSG: dict[str, str] = {
    "expired": "このボタンは無効です（期限切れ/不正）。最新のダイジェストから操作してください。",
    "not_connected": "日程提案には Google の連携が必要です"
    "（@Aico に『連携』と話しかけて許可してください）。",
    "no_slots": "直近5営業日に空き枠が見つかりませんでした。お手数ですが手動でご調整ください。",
    "freebusy_failed": "カレンダーの空き状況を取得できませんでした。"
    "時間をおいて再度お試しください。",
    "draft_failed": "返信下書きの作成に失敗しました。時間をおいて再度お試しください。",
}
_HOLD_TITLE = "仮: 日程候補（Aico）"


@register
class ScheduleProposeSkill(BaseSkill[ScheduleProposeInput, ScheduleProposeOutput]):
    """『🗓 日程候補を提案』押下→候補入り返信下書き＋透明仮予定を作る Skill（送信は人間）。"""

    name: ClassVar[str] = "schedule_propose"
    description: ClassVar[str] = (
        "朝ダイジェストの『🗓 日程候補を提案』ボタン押下を処理するツール。"
        "Slack の interaction で action='schedule_propose' を受け取ったら、"
        "その value（署名トークン）を schedule_token に渡して呼ぶ。"
        "本人カレンダーの空き枠から候補日を計算し、候補入りの返信下書きを Gmail に作成"
        "（送信はしない）、カレンダーに仮予定（他予定を邪魔しない透明ホールド）を置く。"
        "自由文の日程調整相談は対象外（このツールはボタン専用）。"
        "呼び出し時は arguments に `_user_context: {slack_user_id: '<押した本人のuser_id>'}` を"
        "必ず含める（本人解決鍵）。"
    )
    input_schema: ClassVar[type[BaseModel]] = ScheduleProposeInput
    output_schema: ClassVar[type[BaseModel]] = ScheduleProposeOutput

    def __init__(
        self,
        token_store: TokenStore | None = None,
        *,
        gcalendar_factory: object | None = None,
        now_factory: object | None = None,
    ) -> None:
        self._token_store = token_store
        self._gcalendar_factory = gcalendar_factory or GCalendarClient.from_user_token
        # テストで「今」を固定するための注入口（既定は実時刻）。
        self._now_factory = now_factory or (lambda: _dt.datetime.now(tz=_JST))

    def run(self, input: ScheduleProposeInput, ctx: SkillContext) -> ScheduleProposeOutput:
        log = ctx.bind_logger(self.name)

        requester = str(ctx.metadata.get("user_email", "") or "").strip()
        if not requester:
            raise PermissionError("schedule_propose は本人 user_email が必須です")

        thread_id = decode_draft_token(input.schedule_token, requester)
        if not thread_id:
            log.info("schedule_propose_invalid_token")
            return ScheduleProposeOutput(error="expired", message=_ERR_MSG["expired"])

        token = self._token_store.get(requester) if self._token_store else None
        if token is None:
            log.info("schedule_propose_not_connected")
            return ScheduleProposeOutput(error="not_connected", message=_ERR_MSG["not_connected"])

        # 1) 空き枠計算（freebusy は calendar.readonly で可＝旧連携でも動く）。
        now = self._now_factory()  # type: ignore[operator]
        gcal = self._gcalendar_factory(token)  # type: ignore[operator]
        try:
            busy = gcal.freebusy(
                ctx.request_id,
                time_min=now.isoformat(),
                time_max=(now + _dt.timedelta(days=9)).isoformat(),
            )
        except Exception as e:
            # API 障害は「空き枠なし」と別事象（偽の事実を断言しない・レビュー F3）。
            log.warning("schedule_propose_freebusy_failed", err=type(e).__name__)
            return ScheduleProposeOutput(
                error="freebusy_failed", message=_ERR_MSG["freebusy_failed"]
            )
        slots = find_slots(busy, now=now)
        if not slots:
            log.info("schedule_propose_no_slots")
            return ScheduleProposeOutput(error="no_slots", message=_ERR_MSG["no_slots"])

        # 2) 候補入り返信下書き（LLM 不使用・Reply-All/冪等は既存経路を再利用）。
        from teamagent.skills.morning_digest.skill import MorningDigestSkill

        digest_skill = MorningDigestSkill(token_store=self._token_store)
        res = digest_skill.generate_draft_for_thread(
            thread_id, requester, ctx, body_override=build_proposal_body(slots)
        )
        open_url = str(res.get("thread_url", "") or "")
        if res.get("already"):
            log.info("schedule_propose_already")
            return ScheduleProposeOutput(
                already=True,
                open_url=open_url,
                message="この案件には既に下書きがあります（Gmail をご確認ください）。",
            )
        if not res.get("created"):
            err = str(res.get("error") or "draft_failed")
            log.info("schedule_propose_draft_failed", err=err)
            if err in ("not_connected", "reauth_needed"):
                return ScheduleProposeOutput(
                    error="not_connected", message=_ERR_MSG["not_connected"]
                )
            return ScheduleProposeOutput(error="draft_failed", message=_ERR_MSG["draft_failed"])

        # 3) 透明ホールド（書込権限が無い旧連携はスキップ＝下書きだけでも価値がある）。
        holds = 0
        scopes = tuple(getattr(token, "scopes", ()) or ())
        holds_allowed = (not scopes) or (_CAL_EVENTS_SCOPE in scopes)
        if holds_allowed:
            for start, end in slots:
                try:
                    gcal.insert_event(
                        ctx.request_id,
                        summary=_HOLD_TITLE,
                        start_iso=start.isoformat(),
                        end_iso=end.isoformat(),
                        tentative=True,
                        transparent=True,  # 自分の freebusy を潰さない（レビュー F5 裁定）
                        event_id=stable_event_id(
                            start.isoformat(), end.isoformat(), requester, kind="hold"
                        ),
                    )
                    holds += 1
                except DuplicateEventError:
                    holds += 1  # 既存ホールド＝目的は達成済み
                except Exception as e:  # ホールド失敗で下書き成功を無かったことにしない
                    log.warning("schedule_propose_hold_failed", err=type(e).__name__)

        candidates = format_candidates_ja(slots).replace("\n", " / ")
        message = f"🗓 候補 {len(slots)} 件入りの返信下書きを作成しました（未送信）: {candidates}"
        if holds_allowed:
            message += (
                f"\nカレンダーに仮予定 {holds} 件を置きました（透明・他の予定を邪魔しません）。"
            )
        else:
            message += (
                "\n※ カレンダーへの仮予定は未作成です（再連携でカレンダー書き込みを許可すると"
                "自動でホールドされます）。"
            )
        log.info("schedule_propose_done", slots=len(slots), holds=holds)
        return ScheduleProposeOutput(
            created=True, holds_created=holds, open_url=open_url, message=message
        )
