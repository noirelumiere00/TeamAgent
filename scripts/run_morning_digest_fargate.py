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
import re
import sys
import uuid
from typing import Any

import structlog

from teamagent.hmac_durable_state import require_runtime_startup
from teamagent.hmac_keyring import MAIL_ACTION_MAX_TOKEN_TTL_S

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
                # oauth_tokens は FORCE RLS（本人 GUC or admin）。この一覧取得は「配信対象の
                # 列挙」という管理系読み取りなので、policy に用意された admin 経路を明示する
                # （GUC 無しだと接続ロールによっては 0 行になり「誰にも配信されない」事故に
                # なる・2026-07-13 自動モード切替の事前監査で検出）。token 本体は読まない。
                cur.execute("SET app.user_role = 'admin'")
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

# ボタン押下（block_actions）を固定 OpenClaw Slack adapter が署名検証し、caller identity
# plugin の同名 interactive namespace へ渡す action_id。value は HMAC 署名トークン
# （生 thread_id は載せない＝G3）。
_ACTION_MAIL_DRAFT = "mail_draft"
# 📅 カレンダー登録ボタン（v0.3 Task3）。value は event_token（HMAC署名・日時/タイトル入り）。
_ACTION_CALENDAR_EVENT = "calendar_event"
# 🗓 日程候補を提案ボタン（v0.3 Task4）。value は draft_token（同一形式・thread_id 由来）。
_ACTION_SCHEDULE_PROPOSE = "schedule_propose"


def _schedule_button_enabled() -> bool:
    """MORNING_DIGEST_SCHEDULE_BUTTON=1 のときのみ🗓ボタンを描画（既定OFF・§10 E1-2）。"""
    return os.environ.get("MORNING_DIGEST_SCHEDULE_BUTTON", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _calendar_button_enabled() -> bool:
    """MORNING_DIGEST_CALENDAR_BUTTON=1 のときのみ📅ボタンを描画（既定OFF・§10 E1-2）。

    ボタンは押下先の calendar_event tool（USE_CALENDAR_EVENT_TOOL + toolFilter.include）が
    本番で有効になってから ON にする（先に出すと無反応ボタンになる）。"""
    return os.environ.get("MORNING_DIGEST_CALENDAR_BUTTON", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _compact_enabled() -> bool:
    """MORNING_DIGEST_COMPACT=1 のときのみ密度優先描画（既定OFF・旧描画を完全温存）。

    2026-07-13 パイロットFB「Slackとメールの部分が見づらい」対応。ON/OFF は env のみで
    切替可能（taskdef 差し替えだけ・再ビルド不要）。"""
    return os.environ.get("MORNING_DIGEST_COMPACT", "").strip().lower() in {"1", "true", "yes"}


# --- 密度優先描画（MORNING_DIGEST_COMPACT）の表示上限と切り詰め ---
_COMPACT_SUBJ_LEN = 60  # 件名/要約の切詰
_COMPACT_EXCERPT_LEN = 60  # Slack本文抜粋の切詰
_COMPACT_SECTION_CHARS = 2800  # Slack section text 上限3000字の保険
_COMPACT_MAX_BLOCKS = 48  # Slack blocks 上限50個の保険

_MENTION_RE = re.compile(r"<@[A-Z0-9]+\|([^>]+)>")
_MENTION_BARE_RE = re.compile(r"<@[A-Z0-9]+>")
_CHANNEL_TOKEN_RE = re.compile(r"<#[A-Z0-9]+\|([^>]*)>")
_LINK_LABEL_RE = re.compile(r"<https?://[^|>]+\|([^>]+)>")
_LINK_BARE_RE = re.compile(r"<https?://[^>]+>")


def _truncate(s: str, limit: int) -> str:
    """limit 超過時は末尾を「…」に置き換える（1件=1行原則のための単純字数切詰）。"""
    s = s or ""
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


def _flatten_slack_text(raw: str) -> str:
    """Slack 生本文の抜粋整形（compact 用）: メンション/リンク表記を可読化し空白を1つに畳む。

    処理順は「正規化→切詰→escape」（escape は呼び出し側）。`<https://evil|クリック>` の
    ような偽装リンクはラベル文字列だけが残り、リンクとしては絶対に描画されない。
    """
    s = raw or ""
    s = _MENTION_RE.sub(r"@\1", s)
    s = _MENTION_BARE_RE.sub("@メンバー", s)
    s = _CHANNEL_TOKEN_RE.sub(r"#\1", s)
    s = _LINK_LABEL_RE.sub(r"\1", s)
    s = _LINK_BARE_RE.sub("(リンク)", s)
    return re.sub(r"\s+", " ", s).strip()


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
        # 下書き未作成時のみ。押下 identity/value は plugin が heartbeat run へ one-use 束縛する。
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
    # 🗓 日程打診への候補提案（v0.3 Task4・既定OFF）。相手が日程を求めている×To本人のみ。
    # value は draft_token（thread_id 由来・schedule_propose がスレッドへの返信下書きに使う）。
    if getattr(m, "scheduling_request", False) and draft_token and _schedule_button_enabled():
        btns.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🗓 日程候補を提案", "emoji": True},
                "action_id": _ACTION_SCHEDULE_PROPOSE,
                "value": draft_token,
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


def _format_block_kit_compact(digest: Any, user_email: str) -> tuple[str, list[dict[str, Any]]]:
    """密度優先の Block Kit（MORNING_DIGEST_COMPACT=1・2026-07-13 パイロットFB対応）。

    設計原則: DM は「索引」・詳細は元アプリ（Gmail/Slack/Calendar）。1件=1行、
    要約・本文プレビューは出さない（要返信のみ ⏰期限/📌依頼 の構造化1行を許可・
    どちらも無ければ要約60字で代替）。全セクションで「見出し=全数・表示=上限・
    超過=〈他N件〉+リンク」を統一。ボタン群（_reply_buttons）・脚注・PII 規約
    （display は本人 DM のみ・ログ厳禁 G3/G7）は旧描画と共通。
    """
    mail_items = list(getattr(digest, "mail_digest", []) or [])

    def _is_reply(m: Any) -> bool:
        return m.importance == "high" and bool(getattr(m, "to_self", False))

    high = [m for m in mail_items if _is_reply(m)]
    unread = [m for m in mail_items if getattr(m, "is_unread", False) and not _is_reply(m)]
    cal_items = list(getattr(digest, "calendar_events", []) or [])
    slack_unread = list(getattr(digest, "slack_unread", []) or [])

    now = _dt.datetime.now(_JST)
    wd = "月火水木金土日"[now.weekday()]
    # fallback text は通知プレビューに出るため件数のみ（PII ゼロ）。
    text = (
        f"朝ダイジェスト｜要返信{len(high)}・未確認{len(unread)}"
        f"・Slack{len(slack_unread)}・予定{len(cal_items)}"
    )
    header = (
        f"📬 *{now.month}/{now.day}({wd}) の朝ダイジェスト*"
        f"｜🔴{len(high)}・📬{len(unread)}・💬{len(slack_unread)}・📅{len(cal_items)}"
    )
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "divider"},
    ]

    def _push_lines(lines: list[str]) -> None:
        """行リストを 2800 字以内の section に分割して積む（3000 字上限の保険）。"""
        buf: list[str] = []
        size = 0
        for ln in lines:
            if buf and size + len(ln) + 1 > _COMPACT_SECTION_CHARS:
                blocks.append(
                    {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(buf)}}
                )
                buf, size = [], 0
            buf.append(ln)
            size += len(ln) + 1
        if buf:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(buf)}})

    def _subj_who(m: Any) -> tuple[str, str]:
        subj = _slack_escape(
            _truncate(
                getattr(m, "subject_display", "") or m.subject_scrubbed or "(件名なし)",
                _COMPACT_SUBJ_LEN,
            )
        )
        who = _slack_escape(getattr(m, "counterpart_display", "") or m.counterpart_masked)
        return subj, who

    # --- 🔴 要返信（最大5件・各件にボタン）---
    if high:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"🔴 *要返信（{len(high)}件）*"}}
        )
        for m in high[:5]:
            subj, who = _subj_who(m)
            tag = f"`{m.sender_label}` " if getattr(m, "sender_label", "") else ""
            thr = f"〔{m.thread_count}通〕" if getattr(m, "thread_count", 1) > 1 else ""
            body = f"{tag}{who}: *{subj}*{thr}"
            meta: list[str] = []
            if getattr(m, "deadline", None):
                meta.append(f"⏰ {_slack_escape(_truncate(str(m.deadline), 40))}")
            if getattr(m, "ask", ""):
                meta.append(f"📌 {_slack_escape(_truncate(m.ask, _COMPACT_SUBJ_LEN))}")
            if meta:
                body += "\n" + " ｜ ".join(meta)
            elif m.summary:
                body += f"\n_{_slack_escape(_truncate(m.summary, _COMPACT_SUBJ_LEN))}_"
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
            blocks.append({"type": "actions", "elements": _reply_buttons(m)})
        rem = len(high) - 5
        if rem > 0:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"〈他{rem}件〉 <{_GMAIL_INBOX_URL}|受信トレイで見る>",
                    },
                }
            )
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

    # --- 📬 未確認（最大5件・1件=1行・要約なし）---
    if unread:
        lines = [f"📬 *未確認（{len(unread)}件）*"]
        for m in unread[:5]:
            subj, who = _subj_who(m)
            lines.append(f"• {who}: *{subj}*")
        rem = len(unread) - 5
        if rem > 0:
            lines.append(f"• 〈他{rem}件〉 <{_GMAIL_INBOX_URL}|受信トレイで見る>")
        _push_lines(lines)
        blocks.append({"type": "divider"})

    if not high and not unread:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "📭 *メール*: 新着なし"}}
        )
        blocks.append({"type": "divider"})

    # --- 💬 Slack 返信漏れ（最大5件・本文は正規化→60字切詰→escape）---
    if slack_unread:
        lines = [f"💬 *Slack 返信漏れ（{len(slack_unread)}件）*"]
        for it in slack_unread[:5]:
            ch = _slack_escape(
                getattr(it, "channel_name_display", "") or getattr(it, "channel_name_masked", "")
            )
            raw = getattr(it, "excerpt_display", "") or getattr(it, "excerpt_scrubbed", "")
            ex = _slack_escape(_truncate(_flatten_slack_text(raw), _COMPACT_EXCERPT_LEN))
            line = f"• *#{ch or '(不明)'}*: {ex}" if ex else f"• *#{ch or '(不明)'}*"
            link = getattr(it, "permalink", None)
            if link:
                line += f"  <{link}|開く>"  # permalink は実 URL なのでエスケープしない
            lines.append(line)
        rem = len(slack_unread) - 5
        if rem > 0:
            lines.append(f"• 〈他{rem}件〉")
        _push_lines(lines)
        blocks.append({"type": "divider"})

    # --- 📅 今日の予定（最大10件・1行形式は旧描画と共通）---
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
        rem = len(cal_items) - 10
        if rem > 0:
            lines.append(f"• 〈他{rem}件〉 <{_CALENDAR_URL}|カレンダーを開く>")
        _push_lines(lines)
    else:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "📅 *今日の予定*: なし"}}
        )

    # --- 脚注（DLP 注記・旧描画と同一）---
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

    # blocks 50 個上限の保険（静的上限の積算では起きない想定の最終ガード）。
    if len(blocks) > _COMPACT_MAX_BLOCKS:
        blocks = blocks[: _COMPACT_MAX_BLOCKS - 1]
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "_表示しきれない項目があります。Gmail / カレンダーで確認してください。_",
                    }
                ],
            }
        )
    return text, blocks


def _reminders_enabled() -> bool:
    """MORNING_DIGEST_REMINDERS=1 のときのみ予定リマインドを登録（既定OFF・§10 E1-2）。"""
    return os.environ.get("MORNING_DIGEST_REMINDERS", "").strip().lower() in {"1", "true", "yes"}


def _schedule_event_reminders(digest: Any, im_channel: str) -> int:
    """当日予定の「開始 N 分前」リマインドを EventBridge Scheduler に登録する（v0.3 Task5）。

    - 対象: start_at が「今から lead+1 分より先」の予定のみ（過ぎた/直近すぎる予定は skip）
    - 終日予定（date のみ）は対象外
    - payload に short title（≤60字）を載せる（2026-07-14・本人の予定を本人 DM に出す用途に
      限定。「何の予定か分からない」の解消・ユーザー要望）。Lambda はタイトルをログに出さない
    - schedule 名は channel×開始時刻から決定的＝再実行でも二重登録しない（Conflict→成功扱い）
    """
    from teamagent.adapters.scheduler_client import SchedulerClient

    try:
        scheduler = SchedulerClient.from_env()
    except ValueError as exc:
        print(f"[run_morning_digest_fargate] WARN: reminder 設定不備 {exc}", file=sys.stderr)
        return 0
    try:
        lead_min = int(os.environ.get("REMINDER_LEAD_MINUTES", "5"))
    except ValueError:
        lead_min = 5
    lead_min = min(60, max(1, lead_min))

    now = _dt.datetime.now(tz=_JST)
    count = 0
    for ev in list(getattr(digest, "calendar_events", []) or []):
        start_iso = str(getattr(ev, "start_at", "") or "")
        if "T" not in start_iso:
            continue  # 終日 or 不明
        try:
            start = _dt.datetime.fromisoformat(start_iso)
            if start.tzinfo is None:
                start = start.replace(tzinfo=_JST)
        except ValueError:
            continue
        fire_at = start - _dt.timedelta(minutes=lead_min)
        if fire_at <= now + _dt.timedelta(minutes=1):
            continue  # もう間に合わない/過去の予定
        url = str(getattr(ev, "meeting_url", "") or "") or _CALENDAR_URL
        # 本人の予定タイトル（本人 DM 表示用の display）。空なら通知は従来どおり無題で成立。
        title = str(getattr(ev, "summary_display", "") or getattr(ev, "summary_scrubbed", "") or "")
        ok = scheduler.schedule_reminder(
            channel=im_channel,
            start_iso=start_iso,
            fire_at=fire_at,
            url=url,
            request_id=f"reminder-{uuid.uuid4().hex[:8]}",
            title=title,
        )
        if ok:
            count += 1
    return count


async def _deliver_to_slack(
    user_email: str, text: str, blocks: list[dict[str, Any]]
) -> tuple[bool, str | None]:
    """Slack DM 配信（chat.postMessage with user IM channel）。

    返り値 (delivered, im_channel)。im_channel はリマインド登録（v0.3 Task5）が
    通知先として使う（配信失敗時は None）。
    """
    from teamagent.adapters.slack_client import SlackClient

    try:
        slack = SlackClient.from_env()
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: SlackClient.from_env 失敗 {exc}", file=sys.stderr
        )
        return (False, None)

    # email → Slack user_id → IM channel を開く
    try:
        user_id = await _email_to_slack_user_id(slack, user_email)
        if not user_id:
            return (False, None)
        im_channel = await _open_im_channel(slack, user_id)
        if not im_channel:
            return (False, None)
        result = await slack.post_message(
            channel=im_channel,
            text=text,
            request_id=f"morning-digest-{uuid.uuid4().hex[:8]}",
            blocks=blocks,
        )
        return (bool(getattr(result, "ok", False)), im_channel)
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: Slack 配信失敗 {type(exc).__name__}",
            file=sys.stderr,
        )
        return (False, None)


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
        if _compact_enabled():
            text, blocks = _format_block_kit_compact(digest, email)
        else:
            text, blocks = _format_block_kit(digest, email)
        delivered, im_channel = asyncio.run(_deliver_to_slack(email, text, blocks))
    except Exception as exc:
        print(
            f"[run_morning_digest_fargate] WARN: {_mask_email(email)} 配信失敗 "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return "error"
    if delivered:
        digest.delivered = True
        # v0.3 Task5: 当日予定の開始前リマインドをワンタイム登録（flag 既定OFF・fail-open＝
        # 登録失敗してもダイジェスト配信の成功は変えない）。
        if im_channel and _reminders_enabled():
            try:
                n = _schedule_event_reminders(digest, im_channel)
                if n:
                    print(f"[run_morning_digest_fargate] reminders scheduled: {n}", flush=True)
            except Exception as exc:
                print(
                    f"[run_morning_digest_fargate] WARN: reminder 登録失敗 {type(exc).__name__}",
                    file=sys.stderr,
                )
        return "delivered"
    return "error"


def main() -> int:
    require_runtime_startup((("mail_action", MAIL_ACTION_MAX_TOKEN_TTL_S),))
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
