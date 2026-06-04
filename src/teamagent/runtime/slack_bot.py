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
from typing import Any

import structlog
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.adapters.slack_client import SlackClient
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
}
_ACK_DEFAULT = "🤖 受け付けました。処理しています…"


def build_ack_message(message: str) -> str | None:
    """受信メッセージからどの Skill が動くか判定し、受付メッセージを返す。

    本処理 (dispatch_auto) より前に Slack へ即時投稿して「受け付けた」ことを伝える。
    判定は intent.detect_skill と同じヒューリスティックなので、実際に動く Skill と一致する。
    chitchat（雑談/挨拶/お礼/能力質問）は 1 通で即答するため受付メッセージを出さない（None）。
    """
    from teamagent.skills.intent import detect_skill

    try:
        skill = detect_skill(message).skill
    except Exception:
        return _ACK_DEFAULT
    if skill == "chitchat":
        return None  # 雑談は即答（「🔎検索を受け付けました」のような不自然な ack を出さない）
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
        # 動的 instance 生成。Skill 固有の init 引数を扱うため Any 経由
        from teamagent.adapters.embeddings_client import LocalE5Embedder
        from teamagent.skills.search.skill import SearchSkill

        use_contextual = os.environ.get("USE_CONTEXTUAL", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        use_new_schema = os.environ.get("USE_NEW_SCHEMA", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        # Day 8 (2026-05-28) Phase 2: Slack 営業 FB の client_name で Drive 資料を裏で検索して
        # 「関連資料」として attach する機能。USE_FB_DRIVE_MATCH=true で有効化。
        use_fb_drive_match = os.environ.get("USE_FB_DRIVE_MATCH", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        # Day 8 (2026-05-28) Sprint 4-A: Cohere Rerank v3.5 (Bedrock 東京)。
        # USE_COHERE_RERANK=true で有効化、top_k=30 retrieve → Rerank → top-5。
        # Anthropic ベンチで失敗率 -67%、$2/1000 queries (10560 query/月で $21 想定)。
        use_cohere_rerank = os.environ.get("USE_COHERE_RERANK", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        # Day 8 (2026-05-28) Sprint 4-B: prompt v2 (insight + actionable thinking)。
        # PROMPT_VERSION=v1 / v2 / v2c / v2d で切替。
        # v2c は v2 の compact 版 (Sprint 4-D, latency 短縮目的)。
        # Day 9 (2026-05-29) 本番実機検証で v2c + max=800 が回答途中切れ
        # (stop_reason=max_tokens) を起こすと判明。eval は検索 hit rate のみ測り
        # 生成回答の完全性を測らないため見逃していた。v2d はプロンプト側で項目数と
        # 文字数 (550字以内) を絞り、800tok 内で必ず end_turn する compact 版。
        # 実機8件で全件 end_turn (output 397-535tok)、latency 12-18s、hit rate 維持。
        # この結果を受け既定を v2d に変更。
        prompt_version = os.environ.get("PROMPT_VERSION", "v2d")
        # Day 8 (2026-05-28) Sprint 4-D: max_tokens 制限で latency 短縮。
        # Day 9: max=800 維持。v2d プロンプトが上限内で完結するため truncation なし。
        try:
            summary_max_tokens = int(os.environ.get("SEARCH_MAX_TOKENS", "800"))
        except ValueError:
            summary_max_tokens = 800
        # Sprint 5: 反ハルシネーション閾値。Rerank relevance がこの値未満なら
        # 該当 hit を落とし、空なら「資料に記載がありません」と返す。
        # 既定 0.0 = OFF。gold set 実測では 0.4 で expect_zero を綺麗に分離。
        try:
            min_relevance = float(os.environ.get("SEARCH_MIN_RELEVANCE", "0.0"))
        except ValueError:
            min_relevance = 0.0
        # Sprint 5: 集約・一覧クエリモード (「BANT A の案件一覧」等をメタデータ列挙で回答)。
        use_aggregation_mode = os.environ.get("USE_AGGREGATION_MODE", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        instance = SearchSkill(
            embedder=LocalE5Embedder(),
            use_contextual=use_contextual,
            use_new_schema=use_new_schema,
            use_fb_drive_match=use_fb_drive_match,
            use_cohere_rerank=use_cohere_rerank,
            min_relevance=min_relevance,
            use_aggregation_mode=use_aggregation_mode,
            prompt_version=prompt_version,
            summary_max_tokens=summary_max_tokens,
        )
        logger.info(
            "search_skill_initialized",
            use_contextual=use_contextual,
            use_new_schema=use_new_schema,
            use_fb_drive_match=use_fb_drive_match,
            use_cohere_rerank=use_cohere_rerank,
            min_relevance=min_relevance,
            use_aggregation_mode=use_aggregation_mode,
            prompt_version=prompt_version,
            summary_max_tokens=summary_max_tokens,
        )
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
        user_groups: list[str] = []
        if user_email and "@" in user_email:
            user_groups.append(user_email.split("@", 1)[1])

        skill = self.get_karte_skill()
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata={
                "user_email": user_email,
                "user_groups": user_groups,
                "user_role": "member",
            },
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
        user_groups: list[str] = []
        if user_email and "@" in user_email:
            user_groups.append(user_email.split("@", 1)[1])

        skill = self.get_draft_skill()
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata={
                "user_email": user_email,
                "user_groups": user_groups,
                "user_role": "member",
            },
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
        user_groups: list[str] = []
        if user_email and "@" in user_email:
            user_groups.append(user_email.split("@", 1)[1])

        skill = self.get_review_skill()
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata={
                "user_email": user_email,
                "user_groups": user_groups,
                "user_role": "member",
            },
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
        ctx = SkillContext(request_id=request_id, user_id=user_id)
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
            raise

        # レポートを非公開S3に公開し署名付きURL(7日)を通知に添える（URL形式配信）
        report_url: str | None = None
        if out.report_html_path:
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
        """Slack user_id → email を解決する（RLS 評価用、users.info キャッシュ）。

        SLACK_BOT_TOKEN に users:read.email スコープが必要。
        失敗時は None を返す（RLS 経由で fail-safe に何も見えなくなる）。
        """
        if not user_id or user_id == "unknown":
            return None
        if user_id in self._user_email_cache:
            return self._user_email_cache[user_id]
        try:
            from teamagent.adapters.slack_client import SlackClient

            slack = SlackClient(bot_token=os.environ.get("SLACK_BOT_TOKEN", ""))
            # slack_sdk の users_info を直接叩く（adapter に専用 method 未実装のため暫定）
            resp = await slack._client.users_info(user=user_id)
            profile: dict[str, Any] = (resp.get("user") or {}).get("profile", {}) or {}
            email = profile.get("email")
            self._user_email_cache[user_id] = email
            logger.info(
                "slack_user_email_resolved",
                user_id=user_id,
                resolved=bool(email),
            )
            return email
        except Exception:
            logger.exception("slack_user_email_resolve_failed", user_id=user_id)
            return None

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

        # RLS 評価用に Slack user_id → email を解決
        user_email = await self._resolve_user_email(user_id)

        # 会社思想 (Day 7, 2026-05-27): 「資料は全て共有物」
        # user_email の domain を user_groups に自動注入する。
        # → documents.acl_groups に 'vectorinc.co.jp' / 'domain名' が入っていれば
        #   RLS の acl_groups intersect で workspace 全員に見せられる。
        # 将来 Slack User Group → group email[] 解決時に追加で merge する。
        user_groups: list[str] = []
        if user_email and "@" in user_email:
            user_groups.append(user_email.split("@", 1)[1])  # 'vectorinc.co.jp'

        skill = self.get_search_skill()
        ctx = SkillContext(
            request_id=request_id,
            user_id=user_id,
            metadata={
                # PgVectorClient.connection() に渡される RLS GUC
                "user_email": user_email,
                "user_groups": user_groups,
                "user_role": "member",
            },
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
    gate = RequestGate(
        concurrency=_gate_env_int("REQUEST_GATE_CONCURRENCY", 4),
        queue_max=_gate_env_int("REQUEST_GATE_QUEUE_MAX", 64),
    )
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

        # 受付メッセージを即時投稿 (重い処理の前にユーザーへ「受け付けた」と伝える)。
        # chitchat（雑談）は build_ack_message が None を返す → ack を出さず 1 通で即答。
        ack = build_ack_message(query)
        if ack is not None:
            await slack.post_message(
                channel=channel,
                text=ack,
                request_id=request_id,
                thread_ts=thread_ts,
            )

        # 管理画面記録用: skill はここで確定（dispatch を通らない queue_full/timeout でも残す）。
        from teamagent.skills.intent import detect_skill

        skill = detect_skill(query).skill
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
            await slack.post_message(
                channel=channel,
                text="ただいま混雑しています。少し待って再度お試しください。🙏",
                request_id=request_id,
                thread_ts=thread_ts,
            )
        except GateTimeoutError:
            status = "timeout"
            logger.warning("request_gate_timeout", request_id=request_id)
            await slack.post_message(
                channel=channel,
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
            await slack.post_message(
                channel=channel,
                text=f"処理中にエラーが発生しました。`request_id={request_id}`",
                request_id=request_id,
                thread_ts=thread_ts,
            )
        else:
            await slack.post_message(
                channel=channel,
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
        """`/teamagent connect`: 本人専用の Google 連携リンクを ephemeral で返す。

        営業はこのリンクを開いて自分の Google で 7サービス(readonly) を許可するだけで連携完了
        （ターミナル不要）。同意後は connect_web の /oauth2/callback が token を KMS暗号化保存する。
        """
        await ack()
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        user_id = command.get("user_id")
        email = await disp._resolve_user_email(user_id)
        if not email:
            await respond(
                response_type="ephemeral",
                text="メール取得に失敗（管理者に users:read.email スコープを確認してください）。",
            )
            return
        redirect_uri = os.environ.get("OAUTH_REDIRECT_URI", "").strip()
        if not redirect_uri:
            await respond(
                response_type="ephemeral",
                text="連携機能が未設定です（管理者: OAUTH_REDIRECT_URI を設定してください）。",
            )
            return
        try:
            from teamagent.adapters.google_oauth_flow import OAuthConsentFlow

            url, _state = OAuthConsentFlow(redirect_uri=redirect_uri).authorization_url(email)
        except Exception:
            logger.warning("teamagent_connect_url_failed", request_id=request_id, user_id=user_id)
            await respond(
                response_type="ephemeral",
                text="連携リンク生成に失敗（管理者に OAuth 系 env の設定を確認）。",
            )
            return
        logger.info("teamagent_connect_link_issued", request_id=request_id, user_id=user_id)
        await respond(
            response_type="ephemeral",
            text=(
                f"👋 *{email}* の Google を連携します（1回だけ）。\n"
                "下のリンクを開いて 7サービス(readonly) を *許可* してください:\n"
                f"{url}\n\n"
                "「✅ 連携が完了しました」が出れば成功。あとは AI に話しかけるだけです。"
            ),
        )

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
        os.environ.get("VIDEO_APPROVAL_STATE_PATH", ".local_state/video_approval_processed.json")
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

    app = build_app()
    handler = AsyncSocketModeHandler(app, app_token)
    _maybe_start_video_approval_poller(app, loop)  # Phase2 poller（既定 OFF）
    logger.info("slack_bot_start", mode="socket", sentry_enabled=sentry_enabled)
    await handler.start_async()  # type: ignore[no-untyped-call]


def main() -> None:
    """CLI エントリポイント。"""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
