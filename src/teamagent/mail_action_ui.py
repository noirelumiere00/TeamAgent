"""朝digestのインタラクティブ Block Kit 部品（2ボタン: 下書き作成 / Gmailを開く）。

純粋・副作用なし。配信層（run_morning_digest_fargate）と将来の OC 押下ハンドラが同じ部品を
使うため、action_id・value 形式・押下後の表示を一箇所に集約する。

- action_id は OpenClaw のプラグイン名前空間ルーティング規約（最初の `:` 前が namespace）に
  合わせ `aila:` prefix。押下は @openclaw/slack(socket) → registerPluginInteractiveHandler
  (namespace='aila') が捌く（mail_reply(target_thread_id) を呼ぶ）。
- value には Gmail thread_id（不透明 ID・非 PII）だけ入れる。生 messageId・生本文は入れない。
- 押下後に表示する下書き本文は本人宛 DM 限定の PII（ログ厳禁）。
"""

from __future__ import annotations

import json
from typing import Any

from teamagent.gmail_links import gmail_thread_url

ACTION_DRAFT = "aila:mail_draft"  # 「下書き作成」押下 → mail_reply(target_thread_id)

_DRAFT_PREVIEW_MAX = 1200


def _esc(s: str) -> str:
    """Slack mrkdwn の特殊文字（& < >）をエスケープ（リンク偽装/書式崩れ防止）。"""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def encode_value(thread_id: str) -> str:
    """ボタン value（JSON）。t=thread_id（不透明 ID・非 PII）のみ。"""
    return json.dumps({"t": thread_id}, ensure_ascii=False)


def decode_value(value: str | None) -> dict[str, str]:
    """ボタン value をデコード（壊れていても落ちない）。"""
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def mail_action_block(thread_id: str, user_email: str) -> dict[str, Any]:
    """要返信メール 1 件の [✏️ 下書き作成][📩 Gmailを開く] アクション行。

    下書き作成 = action_id 付き（url なし）＝押下が OC に届く。Gmailを開く = url ボタン
    （その案件スレッドを本人アカウントで開くだけ・押下処理不要）。
    """
    elements: list[dict[str, Any]] = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✏️ 下書き作成", "emoji": True},
            "style": "primary",
            "action_id": ACTION_DRAFT,
            "value": encode_value(thread_id),
        }
    ]
    url = gmail_thread_url(thread_id, user_email)
    if url:
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "📩 Gmailを開く", "emoji": True},
                "url": url,
            }
        )
    return {"type": "actions", "elements": elements}


def draft_taken_blocks(
    *, thread_id: str, draft_body: str, user_email: str, subject: str = ""
) -> list[dict[str, Any]]:
    """「下書き作成」押下後に元の2ボタンと差し替える表示（下書き本文＋Gmailを開く）。

    Slack 上では送信しない＝確認・送信は Gmail 側。下書き本文は本人 DM 限定の PII。
    """
    head = "✅ 返信下書きを作成しました（未送信・Slackでは送信しません）。"
    if subject:
        head += f"\n件名: {_esc(subject)}"
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": head}}
    ]
    pv = _esc((draft_body or "").strip())
    if len(pv) > _DRAFT_PREVIEW_MAX:
        pv = pv[:_DRAFT_PREVIEW_MAX].rstrip() + "…"
    if pv:
        quoted = "\n".join(">" + ln for ln in pv.split("\n"))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": quoted}})
    url = gmail_thread_url(thread_id, user_email)
    if url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "📩 Gmailを開く（確認して送信）",
                            "emoji": True,
                        },
                        "url": url,
                    }
                ],
            }
        )
    return blocks
