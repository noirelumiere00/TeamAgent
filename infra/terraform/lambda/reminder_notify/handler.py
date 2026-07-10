"""予定リマインド通知 Lambda（v0.3 Task 5）— SQS(teamagent-reminders.fifo) consumer。

EventBridge Scheduler が「予定開始 N 分前」に SQS へ投げた payload を受け、
本人の Slack DM へ chat.postMessage する。stdlib + boto3 のみ（依存パッケージ無し）。

payload（PII なし・adapters/scheduler_client.py が唯一の生成元）:
  {"v": 1, "channel": "D…", "start_hm": "14:00", "url": "https://…"}

設計:
  - batch_size=1（部分失敗の複雑さを持たない）。失敗は raise → SQS リトライ → DLQ
    （DLQ 滞留は CloudWatch Alarm → ops SNS へ・v0.3 §2.5）
  - Slack bot token は Secrets Manager から cold start 時に1回取得
    （env SLACK_BOT_TOKEN 直指定はローカルテスト用）
  - 通知文に予定タイトルは含めない（G3: SQS/Lambda/CloudWatch に PII を流さない設計）
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

_TOKEN_CACHE: dict[str, str] = {}


def _slack_token() -> str:
    direct = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if direct:
        return direct
    if "token" in _TOKEN_CACHE:
        return _TOKEN_CACHE["token"]
    import boto3

    name = os.environ["SLACK_BOT_TOKEN_SECRET_NAME"]
    resp = boto3.client("secretsmanager").get_secret_value(SecretId=name)
    token = str(resp.get("SecretString", "")).strip()
    if not token:
        raise RuntimeError("slack bot token secret is empty")
    _TOKEN_CACHE["token"] = token
    return token


def _post_message(channel: str, text: str) -> None:
    body = json.dumps({"channel": channel, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=body,
        headers={
            "Authorization": f"Bearer {_slack_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("ok"):
        # 失敗は raise → SQS リトライ（最終的に DLQ → ops 通知）。
        raise RuntimeError(f"slack post failed: {payload.get('error', 'unknown')}")


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    records = event.get("Records") or []
    for record in records:
        body = json.loads(record["body"])
        channel = str(body.get("channel") or "")
        if not channel:
            # 不正 payload はリトライしても直らない＝スキップ（DLQ を汚さない）。
            print(json.dumps({"event": "reminder_skip_invalid", "reason": "no_channel"}))
            continue
        start_hm = str(body.get("start_hm") or "")
        url = str(body.get("url") or "")
        text = (
            f"🔔 まもなく予定があります（{start_hm}〜）"
            if start_hm
            else "🔔 まもなく予定があります"
        )
        if url:
            text += f"\n<{url}|開く>"
        _post_message(channel, text)
        # channel は D で始まる DM id のみ（PII でない）。件数ログだけ出す。
        print(json.dumps({"event": "reminder_sent"}))
    return {"ok": True, "count": len(records)}
