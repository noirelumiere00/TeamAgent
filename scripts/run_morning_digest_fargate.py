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
import datetime as _dt
import json
import os
import sys
import uuid
from typing import Any

import structlog

from teamagent import mail_action_ui as ui
from teamagent.gmail_links import gmail_thread_url

logger = structlog.get_logger(__name__)


def _interactive_enabled() -> bool:
    """USE_INTERACTIVE_MAIL=1/true で、要返信メールをボタン付きカードで配信する。"""
    return os.environ.get("USE_INTERACTIVE_MAIL", "").strip().lower() in ("1", "true", "yes")


def _make_slack(*, interactive: bool) -> Any:
    """配信用 SlackClient。interactive はボタン処理する第2 App の bot token で投稿する。"""
    from teamagent.adapters.slack_client import SlackClient

    if interactive:
        token = os.environ.get("INTERACTIVE_MAIL_BOT_TOKEN", "").strip()
        if token:
            return SlackClient(bot_token=token)
    return SlackClient.from_env()


def _resolve_target_users() -> list[str]:
    """env or DB から対象 user_email リストを取得し、除外リストを差し引く。

    優先順位:
      1. env `MORNING_DIGEST_USERS`（カンマ区切り・明示指定）
      2. RDS `oauth_tokens` の連携済全員（動的抽出）
    どちらの経路でも最後に env `MORNING_DIGEST_EXCLUDE` のユーザーを除外する。
    """
    explicit = os.environ.get("MORNING_DIGEST_USERS", "").strip()
    if explicit:
        users = [e.strip().lower() for e in explicit.split(",") if e.strip()]
    else:
        users = _fetch_connected_users_from_rds()
    return _apply_exclude(users)


def _apply_exclude(users: list[str]) -> list[str]:
    """env `MORNING_DIGEST_EXCLUDE`（カンマ区切り）のユーザーを対象から外す。

    Google 連携を切らずに、テストユーザーや一時停止したい人だけを digest 対象から
    除外する仕組み。明示リスト・RDS 動的抽出のどちらの経路でも最後に適用する。
    """
    raw = os.environ.get("MORNING_DIGEST_EXCLUDE", "").strip()
    if not raw:
        return users
    excluded = {e.strip().lower() for e in raw.split(",") if e.strip()}
    if not excluded:
        return users
    kept = [u for u in users if u.lower() not in excluded]
    removed = len(users) - len(kept)
    if removed:
        print(
            f"[run_morning_digest_fargate] excluded {removed} user(s) via MORNING_DIGEST_EXCLUDE",
            flush=True,
        )
    return kept


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


# Gmail/Calendar への deep link（受信トレイ全体＝DLP 安全。項目別 from: はマスク済みのため不採用）。
_GMAIL_DRAFTS_URL = "https://mail.google.com/mail/u/0/#drafts"
_GMAIL_INBOX_URL = "https://mail.google.com/mail/u/0/#inbox"
_CALENDAR_URL = "https://calendar.google.com/"


_JST = _dt.timezone(_dt.timedelta(hours=9))


def _fmt_time(iso: str | None) -> str:
    """ISO 開始時刻 → JST の HH:MM（本人は日本在勤）。パース失敗時は原文 or '?'。"""
    if not iso:
        return "?"
    try:
        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(_JST)
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return iso[:16]


def _format_block_kit(
    digest: Any, user_email: str, *, interactive: bool = False
) -> tuple[str, list[dict[str, Any]]]:
    """MorningDigestOutput → Slack Block Kit blocks（要返信を最上部・スコアボード・アクション付き）。

    interactive=True のときは要返信メールの詳細は別メッセージ（カード）で配信するため、
    本ヘッダーには件数のみ表示する（カード側に [対応する] 等のボタンが付く）。
    """
    masked = _mask_email(user_email)
    text = f"☀️ おはようございます！{masked} さんの本日のダイジェストです。"

    mail_items = list(getattr(digest, "mail_digest", []) or [])
    high = [m for m in mail_items if m.importance == "high"]
    medium = [m for m in mail_items if m.importance == "medium"]
    low = [m for m in mail_items if m.importance == "low"]
    cal_items = list(getattr(digest, "calendar_events", []) or [])
    slack_items = list(getattr(digest, "slack_unread", []) or [])
    drafts = int(getattr(digest, "drafts_created", 0) or 0)

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "☀️ 今日のダイジェスト", "emoji": True},
        },
    ]

    # --- スコアボード（3秒で全体量・fields 2x2）---
    blocks.append(
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"🔴 *要返信*  `{len(high)}件`"},
                {"type": "mrkdwn", "text": f"✏️ *下書き済*  `{drafts}件`"},
                {"type": "mrkdwn", "text": f"🗓 *今日の予定*  `{len(cal_items)}件`"},
                {"type": "mrkdwn", "text": f"🟡 *要確認*  `{len(medium)}件`"},
            ],
        }
    )
    blocks.append({"type": "divider"})

    # --- 要返信メール（high のみ・本文 section + メタ context の2段で厚く）---
    if high and interactive:
        # interactive: 詳細は個別カード（ボタン付き）で配信。ヘッダーは案内のみ。
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"🔴 *いますぐ返信したい（{len(high)}件）*"
                        "\n下のカードから *対応する / 対応済み / 後で* を選べます。"
                    ),
                },
            }
        )
        blocks.append({"type": "divider"})
    elif high:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"🔴 *いますぐ返信したい（{len(high)}件）*"},
            }
        )
        for m in high[:5]:
            # display fields are PII; rendered to owner DM only, never logged (G3/G7)
            subj = m.subject_display or m.subject_scrubbed or "(件名なし)"
            who = m.counterpart_display or m.counterpart_masked
            tag = f"`{m.sender_label}` " if getattr(m, "sender_label", "") else ""
            thr = f" 〔{m.thread_count}通〕" if getattr(m, "thread_count", 1) > 1 else ""
            body = f"{tag}*{subj}*{thr} — {who}"
            if m.summary:
                body += f"\n_{m.summary}_"
            if getattr(m, "deadline", None):
                body += f"\n⏰ 期限: {m.deadline}"
            if getattr(m, "ask", ""):
                body += f"\n📌 依頼: {m.ask}"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
            # 各件はそのスレッドを直接開く deep-link（thread_id があれば）。
            thread_url = gmail_thread_url(getattr(m, "thread_id", "")) or _GMAIL_INBOX_URL
            meta = []
            if m.has_draft:
                meta.append({"type": "mrkdwn", "text": "✏️ 返信下書き作成済"})
                meta.append({"type": "mrkdwn", "text": f"<{thread_url}|下書きを確認して送信>"})
            meta.append({"type": "mrkdwn", "text": f"<{thread_url}|Gmailでスレッドを開く>"})
            blocks.append({"type": "context", "elements": meta})
        blocks.append({"type": "divider"})
    elif not mail_items:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "📧 *メール*: 直近の新着なし"}}
        )

    # --- 今日の予定（時刻太字・会議室/場所を 📍付きで明記）---
    if cal_items:
        lines = ["🗓 *今日の予定*"]
        for ev in cal_items[:6]:
            loc = (ev.location_scrubbed or "").strip()
            place = f" 📍{loc}" if loc else ""
            mtg_url = (getattr(ev, "meeting_url", "") or "").strip()
            meet = f"  <{mtg_url}|会議リンク>" if mtg_url else ""
            lines.append(
                f"• *{_fmt_time(ev.start_at)}* {ev.summary_scrubbed or '(無題)'}{place}{meet}"
            )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        blocks.append({"type": "divider"})

    # --- 要確認メール（medium・1行圧縮 + 「+N件」省略）---
    if medium:
        lines = [f"🟡 *目を通したい（{len(medium)}件）*"]
        for m in medium[:3]:
            # display fields are PII; rendered to owner DM only, never logged (G3/G7)
            subj = m.subject_display or m.subject_scrubbed or "(件名なし)"
            who = m.counterpart_display or m.counterpart_masked
            lines.append(f"• {subj} — {who}")
        remaining = max(0, len(medium) - 3) + len(low)
        if remaining > 0:
            lines.append(f"• 〈+{remaining}件〉")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

    # --- Slack 未返信（データがある時のみ）---
    if slack_items:
        lines = [f"💬 *Slack 未返信メンション（{len(slack_items)} 件）*"]
        for s in slack_items[:5]:
            link = s.permalink or "(リンクなし)"
            lines.append(f"• {s.channel_name_masked} — <{link}|開く>")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})

    # --- アクションバー（deep link button）---
    actions: list[dict[str, Any]] = []
    if drafts > 0:
        actions.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✏️ 下書きを確認", "emoji": True},
                "url": _GMAIL_DRAFTS_URL,
                "style": "primary",
            }
        )
    actions.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "📥 受信トレイ", "emoji": True},
            "url": _GMAIL_INBOX_URL,
        }
    )
    actions.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "🗓 カレンダー", "emoji": True},
            "url": _CALENDAR_URL,
        }
    )
    blocks.append({"type": "actions", "elements": actions})

    # --- 脚注（DLP マスク注記）---
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "_AiLa morning_digest｜件名・相手・本文は DLP マスク後表示。"
                        "下書きは送信されません（手動送信）。_"
                    ),
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
        # SlackClient は AsyncWebClient を self._client に保持。async メソッドは直接 await する
        # （to_thread に渡すと coroutine が未await のまま返り解決できない）。
        client = getattr(slack, "_client", None)
        if client is None:
            print("[run_morning_digest_fargate] WARN: slack._client 取得失敗", file=sys.stderr)
            return None
        resp = await client.users_lookupByEmail(email=email)
        return str(resp.get("user", {}).get("id", "")) or None
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: lookupByEmail 失敗 {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None


async def _open_im_channel(slack: Any, user_id: str) -> str | None:
    """conversations.open で本人 IM channel を取得（bot scope: im:write）。"""
    try:
        client = getattr(slack, "_client", None)
        if client is None:
            return None
        resp = await client.conversations_open(users=user_id)
        return str(resp.get("channel", {}).get("id", "")) or None
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: conversations.open 失敗 {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None


async def _deliver_interactive(user_email: str, digest: Any) -> bool:
    """interactive 配信: ヘッダー（スコアボード/予定）+ 要返信メール 1件1カード（ボタン付き）。

    各カードを個別メッセージにすることで、ボタン押下時に response_url が
    そのカードだけを差し替える（全体ダイジェストを巻き込まない）。
    """
    slack = _make_slack(interactive=True)
    user_id = await _email_to_slack_user_id(slack, user_email)
    if not user_id:
        return False
    im_channel = await _open_im_channel(slack, user_id)
    if not im_channel:
        return False

    rid = f"morning-intr-{uuid.uuid4().hex[:8]}"
    header_text, header_blocks = _format_block_kit(digest, user_email, interactive=True)
    header = await slack.post_message(
        channel=im_channel, text=header_text, request_id=rid, blocks=header_blocks
    )
    delivered = bool(getattr(header, "ok", False))

    high = [m for m in (getattr(digest, "mail_digest", []) or []) if m.importance == "high"]
    for m in high[:5]:
        tid = str(getattr(m, "thread_id", "") or "")
        if not tid:
            continue  # thread_id 無し＝スレッドを特定できない＝ボタンを出さない
        # display fields are PII; rendered to owner DM card only, never logged (G3/G7).
        # button value は thread_id（非PII）のみ＝summary_item_blocks 内で使用。
        item_blocks = ui.summary_item_blocks(
            thread_id=tid,
            subject=m.subject_display or m.subject_scrubbed,
            counterpart=m.counterpart_display or m.counterpart_masked,
            summary=m.summary,
        )
        subj = m.subject_display or m.subject_scrubbed or "(件名なし)"
        await slack.post_message(
            channel=im_channel,
            text=f"🔴 要返信: {subj}",
            request_id=rid,
            blocks=item_blocks,
        )
    return delivered


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes")


def _build_skill_from_env(skill_cls: Any, token_store: Any) -> Any:
    """env から digest 品質パラメータを読み Skill を構築する（全て後方互換の既定値）。"""
    important = frozenset(
        s.strip().lower() for s in os.environ.get("IMPORTANT_SENDERS", "").split(",") if s.strip()
    )
    internal_domain = os.environ.get("DIGEST_INTERNAL_DOMAIN", "vectorinc.co.jp").strip()
    signature = os.environ.get("DIGEST_SIGNATURE", "")
    try:
        triage_batch = int(os.environ.get("DIGEST_TRIAGE_BATCH", "8"))
    except ValueError:
        triage_batch = 8
    return skill_cls(
        token_store=token_store,
        triage_batch=triage_batch,
        important_senders=important,
        internal_domain=internal_domain,
        signature=signature,
        reply_all=_env_flag("DIGEST_REPLY_ALL", False),
        dedupe_drafts=_env_flag("DIGEST_DEDUPE_DRAFTS", True),
    )


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
    skill = _build_skill_from_env(MorningDigestSkill, token_store)
    skill_input = MorningDigestInput()
    interactive = _interactive_enabled()
    print(f"[run_morning_digest_fargate] interactive={interactive}", flush=True)

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

        if interactive:
            delivered = asyncio.run(_deliver_interactive(email, digest))
        else:
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
