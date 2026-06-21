"""Slack interactivity（block_actions）の解析とルーティング（純粋に近いコア）。

connect_web の `POST /slack/interactivity` から呼ばれる。Slack 署名検証・本人解決
（slack_user_id→email）・実際の chat.update / response_url 送信は呼び出し側の責務。
本モジュールは「payload を解析」し「状態を更新」し「差し替え後の Block Kit を返す」だけ
＝外部 I/O を最小化してテスト容易にする。

状態遷移（mail_thread_state・RLS は呼び出し側が user_email をセット）:
  対応する  → 返信下書きを作成（draft_maker）＋スレッドリンク表示（status=open のまま）
  対応済み  → status=done（再通知停止）
  後で      → status=snoozed, snooze_until=now+N日
  今後通知しない → status=muted
  取り消す  → status=open（ボタンを復元）
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from teamagent import mail_action_ui as ui
from teamagent.adapters.mail_thread_state_store import (
    STATUS_DONE,
    STATUS_MUTED,
    STATUS_OPEN,
    STATUS_SNOOZED,
    MailThreadStateStore,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ParsedAction:
    """block_actions payload から取り出した 1 アクション。"""

    user_id: str  # 押した本人の Slack user id（email 解決は呼び出し側）
    action_id: str
    thread_id: str
    subject: str = ""
    counterpart: str = ""
    sub_action: str = ""  # overflow の selected_option（例: mute）
    channel_id: str = ""
    message_ts: str = ""
    response_url: str = ""


@dataclass(frozen=True)
class DraftResult:
    """draft_maker（mail_reply 相当）の結果（router 用の最小形）。"""

    created: bool
    thread_url: str = ""
    draft_subject: str = ""
    message: str = ""  # 失敗時の案内（未連携 等）


# draft_maker(user_email, thread_id) -> DraftResult
DraftMaker = Callable[[str, str], DraftResult]


@dataclass
class ActionOutcome:
    """ハンドラの結果（呼び出し側が chat.update / response_url で適用する）。"""

    text: str  # フォールバック通知文
    blocks: list[dict[str, Any]] = field(default_factory=list)
    status_written: str | None = None  # 監査/テスト用（書き込んだ状態）
    handled: bool = True


def parse_block_actions(payload: dict[str, Any]) -> ParsedAction | None:
    """Slack interactivity payload（block_actions）を ParsedAction へ。対象外は None。"""
    if payload.get("type") != "block_actions":
        return None
    actions = payload.get("actions") or []
    if not actions:
        return None
    act = actions[0] or {}
    action_id = str(act.get("action_id", ""))
    # overflow は selected_option.value、button は value。
    raw_value = act.get("value")
    sub_action = ""
    if raw_value is None and isinstance(act.get("selected_option"), dict):
        raw_value = act["selected_option"].get("value")
    decoded = ui.decode_value(raw_value if isinstance(raw_value, str) else None)
    sub_action = decoded.get("a", "")
    thread_id = decoded.get("t", "")
    if not action_id or not thread_id:
        return None
    return ParsedAction(
        user_id=str((payload.get("user") or {}).get("id", "")),
        action_id=action_id,
        thread_id=thread_id,
        subject=decoded.get("s", ""),
        counterpart=decoded.get("c", ""),
        sub_action=sub_action,
        channel_id=str((payload.get("channel") or {}).get("id", "")),
        message_ts=str((payload.get("message") or {}).get("ts", "")),
        response_url=str(payload.get("response_url", "")),
    )


class InteractivityRouter:
    """ParsedAction → 状態更新 → 差し替え Block Kit。"""

    def __init__(
        self,
        state_store: MailThreadStateStore,
        *,
        draft_maker: DraftMaker | None = None,
        snooze_days: int = 3,
    ) -> None:
        self._store = state_store
        self._draft_maker = draft_maker
        self._snooze_days = snooze_days

    def handle(self, action: ParsedAction, user_email: str, *, now: _dt.datetime) -> ActionOutcome:
        t, s, c = action.thread_id, action.subject, action.counterpart
        aid = action.action_id

        if aid == ui.ACTION_DONE:
            self._store.set_status(
                user_email, t, STATUS_DONE, subject_scrubbed=s, counterpart_masked=c
            )
            return ActionOutcome(
                text="✅ 対応済みにしました",
                blocks=ui.done_blocks(thread_id=t, subject=s, counterpart=c),
                status_written=STATUS_DONE,
            )

        if aid == ui.ACTION_SNOOZE:
            until = now + _dt.timedelta(days=self._snooze_days)
            self._store.set_status(
                user_email,
                t,
                STATUS_SNOOZED,
                snooze_until=until,
                subject_scrubbed=s,
                counterpart_masked=c,
            )
            return ActionOutcome(
                text=f"⏰ {self._snooze_days}日後に再通知します",
                blocks=ui.snoozed_blocks(
                    thread_id=t, subject=s, counterpart=c, days=self._snooze_days
                ),
                status_written=STATUS_SNOOZED,
            )

        if aid == ui.ACTION_MENU and action.sub_action == ui.MENU_MUTE:
            self._store.set_status(
                user_email, t, STATUS_MUTED, subject_scrubbed=s, counterpart_masked=c
            )
            return ActionOutcome(
                text="🔕 今後通知しません",
                blocks=ui.muted_blocks(thread_id=t, subject=s, counterpart=c),
                status_written=STATUS_MUTED,
            )

        if aid == ui.ACTION_UNDO:
            self._store.set_status(
                user_email, t, STATUS_OPEN, subject_scrubbed=s, counterpart_masked=c
            )
            return ActionOutcome(
                text="↩️ 取り消しました",
                blocks=ui.summary_item_blocks(thread_id=t, subject=s, counterpart=c),
                status_written=STATUS_OPEN,
            )

        if aid == ui.ACTION_TAKE:
            # 行を確保（reminder が拾わないよう open）。下書きは draft_maker に委譲。
            self._store.set_status(
                user_email, t, STATUS_OPEN, subject_scrubbed=s, counterpart_masked=c
            )
            return self._handle_take(user_email, t, s, c)

        logger.info("interactivity_unknown_action", action_id=aid)
        return ActionOutcome(text="", handled=False)

    def _handle_take(self, user_email: str, t: str, s: str, c: str) -> ActionOutcome:
        from teamagent.gmail_links import gmail_thread_url

        if self._draft_maker is None:
            # draft 生成器が無い構成: スレッドを開く導線だけ出す。
            url = gmail_thread_url(t) or ""
            return ActionOutcome(
                text="📩 Gmailでスレッドを開く",
                blocks=ui.taken_blocks(
                    thread_id=t, subject=s, counterpart=c, thread_url=url, note=""
                ),
                status_written=STATUS_OPEN,
            )
        try:
            res = self._draft_maker(user_email, t)
        except Exception as exc:  # 失敗は握ってユーザー向け案内に寄せる（落とさない）。
            logger.warning("interactivity_take_draft_failed", err=type(exc).__name__)
            res = DraftResult(created=False, message=str(exc))

        if res.created:
            url = res.thread_url or gmail_thread_url(t) or ""
            return ActionOutcome(
                text="✅ 返信下書きを作成しました",
                blocks=ui.taken_blocks(
                    thread_id=t,
                    subject=res.draft_subject or s,
                    counterpart=c,
                    thread_url=url,
                ),
                status_written=STATUS_OPEN,
            )
        # 失敗（未連携・権限不足等）: 元のボタンを残しつつ案内を出す。
        msg = res.message or "下書きを作成できませんでした。`連携` で Google を認可してください。"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ {msg}"}},
            *ui.summary_item_blocks(thread_id=t, subject=s, counterpart=c),
        ]
        return ActionOutcome(text=msg, blocks=blocks, status_written=STATUS_OPEN)
