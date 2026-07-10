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


# Gmail/Calendar への deep link（受信トレイ全体＝DLP 安全。項目別 from: はマスク済みのため不採用）。
_GMAIL_DRAFTS_URL = "https://mail.google.com/mail/u/0/#drafts"
_GMAIL_INBOX_URL = "https://mail.google.com/mail/u/0/#inbox"
_CALENDAR_URL = "https://calendar.google.com/"

# ボタン押下（block_actions）を worker(Socket Mode) が受ける action_id。slack_bot.py の
# @app.action と一致させること。value は HMAC 署名トークン（生 thread_id は載せない＝G3）。
_ACTION_MAIL_DRAFT = "mail_draft"
# 📅 カレンダー登録ボタン（v0.3 Task3）。value は event_token（HMAC署名・日時/タイトル入り）。
_ACTION_CALENDAR_EVENT = "calendar_event"


def _calendar_button_enabled() -> bool:
    """MORNING_DIGEST_CALENDAR_BUTTON=1 のときのみ📅ボタンを描画（既定OFF・§10 E1-2）。

    ボタンは押下先の calendar_event tool（USE_CALENDAR_EVENT_TOOL + toolFilter.include）が
    本番で有効になってから ON にする（先に出すと無反応ボタンになる）。"""
    return os.environ.get("MORNING_DIGEST_CALENDAR_BUTTON", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


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


def _fmt_meeting_button_time(start_iso: str | None) -> str:
    """meeting_start(ISO) → 「7/15 14:00」（📅ボタン文言用・JST）。不正は "" で汎用文言に落とす。"""
    if not start_iso:
        return ""
    try:
        dt = _dt.datetime.fromisoformat(start_iso).astimezone(_JST)
        return f"{dt.month}/{dt.day} {dt.strftime('%H:%M')}"
    except (ValueError, TypeError):
        return ""


def _fmt_event_time(start_at: str | None, end_at: str | None) -> str:
    """ISO 文字列（…T10:00:00+09:00 / 日付のみ=終日）を '10:00–11:00' / '終日' に整形する。"""

    def _hm(s: str | None) -> str | None:
        if not s:
            return ""
        if "T" not in s:  # 日付のみ ＝ 終日イベント
            return None
        return s.split("T", 1)[1][:5]  # "HH:MM"

    sh = _hm(start_at)
    if sh is None:
        return "終日"
    eh = _hm(end_at)
    return f"{sh}–{eh}" if eh else (sh or "")


def _mail_line(m: Any) -> tuple[str, str]:
    """1 メール（スレッド）の (件名section本文, 相手) を作る。display は本人 DM のみ・ログ厳禁。"""
    subj = _slack_escape(getattr(m, "subject_display", "") or m.subject_scrubbed or "(件名なし)")
    who = _slack_escape(getattr(m, "counterpart_display", "") or m.counterpart_masked)
    return subj, who


def _reply_buttons(m: Any) -> list[dict[str, Any]]:
    """要返信メール 1 件のボタン行：未作成のみ [✏️ 下書きを作成]、常に [✅ 下書きを確認]。

    作成済みの下書きはスレッドを開けばそこに表示されるので、行内は「確認」1つで足りる。
    旧「📨 下書きを開く」（＝下書きフォルダ直行）は重複のため廃止し、一覧は DM 末尾に集約する。
    """
    btns: list[dict[str, Any]] = []
    thread_url = getattr(m, "thread_gmail_url", "") or _GMAIL_INBOX_URL
    has_draft = bool(getattr(m, "has_draft", False))
    draft_token = getattr(m, "draft_token", "")
    if not has_draft and draft_token:
        # 下書き未作成（オンデマンド/生成失敗）時のみ。押下→worker が block_actions で生成。
        btns.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "✏️ 下書きを作成", "emoji": True},
                "action_id": _ACTION_MAIL_DRAFT,
                "value": draft_token,
                "style": "primary",
            }
        )
    btns.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✅ 下書きを確認", "emoji": True},
            "url": thread_url,  # そのスレッドへワンタップ直行（url ボタン＝非発火）
        }
    )
    # 📅 確定MTGのカレンダー登録（v0.3 Task3・既定OFF）。日時確定×To本人のみ token が発行される。
    # ボタン文言に登録される日時を明示する（何が登録されるか見えない「盲目の同意」を防ぐ。
    # メール本文＝攻撃者制御値を LLM が抽出した日時なので、押す前に本人が検証できることが HITL の実質）。
    event_token = getattr(m, "event_token", "")
    if event_token and _calendar_button_enabled():
        when = _fmt_meeting_button_time(getattr(m, "meeting_start", None))
        label = f"📅 {when} に登録" if when else "📅 カレンダーに登録"
        btns.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": label[:75], "emoji": True},
                "action_id": _ACTION_CALENDAR_EVENT,
                "value": event_token,
            }
        )
    return btns


def _format_block_kit(digest: Any, user_email: str) -> tuple[str, list[dict[str, Any]]]:
    """MorningDigestOutput → Slack Block Kit（要返信→未開封→今日の予定。下書きはボタン生成）。"""
    text = "メールと本日の予定をお送りします。"

    mail_items = list(getattr(digest, "mail_digest", []) or [])

    # 要返信メール ＝ high かつ「本人が To に直接いる」（＝自分が返信すべきもの）。
    # To に自分がいない（CC のみ/メーリス宛）メールは high でも要返信に出さず未開封へ回す。
    def _is_reply(m: Any) -> bool:
        return m.importance == "high" and bool(getattr(m, "to_self", False))

    high = [m for m in mail_items if _is_reply(m)]
    # 未開封 ＝ 未読(UNREAD) かつ 要返信に出ていないもの（To に自分がいない高重要もここ・閲覧のみ）。
    unread = [m for m in mail_items if getattr(m, "is_unread", False) and not _is_reply(m)]
    cal_items = list(getattr(digest, "calendar_events", []) or [])
    slack_unread = list(getattr(digest, "slack_unread", []) or [])

    # 冒頭の枕詞（飾らない一文）。
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "📬 *メールと本日の予定をお送りします。*"},
        },
        {"type": "divider"},
    ]

    # --- 🔴 要返信メール（最大10件・各件にボタン）---
    if high:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"🔴 *要返信メール（{len(high)}件）*"},
            }
        )
        for m in high[:10]:
            subj, who = _mail_line(m)
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
            blocks.append({"type": "actions", "elements": _reply_buttons(m)})
        # 作り置き済みの下書きが1件でもあれば、末尾に「一覧をまとめて開く」を1つだけ集約する
        # （行内の重複を排し、下書きフォルダへの導線はここに一本化）。
        if any(getattr(m, "has_draft", False) for m in high):
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "📁 下書き一覧を開く",
                                "emoji": True,
                            },
                            "url": _GMAIL_DRAFTS_URL,
                        }
                    ],
                }
            )
        blocks.append({"type": "divider"})

    # --- 📬 未確認（未読・最大5件＋「他N件」・件名/相手＋AI要約）---
    if unread:
        lines = [f"📬 *未確認（{len(unread)}件）*"]
        for m in unread[:5]:
            subj, who = _mail_line(m)
            line = f"• *{subj}* — {who}"
            if m.summary:
                line += f"\n　_{_slack_escape(m.summary)}_"
            lines.append(line)
        rem = max(0, len(unread) - 5)
        if rem:
            lines.append(f"• 〈他{rem}件〉")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        blocks.append({"type": "divider"})

    if not high and not unread:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "📭 *メール*: 新着なし"}}
        )
        blocks.append({"type": "divider"})

    # --- 💬 Slack 返信漏れ（未返信メンション・最大5件。display は本人 DM のみ・ログ厳禁 G3/G7）---
    if slack_unread:
        lines = [f"💬 *Slack 返信漏れ（{len(slack_unread)}件）*"]
        for it in slack_unread[:5]:
            ch = _slack_escape(
                getattr(it, "channel_name_display", "") or getattr(it, "channel_name_masked", "")
            )
            ex = _slack_escape(
                getattr(it, "excerpt_display", "") or getattr(it, "excerpt_scrubbed", "")
            )
            line = f"• *#{ch or '(不明)'}*: {ex}" if ex else f"• *#{ch or '(不明)'}*"
            link = getattr(it, "permalink", None)
            if link:
                line += f"  <{link}|開く>"  # permalink は実 URL なのでエスケープしない
            lines.append(line)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
        blocks.append({"type": "divider"})

    # --- 📅 今日の予定（予定・会議室・会議リンク。display は本人 DM のみ・ログ厳禁 G3/G7）---
    if cal_items:
        lines = [f"📅 *今日の予定（{len(cal_items)}件）*"]
        for ev in cal_items[:10]:
            when = _fmt_event_time(getattr(ev, "start_at", None), getattr(ev, "end_at", None))
            title = _slack_escape(
                getattr(ev, "summary_display", "")
                or getattr(ev, "summary_scrubbed", "")
                or "(無題)"
            )
            loc = getattr(ev, "location_display", "") or getattr(ev, "location_scrubbed", "")
            line = f"• `{when}`  {title}"
            if loc:
                line += f"  〔{_slack_escape(loc)}〕"
            url = getattr(ev, "meeting_url", "")
            if url:
                line += f"  <{url}|🔗参加>"  # 会議リンクは実 URL なのでエスケープしない
            lines.append(line)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    else:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "📅 *今日の予定*: なし"}}
        )

    # --- 脚注（DLP 注記）---
    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        "_AiLa｜本人だけに届く DM です（件名・相手は実名表示／監査ログ側はマスク）。"
                        "下書きはボタンを押した時に生成し、送信はされません（手動送信）。_"
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
    # 本人Slack文脈（USE_SLACK_CONTEXT 有効時のみ非 None）。朝ダイジェストの自動下書きにも反映。
    from teamagent.orchestrator.factory import _build_slack_context_provider

    slack_ctx = _build_slack_context_provider()
    # Slack 返信漏れ検知（v0.3 Task1・MORNING_DIGEST_SLACK_UNREAD=1 のときのみ非 None・既定OFF）。
    # Provider は fail-open（未連携ユーザーは空）なので、flag ON でも既存挙動を壊さない。
    slack_unreplied = None
    if os.environ.get("MORNING_DIGEST_SLACK_UNREAD", "").strip().lower() in {"1", "true", "yes"}:
        from teamagent.orchestrator.factory import _build_slack_store
        from teamagent.skills._shared.slack_unreplied import SlackUnrepliedProvider

        slack_unreplied = SlackUnrepliedProvider(slack_store=_build_slack_store())
    if concurrency > 1:
        # 並列時は Bedrock クライアントを事前生成して共有（lazy-init の競合を避ける）。
        from teamagent.adapters.bedrock_client import BedrockClient

        skill = MorningDigestSkill(
            token_store=token_store,
            bedrock=BedrockClient.from_env(),
            deal_provider=slack_ctx,
            slack=slack_unreplied,
        )
    else:
        skill = MorningDigestSkill(
            token_store=token_store, deal_provider=slack_ctx, slack=slack_unreplied
        )
    # concurrency と同じく env 不正値でも落とさない。schema は 0..10、0=自動下書き無効。
    try:
        max_drafts = int(os.environ.get("MORNING_DIGEST_MAX_DRAFTS", "3"))
    except ValueError:
        max_drafts = 3
    max_drafts = min(10, max(0, max_drafts))
    skill_input = MorningDigestInput(max_drafts=max_drafts)

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
