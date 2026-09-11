"""Google Calendar API（v3）アダプタ（Workspace 統合・W2 + v0.3 Task2 書込基盤）。

`events.list` で予定を取得（商談タイミング・面談履歴・接触頻度＝営業文脈）し、v0.3 Task2 で
`insert_event`（登録提案の確定/仮予定）と `freebusy`（空き枠計算）を追加。per-user OAuth
（`from_user_token`）対応＝本人のカレンダーにしか触れない（G1）。

書込の安全境界（G4・旧「書込スコープは持たない」判断の v0.3 での意図的更新）:
  - 許可は **events.insert / freebusy.query（＋既存の read 系）のみ**。
  - delete / update / patch / move / clear / quickAdd / import / acl・calendars・
    calendarList の変更系は `_GCalSafePolicy`（gmail_client と同型の denylist 物理封鎖）で
    **API 呼び出し自体を到達不能**にする（スコープでは防げない層の最終防衛）。
  - `insert_event` は **sendUpdates="none" を強制**し **attendees を受け付けない**
    （相手カレンダーへ招待が飛ばない＝Step0 裁定「自分のカレンダーのみ」）。

認証パターンは他の Google アダプタと統一。googleapiclient / google ライブラリは遅延 import。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, overload

import structlog

from teamagent.adapters.gmail_client import _GmailSafePolicy, _PolicyEnforcedResource
from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.observability import capture_skill_exception

logger = structlog.get_logger(__name__)

# -----------------------------------------------------------
# 破壊的メソッド denylist（adapter 層で物理封鎖・gmail_client と同型）
# -----------------------------------------------------------
# 出典: Google Calendar API v3 公式リファレンス
#   https://developers.google.com/calendar/api/v3/reference
# 許可するのは events.insert / freebusy.query / read 系のみ。以下は到達不能にする。
_GCAL_DESTRUCTIVE_METHODS: frozenset[str] = frozenset(
    {
        # 予定の削除・変更・移動（無断確定/削除の禁止＝指示書 0-4）
        "events.delete",
        "events.update",
        "events.patch",
        "events.move",
        # insert 以外の作成経路（テキスト解釈で作る quickAdd / 一括注入の import は封鎖し
        # 「構造化された insert_event 1 本」に集約＝sendUpdates/attendees 強制を迂回させない）。
        # ⚠️ googleapiclient は Python 予約語にアンダースコアを付ける（import → import_）。
        #    denylist は「実属性名」で書くこと（表記ズレ＝封鎖無効。実在性はテストで照合）。
        "events.quickAdd",
        "events.import_",
        # push 通知チャネル（外部副作用）
        "events.watch",
        "settings.watch",
        "channels.stop",
        # batch 経由の一括実行は個別 request の assert_safe を踏まずに denylist を
        # 迂回できる（レビューで PoC 実証）ため、batch 自体を封鎖する。
        "new_batch_http_request",
        # カレンダー自体の作成/変更/全消去
        "calendars.insert",
        "calendars.update",
        "calendars.patch",
        "calendars.delete",
        "calendars.clear",
        # カレンダー一覧の変更
        "calendarList.insert",
        "calendarList.update",
        "calendarList.patch",
        "calendarList.delete",
        "calendarList.watch",
        # 共有設定（acl）の変更＝情報持ち出し・公開事故の経路
        "acl.insert",
        "acl.update",
        "acl.patch",
        "acl.delete",
        "acl.watch",
    }
)


class _GCalSafePolicy(_GmailSafePolicy):
    """Calendar 版の破壊的呼び出し物理封鎖（gmail_client の enforcement 機構を再利用）。

    `_PolicyEnforcedResource` が service への属性チェーンを method path に組み立て、
    execute() 直前に `assert_safe` を呼ぶ。denylist 該当は RuntimeError（Sentry 通知付き）。
    """

    def __init__(self) -> None:
        super().__init__(denylist=_GCAL_DESTRUCTIVE_METHODS)

    def assert_safe(self, method_path: str) -> None:
        if method_path not in self._denylist:
            return
        logger.error(
            "gcalendar_destructive_call_blocked",
            method_path=method_path,
            policy="GCalSafePolicy",
            scope="calendar.events",
        )
        exc = RuntimeError(
            f"Calendar destructive method '{method_path}' is blocked by adapter-layer denylist. "
            "Even though the OAuth scope grants write access, this method is physically "
            "unreachable through GCalendarClient. If this call is legitimate, the policy must "
            "be revised explicitly (see _GCAL_DESTRUCTIVE_METHODS)."
        )
        capture_skill_exception(
            exc,
            request_id="gcalendar_adapter_policy",
            skill="gcalendar_adapter",
            extra={"method_path": method_path},
        )
        raise exc


@dataclass(frozen=True)
class CalendarEvent:
    """予定1件（営業文脈に必要な最小フィールド）。"""

    event_id: str
    summary: str
    start: str  # ISO 文字列（dateTime か date）
    end: str
    attendees: tuple[str, ...]  # 参加者メール（マスクは上位層の責務）
    location: str = ""  # 会議室/場所（自由文・URL のこともある）
    meeting_url: str = ""  # 会議リンク（Meet=hangoutLink / Zoom 等=conferenceData）
    # 終日予定か。Google は終日を start.date（時刻なし）で返すため **key の有無**が真実源。
    # 文字列に "T" が無いことで判定すると、経路によって "2026-08-21T00:00:00Z" に整形された
    # 終日を時刻付きと誤認する（2026-08-20 の日付ずれ調査で実測）。
    all_day: bool = False


@dataclass(frozen=True)
class CalendarEventDetail(CalendarEvent):
    """予定 1 件＋**要求されたときだけ**取る追加フィールド（pre_meeting_brief 専用）。

    ⚠️ なぜ ``CalendarEvent`` 本体に足さないか: ``description`` は最大 4000 字の自由文で、
    予定を読むすべての消費者（workspace_search / schedule_propose / morning_digest の
    予定表示）に常時同乗させると、ログ・LLM プロンプト・Slack 描画のどこかへ第三者が
    書いた本文が黙って乗る経路が増える。取得は ``extract_events(want_description=True)``
    を明示した呼び出しだけに限定し、型でも別物にしておく。

    ``attendee_list_available`` は「参加者リストが本当に見えているか」。⚠️ Google は
    ゲストリスト非表示（``guestsCanSeeOtherGuests=false``）のとき **空配列を返さず
    本人＋主催者を返す**ため、``bool(attendees)`` では判定できない（実測・付録参照）。
    """

    description: str = ""
    # 参加者のドメインのみ（ローカル部は保持しない＝情報最小化）。登場順・重複排除。
    attendee_domains: tuple[str, ...] = ()
    # 参加者リストが「見えている」か。False は「社外参加者ゼロ」ではなく「判らない」。
    attendee_list_available: bool = False
    organizer_domain: str = ""


def _email_domain(email: str) -> str:
    """メールのドメイン部を小文字で返す（不正なら空文字）。"""
    local, sep, domain = (email or "").strip().lower().partition("@")
    if not sep or not local or not domain or "." not in domain:
        return ""
    return domain


def _attendee_visibility(it: dict[str, Any]) -> tuple[tuple[str, ...], bool]:
    """参加者ドメイン一覧と「リストが見えているか」を返す。

    判定（**空配列判定にしない**）:
      - ``attendeesOmitted=True`` → 見えていない（Google が明示的に省略した）
      - ``guestsCanSeeOtherGuests=False`` → 見えていない（本人＋主催者しか返らない）
      - attendees が空 → 見えていない（そもそも情報が無い）
      - ``self``/主催者以外の attendee が 1 件も無い → 見えていない
    """
    attendees = it.get("attendees") or []
    organizer_email = str(((it.get("organizer") or {}).get("email")) or "")
    domains: list[str] = []
    others = 0
    for a in attendees:
        email = str(a.get("email") or "")
        if not email:
            continue
        dom = _email_domain(email)
        if dom and dom not in domains:
            domains.append(dom)
        if not a.get("self") and email.strip().lower() != organizer_email.strip().lower():
            others += 1
    available = bool(attendees) and not bool(it.get("attendeesOmitted"))
    if it.get("guestsCanSeeOtherGuests") is False:
        available = False
    if others == 0:
        available = False
    return (tuple(domains[:10]), available)


def _extract_meeting_url(it: dict[str, Any]) -> str:
    """Google Meet(hangoutLink) か conferenceData の video entryPoint から会議 URL を取る。"""
    hangout = str(it.get("hangoutLink") or "")
    if hangout:
        return hangout
    conf = it.get("conferenceData") or {}
    for ep in conf.get("entryPoints") or []:
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return str(ep["uri"])
    return ""


_EVENT_ID_RE = re.compile(r"^[a-v0-9]{5,1024}$")  # Google 規定の base32hex（小文字）


def _require_offset(iso: str, field: str) -> None:
    """dateTime に UTC offset が無ければ ValueError（Google は naive を 400 で拒否する）。

    LLM 由来の naive ISO（例 "2026-07-15T10:00:00"）を本番 400 にせず早期に落とす。
    日付のみ（終日）は対象外。
    """
    if "T" not in iso:
        return
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"{field} が ISO8601 として不正です: {e}") from e
    if parsed.tzinfo is None:
        raise ValueError(
            f"{field} に UTC offset がありません（例 '+09:00'）。"
            "Google Calendar API は naive dateTime を 400 で拒否します"
        )


def _event_time(iso: str) -> dict[str, str]:
    """ISO 文字列を events.insert の start/end 形式へ（日付のみ=終日 date / 他は dateTime）。"""
    return {"date": iso} if "T" not in iso else {"dateTime": iso}


@overload
def extract_events(items: list[dict[str, Any]]) -> list[CalendarEvent]: ...


@overload
def extract_events(
    items: list[dict[str, Any]], *, want_description: Literal[False]
) -> list[CalendarEvent]: ...


@overload
def extract_events(
    items: list[dict[str, Any]], *, want_description: Literal[True]
) -> list[CalendarEventDetail]: ...


def extract_events(
    items: list[dict[str, Any]], *, want_description: bool = False
) -> list[CalendarEvent] | list[CalendarEventDetail]:
    """events.list の items[] を CalendarEvent 群へ変換する。

    ``want_description=True`` のときだけ ``CalendarEventDetail``（description /
    参加者ドメイン / 参加者リスト可視性 / 主催者ドメイン付き）を返す。既定は従来どおり
    ``CalendarEvent``＝既存の全消費者の型・内容は 1 バイトも変わらない。
    """
    if want_description:
        detailed: list[CalendarEventDetail] = []
        for it in items or []:
            base = _base_fields(it)
            domains, available = _attendee_visibility(it)
            detailed.append(
                CalendarEventDetail(
                    **base,
                    description=str(it.get("description", "") or ""),
                    attendee_domains=domains,
                    attendee_list_available=available,
                    organizer_domain=_email_domain(
                        str(((it.get("organizer") or {}).get("email")) or "")
                    ),
                )
            )
        return detailed
    return [CalendarEvent(**_base_fields(it)) for it in items or []]


def _base_fields(it: dict[str, Any]) -> dict[str, Any]:
    """CalendarEvent の共通フィールドを 1 箇所で組む（2 経路で解釈をズラさない）。"""
    start_obj = it.get("start") or {}
    end_obj = it.get("end") or {}
    start = start_obj.get("dateTime") or start_obj.get("date") or ""
    end = end_obj.get("dateTime") or end_obj.get("date") or ""
    # 終日判定は date key の有無で行う（値の文字列形ではなく API の構造で見る）。
    all_day = not start_obj.get("dateTime") and bool(start_obj.get("date"))
    attendees = tuple(str(a.get("email")) for a in (it.get("attendees") or []) if a.get("email"))
    return {
        "event_id": str(it.get("id", "")),
        "summary": str(it.get("summary", "")),
        "start": str(start),
        "end": str(end),
        "attendees": attendees,
        "location": str(it.get("location", "") or ""),
        "meeting_url": _extract_meeting_url(it),
        "all_day": all_day,
    }


class DuplicateEventError(Exception):
    """同一 event_id の予定が既に存在する（409 duplicate）。

    冪等キー付き insert のボタン連打・再送で発生する正常系エラー。skill 層は
    「既に登録済みのようです」と応答する（⚠️ UI から手動削除された同 id でも
    409 になるため、表示と実態がズレる可能性は skill 層の文言で吸収する）。
    3層分離: skill に googleapiclient.HttpError を漏らさないためのドメイン例外。
    """


@dataclass(frozen=True)
class InsertedEvent:
    """insert_event の結果（本人カレンダーに作成された予定）。"""

    event_id: str
    html_link: str  # Google カレンダーで開く URL（Slack ボタン用）
    summary: str
    start: str
    end: str
    status: str  # "confirmed" / "tentative"


@dataclass(frozen=True)
class FreeBusyBlock:
    """freebusy.query の busy 区間 1 件（ISO 文字列）。"""

    start: str
    end: str


class GCalendarClient:
    """Google Calendar API v3 の薄ラッパー（read + 封鎖付き最小 write）。"""

    SCOPES_READONLY: tuple[str, ...] = ("https://www.googleapis.com/auth/calendar.readonly",)
    # v0.3 Task2: 書込は events スコープ（insert のみ実装・他は _GCalSafePolicy で封鎖）。
    SCOPES_WRITE: tuple[str, ...] = (
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    )

    def __init__(
        self,
        credentials: Any | None = None,
        *,
        service: Any | None = None,
        scopes: tuple[str, ...] | None = None,
    ) -> None:
        self._credentials = credentials
        self._service = service
        self._scopes = scopes or self.SCOPES_READONLY
        self._policy = _GCalSafePolicy()

    @classmethod
    def from_env(cls) -> GCalendarClient:
        if not (
            os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        ):
            logger.warning(
                "gcalendar_credentials_missing",
                hint="GOOGLE_CLIENT_ID + refresh token、または GOOGLE_APPLICATION_CREDENTIALS",
            )
        return cls(credentials=None)

    @classmethod
    def from_user_token(cls, token: OAuthToken) -> GCalendarClient:
        """per-user: 本人の refresh token から構築（本人のカレンダーのみ参照可）。"""
        from teamagent.adapters.google_auth import build_user_credentials

        return cls(credentials=build_user_credentials(token), scopes=cls.SCOPES_READONLY)

    def list_events(
        self,
        request_id: str,
        *,
        query: str | None = None,
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 20,
        calendar_id: str = "primary",
        want_description: bool = False,
    ) -> list[CalendarEvent]:
        """events.list で予定を取得（q でクライアント名等に絞れる）。

        ``want_description=True`` のときだけ ``CalendarEventDetail``（description・参加者
        ドメイン・参加者リスト可視性つき）を返す。既定 False の呼び出し（既存の全消費者）
        の戻り値は従来と 1 バイトも変わらない。
        """
        service = self._ensure_service()
        params: dict[str, Any] = {
            "calendarId": calendar_id,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if query:
            params["q"] = query
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max

        start = time.perf_counter()
        resp = service.events().list(**params).execute()
        latency_ms = int((time.perf_counter() - start) * 1000)

        items = resp.get("items", []) or []
        events: list[CalendarEvent] = (
            list(extract_events(items, want_description=True))
            if want_description
            else list(extract_events(items))
        )
        logger.info(
            "gcalendar_list_events",
            request_id=request_id,
            query_len=len(query) if query else 0,
            returned=len(events),
            # 上限に張り付いた日は「取り切れていない」＝後続の予定が丸ごと落ちている可能性。
            saturated=len(events) >= max_results,
            latency_ms=latency_ms,
        )
        return events

    def insert_event(
        self,
        request_id: str,
        *,
        summary: str,
        start_iso: str,
        end_iso: str,
        description: str = "",
        location: str = "",
        tentative: bool = False,
        transparent: bool = False,
        event_id: str | None = None,
    ) -> InsertedEvent:
        """events.insert（v0.3 Task2）。**sendUpdates="none" 強制・attendees 不可**。

        - attendees を受け付けない API 面にすることで「相手カレンダーへ招待が飛ぶ」事故を
          型レベルで排除（Step0 裁定: 自分のカレンダーのみ）。
        - ``tentative=True`` で仮予定（Task4 の日程候補ホールド用）。
        - ``event_id`` は冪等キー（Task3 のボタン連打対策）。呼び出し側が HMAC トークン等
          から安定 id（base32hex 小文字 [a-v0-9]{5,1024}・形式はここで検証）を導出して
          渡すと、同一 id の再作成は Google 側が 409 duplicate を返す（本 adapter は握らず
          上げる＝skill 層で「既に登録済み」と応答する）。⚠️ UI から手動削除された同 id
          （cancelled 状態）でも 409 が返る＝「登録済み」表示と実態がズレうる点は skill 層で考慮。
        - ``transparent=True`` で freebusy に busy として乗らない予定にする（Task4 の
          候補ホールドが自分の空き枠計算を潰さないための knob。既定は busy=opaque）。
        - 書込先は **primary（本人カレンダー）固定**。共有カレンダー id を受けない＝
          他ユーザーへ通知が飛ぶ唯一の経路（購読者付き共有カレンダー）を API 面で排除。
        - start/end は offset 付き ISO 必須（naive は Google が 400 のため早期 ValueError）。
        """
        _require_offset(start_iso, "start_iso")
        _require_offset(end_iso, "end_iso")
        if event_id is not None and not _EVENT_ID_RE.match(event_id):
            raise ValueError(
                "event_id は base32hex 小文字 [a-v0-9]{5,1024} 形式が必要です"
                "（hex digest はそのまま使用可・base64url は不可）"
            )
        service = self._ensure_service()
        body: dict[str, Any] = {
            "summary": summary,
            "start": _event_time(start_iso),
            "end": _event_time(end_iso),
            "status": "tentative" if tentative else "confirmed",
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if event_id:
            body["id"] = event_id
        if transparent:
            body["transparency"] = "transparent"  # freebusy に busy として乗せない

        start = time.perf_counter()
        try:
            resp = (
                service.events()
                .insert(calendarId="primary", body=body, sendUpdates="none")
                .execute()
            )
        except Exception as e:
            status = getattr(getattr(e, "resp", None), "status", None) or getattr(
                e, "status_code", None
            )
            if str(status) == "409":
                raise DuplicateEventError(str(event_id or "")) from e
            raise
        logger.info(
            "gcalendar_insert_event",
            request_id=request_id,
            tentative=tentative,
            has_event_id=bool(event_id),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return InsertedEvent(
            event_id=str(resp.get("id", "")),
            html_link=str(resp.get("htmlLink", "")),
            summary=str(resp.get("summary", "")),
            start=str(
                (resp.get("start") or {}).get("dateTime")
                or (resp.get("start") or {}).get("date")
                or ""
            ),
            end=str(
                (resp.get("end") or {}).get("dateTime") or (resp.get("end") or {}).get("date") or ""
            ),
            status=str(resp.get("status", "")),
        )

    def freebusy(
        self,
        request_id: str,
        *,
        time_min: str,
        time_max: str,
        calendar_id: str = "primary",
    ) -> list[FreeBusyBlock]:
        """freebusy.query（v0.3 Task2）。本人の busy 区間を返す（空き枠計算は skill 層の責務）。"""
        _require_offset(time_min, "time_min")
        _require_offset(time_max, "time_max")
        service = self._ensure_service()
        body = {"timeMin": time_min, "timeMax": time_max, "items": [{"id": calendar_id}]}
        start = time.perf_counter()
        resp = service.freebusy().query(body=body).execute()
        busy_raw = ((resp.get("calendars") or {}).get(calendar_id) or {}).get("busy") or []
        blocks = [
            FreeBusyBlock(start=str(b.get("start", "")), end=str(b.get("end", "")))
            for b in busy_raw
            if b.get("start") and b.get("end")
        ]
        logger.info(
            "gcalendar_freebusy",
            request_id=request_id,
            busy_blocks=len(blocks),
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
        return blocks

    def _ensure_service(self) -> Any:
        from googleapiclient.discovery import build

        if self._service is None:
            if self._credentials is None:
                self._credentials = self._build_credentials()
            self._service = build(
                "calendar", "v3", credentials=self._credentials, cache_discovery=False
            )
        # 全 API 呼び出しを denylist 物理封鎖でラップ（テスト注入 service も同様に包む＝
        # どの経路でも events.delete 等は到達不能）。二重ラップは無害（path は毎回新規）。
        if isinstance(self._service, _PolicyEnforcedResource):
            return self._service
        return _PolicyEnforcedResource(self._service, self._policy)

    def _build_credentials(self) -> Any:
        sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if sa_path:
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(
                sa_path, scopes=self._scopes
            )
        refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
        client_id = os.environ.get("GOOGLE_CLIENT_ID")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
        if refresh_token and client_id and client_secret:
            from google.oauth2.credentials import Credentials

            return Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=list(self._scopes),
            )
        raise ValueError("Google 資格情報が未設定です（from_user_token か env を設定してください）")
