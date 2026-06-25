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
from urllib.parse import quote

import structlog

logger = structlog.get_logger(__name__)


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


# Gmail/Calendar への deep link。
# 項目別 `from:<addr>` クエリリンクはマスク済み差出人を漏らすため不採用だが、
# Gmail スレッドID（thread_id）は不透明ID＝非PIIなので項目別 deep link でも DLP 安全。
# 受信トレイ/下書き/スレッドは _gmail_account_base() で本人アカウント固定 URL を都度生成。
_CALENDAR_URL = "https://calendar.google.com/"


def _gmail_account_base(user_email: str) -> str:
    """アカウント選択つき Gmail ベース URL（ハッシュ直前まで）。

    複数 Google ログイン環境で `u/0`（＝先頭アカウント）だと本人と別アカウントで
    開く事故が起きるため、email が判明していれば `?authuser=<email>` で本人に固定する。
    不明時は従来どおり `u/0`。戻り値に `#inbox` / `#drafts` / `#all/<tid>` を連結して使う。
    """
    if user_email and "@" in user_email:
        return f"https://mail.google.com/mail/?authuser={quote(user_email, safe='@')}"
    return "https://mail.google.com/mail/u/0/"


def _gmail_thread_url(thread_id: str | None, user_email: str) -> str | None:
    """スレッド（会話）を開く deep link。返信下書きはスレッド内にインライン表示される。

    `#all/<thread_id>` は受信トレイ/アーカイブ/下書きのどこに在っても確実に開ける。
    thread_id が空なら None（呼び出し側で汎用リンクにフォールバック）。
    """
    if not thread_id:
        return None
    return f"{_gmail_account_base(user_email)}#all/{thread_id}"


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


def _slack_escape(s: str) -> str:
    """Slack mrkdwn の特殊文字をエスケープ。

    実件名/実名(未マスクの display)を mrkdwn に入れるため、メール件名に
    `<https://evil|クリック>` 等を仕込まれてもリンク偽装/書式崩れにならないようにする。
    Slack 仕様では & < > のみエスケープが必要。
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_block_kit(digest: Any, user_email: str) -> tuple[str, list[dict[str, Any]]]:
    """MorningDigestOutput → Slack Block Kit blocks（要返信を最上部・スコアボード・アクション付き）。"""
    masked = _mask_email(user_email)
    text = f"☀️ おはようございます！{masked} さんの本日のダイジェストです。"

    # 本人アカウントに固定した Gmail deep link（複数 Google ログイン環境での誤アカウント回避）。
    inbox_url = f"{_gmail_account_base(user_email)}#inbox"
    drafts_url = f"{_gmail_account_base(user_email)}#drafts"

    mail_items = list(getattr(digest, "mail_digest", []) or [])
    high = [m for m in mail_items if m.importance == "high"]
    medium = [m for m in mail_items if m.importance == "medium"]
    low = [m for m in mail_items if m.importance == "low"]
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
                {"type": "mrkdwn", "text": f"🟡 *要確認*  `{len(medium)}件`"},
            ],
        }
    )
    blocks.append({"type": "divider"})

    # --- 要返信メール（high のみ・本文 section + メタ context の2段で厚く）---
    if high:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"🔴 *いますぐ返信したい（{len(high)}件）*"},
            }
        )
        for m in high[:5]:
            # display fields are PII; rendered to owner DM only, never logged (G3/G7)。
            # 未マスクの実件名/実名は Slack エスケープ必須（リンク偽装/書式崩れ防止）。
            subj = _slack_escape(
                getattr(m, "subject_display", "") or m.subject_scrubbed or "(件名なし)"
            )
            who = _slack_escape(getattr(m, "counterpart_display", "") or m.counterpart_masked)
            tag = f"`{m.sender_label}` " if getattr(m, "sender_label", "") else ""
            thr = f" 〔{m.thread_count}通〕" if getattr(m, "thread_count", 1) > 1 else ""
            body = f"{tag}*{subj}*{thr} — {who}"
            if m.summary:
                body += f"\n_{_slack_escape(m.summary)}_"
            if getattr(m, "deadline", None):
                body += f"\n⏰ 期限: {_slack_escape(str(m.deadline))}"
            if getattr(m, "ask", ""):
                body += f"\n📌 依頼: {_slack_escape(m.ask)}"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
            meta = []
            thread_url = _gmail_thread_url(getattr(m, "thread_id", None), user_email)
            if m.has_draft:
                meta.append({"type": "mrkdwn", "text": "✏️ 返信下書き作成済"})
                if thread_url:
                    # 下書きはスレッド内にインライン表示＝開けば確認してそのまま送信できる。
                    meta.append(
                        {"type": "mrkdwn", "text": f"<{thread_url}|📩 Gmailで下書きを開く>"}
                    )
                else:
                    meta.append({"type": "mrkdwn", "text": f"<{drafts_url}|下書きを見る>"})
            else:
                meta.append(
                    {"type": "mrkdwn", "text": f"<{thread_url or inbox_url}|📩 Gmailで開く>"}
                )
            blocks.append({"type": "context", "elements": meta})
        blocks.append({"type": "divider"})
    elif not mail_items:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "📧 *メール*: 直近の新着なし"}}
        )

    # --- 要確認メール（medium・1行圧縮 + 「+N件」省略）---
    if medium:
        lines = [f"🟡 *目を通したい（{len(medium)}件）*"]
        for m in medium[:3]:
            # display fields are PII; rendered to owner DM only, never logged (G3/G7)
            subj = _slack_escape(
                getattr(m, "subject_display", "") or m.subject_scrubbed or "(件名なし)"
            )
            who = _slack_escape(getattr(m, "counterpart_display", "") or m.counterpart_masked)
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
                "url": drafts_url,
                "style": "primary",
            }
        )
    actions.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "📥 受信トレイ", "emoji": True},
            "url": inbox_url,
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
                        "_AiLa morning_digest｜本人だけに届く DM です（件名・相手は実名表示／"
                        "監査ログ側はマスク）。下書きは送信されません（手動送信）。_"
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
        user_id = str(resp.get("user", {}).get("id", "")) or None
        if user_id is None:
            # 解決はできたが該当ユーザー無し（Slack 未登録等）。配信失敗と区別して記録。
            print(
                f"[run_morning_digest_fargate] WARN: Slack user 未解決 {_mask_email(email)}",
                file=sys.stderr,
            )
        return user_id
    except Exception as exc:
        # ⚠️ {exc} は email を含み得る（PII）ため型名のみ。email はマスク（G3/G7）。
        print(
            f"[run_morning_digest_fargate] WARN: lookupByEmail 失敗 "
            f"{_mask_email(email)} {type(exc).__name__}",
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


def _process_user(skill: Any, skill_input: Any, email: str) -> str:
    """1 ユーザー分を処理し "delivered"/"skipped"/"error" を返す（例外は内側で封じ込め）。

    スレッドから呼ぶため副作用は print（stderr・マスク済）と Slack 配信のみ・共有状態を書かない。
    """
    from teamagent.skills.base import SkillContext

    request_id = f"morning-{uuid.uuid4().hex[:10]}"
    ctx = SkillContext(request_id=request_id, metadata={"user_email": email})
    try:
        digest = skill.run(skill_input, ctx)
    except PermissionError:
        return "skipped"  # 未連携
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: {_mask_email(email)} skill 失敗 "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return "error"
    # 配信(整形+Slack)も封じ込め（1 人の失敗で全体を落とさない）。
    try:
        text, blocks = _format_block_kit(digest, email)
        delivered = asyncio.run(_deliver_to_slack(email, text, blocks))
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: {_mask_email(email)} 配信失敗 "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return "error"
    if delivered:
        digest.delivered = True
        return "delivered"
    return "error"


def main() -> int:
    users = _resolve_target_users()
    if not users:
        print("[run_morning_digest_fargate] no target users (env+RDS empty)", flush=True)
        return 0
    print(f"[run_morning_digest_fargate] start users={len(users)}", flush=True)

    from teamagent.skills.morning_digest.schema import MorningDigestInput
    from teamagent.skills.morning_digest.skill import MorningDigestSkill

    try:
        concurrency = max(1, int(os.environ.get("MORNING_DIGEST_CONCURRENCY", "1")))
    except ValueError:
        concurrency = 1

    token_store = _build_token_store()
    if concurrency > 1:
        # 並列時は Bedrock クライアントを事前生成して共有（lazy-init の競合を避ける）。
        from teamagent.adapters.bedrock_client import BedrockClient

        skill = MorningDigestSkill(token_store=token_store, bedrock=BedrockClient.from_env())
    else:
        skill = MorningDigestSkill(token_store=token_store)
    skill_input = MorningDigestInput()

    # concurrency=1（既定）は従来どおり逐次。>1 で人数に応じた所要時間短縮。
    if concurrency > 1:
        from concurrent.futures import ThreadPoolExecutor

        print(f"[run_morning_digest_fargate] concurrency={concurrency}", flush=True)
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            results = list(ex.map(lambda e: _process_user(skill, skill_input, e), users))
    else:
        results = [_process_user(skill, skill_input, e) for e in users]

    summary = {"users": len(users), "delivered": 0, "skipped": 0, "errors": 0}
    for r in results:
        summary["delivered" if r == "delivered" else "skipped" if r == "skipped" else "errors"] += 1

    print(
        f"[run_morning_digest_fargate] done {json.dumps(summary, ensure_ascii=False)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
