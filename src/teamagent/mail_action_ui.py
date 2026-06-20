"""メールサマリーのインタラクティブ Block Kit 部品（純粋・副作用なし）。

morning_digest 配信 / reminder 再通知 / interactivity ハンドラ（取り消し時の復元）が
同じ部品を使うため、ボタンの action_id・value 形式・押下後の表示を一箇所に集約する。

⚠️ ボタン value には Gmail スレッド ID と DLP マスク済みの件名/相手のみ入れる
（生 messageId・生本文は入れない）。value は JSON 文字列（Slack の 2000 文字上限内）。
"""

from __future__ import annotations

import json
from typing import Any

from teamagent.gmail_links import gmail_thread_url

# action_id（interactivity ハンドラのルーティングキー）。
ACTION_TAKE = "mail_take"  # 対応する → 返信下書きを作成
ACTION_DONE = "mail_done"  # 対応済み → 再通知停止
ACTION_SNOOZE = "mail_snooze"  # 後で → N 日後に再通知
ACTION_MENU = "mail_menu"  # overflow（… メニュー）
ACTION_UNDO = "mail_undo"  # 取り消す → open に戻す

# overflow メニューのサブアクション（selected_option.value の "a"）。
MENU_MUTE = "mute"  # 今後通知しない


def encode_value(thread_id: str, subject: str, counterpart: str, *, sub: str = "") -> str:
    """ボタン value（JSON）。t=thread_id / s=件名(マスク) / c=相手(マスク) / a=サブアクション。"""
    payload: dict[str, str] = {"t": thread_id, "s": subject or "", "c": counterpart or ""}
    if sub:
        payload["a"] = sub
    return json.dumps(payload, ensure_ascii=False)


def decode_value(value: str | None) -> dict[str, str]:
    """ボタン value をデコード（壊れていても落ちない）。"""
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _action_elements(thread_id: str, subject: str, counterpart: str) -> list[dict[str, Any]]:
    """[対応する][対応済み][後で][…] のアクション要素列。"""
    val = encode_value(thread_id, subject, counterpart)
    return [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "対応する", "emoji": True},
            "style": "primary",
            "action_id": ACTION_TAKE,
            "value": val,
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "対応済み", "emoji": True},
            "action_id": ACTION_DONE,
            "value": val,
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "後で", "emoji": True},
            "action_id": ACTION_SNOOZE,
            "value": val,
        },
        {
            "type": "overflow",
            "action_id": ACTION_MENU,
            "options": [
                {
                    "text": {"type": "plain_text", "text": "🔕 今後通知しない", "emoji": True},
                    "value": encode_value(thread_id, subject, counterpart, sub=MENU_MUTE),
                }
            ],
        },
    ]


def summary_item_blocks(
    *,
    thread_id: str,
    subject: str,
    counterpart: str,
    summary: str = "",
    importance_emoji: str = "🔴",
) -> list[dict[str, Any]]:
    """1 メール分の「本文 section + スレッドリンク + アクションボタン」ブロック群（open 状態）。"""
    head = f"{importance_emoji} *{subject or '(件名なし)'}* — {counterpart or '***'}"
    if summary:
        head += f"\n_{summary}_"
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]
    url = gmail_thread_url(thread_id)
    if url:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"<{url}|Gmailでスレッドを開く>"}],
            }
        )
    blocks.append(
        {"type": "actions", "elements": _action_elements(thread_id, subject, counterpart)}
    )
    return blocks


def _undo_action(thread_id: str, subject: str, counterpart: str) -> dict[str, Any]:
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "↩️ 取り消す", "emoji": True},
                "action_id": ACTION_UNDO,
                "value": encode_value(thread_id, subject, counterpart),
            }
        ],
    }


def taken_blocks(
    *, thread_id: str, subject: str, counterpart: str, thread_url: str, note: str = ""
) -> list[dict[str, Any]]:
    """「対応する」後＝返信下書き作成済 + スレッドを開く導線。"""
    text = "✅ 返信下書きを作成しました（Gmail の下書きに保存済・送信していません）。"
    if subject:
        text += f"\n件名: {subject}"
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if thread_url:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<{thread_url}|📩 Gmailでスレッドを開く（下書きを確認して送信）>",
                },
            }
        )
    if note:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    # 送信後に閉じられるよう「対応済み」だけ残す。
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "対応済み", "emoji": True},
                    "action_id": ACTION_DONE,
                    "value": encode_value(thread_id, subject, counterpart),
                }
            ],
        }
    )
    return blocks


def done_blocks(*, thread_id: str, subject: str, counterpart: str) -> list[dict[str, Any]]:
    head = "✅ 対応済みにしました（誤りなら ↩️取り消す で戻せます）。"
    if subject:
        head += f"\n_{subject}_"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": head}},
        _undo_action(thread_id, subject, counterpart),
    ]


def snoozed_blocks(
    *, thread_id: str, subject: str, counterpart: str, days: int = 3
) -> list[dict[str, Any]]:
    head = f"⏰ {days}日後に再通知します（↩️取り消す で今すぐ戻せます）。"
    if subject:
        head += f"\n_{subject}_"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": head}},
        _undo_action(thread_id, subject, counterpart),
    ]


def muted_blocks(*, thread_id: str, subject: str, counterpart: str) -> list[dict[str, Any]]:
    head = "🔕 このスレッドは今後通知しません（↩️取り消す で戻せます）。"
    if subject:
        head += f"\n_{subject}_"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": head}},
        _undo_action(thread_id, subject, counterpart),
    ]
