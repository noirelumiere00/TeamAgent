"""calendar_event Skill 本体 — 本人カレンダーへ予定を登録する（招待は送らない）。

入口は 3 つ:

1. **ボタン経路**（従来・挙動不変）: 朝ダイジェスト「📅 カレンダーに登録」押下 →
   OpenClaw が system event として転送 → ``event_token``（HMAC 署名トークン）を渡す。
2. **自由文経路（freeform）**: DM/スレッドの「カレンダーに追加して」「予定入れといて」→
   ``title`` + ``start`` (+ ``end`` / ``location``)。2026-08-18 の本番 QA で
   「ボタン専用のためお応えできない」と正しく断られた＝**設計の穴**を塞ぐもの。
3. **一括経路**: 共通時刻＋日付・件名の原文を ``bulk_text`` で渡す。一覧提示後に
   ``confirm_bulk=True`` で同じ単発経路へ流す（本人への再確認は不要）。

⚠️ 死守ライン:
  G1 本人カレンダー限定（user_email→token, fail-closed）。未連携/旧スコープは error で案内。
  トークン検証: 署名・所有者照合・失効を decode_event_token が担保（fail-closed）。
    ``event_token`` が来ていたら**必ず**ボタン経路（自由文引数は無視＝署名済みの値が
    LLM 由来の値へ上書きされる経路を作らない）。壊れたトークンは自由文へ**落とさない**。
  無断確定の禁止: 登録は本人の明示依頼（ボタン押下 or 自由文の依頼）でのみ。
    削除/変更 API は adapter で物理封鎖。
  **招待ゼロ**: 入力に attendees / カレンダー ID が存在しない（引数が無い＝作れない）。
    adapter も sendUpdates="none" 強制・primary 固定で二重に封鎖する。
  冪等: event_id を安定フィールドから導出（連打は DuplicateEventError → 「登録済み」案内）。
  自由文の入力検証（freeform のみ）: ISO 8601 必須・所要 0 分超 8 時間以内・
    開始は「過去 1 日〜未来 366 日」以内（LLM が捏造した年や桁違いの日時を弾く）。
  G3 生の予定詳細は value・ログに出さない（message/event_url は本人向け返答のみ）。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
from dataclasses import dataclass
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel

from teamagent.adapters.gcalendar_client import DuplicateEventError, GCalendarClient
from teamagent.adapters.oauth_token_store import TokenStore
from teamagent.skills._shared.google_connection import (
    google_connection_guard,
    needs_reconnect,
    record_google_failure,
)
from teamagent.skills._shared.user_context import USER_CONTEXT_RULE
from teamagent.skills.base import BaseSkill, SkillContext, register
from teamagent.skills.calendar_event.bulk import parse_bulk
from teamagent.skills.calendar_event.schema import CalendarEventInput, CalendarEventOutput
from teamagent.skills.morning_digest.event_token import decode_event_token, stable_event_id

logger = structlog.get_logger(__name__)

_ERR_MSG: dict[str, str] = {
    "expired": "このボタンは無効です（期限切れ/不正）。最新のダイジェストから操作してください。",
    "not_connected": "カレンダー登録には Google の連携が必要です"
    "（@Aico に『連携』と話しかけて許可してください）。",
    "reauth_needed": "カレンダー登録には Google の再連携が必要です"
    "（カレンダー書き込みの権限を追加で許可してください。@Aico に『連携』でやり直せます）。",
    "insert_failed": "カレンダー登録に失敗しました。時間をおいて再度お試しください。",
    "no_input": "登録する予定が分かりませんでした。"
    "予定名と日時（例『A社と打合せ 8/20 15:00-16:00』）を教えてください。",
    "bad_datetime": "日時を解釈できませんでした。"
    "「2026-08-20 15:00」のように、年月日と時刻をはっきり教えてください。",
    "bad_duration": "所要時間が不正です（終了は開始より後、最長 8 時間まで）。",
    "out_of_range": "その日付には登録できません（過去の予定、または 1 年より先の予定です）。",
}
_OK_MSG = "📅 カレンダーに登録しました（あなたのカレンダーのみ・招待は送っていません）。"
_ALREADY_MSG = "この予定は登録済みのようです（連打・もしくは一度手動で消した予定です）。"

_JST = _dt.timezone(_dt.timedelta(hours=9))
# 自由文経路の既定所要（end 省略時）。
_DEFAULT_DURATION_MIN = 60
# 所要の上限。これを超える「予定」は LLM の日時取り違え（日付をまたぐ等）を強く疑う。
_MAX_DURATION_HOURS = 8
# 登録を許す期間。過去は 1 日ぶんだけ許す（当日の遡り登録・時差の丸め誤差を殺さないため）。
_PAST_LIMIT_DAYS = 1
_FUTURE_LIMIT_DAYS = 366


@dataclass(frozen=True)
class _ResolvedEvent:
    """入口（ボタン / 自由文）を問わず確定した「登録する予定」。

    ⚠️ attendees / calendar_id は **意図的に存在しない**。ここに持たせないことが
    「他人へ招待が飛ばない」「他人のカレンダーに書かない」の構造的な担保になる。
    """

    title: str
    start_iso: str
    end_iso: str
    location: str
    id_kind: str  # 冪等 event_id の名前空間（confirm=ボタン / freeform:<title hash>=自由文）
    freeform: bool


def _parse_iso_jst(raw: str) -> _dt.datetime | None:
    """ISO 8601 を JST 付き datetime にする。解釈できなければ None（推測しない）。

    タイムゾーン省略（例 ``2026-08-20T15:00``）は **JST として解釈**する。日付だけ
    （``2026-08-20``）は時刻が決まっていない＝登録してよい情報が揃っていないので拒否する。
    """
    text = raw.strip().replace("/", "-")
    if not text or ("T" not in text and " " not in text):
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_JST)


@register
class CalendarEventSkill(BaseSkill[CalendarEventInput, CalendarEventOutput]):
    """本人カレンダーへ予定を登録する Skill（ボタン押下 / 自由文・招待は送らない）。"""

    name: ClassVar[str] = "calendar_event"
    description: ClassVar[str] = (
        "本人カレンダーへの予定登録専用（招待・変更・削除なし）。"
        "一括はbulk_textで全行を渡し、一覧を一度提示後confirm_bulk=true（再確認不要）"
        "曖昧な日時は確認。空き時間はcalendar_freebusy、"
        "予定確認・一覧はcalendar_freebusy(mode='agenda')、"
        "日程調整ボタンはschedule_propose。"
    ) + USER_CONTEXT_RULE
    input_schema: ClassVar[type[BaseModel]] = CalendarEventInput
    output_schema: ClassVar[type[BaseModel]] = CalendarEventOutput

    def __init__(
        self,
        token_store: TokenStore | None = None,
        *,
        gcalendar_factory: object | None = None,
        now_factory: object | None = None,
    ) -> None:
        self._token_store = token_store
        # テスト差し替え用（既定は GCalendarClient.from_user_token）。
        self._gcalendar_factory = gcalendar_factory or GCalendarClient.from_user_token
        # 自由文経路の日付レンジ判定で使う「今」（calendar_freebusy と同じ注入口）。
        self._now_factory = now_factory or (lambda: _dt.datetime.now(tz=_JST))

    def run(self, input: CalendarEventInput, ctx: SkillContext) -> CalendarEventOutput:
        # 署名付きボタンは一括引数よりも優先する。
        if not input.bulk_text.strip() or input.event_token.strip():
            return self._run_single(input, ctx)
        requester = str(ctx.metadata.get("user_email", "") or "").strip()
        if not requester:
            raise PermissionError("calendar_event は本人 user_email が必須です")
        return self._run_bulk(input, ctx, requester, ctx.bind_logger(self.name))

    @google_connection_guard(lambda input: CalendarEventOutput())
    def _run_single(self, input: CalendarEventInput, ctx: SkillContext) -> CalendarEventOutput:
        log = ctx.bind_logger(self.name)

        # G1: 本人カレンダー限定（fail-closed）。MCP 外殻が slack_user_id→email を解決して注入。
        requester = str(ctx.metadata.get("user_email", "") or "").strip()
        if not requester:
            raise PermissionError("calendar_event は本人 user_email が必須です")

        resolved, failure = self._resolve_event(input, requester, log)
        if failure is not None:
            return failure
        if resolved is None:  # 到達しない（failure が None なら必ず解決済み）
            return CalendarEventOutput(error="no_input", message=_ERR_MSG["no_input"])

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

        event_id = stable_event_id(
            resolved.start_iso, resolved.end_iso, requester, kind=resolved.id_kind
        )
        try:
            gcal = self._gcalendar_factory(token)  # type: ignore[operator]
            inserted = gcal.insert_event(
                ctx.request_id,
                summary=resolved.title or "打合せ",
                start_iso=resolved.start_iso,
                end_iso=resolved.end_iso,
                location=resolved.location,
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
            record_google_failure(e)
            name = type(e).__name__
            # googleapiclient HttpError 403: 権限系（insufficient scope 等）のみ再連携案内。
            # Calendar API は rate limit 系（rateLimitExceeded 等）も 403 を返すため、
            # reason で判別しないとバースト時に全員へ誤った再認可誘導をする（レビュー F4）。
            status = getattr(getattr(e, "resp", None), "status", None)
            if str(status) == "403" and needs_reconnect(e):
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

        log.info("calendar_event_created", freeform=resolved.freeform)  # 詳細は出さない（G3）
        # 出典 URL 方針: 登録した「原本」へ辿れるリンクを message 本文に必ず載せる
        # （event_url フィールドだけだと、message をそのまま返す面では消える）。
        message = f"{_OK_MSG}\n🔗 {inserted.html_link}" if inserted.html_link else _OK_MSG
        return CalendarEventOutput(
            created=True,
            event_url=inserted.html_link,
            message=message,
        )

    def _run_bulk(
        self, input: CalendarEventInput, ctx: SkillContext, requester: str, log: Any
    ) -> CalendarEventOutput:
        now = self._now_factory()  # type: ignore[operator]
        now = now.astimezone(_JST) if now.tzinfo else now.replace(tzinfo=_JST)
        try:
            rows, warnings = parse_bulk(input.bulk_text, today=now.date())
        except ValueError:
            return CalendarEventOutput(
                error="bad_datetime",
                message="一括登録は共通の開始〜終了時刻と、日付・件名を1行ずつ指定してください。",
            )
        rows = [row.model_copy(update={"location": input.location}) for row in rows]
        # 全行を検証してから書く。後半の入力不備で前半だけ登録されるのを防ぐ。
        for row in rows:
            _, failure = self._resolve_event(row, requester, log)
            if failure is not None:
                return failure
        if not input.confirm_bulk:
            lines = [*warnings, f"この {len(rows)} 件を入れます。"]
            lines.extend(
                f"・{row.start[:10]} {row.start[11:16]}〜{row.end[11:16]} {row.title}"
                for row in rows
            )
            return CalendarEventOutput(message="\n".join(lines))

        results = [self._run_single(row, ctx) for row in rows]
        lines = list(warnings)
        lines.extend(
            f"・{row.start[:10]} {row.start[11:16]}〜{row.end[11:16]} {row.title}: "
            + (result.event_url or result.message)
            for row, result in zip(rows, results, strict=True)
        )
        return CalendarEventOutput(
            created=any(r.created for r in results),
            already=all(r.already for r in results),
            error="partial_failure" if any(r.error for r in results) else "",
            message="\n".join(lines),
        )

    # ── 入口の解決（ボタン経路 / 自由文経路）────────────────────────────────────

    def _resolve_event(
        self, input: CalendarEventInput, requester: str, log: Any
    ) -> tuple[_ResolvedEvent | None, CalendarEventOutput | None]:
        """入力から「登録する予定」を確定させる。失敗時は利用者向け出力を返す。

        戻り値は ``(確定した予定, None)`` か ``(None, 失敗出力)`` のどちらか。
        """
        if input.event_token.strip():
            payload = decode_event_token(input.event_token, requester)
            if payload is None:
                log.info("calendar_event_invalid_token")  # token 値は出さない
                return None, CalendarEventOutput(error="expired", message=_ERR_MSG["expired"])
            return (
                _ResolvedEvent(
                    title=payload.title,
                    start_iso=payload.start_iso,
                    end_iso=payload.end_iso,
                    location="",
                    id_kind="confirm",
                    freeform=False,
                ),
                None,
            )

        # ── 自由文経路 ────────────────────────────────────────────────────────
        title = input.title.strip()
        start_raw = input.start.strip()
        if not title or not start_raw:
            log.info("calendar_event_freeform_incomplete")
            return None, CalendarEventOutput(error="no_input", message=_ERR_MSG["no_input"])

        start = _parse_iso_jst(start_raw)
        if start is None:
            log.info("calendar_event_freeform_bad_start")
            return None, CalendarEventOutput(error="bad_datetime", message=_ERR_MSG["bad_datetime"])
        if input.end.strip():
            end = _parse_iso_jst(input.end)
            if end is None:
                log.info("calendar_event_freeform_bad_end")
                return None, CalendarEventOutput(
                    error="bad_datetime", message=_ERR_MSG["bad_datetime"]
                )
        else:
            end = start + _dt.timedelta(minutes=_DEFAULT_DURATION_MIN)

        duration = end - start
        if duration <= _dt.timedelta(0) or duration > _dt.timedelta(hours=_MAX_DURATION_HOURS):
            log.info("calendar_event_freeform_bad_duration")
            return None, CalendarEventOutput(error="bad_duration", message=_ERR_MSG["bad_duration"])

        now = self._now_factory()  # type: ignore[operator]
        now = now.astimezone(_JST) if now.tzinfo else now.replace(tzinfo=_JST)
        if not (
            now - _dt.timedelta(days=_PAST_LIMIT_DAYS)
            <= start
            <= now + _dt.timedelta(days=_FUTURE_LIMIT_DAYS)
        ):
            # LLM が年を取り違える（2025/2126）・相対日を誤算するのを構造的に弾く。
            log.info("calendar_event_freeform_out_of_range")
            return None, CalendarEventOutput(error="out_of_range", message=_ERR_MSG["out_of_range"])

        return (
            _ResolvedEvent(
                title=title,
                start_iso=start.isoformat(),
                end_iso=end.isoformat(),
                location=input.location.strip(),
                # 同じ枠に別件を入れられるよう title も冪等キーの素にする（連打は冪等のまま）。
                id_kind=f"freeform:{hashlib.sha256(title.encode('utf-8')).hexdigest()[:8]}",
                freeform=True,
            ),
            None,
        )
