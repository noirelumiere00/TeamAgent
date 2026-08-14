"""calendar_freebusy Skill 本体 — 「空いてる時間ある？」等の自由文照会に答える（read-only）。

経路: Slack の自由文（「明日空いてる？」「60分のMTGどこに入る？」）→ OpenClaw →
SOUL 指示でエージェントが本ツールを呼ぶ。本人カレンダーの freebusy（読み取りのみ）から
営業時間内の空きウィンドウと、指定所要時間が収まる開始候補を決定的に整形して返す。

⚠️ 死守ライン:
  G1 本人カレンダー限定（user_email→token, fail-closed）。未連携は oauth_connect へ誘導。
  read-only: 書込 API は一切呼ばない（freebusy のみ・insert/update/delete 不使用）。
  日付計算を LLM に任せない: date 省略時はサーバが JST 翌日を採用（曜日ずれのねつ造防止）。
  API 障害は「空きなし」と混同しない（error='freebusy_failed'・schedule_propose と同じ裁定）。
  対象日が土日なら non_business_note を必ず message 先頭に出す（祝日は未判定と正直に言う）。
"""

from __future__ import annotations

import datetime as _dt
from typing import ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.gcalendar_client import GCalendarClient
from teamagent.adapters.oauth_token_store import TokenStore
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.calendar_freebusy.free_windows import (
    compute_free_windows,
    day_label_ja,
    enumerate_candidate_starts,
    format_freebusy_ja,
    non_business_note,
)
from teamagent.skills.calendar_freebusy.schema import (
    CalendarFreeBusyInput,
    CalendarFreeBusyOutput,
    FreeWindow,
)

logger = structlog.get_logger(__name__)

_JST = _dt.timezone(_dt.timedelta(hours=9))

_ERR_MSG: dict[str, str] = {
    "not_connected": "空き時間の照会には Google の連携が必要です"
    "（@NewsTV AI に『連携』と話しかけて許可してください）。",
    "freebusy_failed": "カレンダーの空き状況を取得できませんでした。"
    "時間をおいて再度お試しください。",
    "bad_date": "指定の日付が実在しません（YYYY-MM-DD で指定するか、省略してください）。",
}


@register
class CalendarFreeBusySkill(BaseSkill[CalendarFreeBusyInput, CalendarFreeBusyOutput]):
    """自由文の空き時間照会に答える Skill（freebusy 読み取りのみ・書込なし）。"""

    name: ClassVar[str] = "calendar_freebusy"
    description: ClassVar[str] = (
        "空いてる時間・空き枠・◯分のMTGどこに入る、の自由文照会に答える読み取り専用ツール。"
        "日付省略時は明日を自動採用。予定の作成・変更・削除は一切しない。"
        "呼び出し時は arguments に `_user_context: {slack_user_id: '<依頼した本人のuser_id>'}` を"
        "必ず含める（本人解決鍵）。"
    )
    input_schema: ClassVar[type[BaseModel]] = CalendarFreeBusyInput
    output_schema: ClassVar[type[BaseModel]] = CalendarFreeBusyOutput

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

    def run(self, input: CalendarFreeBusyInput, ctx: SkillContext) -> CalendarFreeBusyOutput:
        log = ctx.bind_logger(self.name)

        # ① G1: 本人限定（fail-closed）。MCP 外殻が slack_user_id→email を解決して注入。
        requester = str(ctx.metadata.get("user_email", "") or "").strip()
        if not requester:
            raise PermissionError("calendar_freebusy は本人 user_email が必須です")

        # ② 対象日。date 省略時はサーバの JST 翌日（LLM の日付計算を信用しない）。
        if input.date:
            try:
                target = _dt.date.fromisoformat(input.date)
            except ValueError:
                # schema の pattern は形式のみ通す（例: 2026-13-45）。実在しない日付は fail-closed。
                log.info("calendar_freebusy_bad_date")
                return CalendarFreeBusyOutput(error="bad_date", message=_ERR_MSG["bad_date"])
        else:
            now = self._now_factory()  # type: ignore[operator]
            now_jst = now.astimezone(_JST) if now.tzinfo else now.replace(tzinfo=_JST)
            target = now_jst.date() + _dt.timedelta(days=1)

        # ④ 未連携は oauth_connect へ誘導（freebusy は calendar.readonly で動く）。
        token = self._token_store.get(requester) if self._token_store else None
        if token is None:
            log.info("calendar_freebusy_not_connected")
            return CalendarFreeBusyOutput(error="not_connected", message=_ERR_MSG["not_connected"])

        # ⑤ freebusy（読み取りのみ）。API 障害は「空きなし」と別事象（偽の事実を断言しない・
        #    schedule_propose/skill.py の F3 裁定と同じ）。
        time_min = _dt.datetime(target.year, target.month, target.day, tzinfo=_JST)
        time_max = time_min + _dt.timedelta(days=input.days)
        gcal = self._gcalendar_factory(token)  # type: ignore[operator]
        try:
            busy = list(
                gcal.freebusy(
                    ctx.request_id,
                    time_min=time_min.isoformat(),
                    time_max=time_max.isoformat(),
                )
            )
        except Exception as e:
            log.warning("calendar_freebusy_failed", err=type(e).__name__)
            return CalendarFreeBusyOutput(
                error="freebusy_failed", message=_ERR_MSG["freebusy_failed"]
            )

        # ⑥ 空きウィンドウ計算→開始候補列挙→決定的整形（書込 API は一切呼ばない）。
        #    ③ 対象日が土日なら non_business_note が先頭セクション先頭に入る＝message 先頭。
        sections: list[str] = []
        free_windows: list[FreeWindow] = []
        candidates: list[str] = []
        for offset in range(input.days):
            day = target + _dt.timedelta(days=offset)
            windows = compute_free_windows(busy, day=day)
            day_candidates = [
                f"{day_label_ja(day)} {s.strftime('%H:%M')}〜{e.strftime('%H:%M')}"
                for s, e in enumerate_candidate_starts(windows, duration_min=input.duration_min)
            ]
            sections.append(
                format_freebusy_ja(day, windows, day_candidates, non_business_note(day))
            )
            free_windows.extend(
                FreeWindow(
                    start=s.isoformat(),
                    end=e.isoformat(),
                    label=f"{day_label_ja(day)} {s.strftime('%H:%M')}〜{e.strftime('%H:%M')}",
                )
                for s, e in windows
            )
            candidates.extend(day_candidates)

        log.info(
            "calendar_freebusy_done",
            days=input.days,
            busy=len(busy),
            windows=len(free_windows),
            candidates=len(candidates),
        )  # 予定詳細（busy の中身）はログに出さない
        return CalendarFreeBusyOutput(
            date_label=day_label_ja(target),
            non_business_note=non_business_note(target),
            free_windows=free_windows,
            candidates=candidates,
            busy_count=len(busy),
            message="\n\n".join(sections),
        )
