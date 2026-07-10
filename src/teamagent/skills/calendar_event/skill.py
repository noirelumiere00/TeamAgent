"""calendar_event Skill 本体 — 朝ダイジェスト「📅 カレンダーに登録」ボタン押下を処理する。

経路: Slack のボタン押下 → OpenClaw(socket) が system event としてエージェントへ転送 →
SOUL 指示でエージェントが本ツールを呼ぶ（value=署名トークンを event_token に渡す）。
本ツールは HMAC 署名トークン（日時/タイトル入り）を検証し、**本人の primary カレンダー**へ
予定を登録する（招待は送らない＝adapter が sendUpdates="none" 強制・attendees 無し）。

⚠️ 死守ライン:
  G1 本人カレンダー限定（user_email→token, fail-closed）。未連携/旧スコープは error で案内。
  トークン検証: 署名・所有者照合・失効を decode_event_token が担保（fail-closed）。
  無断確定の禁止: 登録は本人のボタン押下（明示依頼）でのみ・削除/変更 API は adapter で物理封鎖。
  冪等: event_id をトークンから導出（連打は DuplicateEventError → 「登録済み」案内）。
  G3 生の予定詳細は value・ログに出さない（message/event_url は本人向け返答のみ）。
"""

from __future__ import annotations

from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.gcalendar_client import DuplicateEventError, GCalendarClient
from teamagent.adapters.oauth_token_store import TokenStore
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.calendar_event.schema import CalendarEventInput, CalendarEventOutput
from teamagent.skills.morning_digest.event_token import decode_event_token, stable_event_id

logger = structlog.get_logger(__name__)

_ERR_MSG: dict[str, str] = {
    "expired": "このボタンは無効です（期限切れ/不正）。最新のダイジェストから操作してください。",
    "not_connected": "カレンダー登録には Google の連携が必要です"
    "（@AiLa に『連携』と話しかけて許可してください）。",
    "reauth_needed": "カレンダー登録には Google の再連携が必要です"
    "（カレンダー書き込みの権限を追加で許可してください。@AiLa に『連携』でやり直せます）。",
    "insert_failed": "カレンダー登録に失敗しました。時間をおいて再度お試しください。",
}
_OK_MSG = "📅 カレンダーに登録しました（あなたのカレンダーのみ・招待は送っていません）。"
_ALREADY_MSG = "この予定は登録済みのようです（連打・もしくは一度手動で消した予定です）。"


@register
class CalendarEventSkill(BaseSkill[CalendarEventInput, CalendarEventOutput]):
    """『📅 カレンダーに登録』押下→本人カレンダーへ予定登録する Skill（招待は送らない）。"""

    name: ClassVar[str] = "calendar_event"
    description: ClassVar[str] = (
        "朝ダイジェストの『📅 カレンダーに登録』ボタン押下を処理するツール。"
        "Slack の interaction で action='calendar_event' を受け取ったら、"
        "その value（署名トークン）を "
        "event_token に渡して呼ぶ。本人のカレンダーにのみ予定を登録し（相手への招待は送らない）、"
        "カレンダーで開くリンク(event_url)と案内文(message)を返す。"
        "自由文からの予定作成は対象外（このツールはボタン専用。日程調整の相談は対象外）。"
        "呼び出し時は arguments に `_user_context: {slack_user_id: '<押した本人のuser_id>'}` を"
        "必ず含める（本人解決鍵）。"
    )
    input_schema: ClassVar[type[BaseModel]] = CalendarEventInput
    output_schema: ClassVar[type[BaseModel]] = CalendarEventOutput

    def __init__(
        self,
        token_store: TokenStore | None = None,
        *,
        gcalendar_factory: object | None = None,
    ) -> None:
        self._token_store = token_store
        # テスト差し替え用（既定は GCalendarClient.from_user_token）。
        self._gcalendar_factory = gcalendar_factory or GCalendarClient.from_user_token

    def run(self, input: CalendarEventInput, ctx: SkillContext) -> CalendarEventOutput:
        log = ctx.bind_logger(self.name)

        # G1: 本人カレンダー限定（fail-closed）。MCP 外殻が slack_user_id→email を解決して注入。
        requester = str(ctx.metadata.get("user_email", "") or "").strip()
        if not requester:
            raise PermissionError("calendar_event は本人 user_email が必須です")

        payload = decode_event_token(input.event_token, requester)
        if payload is None:
            log.info("calendar_event_invalid_token")  # token 値は出さない
            return CalendarEventOutput(error="expired", message=_ERR_MSG["expired"])

        token = self._token_store.get(requester) if self._token_store else None
        if token is None:
            log.info("calendar_event_not_connected")
            return CalendarEventOutput(error="not_connected", message=_ERR_MSG["not_connected"])
        scopes = tuple(getattr(token, "scopes", ()) or ())
        if scopes and "https://www.googleapis.com/auth/calendar.events" not in scopes:
            # 旧7スコープ連携（calendar.events 追加前）。事前検知して再連携を案内する
            # （403 を本番で踏ませない）。scopes 未記録の古い行は通して API 判定に委ねる。
            log.info("calendar_event_reauth_needed")
            return CalendarEventOutput(error="reauth_needed", message=_ERR_MSG["reauth_needed"])

        gcal = self._gcalendar_factory(token)  # type: ignore[operator]
        event_id = stable_event_id(payload.start_iso, payload.end_iso, requester)
        try:
            inserted = gcal.insert_event(
                ctx.request_id,
                summary=payload.title or "打合せ",
                start_iso=payload.start_iso,
                end_iso=payload.end_iso,
                event_id=event_id,
            )
        except DuplicateEventError:
            log.info("calendar_event_already")
            return CalendarEventOutput(already=True, message=_ALREADY_MSG)
        except ValueError as e:
            # LLM 由来日時の最終検証（encode 前に検証済みだが多層防御）。
            log.warning("calendar_event_bad_datetime", err=type(e).__name__)
            return CalendarEventOutput(error="expired", message=_ERR_MSG["expired"])
        except Exception as e:
            name = type(e).__name__
            # googleapiclient HttpError 403: 権限系（insufficient scope 等）のみ再連携案内。
            # Calendar API は rate limit 系（rateLimitExceeded 等）も 403 を返すため、
            # reason で判別しないとバースト時に全員へ誤った再認可誘導をする（レビュー F4）。
            status = getattr(getattr(e, "resp", None), "status", None)
            if str(status) == "403":
                reason = (
                    f"{getattr(e, 'reason', '') or ''} {getattr(e, 'error_details', '') or ''}"
                ).lower()
                if any(k in reason for k in ("insufficient", "permission", "scope", "forbidden")):
                    log.info("calendar_event_reauth_needed", via="api_403")
                    return CalendarEventOutput(
                        error="reauth_needed", message=_ERR_MSG["reauth_needed"]
                    )
            log.warning("calendar_event_insert_failed", err=name)
            return CalendarEventOutput(error="insert_failed", message=_ERR_MSG["insert_failed"])

        log.info("calendar_event_created")  # 予定詳細はログに出さない（G3）
        return CalendarEventOutput(
            created=True,
            event_url=inserted.html_link,
            message=_OK_MSG,
        )
