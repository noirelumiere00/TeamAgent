"""本人 Google 連携の失敗を、既存の開始リンクと一度だけの案内に揃える。"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar

from pydantic import BaseModel

from teamagent.skills._shared.mail_connection import (
    FETCH_NEEDS_RECONNECT,
    MailConnectionError,
    classify_google_fetch_failure,
)
from teamagent.skills.base import SkillContext

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class GoogleConnectionError(PermissionError):
    """本人の連携が必要。身元不明・設定不備の PermissionError とは区別する。"""


@dataclass
class _Call:
    failed: bool = False


_CALL: ContextVar[_Call | None] = ContextVar("google_connection_call", default=None)
_NOTICES: dict[str, float] = {}
_LOCK = threading.Lock()


def record_google_failure(
    exc: BaseException | None = None, ctx: SkillContext | None = None
) -> None:
    """部分成功として握る経路も、外側の Skill に連携失敗だけを伝える。"""
    call = _CALL.get()
    if exc is None or needs_reconnect(exc):
        if call is not None:
            call.failed = True
        elif ctx is not None:
            from teamagent.skills.oauth_connect.skill import start_links_enabled

            if start_links_enabled():
                ctx.metadata["_google_connection_failed"] = True


def needs_reconnect(exc: BaseException) -> bool:
    if isinstance(exc, GoogleConnectionError | MailConnectionError):
        return True
    if classify_google_fetch_failure(exc) in FETCH_NEEDS_RECONNECT:
        return True
    return exc.__cause__ is not None and needs_reconnect(exc.__cause__)


def _failed(output: BaseModel) -> bool:
    return getattr(output, "error", "") in {
        "not_connected",
        "reauth_needed",
        "calendar_not_connected",
    } or any(
        getattr(output, field, "") in FETCH_NEEDS_RECONNECT
        for field in ("mail_fetch", "calendar_fetch")
    )


def reconnect_message(ctx: SkillContext, *, url: str | None = None) -> str:
    from teamagent.adapters.google_oauth_flow import OAuthConsentFlow, consume_state_once
    from teamagent.hmac_durable_state import HMAC_STATE_SCOPE_ENV, HMAC_STATE_TABLE_ENV
    from teamagent.skills.oauth_connect.skill import (
        _start_link_base,
        google_start_link,
        start_links_enabled,
    )

    email = ctx.metadata.get("user_email")
    if not isinstance(email, str) or not email.strip():
        raise PermissionError("本人を確認できませんでした。")
    email = email.strip().lower()
    parts = [
        str(ctx.metadata.get(k) or "")
        for k in ("verified_slack_team_id", "channel_id", "thread_ts", "_google_connection_session")
    ]
    if parts[2]:
        parts[3] = ""
    key = hashlib.sha256("\x1f".join([email, *parts]).encode()).hexdigest()
    has_conversation = bool(parts[1] and (parts[2] or parts[3]))
    with _LOCK:
        now = time.monotonic()
        for old in [k for k, expiry in _NOTICES.items() if expiry <= now]:
            del _NOTICES[old]
        already_sent = (
            key in _NOTICES if has_conversation else ctx.metadata.get("_google_notice_key") == key
        )
        if already_sent:
            return ""
        try:
            if url is None:
                url, state = OAuthConsentFlow(
                    redirect_uri=os.environ.get("OAUTH_REDIRECT_URI", "").strip()
                ).authorization_url(email)
                base = _start_link_base()
                if start_links_enabled() and base:
                    url = google_start_link(base, state)
        except Exception as exc:
            ctx.bind_logger("google_connection").warning(
                "google_reconnect_link_failed", error=type(exc).__name__
            )
            return "Google の連携リンクを準備できませんでした。復旧をお待ちください。"
        if has_conversation:
            if os.environ.get(HMAC_STATE_TABLE_ENV) and os.environ.get(HMAC_STATE_SCOPE_ENV):
                try:
                    # OAuth state と同じ条件付き書込を別 prefix で使い、worker 間でも一度にする。
                    if not consume_state_once(key, record_prefix="GOOGLE_RECONNECT#"):
                        return ""
                except Exception as exc:
                    ctx.bind_logger("google_connection").warning(
                        "google_reconnect_notice_store_failed", error=type(exc).__name__
                    )
            # shortcut: 共有台帳が使えない場合はプロセス内で30分間抑止する。
            if len(_NOTICES) >= 4096:
                del _NOTICES[next(iter(_NOTICES))]
            _NOTICES[key] = now + 1800
        ctx.metadata["_google_notice_key"] = key
    return (
        f"Google の連携が切れています。こちらから 1 クリックで再連携できます: <{url}> "
        "再連携後に元の依頼を続けられます。"
    )


def google_connection_guard(
    empty_output: Callable[[Any], OutputT],
) -> Callable[
    [Callable[[Any, InputT, SkillContext], OutputT]], Callable[[Any, InputT, SkillContext], OutputT]
]:
    """既存の返却型を保つ。入れ子の Skill は最外側で一度だけ案内する。"""

    def decorate(
        run: Callable[[Any, InputT, SkillContext], OutputT],
    ) -> Callable[[Any, InputT, SkillContext], OutputT]:
        @wraps(run)
        def guarded(self: Any, input: InputT, ctx: SkillContext) -> OutputT:
            from teamagent.skills.oauth_connect.skill import start_links_enabled

            if not start_links_enabled():
                return run(self, input, ctx)
            parent = _CALL.get()
            call = parent or _Call()
            token = _CALL.set(call)
            try:
                try:
                    output = run(self, input, ctx)
                except Exception as exc:
                    if not needs_reconnect(exc):
                        raise
                    call.failed = True
                    if parent is not None:
                        raise
                    output = empty_output(input)
                pending = ctx.metadata.pop("_google_connection_failed", False)
                call.failed |= _failed(output) or pending
                url = getattr(output, "url", None) if self.name == "oauth_connect" else None
                call.failed |= bool(url)
                if parent is not None and call.failed:
                    raise GoogleConnectionError("Google connection required")
                if not call.failed:
                    return output
                message = reconnect_message(ctx, url=url)
                updates: dict[str, Any] = {"error": "reauth_needed", "message": message}
                for field in ("summary", "answer", "note"):
                    if field in type(output).model_fields:
                        updates[field] = message
                if self.name == "oauth_connect":
                    updates.update(url=url if message else None, slack_url=None)
                return output.model_copy(update=updates)
            finally:
                _CALL.reset(token)

        return guarded

    return decorate
