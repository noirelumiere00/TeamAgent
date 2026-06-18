"""Fargate Scheduled Task 用 morning_digest エントリポイント。

EventBridge cron (平日 0:30 UTC = 9:30 JST) が ECS RunTask で本スクリプトを起動する。

役割:
  1. 対象ユーザー解決（env `MORNING_DIGEST_USERS` 明示優先・無ければ RDS `oauth_tokens` 動的抽出）
  2. 各ユーザーごとに `MorningDigestSkill.run()` を実行
  3. 結果を Slack DM（Block Kit）で本人に配信
  4. CloudWatch Logs に JSON 構造化ログで結果サマリ出力

⚠️ 安全規則:
  - 生メール本文・生件名・生 From を一切ログに出さない（masked のみ）
  - 連携未済ユーザーは Skill 内で fail-closed・本スクリプトは skip して次へ
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


def _resolve_target_users() -> list[str]:
    """env or DB から対象 user_email リストを取得。

    優先順位:
      1. env `MORNING_DIGEST_USERS`（カンマ区切り・明示指定）
      2. RDS `oauth_tokens` の連携済全員（動的抽出）
    """
    explicit = os.environ.get("MORNING_DIGEST_USERS", "").strip()
    if explicit:
        return [e.strip().lower() for e in explicit.split(",") if e.strip()]
    return _fetch_connected_users_from_rds()


def _fetch_connected_users_from_rds() -> list[str]:
    """RDS oauth_tokens から連携済 user_email を取得。"""
    import psycopg

    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("[run_morning_digest_fargate] WARN: DATABASE_URL 未設定", file=sys.stderr)
        return []
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_email FROM oauth_tokens")
                rows = cur.fetchall()
        return [str(r[0]).strip().lower() for r in rows if r and r[0]]
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: RDS 連携済抽出失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
        return []


def _build_token_store() -> Any:
    """factory.py の _build_token_store と同等（RDS + KMS or InMemory）。"""
    from teamagent.orchestrator.factory import _build_token_store

    return _build_token_store()


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    return f"{local[:1] if local else ''}***@{domain}"


def _format_block_kit(digest: Any, user_email: str) -> tuple[str, list[dict[str, Any]]]:
    """MorningDigestOutput → Slack Block Kit blocks。"""
    masked = _mask_email(user_email)
    text = f"☀️ おはようございます！{masked} さんの本日のダイジェストです。"

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "☀️ 今日のダイジェスト", "emoji": True},
        },
    ]

    # --- メール ---
    mail_items = list(getattr(digest, "mail_digest", []) or [])
    if mail_items:
        high = [m for m in mail_items if m.importance == "high"]
        medium = [m for m in mail_items if m.importance == "medium"]
        lines = [f"*📧 メール（要返信 {len(high)} / 確認 {len(medium)}）*"]
        for m in mail_items[:8]:
            badge = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(m.importance, "⚪")
            draft_mark = " ✏️" if m.has_draft else ""
            subj = m.subject_scrubbed or "(件名なし)"
            line = f"{badge} *{subj}* — {m.counterpart_masked}{draft_mark}"
            if m.summary:
                line += f"\n      _{m.summary}_"
            lines.append(line)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    else:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "*📧 メール*: 直近の新着なし"}}
        )

    # --- カレンダー ---
    cal_items = list(getattr(digest, "calendar_events", []) or [])
    if cal_items:
        lines = ["*🗓 今日の予定*"]
        for ev in cal_items[:6]:
            start = ev.start_at or "?"
            lines.append(
                f"• {start} *{ev.summary_scrubbed or '(無題)'}* {ev.location_scrubbed or ''}"
            )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

    # --- Slack 未返信 ---
    slack_items = list(getattr(digest, "slack_unread", []) or [])
    if slack_items:
        lines = [f"*💬 Slack 未返信メンション（{len(slack_items)} 件）*"]
        for s in slack_items[:5]:
            link = s.permalink or "(リンクなし)"
            lines.append(f"• {s.channel_name_masked} — <{link}|開く>")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

    # --- 下書き ---
    drafts = int(getattr(digest, "drafts_created", 0) or 0)
    if drafts > 0:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"✏️ 重要メール {drafts} 件に返信下書きを作成しました（Gmail の下書きフォルダに保存・送信は手動）",
                    }
                ],
            }
        )

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "_TeamAgent AiLa morning_digest｜日付/件名/相手は DLP マスク後表示_",
                }
            ],
        }
    )
    return text, blocks


async def _deliver_to_slack(user_email: str, text: str, blocks: list[dict[str, Any]]) -> bool:
    """Slack DM 配信（chat.postMessage with user IM channel）。"""
    from teamagent.adapters.slack_client import SlackClient

    try:
        slack = SlackClient.from_env()
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: SlackClient.from_env 失敗 {exc}", file=sys.stderr
        )
        return False

    # email → Slack user_id → IM channel を開く
    try:
        user_id = await _email_to_slack_user_id(slack, user_email)
        if not user_id:
            return False
        im_channel = await _open_im_channel(slack, user_id)
        if not im_channel:
            return False
        result = await slack.post_message(
            channel=im_channel,
            text=text,
            request_id=f"morning-digest-{uuid.uuid4().hex[:8]}",
            blocks=blocks,
        )
        return bool(getattr(result, "ok", False))
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: Slack 配信失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
        return False


async def _email_to_slack_user_id(slack: Any, email: str) -> str | None:
    """users.lookupByEmail で Slack user_id を解決（bot scope: users:read.email）。"""
    try:
        client = getattr(slack, "_web_client", None) or getattr(slack, "client", None)
        if client is None:
            return None
        resp = await asyncio.to_thread(client.users_lookupByEmail, email=email)
        return str(resp.get("user", {}).get("id", "")) or None
    except Exception:
        return None


async def _open_im_channel(slack: Any, user_id: str) -> str | None:
    """conversations.open で本人 IM channel を取得（bot scope: im:write）。"""
    try:
        client = getattr(slack, "_web_client", None) or getattr(slack, "client", None)
        if client is None:
            return None
        resp = await asyncio.to_thread(client.conversations_open, users=user_id)
        return str(resp.get("channel", {}).get("id", "")) or None
    except Exception:
        return None


def main() -> int:
    users = _resolve_target_users()
    if not users:
        print("[run_morning_digest_fargate] no target users (env+RDS empty)", flush=True)
        return 0
    print(f"[run_morning_digest_fargate] start users={len(users)}", flush=True)

    from teamagent.skills.base import SkillContext
    from teamagent.skills.morning_digest.schema import MorningDigestInput
    from teamagent.skills.morning_digest.skill import MorningDigestSkill

    token_store = _build_token_store()
    skill = MorningDigestSkill(token_store=token_store)
    skill_input = MorningDigestInput()

    summary = {"users": len(users), "delivered": 0, "skipped": 0, "errors": 0}
    for email in users:
        request_id = f"morning-{uuid.uuid4().hex[:10]}"
        ctx = SkillContext(request_id=request_id, metadata={"user_email": email})
        try:
            digest = skill.run(skill_input, ctx)
        except PermissionError:
            # 未連携・skip
            summary["skipped"] += 1
            continue
        except Exception as exc:
            print(
                f"[run_morning_digest_fargate] WARN: {email} skill 失敗 {type(exc).__name__}",
                file=sys.stderr,
            )
            summary["errors"] += 1
            continue

        text, blocks = _format_block_kit(digest, email)
        delivered = asyncio.run(_deliver_to_slack(email, text, blocks))
        if delivered:
            digest.delivered = True
            summary["delivered"] += 1
        else:
            summary["errors"] += 1

    print(
        f"[run_morning_digest_fargate] done {json.dumps(summary, ensure_ascii=False)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
