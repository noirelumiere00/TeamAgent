"""Slack Bot ランタイム（Socket Mode）。

Sprint 1 末：mention テキストを SearchSkill にディスパッチして結果を返す。
DM では Sprint 1 時点では echo（次の Sprint で SearchSkill 接続）。

Usage:
    SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... \\
    python -m teamagent.runtime.slack_bot

CLAUDE.md 6-bis：
- 3層分離：本ファイルは Runtime 層。Slack API / Bedrock / pgvector は adapters 経由
- 構造化ログ：request_id を毎イベント生成して伝播
- prompt のファイル化：SearchSkill 経由で prompts/search/v1/system.md を読む
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.adapters.slack_client import SlackClient
from teamagent.hmac_durable_state import require_runtime_startup
from teamagent.hmac_keyring import (
    MAIL_ACTION_MAX_TOKEN_TTL_S,
    REPORT_LINK_MAX_TOKEN_TTL_S,
)
from teamagent.identity import build_rls_metadata, no_access_metadata
from teamagent.observability.sentry import (
    capture_event_exception,
    capture_skill_exception,
)
from teamagent.runtime.metrics_snapshot import MetricsSnapshotter
from teamagent.runtime.request_gate import (
    GateTimeoutError,
    QueueFullError,
    RequestGate,
)
from teamagent.runtime.usage_recorder import UsageEvent, UsageRecorder, UsageTrace
from teamagent.skills.base import SkillContext
from teamagent.skills.router import SkillRouter
from teamagent.skills.search.schema import SearchInput, SearchOutput

logger = structlog.get_logger(__name__)


def _gate_env_int(name: str, default: int) -> int:
    """env を int として読む（空・不正値は default）。RequestGate のチューニング用。"""
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# snapshot タスクの強参照を保持（GC でタスクが消えないように）
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


def _start_snapshotter(snapshotter: MetricsSnapshotter) -> None:
    """実行中ループがあれば runtime_metrics snapshot タスクを開始する。

    同期テスト等でループが無いときは何もしない（実機は asyncio.run 配下なので走る）。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(snapshotter.run())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


# Slack の @mention は <@U12345> 形式で来るので、テキストから剥がすための正規表現
_MENTION_PATTERN = re.compile(r"<@[A-Z0-9]+>\s*")


def strip_mention(text: str) -> str:
    """app_mention イベントのテキストから先頭の `<@BOT_ID>` を取り除く。

    例:
        "<@U082ABC> A社の前回提案は？" → "A社の前回提案は？"
    """
    return _MENTION_PATTERN.sub("", text, count=1).strip()


def _slack_thread_permalink(source_uri: str) -> str | None:
    """slack://CHANNEL_ID/THREAD_TS を Slack permalink URL に変換する。

    変換例: slack://C091ZSVTKF1/1748244936.050099
          → https://vectorinc.slack.com/archives/C091ZSVTKF1/p1748244936050099

    SLACK_WORKSPACE 環境変数が未設定の場合は None を返す。
    """
    # SLACK_WORKSPACE はワークスペース名のみ（例: "vectorinc"）
    workspace = os.environ.get("SLACK_WORKSPACE")
    if not workspace:
        return None
    if not source_uri.startswith("slack://"):
        return None
    rest = source_uri[len("slack://") :]
    parts = rest.split("/", 1)
    if len(parts) != 2:
        return None
    channel_id, thread_ts = parts[0], parts[1]
    # "1748244936.050099" → "1748244936050099" (小数点を除去)
    ts_digits = thread_ts.replace(".", "")
    return f"https://{workspace}.slack.com/archives/{channel_id}/p{ts_digits}"


# /teamagent_search で受け付けるオプションのホワイトリスト
# 未知キーは query にそのまま残す（=有効な値が誤って options に吸われない fail-safe）
_SLASH_COMMAND_ALLOWED_KEYS: frozenset[str] = frozenset({"industry", "top_k"})

_KV_PATTERN = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<val>\"[^\"]*\"|\S+)")


def parse_command_text(text: str) -> tuple[str, dict[str, str]]:
    """Slack スラッシュコマンドの text を「自然文クエリ + key=value オプション」に分解する。

    例:
        "案件 industry=飲食"             → ("案件", {"industry": "飲食"})
        "industry=飲食 top_k=10 PR事例"  → ("PR事例", {"industry": "飲食", "top_k": "10"})
        'industry="飲食 業界" 案件'       → ("案件", {"industry": "飲食 業界"})

    未知のキー（ホワイトリスト外）は options に取らず、query 側にそのまま残す。
    "Escape channels, users, and links" は Slack App 設定で OFF にする前提。
    """
    options: dict[str, str] = {}

    def _take(m: re.Match[str]) -> str:
        key = m.group("key")
        if key not in _SLASH_COMMAND_ALLOWED_KEYS:
            return m.group(0)  # 未知キーは query に残す
        val = m.group("val").strip('"')
        options[key] = val
        return ""

    residual = _KV_PATTERN.sub(_take, text)
    query = " ".join(residual.split()).strip()
    return query, options


def _format_hit_source_label(hit: Any) -> str:
    """SearchHitOut から「出典 + ページ」の表示ラベルを組み立てる。

    優先順位：
      1. source_type='slack' → 💬 channel_name（新スキーマ）
      2. file_name + page_num（構造化、Sprint 2 で追加）
      3. source 文字列（後方互換）
      4. chunk #N（最終フォールバック）
    """
    source_type = getattr(hit, "source_type", None)
    if source_type == "slack":
        channel = getattr(hit, "channel_name", None) or "Slack"
        return f"💬 *{channel}*"
    file_name = getattr(hit, "file_name", None)
    page_num = getattr(hit, "page_num", None)
    if file_name:
        if page_num is not None:
            return f"📄 *{file_name}* (p.{page_num})"
        return f"📄 *{file_name}*"
    if hit.source:
        return f"📄 {hit.source}"
    return f"chunk #{hit.chunk_id}"


def build_suggestions(output: SearchOutput) -> list[str] | None:
    """検索結果に添える「その他の提案」(関連 Skill への自然な導線)を最大3件返す。

    要件「ただの検索Bot ではなく提案までする相棒」を、追加 LLM コスト 0・レイテンシ 0 で満たす。
    提案文は **そのまま打てば次の Skill が起動する実トリガー語**にする（＝ワンクリック相当の導線）。
    hit に client_name があれば文脈語として差し込む。hits 0 件なら None（提案を出さない）。
    """
    if not output.hits:
        return None
    client = next(
        (h.client_name for h in output.hits if getattr(h, "client_name", None)),
        None,
    )
    karte = f"「{client}の状況を教えて」" if client else "「〇〇社の状況を教えて」"
    return [
        f"🗂️ 取引先の経緯・温度感をまとめる → {karte}",
        "📝 この内容で提案のたたき台を作る → 「〇〇社向けの提案を作って」",
        "🎬 競合のショート動画を分析する → 動画URLを貼る / 「TikTokで〇〇を検索して」",
    ]


def format_search_response(output: SearchOutput) -> str:
    """SearchOutput を Slack に表示する文字列（フォールバック / 通知用）に整形する。

    Block Kit を使う場合も text フィールドにこれを入れて、通知やインデックス用に保持する。
    引用フォーマット：📄 file_name (p.N) — score=0.91 → Drive で開く
    """
    lines = [output.answer, ""]
    if output.hits:
        lines.append("*参考資料:*")
        for hit in output.hits[:5]:
            label = _format_hit_source_label(hit)
            link = f" → <{hit.drive_url}|Drive で開く>" if hit.drive_url else ""
            lines.append(f"• {label}  _score={hit.score:.2f}_{link}")
    suggestions = build_suggestions(output)
    if suggestions:
        lines.append("")
        lines.append("*💡 その他の提案:*")
        lines.extend(f"• {s}" for s in suggestions)
    lines.append("")
    lines.append(f"_推算コスト: ${output.total_cost_usd:.4f}_")
    return "\n".join(lines)


def build_search_blocks(output: SearchOutput) -> list[dict[str, Any]]:
    """SearchOutput を Slack Block Kit に整形する。

    Drive URL があれば各 hit を「Drive で開く」ボタン付きで表示する。
    Block Kit が無効な環境（通知中心）でも text フィールドで読める形を保つ。
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": output.answer},
        }
    ]
    if output.hits:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "*参考資料*"}],
            }
        )
        for hit in output.hits[:5]:
            label = _format_hit_source_label(hit)
            # 出典 + score を1行で見やすく（score は item context として末尾に）
            line = f"• {label}  _score={hit.score:.2f}_"
            section: dict[str, Any] = {
                "type": "section",
                "text": {"type": "mrkdwn", "text": line},
            }
            # ボタン優先順位: Slack thread > Drive（source_type で判定）
            source_type = getattr(hit, "source_type", None)
            source_uri = getattr(hit, "source_uri", None)
            if source_type == "slack" and source_uri:
                permalink = _slack_thread_permalink(source_uri)
                if permalink:
                    section["accessory"] = {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "💬 Slack で開く"},
                        "url": permalink,
                        "action_id": f"open_slack_{hit.chunk_id}",
                    }
            elif hit.drive_url:
                section["accessory"] = {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📎 Drive で開く"},
                    "url": hit.drive_url,
                    "action_id": f"open_drive_{hit.chunk_id}",
                }
            blocks.append(section)

    suggestions = build_suggestions(output)
    if suggestions:  # hits があるときだけ（no-hits は divider を出さない契約を守る）
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*💡 その他の提案*\n" + "\n".join(f"• {s}" for s in suggestions),
                },
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"_推算コスト: ${output.total_cost_usd:.4f}_",
                }
            ],
        }
    )
    return blocks


def _first_line(analysis: str) -> str:
    """動画分析テキストから 1 行サマリを抜き出す (横断まとめの個別索引用)。"""
    lines = [ln.strip() for ln in analysis.splitlines()]
    for i, ln in enumerate(lines):
        if "一行サマリ" in ln:
            for nxt in lines[i + 1 :]:
                if nxt:
                    return nxt[:90]
    for ln in lines:
        if ln and not ln.startswith("#"):
            return ln[:90]
    return (analysis.strip()[:90]) or "（要約なし）"


def _fmt_count(n: int) -> str:
    """再生数等を 1.2万 / 3.4M 風に短縮表示する。"""
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def _format_tiktok_response(out: Any) -> str:
    """TikTokSearchOutput を Slack メッセージに整形する (上位動画リスト + 横断分析)。"""
    type_label = {
        "keyword": "キーワード",
        "hashtag": "ハッシュタグ",
        "keyword(fallback)": "キーワード(タグ→検索)",
    }.get(out.search_type, out.search_type)
    header = f"*🎵 TikTok 検索「{out.query}」* （{type_label} / {out.count}本）"

    if out.count == 0:
        return (
            f"{header}\n\n"
            "動画を取得できませんでした。キーワードを変えるか、少し時間をおいて再度お試しください。"
        )

    rows: list[str] = []
    for v in out.videos:
        title = (v.desc or "").replace("\n", " ").strip()[:50] or "（説明なし）"
        rows.append(
            f"{v.rank}. <{v.url}|@{v.author}> ▶︎{_fmt_count(v.play_count)} "
            f"♥{_fmt_count(v.digg_count)} 💬{_fmt_count(v.comment_count)} "
            f"🔖{_fmt_count(v.collect_count)} (係数{v.engagement_rate:.1%})\n   {title}"
        )
    table = "\n".join(rows)

    parts = [header, "", table]
    if out.analysis:
        cost = f" （分析 ${out.total_cost_usd:.4f}）" if out.total_cost_usd else ""
        parts += ["", f"*📊 横断分析*{cost}", out.analysis]
    return "\n".join(parts)


def _tiktok_error_reply(err: str) -> str:
    """TikTokScrapeError のマーカーをユーザー向け案内に変換する。"""
    if "TIKTOK_NODE_UNAVAILABLE" in err or "TIKTOK_SCRAPER_MISSING" in err:
        return (
            "🎵 TikTok 検索の実行環境が未整備です（Node.js / スクレイパ依存の未インストール）。"
            "サーバ側のセットアップが必要です。"
        )
    if "TIKTOK_NO_OUTPUT" in err:
        return (
            "🎵 TikTok 検索を起動できませんでした（Chrome 未検出の可能性）。"
            "サーバに Google Chrome が必要です。"
        )
    if "TIKTOK_TIMEOUT" in err:
        return "🎵 TikTok 検索がタイムアウトしました。少し時間をおいて再度お試しください。"
    if "TIKTOK_EMPTY_RESULT" in err:
        return (
            "🎵 該当する動画が見つかりませんでした（地域制限・CAPTCHA・0件の可能性）。"
            "キーワードを変えてお試しください。"
        )
    return "🎵 TikTok 検索でエラーが発生しました。少し時間をおいて再度お試しください。"


def _video_approval_error_reply(err: str) -> str:
    """VideoApproval の例外マーカーをユーザー向け案内に変換する。"""
    if "VIDEO_PROXY_NO_FFMPEG" in err:
        return (
            "🎬 納品動画が大きく、軽量化に必要な ffmpeg がサーバに見つかりませんでした。"
            "ffmpeg の導入が必要です。"
        )
    if "VIDEO_PROXY" in err:
        return (
            "🎬 納品動画の軽量化（ffmpeg 変換）に失敗しました。"
            "ファイル形式をご確認のうえ再度お試しください。"
        )
    if "DRIVE_FILE_TOO_LARGE" in err:
        return (
            "🎬 納品動画の容量が大きすぎて取得できませんでした。"
            "圧縮版か、別途 GCS 経由でのチェックをご検討ください。"
        )
    if "DRIVE_DOWNLOAD_FAILED" in err or "DRIVE_BAD_URL" in err:
        return (
            "🎬 Drive の納品動画を取得できませんでした（URL かアクセス権の可能性）。"
            "シートの FIX動画URL と共有設定をご確認ください。"
        )
    if "GEMINI" in err:
        return (
            "🎬 動画審査は Gemini の認証設定後に有効化されます"
            "（Vertex AI: GEMINI_USE_VERTEX、または GEMINI_API_KEY）。"
        )
    if "invalid_scope" in err or "insufficient" in err.lower():
        return (
            "🎬 シート/Drive の読取権限が不足しています（OAuth スコープ）。"
            "GOOGLE_FORCE_OAUTH=1 と個人 OAuth の設定をご確認ください。"
        )
    return "🎬 動画の一次チェック中にエラーが発生しました。"


def _format_oplog_response(out: Any) -> str:
    """OperationLogOutput を Slack メッセージに整形する (ログ本文 + 構造化サマリ)。"""
    header = f"*🧾 営業活動ログ* （{out.source_message_count} 件のやり取りから）"
    fields: list[str] = []
    if out.deal_phase:
        fields.append(f"フェーズ: *{out.deal_phase}*")
    if out.action:
        fields.append(f"アクション: {out.action}")
    if out.next_step:
        fields.append(f"次の一手: {out.next_step}")
    bant = out.bant
    bant_parts = [
        f"{label}: {val}"
        for label, val in (
            ("予算", bant.budget),
            ("決裁", bant.authority),
            ("課題", bant.need),
            ("時期", bant.timeline),
        )
        if val
    ]
    parts = [header, "", out.log_entry]
    if fields:
        parts += ["", "*— CRM 項目 —*", " / ".join(fields)]
    if bant_parts:
        parts += [f"BANT: {' / '.join(bant_parts)}"]
    if out.total_cost_usd:
        parts += ["", f"_概算コスト: ${out.total_cost_usd:.4f}_"]
    return "\n".join(parts)


def _mail_ref_label(ref: Any) -> str:
    """InternalRef を Slack mrkdwn の 1 行に整形する（slack:// は permalink へ変換）。"""
    icon = {"slack": "💬", "drive": "📄", "doc": "📄", "fb": "🗒️"}.get(ref.kind, "🔖")
    title = ref.title or "社内ナレッジ"
    url: str | None = None
    if ref.kind == "slack" and ref.source_uri:
        url = _slack_thread_permalink(ref.source_uri)  # SLACK_WORKSPACE 未設定なら None
    if url is None and ref.drive_url:
        url = ref.drive_url
    score = f"（関連度 {ref.score:.2f}）" if ref.score else ""
    if url:
        return f"{icon} <{url}|{title}> {score}".rstrip()
    snippet = (
        ref.snippet[:80] + "…" if ref.snippet and len(ref.snippet) > 80 else (ref.snippet or "")
    )
    line = f"{icon} {title} {score}".rstrip()
    return f"{line}\n   {snippet}" if snippet else line


def _format_mail_link_response(out: Any) -> str:
    """MailInternalContextOutput を Slack 表示用に整形（シグナル + 社内参照リンク）。"""
    sig = out.mail_signal
    lines = [f"*🔗 {out.client_name} — メール×社内ナレッジ*"]
    mail_bits = [f"直近メール {sig.recent_count} 件"]
    if sig.counterpart_domains:
        mail_bits.append("相手: " + ", ".join(sig.counterpart_domains))
    if sig.latest_at:
        mail_bits.append(f"最終 {sig.latest_at[:10]}")
    lines.append("📧 " + " / ".join(mail_bits))
    if out.summary:
        lines += ["", out.summary]
    if out.internal_refs:
        lines += ["", "*— 社内の関連スレッド・資料 —*"]
        lines += [f"• {_mail_ref_label(r)}" for r in out.internal_refs]
    else:
        lines += ["", "社内に該当する会話・資料は見つかりませんでした。"]
    if out.note:
        lines += ["", f"_{out.note}_"]
    if out.total_cost_usd:
        lines += [f"_概算コスト: ${out.total_cost_usd:.4f}_"]
    return "\n".join(lines)


def _format_mail_followup_response(out: Any) -> str:
    """MailFollowupOutput を Slack メッセージに整形する（放置日数つきトリアージ）。"""
    if not out.items:
        return (
            f"📭 {out.client_name} について、対象期間に放置気味の受信メールは見つかりませんでした。"
            f"\n_{out.note}_"
        )
    lines = [f"*📬 {out.client_name} — 相手から来たまま動いていないメール* （{len(out.items)} 件）"]
    for it in out.items:
        subj = it.subject_scrubbed or "(件名なし)"
        lines.append(f"• *{it.idle_days}日経過* / {it.counterpart_masked} / {subj}")
    lines += ["", f"_{out.note}_"]
    return "\n".join(lines)


def _format_mail_summary_response(out: Any) -> str:
    """MailSummaryOutput を Slack メッセージに整形する（横断要約 + 件名リスト）。"""
    lines = [f"*📨 {out.client_name} — メール要約* （{out.scanned_count} 件）"]
    if out.summary:
        lines += ["", out.summary]
    if out.highlights:
        lines += ["", "*— 対象メール —*"]
        for h in out.highlights:
            subj = h.subject_scrubbed or "(件名なし)"
            when = f"（{h.occurred_at[:10]}）" if h.occurred_at else ""
            lines.append(f"• {h.counterpart_masked} / {subj}{when}")
    if out.total_cost_usd:
        lines += ["", f"_概算コスト: ${out.total_cost_usd:.4f}_"]
    return "\n".join(lines)


def _format_mail_reply_response(out: Any) -> str:
    """MailReplyOutput を Slack メッセージに整形する（下書き全文を提示・送信はしない）。"""
    if not out.created:
        return f"✍️ {out.client_name}: {out.note}"
    lines = [
        f"*✍️ {out.client_name} — 返信ドラフトを作成しました*",
        f"宛先: {out.to_display}　件名: {out.draft_subject}",
        "",
        "```",
        out.draft_body,
        "```",
        "",
        f"_{out.note}_",
    ]
    if out.total_cost_usd:
        lines += [f"_概算コスト: ${out.total_cost_usd:.4f}_"]
    return "\n".join(lines)


# Skill ごとの受付メッセージ (どの処理を始めたか + 想定待ち時間をユーザーに伝える)。
# 重い処理 (動画/TikTok は実ブラウザや Gemini を使うため数十秒) ほど明示する価値が高い。
_ACK_BY_SKILL: dict[str, str] = {
    "search": "🔎 検索を受け付けました。資料を探しています…（10〜20秒）",
    "clientkarte": "🗂️ カルテ作成を受け付けました。履歴をまとめています…（10〜20秒）",
    "proposal_draft": "📝 提案ドラフトを受け付けました。過去提案を参照しています…（15〜30秒）",
    "proposal_review": "🔬 提案レビューを受け付けました。照合・診断しています…（15〜30秒）",
    "video_analysis": "🎬 動画分析を受け付けました。取得して解析しています…（30〜90秒）",
    "video_approval": "🎬 動画の一次チェックを受け付けました。オリエンと照合中…（30〜90秒）",
    "tiktok_search": "🎵 TikTok 検索を受け付けました。収集して分析しています…（30〜90秒）",
    "video_algorithm": "🔎 VSEO動画アルゴリズム分析を受付。上位動画を取得し解析中…（1〜3分）",
    "operation_log": "🧾 営業ログ化を受け付けました。スレッドを読んでまとめています…（10〜20秒）",
    "mail_to_internal_context": "🔗 メールと社内ナレッジの突き合わせを受け付けました…（10〜20秒）",
    "mail_followup": "📬 受信箱のトリアージを受け付けました。確認しています…（5〜15秒）",
    "mail_summary": "📨 メールの要約を受け付けました。受信箱を読んでいます…（10〜20秒）",
    "mail_reply": "✍️ 返信ドラフトの作成を受け付けました。起草しています…（10〜25秒）",
}
_ACK_DEFAULT = "🤖 受け付けました。処理しています…"

# 本人の受信箱に由来する個人情報を扱う Skill。@メンション元チャンネルに結果をブロードキャスト
# せず、本人にだけ ephemeral で返す（共有チャンネルでの情報漏えい防止・G3）。DM は元々本人限定。
_PRIVATE_SKILLS: frozenset[str] = frozenset(
    {"mail_reply", "mail_summary", "mail_to_internal_context", "mail_followup", "connect"}
)


async def _send_or_update(
    slack: Any,
    *,
    channel: str,
    ack_ts: str | None,
    text: str,
    request_id: str,
    thread_ts: str | None,
    blocks: list[dict[str, Any]] | None = None,
) -> None:
    """受付メッセージの ts があれば chat.update で書き換え、無ければ通常投稿する。

    「考え中 → 結果」を一つのメッセージで完結させ Slack タイムラインを汚さないための
    薄いヘルパ。update に失敗したら通常投稿にフォールバック（ユーザー無影響・graceful）。
    """
    if ack_ts:
        upd = await slack.update_message(
            channel=channel,
            ts=ack_ts,
            text=text,
            request_id=request_id,
            blocks=blocks,
        )
        if upd.ok:
            return
    await slack.post_message(
        channel=channel,
        text=text,
        request_id=request_id,
        thread_ts=thread_ts,
        blocks=blocks,
    )


def build_ack_message(message: str) -> str | None:
    """受信メッセージからどの Skill が動くか判定し、受付メッセージを返す。

    本処理 (dispatch_auto) より前に Slack へ即時投稿して「受け付けた」ことを伝える。
    判定は intent.detect_skill と同じヒューリスティックなので、実際に動く Skill と一致する。
    chitchat（雑談/挨拶/お礼/能力質問）は 1 通で即答するため受付メッセージを出さない（None）。
    """
    from teamagent.skills.intent import detect_skill, extract_search_topic

    try:
        skill = detect_skill(message).skill
    except Exception:
        return _ACK_DEFAULT
    if skill in ("chitchat", "connect"):
        return None  # 雑談/連携は即答（不自然な「受け付けました」ack を出さない）
    if skill == "search":
        topic = extract_search_topic(message)
        if topic:
            # 話題を復唱（要件:「受け付けました。〇〇について検索します」）。
            return f"🔎 受け付けました。『{topic}』について検索します（資料を探索中…10〜20秒）"
        # 話題が抽出できなければ従来の汎用 search ack（「受け付けました」「検索」を温存）。
    return _ACK_BY_SKILL.get(skill, _ACK_DEFAULT)


class SkillDispatcher:
    """mention テキストを Skill に振り分けて結果を返す。

    Sprint 1 末：常に "search" Skill にディスパッチ。
    Sprint 2+ でルールベース or Claude Haiku ベースのルーターを実装。
    """

    def __init__(self, router: SkillRouter | None = None) -> None:
        self._skill_cache: dict[str, Any] = {}
        # Slack user_id → email cache（RLS GUC 注入用、users.info の呼び出しを削減）
        self._user_email_cache: dict[str, str | None] = {}
        # per-user OAuth TokenStore（mail_* Skill が本人トークンを引く）。遅延構築・キャッシュ。
        self._token_store: Any | None = None
        if router is not None:
            self._router = router
        else:
            # USE_LLM_ROUTER=true で Haiku 4.5 ベースの自然文判定を有効化
            use_llm_router = os.environ.get("USE_LLM_ROUTER", "false").lower() in (
                "1",
                "true",
                "yes",
            )
            if use_llm_router:
                from teamagent.adapters.bedrock_client import BedrockClient

                haiku_model_id = os.environ.get(
                    "BEDROCK_HAIKU_MODEL_ID",
                    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                )
                haiku = BedrockClient(
                    region=os.environ.get("AWS_REGION", "us-east-1"),
                    model_id=haiku_model_id,
                )
                self._router = SkillRouter(bedrock=haiku)
                logger.info("router_initialized", llm_fallback=True)
            else:
                self._router = SkillRouter()
                logger.info("router_initialized", llm_fallback=False)

    def get_search_skill(self) -> Any:
        """SearchSkill インスタンスをキャッシュして返す（embedder ロードが重い）。

        環境変数 USE_CONTEXTUAL=true で Contextual Retrieval 版に切替。
        proposals_chunks_contextual テーブルを参照、contextualized_text 列を検索。

        Skill ごとに __init__ 引数が異なるため、ここでは search 専用の生成ロジックを持つ。
        Sprint 2 で Router を導入したら抽象化する。
        """
        if "search" in self._skill_cache:
            return self._skill_cache["search"]
        # QW-2: env→引数解決は orchestrator.factory に集約（唯一の真実源）。
        # かつて本メソッドは rerank_pool_size / min_relevance_fallback / use_client_boost /
        # use_knowledge_filters を渡さずコンストラクタ既定（30/0.0/False/False）に落ち、本番 env を
        # 入れても slack_bot 経路では黙って無効だった（構築ドリフト）。build_search_skill_from_env()
        # に委譲し、factory（MCP/OpenClaw 経路）と同一 env・同一ノブで構築する。
        from teamagent.orchestrator.factory import (
            build_search_skill_from_env,
            resolve_search_skill_config,
        )

        instance = build_search_skill_from_env()
        logger.info("search_skill_initialized", source="slack_bot", **resolve_search_skill_config())
        self._skill_cache["search"] = instance
        return instance

    def get_karte_skill(self) -> Any:
        """ClientKarteSkill インスタンスをキャッシュして返す。"""
        if "clientkarte" in self._skill_cache:
            return self._skill_cache["clientkarte"]
        from teamagent.skills.clientkarte.skill import ClientKarteSkill

        karte_prompt_version = os.environ.get("KARTE_PROMPT_VERSION", "v1")
        instance = ClientKarteSkill(prompt_version=karte_prompt_version)
        logger.info("clientkarte_skill_initialized", prompt_version=karte_prompt_version)
        self._skill_cache["clientkarte"] = instance
        return instance

    async def run_karte(
        self,
        client_name: str,
        request_id: str,
        user_id: str | None,
        *,
        limit: int = 20,
    ) -> Any:
        """ClientKarteSkill を実行してカルテを返す。RLS 用に user_email を解決する。"""
        user_email = await self._resolve_user_email(user_id)
        skill = self.get_karte_skill()
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata=build_rls_metadata(user_email) or no_access_metadata(),
        )
        from teamagent.skills.clientkarte.schema import ClientKarteInput

        input_obj = ClientKarteInput(client_name=client_name, limit=limit)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, skill.run, input_obj, ctx)

    def get_draft_skill(self) -> Any:
        """ProposalDraftSkill をキャッシュして返す。検索基盤 (SearchSkill) を再利用する。"""
        if "proposal_draft" in self._skill_cache:
            return self._skill_cache["proposal_draft"]
        from teamagent.skills.proposal.skill import ProposalDraftSkill

        draft_prompt_version = os.environ.get("DRAFT_PROMPT_VERSION", "v1")
        # 同じ SearchSkill インスタンス (本番 88% 構成) を注入して retrieval を共有
        instance = ProposalDraftSkill(
            search=self.get_search_skill(),
            prompt_version=draft_prompt_version,
        )
        logger.info("proposal_draft_skill_initialized", prompt_version=draft_prompt_version)
        self._skill_cache["proposal_draft"] = instance
        return instance

    async def run_draft(
        self,
        brief: str,
        request_id: str,
        user_id: str | None,
        *,
        industry: str | None = None,
        top_k: int = 8,
    ) -> Any:
        """ProposalDraftSkill を実行して提案ドラフト骨子を返す。"""
        user_email = await self._resolve_user_email(user_id)
        skill = self.get_draft_skill()
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata=build_rls_metadata(user_email) or no_access_metadata(),
        )
        from teamagent.skills.proposal.schema import ProposalDraftInput

        input_obj = ProposalDraftInput(brief=brief, industry=industry, top_k=top_k)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, skill.run, input_obj, ctx)

    def get_review_skill(self) -> Any:
        """ProposalReviewSkill をキャッシュして返す。検索基盤 (SearchSkill) を再利用する。"""
        if "proposal_review" in self._skill_cache:
            return self._skill_cache["proposal_review"]
        from teamagent.skills.proposal_review.skill import ProposalReviewSkill

        review_prompt_version = os.environ.get("REVIEW_PROMPT_VERSION", "v1")
        instance = ProposalReviewSkill(
            search=self.get_search_skill(),
            prompt_version=review_prompt_version,
        )
        logger.info("proposal_review_skill_initialized", prompt_version=review_prompt_version)
        self._skill_cache["proposal_review"] = instance
        return instance

    async def run_review(
        self,
        proposal_text: str,
        request_id: str,
        user_id: str | None,
        *,
        industry: str | None = None,
    ) -> Any:
        """ProposalReviewSkill を実行して提案の診断を返す。"""
        user_email = await self._resolve_user_email(user_id)
        skill = self.get_review_skill()
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata=build_rls_metadata(user_email) or no_access_metadata(),
        )
        from teamagent.skills.proposal_review.schema import ProposalReviewInput

        input_obj = ProposalReviewInput(proposal_text=proposal_text, industry=industry)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, skill.run, input_obj, ctx)

    def get_video_skill(self) -> Any:
        """VideoAnalysisSkill をキャッシュして返す (GEMINI_API_KEY は遅延解決)。"""
        if "video_analysis" in self._skill_cache:
            return self._skill_cache["video_analysis"]
        from teamagent.skills.video.skill import VideoAnalysisSkill

        instance = VideoAnalysisSkill(prompt_version=os.environ.get("VIDEO_PROMPT_VERSION", "v1"))
        logger.info("video_analysis_skill_initialized")
        self._skill_cache["video_analysis"] = instance
        return instance

    async def run_video(
        self, url: str, request_id: str, user_id: str | None, *, focus: str | None = None
    ) -> Any:
        """VideoAnalysisSkill を実行して競合動画の構造分析を返す。"""
        skill = self.get_video_skill()
        ctx = SkillContext(request_id=request_id, user_id=user_id)
        from teamagent.skills.video.schema import VideoAnalysisInput

        input_obj = VideoAnalysisInput(url=url, focus=focus)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, skill.run, input_obj, ctx)

    async def run_video_bytes(
        self, data: bytes, mime: str, request_id: str, user_id: str | None
    ) -> Any:
        """アップロードされた動画 bytes を分析する。"""
        skill = self.get_video_skill()
        ctx = SkillContext(request_id=request_id, user_id=user_id)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: skill.analyze_bytes(data, mime, ctx))

    async def run_video_batch(
        self, urls: list[str], request_id: str, user_id: str | None
    ) -> tuple[str, float]:
        """複数動画 URL を並行分析し、横断まとめ + 個別サマリを返す。"""

        sem = asyncio.Semaphore(4)  # 同時実行を絞り memory / rate を保護

        async def _one(u: str) -> tuple[str | None, float]:
            async with sem:
                try:
                    out = await self.run_video(u, request_id, user_id)
                    return out.analysis, out.total_cost_usd
                except Exception:
                    logger.warning("video_batch_item_failed", request_id=request_id)
                    return None, 0.0

        results = await asyncio.gather(*[_one(u) for u in urls])
        return await self._synthesize_many(results, request_id, user_id, kind="URL")

    async def run_video_uploads(
        self,
        items: list[tuple[bytes, str]],
        request_id: str,
        user_id: str | None,
    ) -> tuple[str, float]:
        """複数のアップロード動画 (bytes, mime) を分析し、横断まとめを返す。"""

        sem = asyncio.Semaphore(3)

        async def _one(item: tuple[bytes, str]) -> tuple[str | None, float]:
            data, mime = item
            async with sem:
                try:
                    out = await self.run_video_bytes(data, mime, request_id, user_id)
                    return out.analysis, out.total_cost_usd
                except Exception:
                    logger.warning("video_upload_item_failed", request_id=request_id)
                    return None, 0.0

        results = await asyncio.gather(*[_one(it) for it in items])
        return await self._synthesize_many(results, request_id, user_id, kind="アップロード")

    async def _synthesize_many(
        self,
        results: list[tuple[str | None, float]],
        request_id: str,
        user_id: str | None,
        *,
        kind: str,
    ) -> tuple[str, float]:
        """個別分析リスト → (1本ならそのまま / 複数なら横断まとめ + 個別サマリ)。"""
        ok = [(a, c) for (a, c) in results if a]
        total = sum(c for (_, c) in results)
        if not ok:
            return (
                f"🎬 {kind}の動画を分析できませんでした（非公開・取得不可・容量超過の可能性）。",
                total,
            )
        if len(ok) == 1:
            return f"*🎬 動画分析* （${total:.4f}）\n\n{ok[0][0]}", total

        skill = self.get_video_skill()
        ctx = SkillContext(request_id=request_id, user_id=user_id)
        loop = asyncio.get_running_loop()
        analyses = [a for (a, _) in ok]
        synth, scost = await loop.run_in_executor(None, skill.synthesize_batch, analyses, ctx)
        index = "\n".join(f"{i + 1}. {_first_line(a)}" for i, a in enumerate(analyses))
        header = f"*🎬 {len(ok)}本の横断分析* （${total + scost:.4f}）"
        return f"{header}\n\n{synth}\n\n*— 個別サマリ —*\n{index}", total + scost

    def get_tiktok_skill(self) -> Any:
        """TikTokSearchSkill をキャッシュして返す。"""
        if "tiktok_search" in self._skill_cache:
            return self._skill_cache["tiktok_search"]
        from teamagent.skills.tiktok_search.skill import TikTokSearchSkill

        instance = TikTokSearchSkill(prompt_version=os.environ.get("TIKTOK_PROMPT_VERSION", "v1"))
        logger.info("tiktok_search_skill_initialized")
        self._skill_cache["tiktok_search"] = instance
        return instance

    async def run_tiktok(
        self,
        query: str,
        request_id: str,
        user_id: str | None,
        *,
        search_type: str = "keyword",
        max_videos: int = 10,
    ) -> Any:
        """TikTokSearchSkill を別スレッドで実行 (Node subprocess + Gemini が同期 I/O)。"""
        skill = self.get_tiktok_skill()
        ctx = SkillContext(request_id=request_id, user_id=user_id)
        from teamagent.skills.tiktok_search.schema import TikTokSearchInput

        input_obj = TikTokSearchInput(query=query, search_type=search_type, max_videos=max_videos)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, skill.run, input_obj, ctx)

    def get_oplog_skill(self) -> Any:
        """OperationLogSkill をキャッシュして返す。"""
        if "operation_log" in self._skill_cache:
            return self._skill_cache["operation_log"]
        from teamagent.skills.operation_log.skill import OperationLogSkill

        instance = OperationLogSkill(prompt_version=os.environ.get("OPLOG_PROMPT_VERSION", "v1"))
        logger.info("operation_log_skill_initialized")
        self._skill_cache["operation_log"] = instance
        return instance

    async def run_oplog(
        self, channel_id: str, thread_ts: str, request_id: str, user_id: str | None
    ) -> Any:
        """OperationLogSkill を別スレッドで実行 (Slack 取得 + Bedrock が同期 I/O)。"""
        skill = self.get_oplog_skill()
        ctx = SkillContext(request_id=request_id, user_id=user_id)
        from teamagent.skills.operation_log.schema import OperationLogInput

        input_obj = OperationLogInput(channel_id=channel_id, thread_ts=thread_ts)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, skill.run, input_obj, ctx)

    # ── メール系 Skill（per-user OAuth・readonly）─────────────────────────────

    def _get_token_store(self) -> Any:
        """per-user OAuth TokenStore を遅延構築してキャッシュ（factory._build_token_store と同型）。

        OAUTH_KMS_KEY_ID + RDS が無ければ InMemory（空＝全員未連携）にフォールバックする。
        本番 Bot プロセスは RDS pgvector + KMS に到達できる必要がある（届かないと全員未連携）。
        """
        if self._token_store is not None:
            return self._token_store
        key_id = os.environ.get("OAUTH_KMS_KEY_ID")
        if not key_id:
            from teamagent.adapters.oauth_token_store import InMemoryTokenStore

            logger.warning(
                "mail_token_store_inmemory", reason="OAUTH_KMS_KEY_ID 未設定（全員未連携）"
            )
            self._token_store = InMemoryTokenStore()
            return self._token_store
        from teamagent.adapters.oauth_token_store import KmsCipher, RdsTokenStore
        from teamagent.adapters.pgvector_client import PgVectorClient

        self._token_store = RdsTokenStore(PgVectorClient.from_env(), KmsCipher(key_id))
        logger.info("mail_token_store_rds_initialized")
        return self._token_store

    async def _connect_message(self, user_id: str | None, request_id: str) -> str:
        """本人専用の Google＋Slack 連携（認可）リンク文面を作る。スラッシュ/メンション双方で共用。

        スラッシュコマンド未登録の Slack でも、@メンション/DM で「連携」と言えばこの文面を返せる。
        Google（メール/カレンダー）に加え、Slack 個人トークン（本人検索/巡回）の認可URLも並記する。
        Slack 側は SLACK_OAUTH_REDIRECT_URI が未設定なら出さず（後方互換）、その旨を1行添える。
        """
        email = await self._resolve_user_email(user_id)
        if not email:
            return (
                "🔗 連携の準備に失敗しました"
                "（管理者へ: Bot に users:read.email スコープが必要です）。"
            )
        redirect_uri = os.environ.get("OAUTH_REDIRECT_URI", "").strip()
        if not redirect_uri:
            return "🔗 連携機能が未設定です（管理者へ: OAUTH_REDIRECT_URI を設定してください）。"
        try:
            from teamagent.adapters.google_oauth_flow import OAuthConsentFlow

            url_google, _state = OAuthConsentFlow(redirect_uri=redirect_uri).authorization_url(
                email
            )
        except Exception:
            logger.warning("connect_url_failed", request_id=request_id, user_id=user_id)
            return "🔗 連携リンクの生成に失敗しました（管理者へ: OAuth 系 env をご確認ください）。"

        # Slack 個人トークン(xoxp) の認可URL。未設定時は Slack リンクを出さない。
        slack_redirect = os.environ.get("SLACK_OAUTH_REDIRECT_URI", "").strip()
        url_slack: str | None = None
        if slack_redirect:
            try:
                from teamagent.adapters.slack_oauth_flow import SlackOAuthConsentFlow

                url_slack, _ = SlackOAuthConsentFlow(redirect_uri=slack_redirect).authorization_url(
                    email
                )
            except Exception:
                logger.warning("connect_slack_url_failed", request_id=request_id, user_id=user_id)
                url_slack = None

        logger.info(
            "connect_link_issued",
            request_id=request_id,
            user_id=user_id,
            slack_included=bool(url_slack),
        )
        lines = [
            f"👋 *{email}* を連携します（1回だけ・所要1分）。\n",
            "下のリンクを開き、表示される権限を *許可* してください:\n",
            "*① Google を連携*（メールの読み取り・下書き作成、カレンダー等）\n",
            f"{url_google}\n",
        ]
        if url_slack:
            lines.append("\n*② Slack を連携*（本人としての検索・チャンネル巡回）\n")
            lines.append(f"{url_slack}\n")
        else:
            lines.append(
                "\n※ Slack 連携は現在未設定です"
                "（管理者へ: SLACK_OAUTH_REDIRECT_URI を設定してください）。\n"
            )
        lines.append("\n「✅ 連携が完了しました」が出れば成功です。あとは AI に話しかけるだけ。")
        return "".join(lines)

    def get_mail_link_skill(self) -> Any:
        """MailToInternalContextSkill をキャッシュ（per-user token + SearchSkill 再利用）。"""
        if "mail_to_internal_context" in self._skill_cache:
            return self._skill_cache["mail_to_internal_context"]
        from teamagent.skills.mail_to_internal_context.skill import MailToInternalContextSkill

        use_summary = os.environ.get("USE_MAIL_LINK_SUMMARY", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        bedrock = None
        if use_summary:
            from teamagent.adapters.bedrock_client import BedrockClient

            bedrock = BedrockClient.from_env()
        instance = MailToInternalContextSkill(
            token_store=self._get_token_store(),
            search_skill=self.get_search_skill(),
            bedrock=bedrock,
            use_summary=use_summary,
        )
        logger.info("mail_to_internal_context_skill_initialized", use_summary=use_summary)
        self._skill_cache["mail_to_internal_context"] = instance
        return instance

    async def run_mail_link(self, client_name: str, request_id: str, user_id: str | None) -> Any:
        """MailToInternalContextSkill を別スレッドで実行（Gmail/pgvector が同期 I/O）。"""
        user_email = await self._resolve_user_email(user_id)
        user_groups: list[str] = []
        if user_email and "@" in user_email:
            user_groups.append(user_email.split("@", 1)[1])

        skill = self.get_mail_link_skill()
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata={
                "user_email": user_email,
                "user_groups": user_groups,
                "user_role": "member",
            },
        )
        from teamagent.skills.mail_to_internal_context.schema import MailInternalContextInput

        input_obj = MailInternalContextInput(client_name=client_name)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, skill.run, input_obj, ctx)

    def get_mail_followup_skill(self) -> Any:
        """MailFollowupSkill をキャッシュして返す（per-user token・LLM 不使用）。"""
        if "mail_followup" in self._skill_cache:
            return self._skill_cache["mail_followup"]
        from teamagent.skills.mail_followup.skill import MailFollowupSkill

        instance = MailFollowupSkill(token_store=self._get_token_store())
        logger.info("mail_followup_skill_initialized")
        self._skill_cache["mail_followup"] = instance
        return instance

    async def run_mail_followup(
        self,
        client_name: str,
        request_id: str,
        user_id: str | None,
        *,
        idle_days: int | None = None,
    ) -> Any:
        """MailFollowupSkill を別スレッドで実行（Gmail が同期 I/O）。"""
        user_email = await self._resolve_user_email(user_id)
        user_groups: list[str] = []
        if user_email and "@" in user_email:
            user_groups.append(user_email.split("@", 1)[1])

        skill = self.get_mail_followup_skill()
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata={
                "user_email": user_email,
                "user_groups": user_groups,
                "user_role": "member",
            },
        )
        from teamagent.skills.mail_followup.schema import MailFollowupInput

        input_obj = MailFollowupInput(client_name=client_name, idle_days=idle_days)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, skill.run, input_obj, ctx)

    def get_mail_summary_skill(self) -> Any:
        """MailSummarySkill をキャッシュして返す（per-user token・Bedrock は遅延構築）。"""
        if "mail_summary" in self._skill_cache:
            return self._skill_cache["mail_summary"]
        from teamagent.skills.mail_summary.skill import MailSummarySkill

        instance = MailSummarySkill(token_store=self._get_token_store())
        logger.info("mail_summary_skill_initialized")
        self._skill_cache["mail_summary"] = instance
        return instance

    async def run_mail_summary(self, client_name: str, request_id: str, user_id: str | None) -> Any:
        """MailSummarySkill を別スレッドで実行（Gmail/Bedrock が同期 I/O）。"""
        user_email = await self._resolve_user_email(user_id)
        user_groups: list[str] = []
        if user_email and "@" in user_email:
            user_groups.append(user_email.split("@", 1)[1])

        skill = self.get_mail_summary_skill()
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata={"user_email": user_email, "user_groups": user_groups, "user_role": "member"},
        )
        from teamagent.skills.mail_summary.schema import MailSummaryInput

        input_obj = MailSummaryInput(client_name=client_name)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, skill.run, input_obj, ctx)

    def get_mail_reply_skill(self) -> Any:
        """MailReplySkill をキャッシュして返す（per-user token・Bedrock は遅延構築）。

        gmail.modify（下書き作成）を使う。送信/削除は adapter denylist で物理封鎖。
        """
        if "mail_reply" in self._skill_cache:
            return self._skill_cache["mail_reply"]
        from teamagent.skills.mail_reply.skill import MailReplySkill

        instance = MailReplySkill(token_store=self._get_token_store())
        logger.info("mail_reply_skill_initialized")
        self._skill_cache["mail_reply"] = instance
        return instance

    async def run_mail_reply(
        self,
        client_name: str,
        request_id: str,
        user_id: str | None,
        *,
        instructions: str | None = None,
    ) -> Any:
        """MailReplySkill を別スレッドで実行（Gmail/Bedrock が同期 I/O）。下書き保存のみ。"""
        user_email = await self._resolve_user_email(user_id)
        user_groups: list[str] = []
        if user_email and "@" in user_email:
            user_groups.append(user_email.split("@", 1)[1])

        skill = self.get_mail_reply_skill()
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata={"user_email": user_email, "user_groups": user_groups, "user_role": "member"},
        )
        from teamagent.skills.mail_reply.schema import MailReplyInput

        input_obj = MailReplyInput(client_name=client_name, instructions=instructions)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, skill.run, input_obj, ctx)

    def get_chitchat_skill(self) -> Any:
        """ChitchatSkill をキャッシュして返す（RAG/embedder を持たない軽量会話 Skill）。"""
        if "chitchat" in self._skill_cache:
            return self._skill_cache["chitchat"]
        from teamagent.skills.chitchat.skill import ChitchatSkill

        instance = ChitchatSkill(prompt_version=os.environ.get("CHITCHAT_PROMPT_VERSION", "v1"))
        logger.info("chitchat_skill_initialized")
        self._skill_cache["chitchat"] = instance
        return instance

    async def run_chitchat(self, message: str, request_id: str, user_id: str | None) -> Any:
        """ChitchatSkill を別スレッドで実行（Bedrock が同期 I/O）。検索/RAG は走らせない。"""
        skill = self.get_chitchat_skill()
        ctx = SkillContext(request_id=request_id, user_id=user_id)
        from teamagent.skills.chitchat.schema import ChitchatInput

        input_obj = ChitchatInput(message=message)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, skill.run, input_obj, ctx)

    def get_video_approval_skill(self) -> Any:
        """VideoApprovalSkill をキャッシュして返す (Gemini/Drive は遅延解決)。"""
        if "video_approval" in self._skill_cache:
            return self._skill_cache["video_approval"]
        from teamagent.skills.video_approval.skill import VideoApprovalSkill

        instance = VideoApprovalSkill(
            prompt_version=os.environ.get("VIDEO_APPROVAL_PROMPT_VERSION", "v1")
        )
        logger.info("video_approval_skill_initialized")
        self._skill_cache["video_approval"] = instance
        return instance

    def _extract_orientation(self, sheet_id: str, management_no: str, request_id: str) -> Any:
        """案件シートから 1 クリエイティブ分のオリエン + 納品動画URLを取り出す (同期 I/O)。"""
        from teamagent.skills.video_approval.sheet_orientation import OrientationExtractor

        extractor = OrientationExtractor(
            client_name=os.environ.get("VIDEO_APPROVAL_CLIENT_NAME") or None
        )
        return extractor.extract(sheet_id, management_no, request_id=request_id)

    async def run_video_approval(
        self,
        management_no: str | None,
        request_id: str,
        user_id: str | None,
        *,
        sheet_id: str | None = None,
    ) -> str:
        """管理番号 → オリエン抽出 → 納品動画 DL → 一次FB審査 → Slack 整形文を返す。

        テスト段階の出力先は Slack。シート列への書込は spreadsheets 再認証後に解禁。
        失敗系はユーザー向けの案内文に変換して返す（例外は投げない）。
        """
        resolved_sheet = sheet_id or os.environ.get("VIDEO_APPROVAL_SHEET_ID")
        if not resolved_sheet:
            return (
                "🎬 審査対象のスプレッドシートが未設定です。"
                "メッセージにシート URL を含めるか、VIDEO_APPROVAL_SHEET_ID を設定してください。"
            )
        if not management_no:
            return (
                "🎬 管理番号を指定してください。例: `@TeamAgent 動画チェック E01-01`\n"
                "（管理番号は投稿管理シートの B 列の値です）"
            )

        loop = asyncio.get_running_loop()
        try:
            extract = await loop.run_in_executor(
                None, self._extract_orientation, resolved_sheet, management_no, request_id
            )
        except Exception as e:  # シート読取失敗 (権限/スコープ等)
            logger.exception("video_approval_extract_failed", request_id=request_id)
            return _video_approval_error_reply(str(e))

        if extract is None:
            return (
                f"🎬 管理番号 `{management_no}` が投稿管理シートに見つかりませんでした。"
                "番号をご確認ください。"
            )
        if not extract.has_drive_video:
            current = extract.video_url or "（空）"
            return (
                f"🎬 `{management_no}` はまだ納品動画（Drive URL）が入っていません"
                f"（現在の値: {current}）。編集者の入稿後に再度お試しください。"
            )

        skill = self.get_video_approval_skill()
        ctx = SkillContext(request_id=request_id, user_id=user_id)
        from teamagent.skills.video_approval.schema import VideoApprovalInput

        input_obj = VideoApprovalInput(orientation=extract.orientation, video_url=extract.video_url)
        try:
            out = await loop.run_in_executor(None, skill.run, input_obj, ctx)
        except Exception as e:  # Drive DL / Gemini 失敗
            logger.exception("video_approval_run_failed", request_id=request_id)
            return _video_approval_error_reply(str(e))

        from teamagent.skills.video_approval.sheet_writeback import format_check_slack

        return format_check_slack(
            out,
            management_no=management_no,
            creative_name=extract.orientation.main_message,
            video_url=extract.video_url,
        )

    def get_video_algorithm_skill(self) -> Any:
        """VideoAlgorithmSkill をキャッシュして返す。"""
        if "video_algorithm" in self._skill_cache:
            return self._skill_cache["video_algorithm"]
        from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill

        instance = VideoAlgorithmSkill(
            prompt_version=os.environ.get("VIDEO_ALGO_PROMPT_VERSION", "v1")
        )
        logger.info("video_algorithm_skill_initialized")
        self._skill_cache["video_algorithm"] = instance
        return instance

    async def run_video_algorithm(
        self,
        query: str | None,
        request_id: str,
        user_id: str | None,
        *,
        reply_channel: str | None = None,
        reply_thread_ts: str | None = None,
    ) -> str:
        """検索KWの上位動画を分析し、HTMLレポートを Slack に添付して通知文を返す。

        Slack は『通知』のみ（全詳細は添付 HTML に埋め込む）。reply_channel があれば
        その場所にレポートをファイル添付する。
        """
        if not query:
            return (
                "🔎 分析する検索キーワードを指定してください。例: `@TeamAgent VSEO分析 新宿 ランチ`"
            )
        skill = self.get_video_algorithm_skill()
        ctx_metadata: dict[str, Any] = {}
        # 既定 OFF 時は resolver/API 呼出しも含め完全 no-op。quota ON の旧 Socket Mode 経路でも
        # MCP と同じ本人 email を注入し、空 email による計量迂回を許さない。
        from teamagent.adapters.quota_store import VideoQuotaStore

        if VideoQuotaStore.enabled():
            user_email = await self._resolve_user_email(user_id)
            if user_email:
                ctx_metadata["user_email"] = user_email
        ctx = SkillContext(request_id=request_id, user_id=user_id, metadata=ctx_metadata)
        from teamagent.skills.video_algorithm.schema import (
            VideoAlgorithmInput,
            VideoAlgorithmOutput,
        )

        input_obj = VideoAlgorithmInput(
            query=query,
            max_videos=int(os.environ.get("VIDEO_ALGO_MAX_VIDEOS", "5")),
            client_name=os.environ.get("VIDEO_APPROVAL_CLIENT_NAME") or None,
        )
        loop = asyncio.get_running_loop()
        out: VideoAlgorithmOutput
        try:
            out = await loop.run_in_executor(None, skill.run, input_obj, ctx)
        except RuntimeError as e:
            if "TIKTOK" in str(e):
                return _tiktok_error_reply(str(e))
            if "GEMINI" in str(e):
                return "🔎 動画分析は Gemini の認証設定後に有効化されます（Vertex/APIキー）。"
            if "VIDEO_ALGORITHM_IN_PROGRESS" in str(e):
                return "🔎 同じ条件の動画分析がまだ処理中です。完了通知を待ってください。"
            if "VIDEO_QUOTA_IDENTITY_REQUIRED" in str(e):
                return "🔎 利用者を確認できないため動画分析を開始できませんでした。"
            if "VIDEO_QUOTA_EXCEEDED" in str(e):
                return str(e).split(":", 1)[-1].strip()
            if "VIDEO_ALGORITHM_CACHE_UNAVAILABLE" in str(e):
                return "🔎 二重課金防止を確認できないため、動画分析を開始しませんでした。"
            if "VIDEO_ALGORITHM_LEASE_LOST" in str(e):
                return (
                    "🔎 二重実行を避けるため動画分析を中止しました。"
                    "時間を置いて再実行してください。"
                )
            raise

        try:
            # レポートを非公開S3に公開し署名付きURL(7日)を通知に添える（URL形式配信）。
            # §M: skill が既に発行済み(out.report_url)ならそれを再利用（二重publish回避）。
            report_url: str | None = out.report_url
            if report_url is None and out.report_html_path:
                from teamagent.adapters.report_publish import publish_html_file

                path = out.report_html_path
                report_url = await loop.run_in_executor(
                    None,
                    lambda: publish_html_file(path, request_id=request_id, query=query or ""),
                )

            # HTML レポートを Slack にも添付（オフライン閲覧用・通知文は dispatch 経由で別途投稿）
            if out.report_html_path and reply_channel:
                try:
                    from teamagent.adapters.slack_client import SlackClient

                    slack = SlackClient(bot_token=os.environ.get("SLACK_BOT_TOKEN", ""))
                    await slack.upload_file(
                        reply_channel,
                        out.report_html_path,
                        request_id,
                        title=f"VSEO分析レポート_{query}.html",
                        thread_ts=reply_thread_ts,
                    )
                except Exception:
                    logger.warning("video_algorithm_report_upload_failed", request_id=request_id)

            summary = out.slack_summary
            if report_url:
                summary += f"\n🔗 *レポートURL*（7日間有効・ブラウザで開けます）: {report_url}"
            return summary
        finally:
            skill.cleanup_output(out)

    async def dispatch_auto(
        self,
        message: str,
        request_id: str,
        user_id: str | None,
        *,
        channel_id: str | None = None,
        thread_ts: str | None = None,
        reply_channel: str | None = None,
        reply_thread_ts: str | None = None,
        trace: UsageTrace | None = None,
    ) -> tuple[str, list[dict[str, Any]] | None]:
        """メッセージ内容から Skill を自動判定して実行し、(text, blocks) を返す。

        trace を渡すと、実行した Skill の LLM コスト（total_cost_usd）を trace.cost_usd に
        書き戻す（管理画面 usage_events 用）。記録は呼び出し側ハンドラの出口で行う。

        スラッシュコマンド不要で、@メンション / DM の自然文から
        search / clientkarte / proposal_draft / tiktok_search / operation_log を振り分ける。
        曖昧なら search。戻り値の blocks が None ならテキストのみ投稿する。

        channel_id / thread_ts は operation_log がスレッド会話を取得するのに使う
        (メンションされたスレッドを CRM ログ化する)。
        reply_channel / reply_thread_ts は返信先 (video_algorithm が HTML レポートを
        その場所へファイル添付するのに使う)。
        """
        from teamagent.skills.intent import detect_skill

        intent = detect_skill(message)
        logger.info(
            "skill_auto_route",
            request_id=request_id,
            skill=intent.skill,
            client_name=intent.client_name,
            reason=intent.reason,
        )

        # 雑談/挨拶/お礼/能力質問 → 会話AI応答（検索/RAG は走らせない・1通で即答）
        if intent.skill == "chitchat":
            out = await self.run_chitchat(message, request_id, user_id)
            return out.reply, None

        # Google 連携（スラッシュコマンド未登録でも @メンション/DM で認可リンクを返す）。
        # 認可リンクは本人専用 → _PRIVATE_SKILLS により ephemeral 配信される。
        if intent.skill == "connect":
            return await self._connect_message(user_id, request_id), None

        if intent.skill == "video_approval":
            text = await self.run_video_approval(
                intent.management_no, request_id, user_id, sheet_id=intent.sheet_id
            )
            return text, None

        if intent.skill == "video_algorithm":
            text = await self.run_video_algorithm(
                intent.query,
                request_id,
                user_id,
                reply_channel=reply_channel,
                reply_thread_ts=reply_thread_ts,
            )
            return text, None

        if intent.skill == "video_analysis" and intent.video_urls:
            # 複数 URL は並行分析 + 横断まとめ (per-item 失敗は内部で握りつぶす)
            if len(intent.video_urls) > 1:
                text, _cost = await self.run_video_batch(
                    list(intent.video_urls), request_id, user_id
                )
                return text, None
            # 単一 URL: エラーマーカーをユーザー案内に変換
            try:
                video = await self.run_video(intent.video_urls[0], request_id, user_id)
            except RuntimeError as e:
                if "VIDEO_DOWNLOAD_FAILED" in str(e):
                    return (
                        "🎬 この動画を取得できませんでした。"
                        "非公開・削除済み、または容量が大きすぎる可能性があります。"
                        "別の公開動画でお試しください。",
                        None,
                    )
                if "VIDEO_URL_NOT_FETCHABLE" in str(e):
                    return (
                        "🎬 この動画は直接取得できませんでした。"
                        "公開URLか確認のうえ、別の動画でお試しください。",
                        None,
                    )
                if "GEMINI" in str(e):
                    return (
                        "🎬 動画分析は Gemini の認証設定後に有効化されます"
                        "（Vertex AI: GCP プロジェクト + GEMINI_USE_VERTEX、"
                        "または AI Studio: GEMINI_API_KEY）。",
                        None,
                    )
                raise
            header = f"*🎬 動画分析* （${video.total_cost_usd:.4f} / {video.model_id}）"
            return f"{header}\n\n{video.analysis}", None

        if intent.skill == "tiktok_search" and intent.query:
            try:
                out = await self.run_tiktok(
                    intent.query,
                    request_id,
                    user_id,
                    search_type=intent.search_type,
                )
            except RuntimeError as e:
                return _tiktok_error_reply(str(e)), None
            return _format_tiktok_response(out), None

        if intent.skill == "operation_log":
            # スレッド内でメンションされたらそのスレッドをログ化。スレッド外なら案内。
            if not (channel_id and thread_ts):
                return (
                    "🧾 営業ログ化は、ログにしたい *スレッド内* で "
                    "「@TeamAgent ログ化して」とメンションしてください。",
                    None,
                )
            oplog = await self.run_oplog(channel_id, thread_ts, request_id, user_id)
            return _format_oplog_response(oplog), None

        if intent.skill == "mail_to_internal_context":
            # client が抽出できなければ本人に指定を促す（None を Skill に渡さない）。
            if not intent.client_name:
                return (
                    "📧 どのクライアントのメールか教えてください。"
                    "例: 「@TeamAgent ○○社のメール、社内で何か話してた?」",
                    None,
                )
            # 外側ハンドラは全例外を汎用エラー化するため、未連携はここで案内に変換する。
            try:
                link = await self.run_mail_link(intent.client_name, request_id, user_id)
            except PermissionError:
                return (
                    "🔗 メール連携が必要です。`/teamagent connect` で自分の Google を"
                    "認可してください（gmail.readonly のみ・読み取り専用）。",
                    None,
                )
            if trace is not None:
                trace.cost_usd = getattr(link, "total_cost_usd", 0.0)
            return _format_mail_link_response(link), None

        if intent.skill == "mail_followup":
            if not intent.client_name:
                return (
                    "📬 どのクライアントの要返信メールか教えてください。"
                    "例: 「@TeamAgent ○○社の要返信メール教えて」",
                    None,
                )
            try:
                fu = await self.run_mail_followup(
                    intent.client_name, request_id, user_id, idle_days=intent.followup_days
                )
            except PermissionError:
                return (
                    "🔗 メール連携が必要です。`/teamagent connect` で自分の Google を"
                    "認可してください（gmail.readonly のみ・読み取り専用）。",
                    None,
                )
            return _format_mail_followup_response(fu), None

        if intent.skill == "mail_summary":
            if not intent.client_name:
                return (
                    "📨 どのクライアントのメールを要約しますか? "
                    "例: 「@TeamAgent ○○社のメール要約して」",
                    None,
                )
            try:
                summ = await self.run_mail_summary(intent.client_name, request_id, user_id)
            except PermissionError:
                return (
                    "🔗 メール連携が必要です。`/teamagent connect` で自分の Google を"
                    "認可してください。",
                    None,
                )
            if trace is not None:
                trace.cost_usd = getattr(summ, "total_cost_usd", 0.0)
            return _format_mail_summary_response(summ), None

        if intent.skill == "mail_reply":
            if not intent.client_name:
                return (
                    "✍️ どのクライアント宛の返信を作成しますか? "
                    "例: 「@TeamAgent ○○社のメールに返信作って」",
                    None,
                )
            try:
                rep = await self.run_mail_reply(intent.client_name, request_id, user_id)
            except PermissionError:
                return (
                    "🔗 下書き作成にはメールの連携（再認可）が必要です。`/teamagent connect` で"
                    "自分の Google を認可（メールの下書き作成を許可）してからお試しください。",
                    None,
                )
            if trace is not None:
                trace.cost_usd = getattr(rep, "total_cost_usd", 0.0)
            return _format_mail_reply_response(rep), None

        if intent.skill == "clientkarte" and intent.client_name:
            karte = await self.run_karte(intent.client_name, request_id, user_id)
            if trace is not None:
                trace.cost_usd = getattr(karte, "total_cost_usd", 0.0)
            header = f"*🗂️ {karte.client_name} カルテ* （FB {karte.event_count} 件）"
            return f"{header}\n\n{karte.answer}", None

        if intent.skill == "proposal_review":
            review = await self.run_review(message, request_id, user_id)
            if trace is not None:
                trace.cost_usd = review.total_cost_usd
            header = (
                f"*🔎 提案レビュー* （照合 {review.source_count} 件 / "
                f"${review.total_cost_usd:.3f}）"
            )
            return f"{header}\n\n{review.review}", None

        if intent.skill == "proposal_draft":
            draft = await self.run_draft(message, request_id, user_id)
            if trace is not None:
                trace.cost_usd = draft.total_cost_usd
            header = (
                f"*📝 提案ドラフト* （参照 {draft.source_count} 件 / ${draft.total_cost_usd:.3f}）"
            )
            return f"{header}\n\n{draft.draft}", None

        # 既定: 横断検索 (Block Kit 付き)
        output = await self.run_search(message, request_id, user_id)
        if trace is not None:
            trace.cost_usd = output.total_cost_usd
        return format_search_response(output), build_search_blocks(output)

    async def _resolve_user_email(self, user_id: str | None) -> str | None:
        """Slack user_id → email を解決する（RLS 評価用）。adapter に委譲し外部/ゲストを拒否。

        SLACK_BOT_TOKEN に users:read.email スコープが必要。失敗/外部ユーザ/ゲストは None
        （RLS 経由で fail-safe に何も見えなくなる）。返る email は normalize 済み。
        """
        if not user_id or user_id == "unknown":
            return None
        if user_id in self._user_email_cache:
            return self._user_email_cache[user_id]
        slack = SlackClient(bot_token=os.environ.get("SLACK_BOT_TOKEN", ""))
        email = await slack.resolve_user_email(user_id, request_id="-")
        self._user_email_cache[user_id] = email
        return email

    def _mail_draft_quota_ok(self, email: str, *, limit: int = 10) -> bool:
        """1 人 1 日あたりの下書き生成上限（コスト/連打対策）。worker 常駐の in-memory カウンタ。"""
        import datetime as _dt

        today = _dt.date.today().isoformat()
        counts: dict[str, tuple[str, int]] = getattr(self, "_mail_draft_counts", {})
        self._mail_draft_counts = counts
        day, n = counts.get(email, (today, 0))
        return today != day or n < limit

    def _mail_draft_quota_consume(self, email: str) -> None:
        """下書きを 1 件作成できた時だけカウントを進める（失敗時は消費しない）。"""
        import datetime as _dt

        today = _dt.date.today().isoformat()
        counts: dict[str, tuple[str, int]] = getattr(self, "_mail_draft_counts", {})
        self._mail_draft_counts = counts
        day, n = counts.get(email, (today, 0))
        counts[email] = (today, (n + 1) if today == day else 1)

    def _generate_mail_draft(self, thread_id: str, email: str, request_id: str) -> dict[str, Any]:
        """ボタン押下からの単一スレッド下書き生成（同期・asyncio.to_thread から呼ぶ）。"""
        from teamagent.skills.base import SkillContext
        from teamagent.skills.morning_digest.skill import MorningDigestSkill

        skill = MorningDigestSkill(token_store=self._get_token_store())
        ctx = SkillContext(request_id=request_id, metadata={"user_email": email})
        return skill.generate_draft_for_thread(thread_id, email, ctx)

    async def run_search(
        self,
        query: str,
        request_id: str,
        user_id: str | None,
        *,
        filter_industry_override: str | None = None,
        top_k: int = 5,
    ) -> SearchOutput:
        """SearchSkill を別スレッドで実行（同期 I/O が含まれるため）。

        SkillRouter で クエリを判定し、industry キーワードが含まれていれば
        SearchInput.filter_industry に自動付与する。

        filter_industry_override / top_k はスラッシュコマンドから明示指定する用途。
        - filter_industry_override が非 None なら Router の auto-detection より優先
        - top_k は 1〜20 の範囲にサニタイズ（呼び出し側で済んでいる前提だが二重防御）

        Slack user_id → email を解決して ctx.metadata['user_email'] に注入。
        SearchSkill 側で PgVectorClient.connection(app_role='teamagent_app',
        user_email=...) として RLS policy が評価される（documents/chunks 切替後に有効）。
        """
        decision = self._router.route(query, request_id=request_id)
        logger.info(
            "skill_router_decision",
            request_id=request_id,
            query_type=decision.query_type.value,
            confidence=decision.confidence,
            filter=decision.extracted_filter,
            reason=decision.reason,
            filter_industry_override=filter_industry_override,
            top_k=top_k,
        )

        # 明示指定が最優先、無ければ router の自動検出
        filter_industry = filter_industry_override or decision.extracted_filter.get("industry")
        # strict_industry: スラッシュコマンドで明示指定された場合のみ True。
        # Router 自動付与は soft (industry IS NULL も許容) で Slack docs を巻き込み除外しない。
        strict_industry = filter_industry_override is not None
        # 注：meta / compare は今は通常検索で代用（Sprint 2 で本格実装）
        # query_type=COMPARE/META はログ出すだけで content と同じ動作にする

        # top_k を 1〜20 にサニタイズ（API 経由の異常値防御）
        top_k_safe = max(1, min(top_k, 20))

        # RLS 評価用に Slack user_id → email を解決。会社思想「資料は全て共有物」に従い
        # build_rls_metadata が email domain を user_groups に入れる（acl_groups intersect 用）。
        # 将来 Slack User Group → group email[] 解決時は ResolvedIdentity.groups へ merge する。
        user_email = await self._resolve_user_email(user_id)
        skill = self.get_search_skill()
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata=build_rls_metadata(user_email) or no_access_metadata(),
        )
        input_obj = SearchInput(
            query=query,
            top_k=top_k_safe,
            filter_industry=filter_industry,
            strict_industry=strict_industry,
        )
        loop = asyncio.get_running_loop()
        output: SearchOutput = await loop.run_in_executor(
            None,
            skill.run,
            input_obj,
            ctx,
        )
        return output


_GMAIL_DRAFTS_URL = "https://mail.google.com/mail/u/0/#drafts"


def _swap_draft_button(
    blocks: list[dict[str, Any]], block_id: str, open_url: str
) -> list[dict[str, Any]]:
    """押下された「✏️ 下書きを作成」ボタンを「📨 作成した下書きを開く」直リンクに差し替える。

    block_id でその actions ブロックを特定し、action_id=mail_draft の要素のみ url ボタンに置換。
    「🔍 確認する」等は残す。該当が無ければ blocks をそのまま返す（fail-open）。
    """
    out: list[dict[str, Any]] = []
    for b in blocks:
        if b.get("type") == "actions" and str(b.get("block_id", "")) == block_id:
            new_el: list[dict[str, Any]] = []
            for e in b.get("elements", []):
                if e.get("action_id") == "mail_draft":
                    new_el.append(
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "📨 作成した下書きを開く",
                                "emoji": True,
                            },
                            "url": open_url,
                        }
                    )
                else:
                    new_el.append(e)
            out.append({**b, "elements": new_el})
        else:
            out.append(b)
    return out


def build_app(dispatcher: SkillDispatcher | None = None) -> AsyncApp:
    """Bolt AsyncApp を構築する。

    SLACK_BOT_TOKEN は必須。
    Socket Mode で動かすには SLACK_APP_TOKEN も必要（main() でチェック）。
    """
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("SLACK_BOT_TOKEN が未設定です")

    app = AsyncApp(token=bot_token)
    slack = SlackClient(bot_token=bot_token)
    disp = dispatcher or SkillDispatcher()
    # 入口の総量規制（同時≤concurrency・超過はFIFOキュー・キュー満杯/待ち過ぎは明示拒否）。
    # プロセスに1個を全ハンドラで共有する。重い dispatch_auto だけを gate.submit で通し、
    # ack/受付メッセージは gate の外で先に出す（Slack の3秒ackを守る）。
    _acq_timeout = _gate_env_int("REQUEST_GATE_ACQUIRE_TIMEOUT_S", 120)
    gate = RequestGate(
        concurrency=_gate_env_int("REQUEST_GATE_CONCURRENCY", 4),
        queue_max=_gate_env_int("REQUEST_GATE_QUEUE_MAX", 64),
        # キュー待ち上限秒（既定120s）。無限待ち回避＋「順番待ちが長い」通知発火。0以下で無制限。
        acquire_timeout_s=float(_acq_timeout) if _acq_timeout > 0 else None,
    )
    # 動画アップロード処理は dispatch_auto(=gate配下) の外で走るため、別枠の小さめ上限で
    # 同時本数を絞る。多人数が同時に動画を投げても DL(最大20MB×10)+Gemini が一気に並走して
    # OOM/帯域/Vertexスロットルを起こさないようにする（既定2・env で調整可）。
    video_upload_sem = asyncio.Semaphore(_gate_env_int("VIDEO_UPLOAD_CONCURRENCY", 2))
    # 管理画面テレメトリ（best-effort・DATABASE_URL 未設定なら無効化して bot は通常起動）:
    #  - recorder: 1リクエスト1行を usage_events に記録（各ハンドラ出口）。
    #  - snapshotter: 15秒ごとに GateMetrics/PoolStats を runtime_metrics に snapshot。
    recorder: UsageRecorder | None = None
    try:
        telemetry_pg = PgVectorClient.from_env()
        recorder = UsageRecorder(telemetry_pg)
        _start_snapshotter(
            MetricsSnapshotter(
                gate,
                telemetry_pg,
                interval_s=float(_gate_env_int("RUNTIME_METRICS_INTERVAL_S", 15)),
            )
        )
    except Exception:
        logger.warning("dashboard_telemetry_disabled", exc_info=True)

    async def _video_upload_reply(
        files: list[dict[str, Any]], request_id: str, user_id: str | None
    ) -> str | None:
        """添付に動画があれば DL → 分析して返信文を返す。動画が無ければ None。"""
        video_files = [f for f in files if str(f.get("mimetype", "")).startswith("video/")]
        if not video_files:
            return None
        # 重い処理（DL＋Gemini分析）だけを同時実行上限の下で実行する（プロセス全体の総量規制）。
        async with video_upload_sem:
            items: list[tuple[bytes, str]] = []
            for f in video_files[:10]:  # アップロードは最大 10 本
                file_url = f.get("url_private_download") or f.get("url_private")
                if not file_url:
                    continue
                try:
                    data = await slack.download_file(file_url, request_id=request_id, max_mb=20)
                except Exception:
                    logger.warning("slack_file_download_failed", request_id=request_id)
                    continue
                items.append((data, str(f.get("mimetype", "video/mp4"))))
            if not items:
                return "🎬 アップロード動画を取得できませんでした（容量超過 20MB の可能性）。"
            text, _cost = await disp.run_video_uploads(items, request_id, user_id)
            return text

    @app.event("app_mention")
    async def handle_app_mention(event: dict[str, Any]) -> None:
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        user_id = event.get("user", "unknown")
        raw_text = event.get("text", "")
        query = strip_mention(raw_text)
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts")

        logger.info(
            "slack_app_mention_dispatch",
            request_id=request_id,
            user_id=user_id,
            channel=channel,
            raw_len=len(raw_text),
            query_len=len(query),
        )

        # 動画ファイルの添付があれば最優先で分析 (URL もテキストも不要)
        upload_reply = await _video_upload_reply(event.get("files") or [], request_id, user_id)
        if upload_reply is not None:
            await slack.post_message(
                channel=channel,
                text=upload_reply,
                request_id=request_id,
                thread_ts=thread_ts,
            )
            return

        if not query:
            await slack.post_message(
                channel=channel,
                text="何か質問してください。例: `@TeamAgent A社の前回提案は？`",
                request_id=request_id,
                thread_ts=thread_ts,
            )
            return

        # 管理画面記録用 + 配信方法判定: skill をここで確定（queue_full/timeout でも残す）。
        from teamagent.skills.intent import detect_skill

        skill = detect_skill(query).skill
        # メール系（本人受信箱由来）は @メンション元チャンネルに出さず、本人にだけ ephemeral で
        # 返す（共有チャンネルへの情報漏えい防止・G3）。user_id 不明時は安全側で通常投稿
        # （その場合スキルは user_email 未解決で fail-closed＝機微内容は生成されない）。
        is_private = skill in _PRIVATE_SKILLS and bool(user_id) and user_id != "unknown"

        # 受付メッセージを即時投稿 (重い処理の前にユーザーへ「受け付けた」と伝える)。
        # chitchat（雑談）は build_ack_message が None を返す → ack を出さず 1 通で即答。
        # 通常投稿の場合は ts を保持し、最終結果を chat.update で**同じメッセージに書き換える**
        # （Slack タイムラインを「受付」「結果」の2通で汚さず、見た目を「考え中→結果」に統合）。
        ack = build_ack_message(query)
        ack_ts: str | None = None  # 通常投稿のみ保持（ephemeral は update 非対応）
        if ack is not None:
            if is_private:
                await slack.post_ephemeral(
                    channel=channel,
                    user=user_id,
                    text=ack,
                    request_id=request_id,
                    thread_ts=thread_ts,
                )
            else:
                ack_result = await slack.post_message(
                    channel=channel,
                    text=ack,
                    request_id=request_id,
                    thread_ts=thread_ts,
                )
                if ack_result.ok and ack_result.ts:
                    ack_ts = ack_result.ts

        trace = UsageTrace()
        t0 = time.perf_counter()
        status = "ok"
        error_code: str | None = None
        try:
            # operation_log 用: スレッド内メンションなら、その親スレッドをログ化対象にする。
            # event["thread_ts"] はスレッド内のときだけ存在 (トップレベルでは None)。
            oplog_thread = event.get("thread_ts")
            # 重い本処理だけを Gate に通す（同時≤4・超過はキュー）。ack は上で投稿済み。
            text, blocks = await gate.submit(
                disp.dispatch_auto,
                query,
                request_id,
                user_id,
                channel_id=channel if oplog_thread else None,
                thread_ts=oplog_thread,
                reply_channel=channel,
                reply_thread_ts=thread_ts,
                trace=trace,
            )
        except QueueFullError:
            status = "queue_full"
            logger.warning("request_gate_queue_full", request_id=request_id)
            await _send_or_update(
                slack,
                channel=channel,
                ack_ts=ack_ts,
                text="ただいま混雑しています。少し待って再度お試しください。🙏",
                request_id=request_id,
                thread_ts=thread_ts,
            )
        except GateTimeoutError:
            status = "timeout"
            logger.warning("request_gate_timeout", request_id=request_id)
            await _send_or_update(
                slack,
                channel=channel,
                ack_ts=ack_ts,
                text="順番待ちが長くなっています。後ほど再度お試しください。🙏",
                request_id=request_id,
                thread_ts=thread_ts,
            )
        except Exception as e:
            status = "error"
            error_code = type(e).__name__
            logger.exception("skill_dispatch_failed", request_id=request_id)
            # Sentry へ送信（DSN 未設定なら no-op）。スクラブは before_send で実施
            capture_skill_exception(
                e,
                request_id=request_id,
                skill="auto",
                user_id=user_id,
                extra={"channel": channel, "query_len": len(query)},
            )
            await _send_or_update(
                slack,
                channel=channel,
                ack_ts=ack_ts,
                text=f"処理中にエラーが発生しました。`request_id={request_id}`",
                request_id=request_id,
                thread_ts=thread_ts,
            )
        else:
            if is_private:
                # メール系の結果は本人にだけ ephemeral 配信（チャンネルに漏らさない・G3）。
                await slack.post_ephemeral(
                    channel=channel,
                    user=user_id,
                    text=text,
                    request_id=request_id,
                    thread_ts=thread_ts,
                    blocks=blocks,
                )
            else:
                await _send_or_update(
                    slack,
                    channel=channel,
                    ack_ts=ack_ts,
                    text=text,
                    request_id=request_id,
                    thread_ts=thread_ts,
                    blocks=blocks,
                )
        finally:
            # 応答投稿の後に best-effort 記録（失敗してもユーザ影響なし・本文は保存しない）。
            if recorder is not None:
                email = await disp._resolve_user_email(user_id)
                await recorder.record(
                    UsageEvent(
                        request_id=request_id,
                        skill=skill,
                        status=status,
                        user_email=email,
                        user_id=user_id,
                        cost_usd=trace.cost_usd,
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                        error_code=error_code,
                        query_chars=len(query),
                        via="mention",
                    )
                )

    @app.event("message")
    async def handle_message(event: dict[str, Any]) -> None:
        # bot 自身のメッセージは無視
        if event.get("bot_id"):
            return
        if event.get("channel_type") != "im":
            return  # DM のみ反応

        request_id = f"req-{uuid.uuid4().hex[:12]}"
        user_id = event.get("user", "unknown")
        channel = event.get("channel", "")
        text = event.get("text", "")

        logger.info(
            "slack_dm",
            request_id=request_id,
            user_id=user_id,
            channel=channel,
            text_len=len(text),
        )

        # 動画ファイルの添付があれば最優先で分析
        upload_reply = await _video_upload_reply(event.get("files") or [], request_id, user_id)
        if upload_reply is not None:
            await slack.post_message(channel=channel, text=upload_reply, request_id=request_id)
            return

        if not text:
            return

        # 受付メッセージを即時投稿。chitchat は None → ack を出さず即答。
        ack = build_ack_message(text)
        if ack is not None:
            await slack.post_message(
                channel=channel,
                text=ack,
                request_id=request_id,
            )

        from teamagent.skills.intent import detect_skill

        skill = detect_skill(text).skill
        trace = UsageTrace()
        t0 = time.perf_counter()
        status = "ok"
        error_code: str | None = None
        try:
            # 重い本処理だけを Gate に通す（同時≤4・超過はキュー）。ack は上で投稿済み。
            reply, blocks = await gate.submit(
                disp.dispatch_auto, text, request_id, user_id, reply_channel=channel, trace=trace
            )
        except QueueFullError:
            status = "queue_full"
            logger.warning("request_gate_queue_full", request_id=request_id)
            await slack.post_message(
                channel=channel,
                text="ただいま混雑しています。少し待って再度お試しください。🙏",
                request_id=request_id,
            )
        except GateTimeoutError:
            status = "timeout"
            logger.warning("request_gate_timeout", request_id=request_id)
            await slack.post_message(
                channel=channel,
                text="順番待ちが長くなっています。後ほど再度お試しください。🙏",
                request_id=request_id,
            )
        except Exception as e:
            status = "error"
            error_code = type(e).__name__
            logger.exception("skill_dispatch_failed", request_id=request_id)
            capture_skill_exception(
                e,
                request_id=request_id,
                skill="auto",
                user_id=user_id,
                extra={"channel": channel, "text_len": len(text), "via": "dm"},
            )
            await slack.post_message(
                channel=channel,
                text=f"処理中にエラーが発生しました。`request_id={request_id}`",
                request_id=request_id,
            )
        else:
            await slack.post_message(
                channel=channel,
                text=reply,
                request_id=request_id,
                blocks=blocks,
            )
        finally:
            if recorder is not None:
                email = await disp._resolve_user_email(user_id)
                await recorder.record(
                    UsageEvent(
                        request_id=request_id,
                        skill=skill,
                        status=status,
                        user_email=email,
                        user_id=user_id,
                        cost_usd=trace.cost_usd,
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                        error_code=error_code,
                        query_chars=len(text),
                        via="dm",
                    )
                )

    @app.command("/teamagent_connect")
    async def handle_teamagent_connect(
        ack: Any,
        respond: Any,
        command: dict[str, Any],
    ) -> None:
        """`/teamagent_connect`: 本人専用の Google 連携リンクを ephemeral で返す。

        ※ スラッシュコマンド未登録の Slack でも、@メンション/DM で「連携」と話しかければ
        同じ文面（dispatcher の connect 経路）が返る。同意後は connect_web の /oauth2/callback が
        token を KMS 暗号化保存する。文面生成は `SkillDispatcher._connect_message` に集約。
        """
        await ack()
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        text = await disp._connect_message(command.get("user_id"), request_id)
        await respond(response_type="ephemeral", text=text)

    @app.command("/teamagent_search")
    async def handle_teamagent_search(
        ack: Any,
        respond: Any,
        command: dict[str, Any],
    ) -> None:
        """Slack スラッシュコマンド `/teamagent_search <query> [industry=...] [top_k=N]`

        3 秒制約: ack() を先頭で即時に呼ぶ。検索本体は response_url 経由で `respond()` する。
        コマンドが何も渡されなかったら使い方を ephemeral で返す。
        エラーは ephemeral で本人にだけ通知（チャネルを汚さない）。
        """
        await ack()

        request_id = f"req-{uuid.uuid4().hex[:12]}"
        user_id = command.get("user_id")
        channel_id = command.get("channel_id", "")
        raw_text = (command.get("text") or "").strip()

        logger.info(
            "slack_slash_command",
            request_id=request_id,
            user_id=user_id,
            channel=channel_id,
            text_len=len(raw_text),
        )

        if not raw_text:
            await respond(
                response_type="ephemeral",
                text=(
                    "使い方: `/teamagent_search <自然文クエリ> [industry=飲食] [top_k=10]`\n"
                    "例: `/teamagent_search 飲食店PR事例 industry=飲食 top_k=5`"
                ),
            )
            return

        query, options = parse_command_text(raw_text)
        if not query:
            await respond(
                response_type="ephemeral",
                text=(
                    "クエリ本文が空です。`/teamagent_search 飲食店事例` のように指定してください。"
                ),
            )
            return

        filter_industry = options.get("industry")
        top_k_raw = options.get("top_k", "5")
        try:
            top_k_val = int(top_k_raw)
        except ValueError:
            top_k_val = 5

        try:
            output = await disp.run_search(
                query,
                request_id,
                user_id,
                filter_industry_override=filter_industry,
                top_k=top_k_val,
            )
        except Exception as e:
            logger.exception("slash_command_search_failed", request_id=request_id)
            capture_skill_exception(
                e,
                request_id=request_id,
                skill="search",
                user_id=user_id,
                extra={"channel": channel_id, "via": "slash"},
            )
            await respond(
                response_type="ephemeral",
                text=f"検索中にエラーが発生しました。`request_id={request_id}`",
            )
            return

        # 結果は in_channel で公開（営業同士で共有価値があるため）。
        # コマンド本人のみに見せたいなら response_type="ephemeral" に変える。
        await respond(
            response_type="in_channel",
            text=format_search_response(output),
            blocks=build_search_blocks(output),
        )

    @app.command("/teamagent_karte")
    async def handle_teamagent_karte(
        ack: Any,
        respond: Any,
        command: dict[str, Any],
    ) -> None:
        """Slack スラッシュコマンド `/teamagent_karte <クライアント名>`

        指定クライアントの提案履歴・温度感推移・次アクションを 1 枚のカルテで返す。
        ※ Slack アプリ側で `/teamagent_karte` コマンドの登録が必要。
        """
        await ack()

        request_id = f"req-{uuid.uuid4().hex[:12]}"
        user_id = command.get("user_id")
        channel_id = command.get("channel_id", "")
        client_name = (command.get("text") or "").strip()

        logger.info(
            "slack_slash_karte",
            request_id=request_id,
            user_id=user_id,
            channel=channel_id,
            text_len=len(client_name),
        )

        if not client_name:
            await respond(
                response_type="ephemeral",
                text=(
                    "使い方: `/teamagent_karte <クライアント名>`\n例: `/teamagent_karte 日本ガイシ`"
                ),
            )
            return

        try:
            output = await disp.run_karte(client_name, request_id, user_id)
        except Exception as e:
            logger.exception("slash_command_karte_failed", request_id=request_id)
            capture_skill_exception(
                e,
                request_id=request_id,
                skill="clientkarte",
                user_id=user_id,
                extra={"channel": channel_id, "via": "slash"},
            )
            await respond(
                response_type="ephemeral",
                text=f"カルテ生成中にエラーが発生しました。`request_id={request_id}`",
            )
            return

        header = f"*🗂️ {output.client_name} カルテ* （FB {output.event_count} 件）"
        await respond(
            response_type="in_channel",
            text=f"{header}\n\n{output.answer}",
        )

    @app.command("/teamagent_draft")
    async def handle_teamagent_draft(
        ack: Any,
        respond: Any,
        command: dict[str, Any],
    ) -> None:
        """Slack スラッシュコマンド `/teamagent_draft <案件ブリーフ> [industry=...]`

        新規案件ブリーフから類似の過去提案を検索し、提案ドラフト骨子を返す。
        ※ Slack アプリ側で `/teamagent_draft` コマンドの登録が必要。
        """
        await ack()

        request_id = f"req-{uuid.uuid4().hex[:12]}"
        user_id = command.get("user_id")
        channel_id = command.get("channel_id", "")
        raw_text = (command.get("text") or "").strip()

        logger.info(
            "slack_slash_draft",
            request_id=request_id,
            user_id=user_id,
            channel=channel_id,
            text_len=len(raw_text),
        )

        if not raw_text:
            await respond(
                response_type="ephemeral",
                text=(
                    "使い方: `/teamagent_draft <案件ブリーフ> [industry=飲食]`\n"
                    "例: `/teamagent_draft 飲食チェーンのTikTok集客 認知拡大 industry=飲食`"
                ),
            )
            return

        brief, options = parse_command_text(raw_text)
        industry = options.get("industry")

        try:
            output = await disp.run_draft(brief or raw_text, request_id, user_id, industry=industry)
        except Exception as e:
            logger.exception("slash_command_draft_failed", request_id=request_id)
            capture_skill_exception(
                e,
                request_id=request_id,
                skill="proposal_draft",
                user_id=user_id,
                extra={"channel": channel_id, "via": "slash"},
            )
            await respond(
                response_type="ephemeral",
                text=f"ドラフト生成中にエラーが発生しました。`request_id={request_id}`",
            )
            return

        header = (
            f"*📝 提案ドラフト* （参照 {output.source_count} 件 / ${output.total_cost_usd:.3f}）"
        )
        await respond(
            response_type="ephemeral",
            text=f"{header}\n\n{output.draft}",
        )

    @app.command("/teamagent_review")
    async def handle_teamagent_review(
        ack: Any,
        respond: Any,
        command: dict[str, Any],
    ) -> None:
        """Slack スラッシュコマンド `/teamagent_review <提案テキスト>`

        提案を過去の勝ち筋・失注理由と照合して診断する。
        ※ Slack アプリ側での `/teamagent_review` コマンド登録が必要。
        """
        await ack()

        request_id = f"req-{uuid.uuid4().hex[:12]}"
        user_id = command.get("user_id")
        channel_id = command.get("channel_id", "")
        proposal_text = (command.get("text") or "").strip()

        logger.info(
            "slack_slash_review",
            request_id=request_id,
            user_id=user_id,
            channel=channel_id,
            text_len=len(proposal_text),
        )

        if not proposal_text:
            await respond(
                response_type="ephemeral",
                text=(
                    "使い方: `/teamagent_review <提案テキスト>`\n"
                    "提案の骨子や本文を貼り付けてください。過去の勝ち筋と照合して診断します。"
                ),
            )
            return

        try:
            output = await disp.run_review(proposal_text, request_id, user_id)
        except Exception as e:
            logger.exception("slash_command_review_failed", request_id=request_id)
            capture_skill_exception(
                e, request_id=request_id, skill="proposal_review", user_id=user_id
            )
            await respond(
                response_type="ephemeral",
                text=f"レビュー中にエラーが発生しました。`request_id={request_id}`",
            )
            return

        header = (
            f"*🔎 提案レビュー* （照合 {output.source_count} 件 / ${output.total_cost_usd:.3f}）"
        )
        await respond(response_type="ephemeral", text=f"{header}\n\n{output.review}")

    @app.command("/teamagent_video")
    async def handle_teamagent_video(
        ack: Any,
        respond: Any,
        command: dict[str, Any],
    ) -> None:
        """Slack スラッシュコマンド `/teamagent_video <動画URL>`

        競合 PR 動画(YouTube/Shorts)を Gemini で構造分析する。
        ※ Slack アプリ側でのコマンド登録 + GEMINI_API_KEY の設定が必要。
        """
        await ack()

        request_id = f"req-{uuid.uuid4().hex[:12]}"
        user_id = command.get("user_id")
        channel_id = command.get("channel_id", "")
        raw_text = (command.get("text") or "").strip()

        logger.info("slack_slash_video", request_id=request_id, user_id=user_id, channel=channel_id)

        from teamagent.skills.intent import extract_video_url

        url = extract_video_url(raw_text)
        if not url:
            await respond(
                response_type="ephemeral",
                text=(
                    "使い方: `/teamagent_video <動画URL>`\n"
                    "例: `/teamagent_video https://youtube.com/shorts/xxxx`\n"
                    "（対応: YouTube / Shorts。TikTok/IG は今後対応）"
                ),
            )
            return

        try:
            output = await disp.run_video(url, request_id, user_id)
        except Exception as e:
            if isinstance(e, RuntimeError) and (
                "VIDEO_URL_NOT_FETCHABLE" in str(e) or "VIDEO_DOWNLOAD_FAILED" in str(e)
            ):
                await respond(
                    response_type="ephemeral",
                    text=(
                        "🎬 この動画を取得できませんでした。"
                        "非公開・削除済み・容量超過の可能性があります。別の動画でお試しください。"
                    ),
                )
                return
            if isinstance(e, RuntimeError) and "GEMINI" in str(e):
                await respond(
                    response_type="ephemeral",
                    text="🎬 動画分析は Gemini の認証設定後に有効化されます（Vertex AI/APIキー）。",
                )
                return
            logger.exception("slash_command_video_failed", request_id=request_id)
            capture_skill_exception(
                e, request_id=request_id, skill="video_analysis", user_id=user_id
            )
            await respond(
                response_type="ephemeral",
                text=f"動画分析中にエラーが発生しました。`request_id={request_id}`",
            )
            return

        header = f"*🎬 動画分析* （${output.total_cost_usd:.4f} / {output.model_id}）"
        await respond(response_type="in_channel", text=f"{header}\n\n{output.analysis}")

    @app.action("calendar_event")
    async def handle_calendar_event(
        ack: Any,
        body: dict[str, Any],
        action: dict[str, Any],
        respond: Any,
    ) -> None:
        """朝ダイジェストの「📅 カレンダーに登録」押下 → 本人カレンダーへ予定登録（v0.3 Task3）。

        OpenClaw と worker が同一 Slack app で Socket Mode 二重接続する構成では interaction が
        どちらへ届くか不定のため、mail_draft と対称に worker 側にもハンドラを置く（無反応
        ボタン防止・レビュー F2）。判断ロジックは CalendarEventSkill を再利用（二重実装しない）。
        """
        await ack()  # Slack の 3 秒制約：まず即 ack
        request_id = f"act-{uuid.uuid4().hex[:12]}"
        user_id = (body.get("user") or {}).get("id")
        token_value = str((action or {}).get("value") or "")
        logger.info("slack_action_calendar_event", request_id=request_id, user_id=user_id)

        email = await disp._resolve_user_email(user_id)
        if not email:
            await respond(
                response_type="ephemeral",
                text="ユーザーを特定できませんでした（社外/ゲストは対象外です）。",
            )
            return

        from teamagent.skills.base import SkillContext
        from teamagent.skills.calendar_event.schema import CalendarEventInput
        from teamagent.skills.calendar_event.skill import CalendarEventSkill

        skill = CalendarEventSkill(token_store=disp._get_token_store())
        out = await asyncio.to_thread(
            skill.run,
            CalendarEventInput(event_token=token_value),
            SkillContext(request_id=request_id, metadata={"user_email": email}),
        )
        text = out.message
        if out.event_url:
            text += f"\n<{out.event_url}|カレンダーで開く>"
        await respond(response_type="ephemeral", text=text)

    @app.action("schedule_propose")
    async def handle_schedule_propose(
        ack: Any,
        body: dict[str, Any],
        action: dict[str, Any],
        respond: Any,
    ) -> None:
        """朝ダイジェストの「🗓 日程候補を提案」押下（v0.3 Task4・calendar_event と対称）。"""
        await ack()
        request_id = f"act-{uuid.uuid4().hex[:12]}"
        user_id = (body.get("user") or {}).get("id")
        token_value = str((action or {}).get("value") or "")
        logger.info("slack_action_schedule_propose", request_id=request_id, user_id=user_id)
        try:
            await respond(
                response_type="ephemeral",
                text="🗓 空き枠を確認して下書きを作成中…数秒お待ちください。",
            )
        except Exception:
            pass

        email = await disp._resolve_user_email(user_id)
        if not email:
            await respond(
                response_type="ephemeral",
                text="ユーザーを特定できませんでした（社外/ゲストは対象外です）。",
            )
            return

        from teamagent.skills.base import SkillContext
        from teamagent.skills.schedule_propose.schema import ScheduleProposeInput
        from teamagent.skills.schedule_propose.skill import ScheduleProposeSkill

        skill = ScheduleProposeSkill(token_store=disp._get_token_store())
        out = await asyncio.to_thread(
            skill.run,
            ScheduleProposeInput(schedule_token=token_value),
            SkillContext(request_id=request_id, metadata={"user_email": email}),
        )
        text = out.message
        if out.open_url:
            text += f"\n<{out.open_url}|Gmailで開く>"
        await respond(response_type="ephemeral", text=text)

    @app.action("mail_draft")
    async def handle_mail_draft(
        ack: Any,
        body: dict[str, Any],
        action: dict[str, Any],
        client: Any,
        respond: Any,
    ) -> None:
        """朝ダイジェストの「✏️ 下書きを作成」押下 → スレッド全文を読み Reply-All 下書きを生成。

        Socket Mode 経由で worker(常駐) が受ける。生 thread_id は載らず HMAC 署名トークンを検証
        （押下者と所有者の二重照合・期限切れ拒否＝fail-closed）。3 秒以内に ack し、生成は
        別スレッドで実行、完了後にボタンを「📨 作成した下書きを開く」直リンクへ chat.update する。
        """
        await ack()  # Slack の 3 秒制約：まず即 ack
        request_id = f"act-{uuid.uuid4().hex[:12]}"
        user_id = (body.get("user") or {}).get("id")
        token_value = str((action or {}).get("value") or "")
        block_id = str((action or {}).get("block_id") or "")
        channel = (body.get("channel") or {}).get("id") or (body.get("container") or {}).get(
            "channel_id"
        )
        message = body.get("message") or {}
        ts = message.get("ts")
        logger.info("slack_action_mail_draft", request_id=request_id, user_id=user_id)
        try:
            await respond(response_type="ephemeral", text="✏️ 下書きを作成中…数秒お待ちください。")
        except Exception:
            pass

        email = await disp._resolve_user_email(user_id)
        if not email:
            await respond(
                response_type="ephemeral",
                text="ユーザーを特定できませんでした（社外/ゲストは対象外です）。",
            )
            return

        from teamagent.skills.morning_digest.draft_token import decode_draft_token

        thread_id = decode_draft_token(token_value, email)
        if not thread_id:
            await respond(
                response_type="ephemeral",
                text="このボタンは無効です（期限切れ/不正）。最新のダイジェストから操作してください。",
            )
            return
        if not disp._mail_draft_quota_ok(email):
            await respond(
                response_type="ephemeral",
                text="本日の下書き作成上限（10件/日）に達しました。明日また利用できます。",
            )
            return

        result = await asyncio.to_thread(disp._generate_mail_draft, thread_id, email, request_id)
        err = result.get("error")
        if result.get("created"):
            disp._mail_draft_quota_consume(email)
        elif result.get("already"):
            pass  # 既存下書き有り＝開くだけにする
        elif err in ("not_connected", "reauth_needed"):
            await respond(
                response_type="ephemeral",
                text=(
                    "下書き作成には Google の再連携が必要です（下書き権限）。"
                    "AiLa に『連携』と話しかけて Google を許可してください。"
                ),
            )
            return
        else:
            await respond(
                response_type="ephemeral",
                text=(
                    "下書きを作成できませんでした"
                    "（スレッドが見つからない/一斉送信/本人宛でない 等）。"
                ),
            )
            return

        # 成功（または既存）→ ボタンをその下書きへの直リンクに置換して message を更新。
        open_url = _GMAIL_DRAFTS_URL
        try:
            new_blocks = _swap_draft_button(message.get("blocks") or [], block_id, open_url)
            await client.chat_update(
                channel=channel,
                ts=ts,
                blocks=new_blocks,
                text="メールと本日の予定をお送りします。",
            )
        except Exception:
            # 更新に失敗しても、開くリンクだけは本人に返す（fail-open）。
            await respond(
                response_type="ephemeral",
                text=f"✅ 下書きを作成しました。<{open_url}|Gmail の下書きを開く>",
            )

    # Bolt のグローバルエラーハンドラ — ハンドラ外で起きた例外を Sentry に飛ばす
    @app.error
    async def handle_bolt_error(error: BaseException, body: dict[str, Any]) -> None:
        event_type = (body.get("event") or {}).get("type") or body.get("type") or "unknown"
        logger.exception(
            "bolt_global_error",
            event_type=event_type,
        )
        capture_event_exception(
            error,
            event_type=f"bolt:{event_type}",
            extra={
                "team_id": body.get("team_id"),
                "api_app_id": body.get("api_app_id"),
            },
        )

    return app


def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """asyncio Task で握りつぶされた例外を Sentry / structlog に拾う。

    Bolt / Socket Mode の long-running loop では `fire_and_forget` 的に
    タスクが落ちると default handler は warning だけ出して終わる。
    ここで明示的に Sentry に飛ばす。
    """
    exc = context.get("exception")
    message = context.get("message", "asyncio_unhandled")
    logger.error(
        "asyncio_unhandled_exception",
        message=message,
        exc_type=type(exc).__name__ if exc else None,
    )
    if isinstance(exc, BaseException):
        capture_event_exception(exc, event_type="asyncio:unhandled", extra={"message": message})


def _maybe_start_video_approval_poller(app: AsyncApp, loop: asyncio.AbstractEventLoop) -> None:
    """Phase2: シート定期監視→自動一次審査→Slack通知 の poller を起動（既定 OFF・安全）。

    `USE_VIDEO_APPROVAL_POLLING=true` かつ `VIDEO_APPROVAL_POLL_CHANNEL`/`VIDEO_APPROVAL_SHEET_ID`
    が揃ったときだけ起動。投稿のみ（シート書込はしない）。冪等性=ローカル永続+初回ベースライン。
    """
    if os.environ.get("USE_VIDEO_APPROVAL_POLLING", "false").lower() not in ("1", "true", "yes"):
        return
    channel = os.environ.get("VIDEO_APPROVAL_POLL_CHANNEL")
    sheet_id = os.environ.get("VIDEO_APPROVAL_SHEET_ID")
    if not channel or not sheet_id:
        logger.warning(
            "video_approval_poll_disabled_missing_config",
            has_channel=bool(channel),
            has_sheet=bool(sheet_id),
        )
        return
    channel_s: str = channel
    sheet_id_s: str = sheet_id

    import uuid as _uuid

    from teamagent.runtime.video_approval_poller import ProcessedStore, poll_loop
    from teamagent.skills.video_approval.sheet_orientation import OrientationExtractor

    dispatcher = SkillDispatcher()
    store = ProcessedStore(
        os.environ.get(
            "VIDEO_APPROVAL_STATE_PATH",
            # Fargate task-scoped /tmp; the container runs as non-root.
            "/tmp/teamagent/state/video_approval_processed.json",  # nosec B108
        )
    )
    interval = int(os.environ.get("VIDEO_APPROVAL_POLL_INTERVAL_SEC", "300"))

    async def _list() -> list[Any]:
        def _sync() -> list[Any]:
            ex = OrientationExtractor(
                client_name=os.environ.get("VIDEO_APPROVAL_CLIENT_NAME") or None
            )
            return ex.list_creatives(sheet_id_s, request_id="vapoll")

        return await loop.run_in_executor(None, _sync)

    async def _run_one(mgmt: str) -> str:
        return await dispatcher.run_video_approval(
            mgmt, "vapoll-" + _uuid.uuid4().hex[:8], None, sheet_id=sheet_id_s
        )

    async def _post(text: str) -> None:
        await app.client.chat_postMessage(channel=channel_s, text=text)

    loop.create_task(
        poll_loop(
            list_creatives=_list,
            run_one=_run_one,
            post=_post,
            store=store,
            interval_sec=interval,
        )
    )
    logger.info("video_approval_poll_enabled", channel=channel_s, interval_sec=interval)


def _configure_runtime_concurrency(loop: asyncio.AbstractEventLoop) -> None:
    """本番の 4 同時実行に向けて event loop の default executor を明示サイズで張り替える。

    既定の ThreadPoolExecutor は max_workers≈cpu+4（2vCPU なら ~6）と狭い。各 skill.run を
    run_in_executor(None, ...) で逃がす本実装では、4 並列＋動画内部並列で枯渇 → DB 借用待ち →
    PgVector の PoolTimeout 連鎖を招きうる。十分広い専用 executor に張り替えて吸収する
    （論理同時実行は RequestGate=4 で別途キャップ済みなので過走しない）。スレッドは遅延生成のため
    広めでも常駐コストは小さい。BLAS/torch のスレッド数は env（OMP_NUM_THREADS 等）で 1 に寄せる
    運用（ec2.overrides.env）で 4 並列 embed の CPU オーバーサブスクリプションを抑える。
    """
    workers = _gate_env_int("RUNTIME_EXECUTOR_WORKERS", 24)
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ta-skill")
    )
    logger.info("runtime_concurrency_configured", executor_workers=workers)


async def _run() -> None:
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        raise RuntimeError("SLACK_APP_TOKEN が未設定です（xapp- で始まる Socket Mode 用トークン）")

    # Sentry init は async 文脈内で実施
    # （AsyncioIntegration が起動済み event loop を取りこぼさないため）
    from teamagent.observability.sentry import init_sentry

    sentry_enabled = init_sentry()

    # asyncio Task 内の握りつぶされた例外を Sentry に拾う
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_asyncio_exception_handler)
    _configure_runtime_concurrency(
        loop
    )  # 4 同時に向け executor 幅を拡張（枯渇→PoolTimeout 連鎖防止）

    app = build_app()
    handler = AsyncSocketModeHandler(app, app_token)
    _maybe_start_video_approval_poller(app, loop)  # Phase2 poller（既定 OFF）
    logger.info("slack_bot_start", mode="socket", sentry_enabled=sentry_enabled)
    from teamagent.runtime.worker_health import run_bot_heartbeat

    heartbeat = asyncio.create_task(
        run_bot_heartbeat(socket_client=handler.client, web_client=app.client)
    )
    try:
        await handler.start_async()  # type: ignore[no-untyped-call]
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass


def main() -> None:
    """CLI エントリポイント。"""
    # 構造化ログの出力形式を確定（STRUCTLOG_FORMAT=json で CloudWatch 向け JSON）。
    from teamagent.observability.logging_config import configure_logging

    configure_logging()
    require_runtime_startup(
        (
            ("mail_action", MAIL_ACTION_MAX_TOKEN_TTL_S),
            ("report_link", REPORT_LINK_MAX_TOKEN_TTL_S),
        ),
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
