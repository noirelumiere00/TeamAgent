"""Fargate Scheduled Task: メール「後で」リマインダの再通知スキャン。

EventBridge cron（平日日中・例 0,3,6 UTC）が ECS RunTask で本スクリプトを起動する。
mail_thread_state で status=snoozed かつ snooze_until<=now の行を抽出し、本人の Slack DM に
同じボタン付きカードを再投稿して、status を open に戻す（one-shot 再通知＝多重ナグ防止）。

⚠️ 安全規則:
  - 生件名・生 From は出さない（subject_scrubbed / counterpart_masked は DLP マスク後）。
  - ボタンを処理する第2 App の bot token（INTERACTIVE_MAIL_BOT_TOKEN）で投稿する
    （SLACK_BOT_TOKEN=OpenClaw App で投稿するとボタン押下が interactivity 経路に来ない）。
  - 全行走査は RLS の admin ロールで行う（store 側で app.user_role='admin'）。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import sys
import uuid
from typing import Any

from teamagent import mail_action_ui as ui


def _build_state_store() -> Any:
    from teamagent.adapters.mail_thread_state_store import RdsMailThreadStateStore
    from teamagent.adapters.pgvector_client import PgVectorClient

    return RdsMailThreadStateStore(PgVectorClient.from_env())


def _make_slack() -> Any:
    """第2 App（ボタン処理する App）の bot token で投稿する SlackClient。"""
    from teamagent.adapters.slack_client import SlackClient

    token = os.environ.get("INTERACTIVE_MAIL_BOT_TOKEN", "").strip()
    if token:
        return SlackClient(bot_token=token)
    return SlackClient.from_env()


def reminder_card_blocks(item: Any) -> list[dict[str, Any]]:
    """1 件の再通知カード（見出し context + summary_item_blocks のボタン群）。純粋・テスト容易。"""
    blocks: list[dict[str, Any]] = [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "📌 *再通知*（「後で」の期限が来ました）"}],
        }
    ]
    blocks.extend(
        ui.summary_item_blocks(
            thread_id=str(getattr(item, "thread_id", "") or ""),
            subject=str(getattr(item, "subject_scrubbed", "") or ""),
            counterpart=str(getattr(item, "counterpart_masked", "") or ""),
        )
    )
    return blocks


async def _email_to_slack_user_id(slack: Any, email: str) -> str | None:
    client = getattr(slack, "_client", None)
    if client is None:
        return None
    try:
        resp = await client.users_lookupByEmail(email=email)
        return str(resp.get("user", {}).get("id", "")) or None
    except Exception as exc:
        print(
            f"[run_reminder_scan_fargate] WARN: lookupByEmail 失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
        return None


async def _open_im_channel(slack: Any, user_id: str) -> str | None:
    client = getattr(slack, "_client", None)
    if client is None:
        return None
    try:
        resp = await client.conversations_open(users=user_id)
        return str(resp.get("channel", {}).get("id", "")) or None
    except Exception as exc:
        print(
            f"[run_reminder_scan_fargate] WARN: conversations.open 失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
        return None


async def _deliver_reminders(store: Any, slack: Any, due: list[Any], now: _dt.datetime) -> int:
    """due 各件を本人 DM に再通知し、status を open に戻す。再通知できた件数を返す。"""
    sent = 0
    # ユーザーごとに IM channel を1回だけ解決する。
    channel_by_email: dict[str, str | None] = {}
    for item in due:
        email = str(getattr(item, "user_email", "") or "")
        thread_id = str(getattr(item, "thread_id", "") or "")
        if not email or not thread_id:
            continue
        if email not in channel_by_email:
            uid = await _email_to_slack_user_id(slack, email)
            channel_by_email[email] = await _open_im_channel(slack, uid) if uid else None
        channel = channel_by_email[email]
        if not channel:
            continue
        try:
            await slack.post_message(
                channel=channel,
                text=f"📌 再通知: {getattr(item, 'subject_scrubbed', '') or '(件名なし)'}",
                request_id=f"reminder-{uuid.uuid4().hex[:8]}",
                blocks=reminder_card_blocks(item),
            )
            store.reopen_after_reminder(email, thread_id, now)
            sent += 1
        except Exception as exc:
            print(
                f"[run_reminder_scan_fargate] WARN: 再通知失敗 {type(exc).__name__}",
                file=sys.stderr,
            )
    return sent


def main() -> int:
    now = _dt.datetime.now(_dt.UTC)
    try:
        store = _build_state_store()
        due = store.list_due(now)
    except Exception as exc:
        print(
            f"[run_reminder_scan_fargate] WARN: due 抽出失敗 {type(exc).__name__}", file=sys.stderr
        )
        return 0
    if not due:
        print("[run_reminder_scan_fargate] no due reminders", flush=True)
        return 0
    slack = _make_slack()
    sent = asyncio.run(_deliver_reminders(store, slack, due, now))
    print(
        f"[run_reminder_scan_fargate] done {json.dumps({'due': len(due), 'sent': sent})}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
