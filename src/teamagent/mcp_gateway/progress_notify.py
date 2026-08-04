"""ツール実行中の進捗メッセージを Slack に直接送信する（v0.3.1 Task7 Phase B）。

背景: OpenClaw が :eyes: で受付を示した後、重いツール（RAG 検索・動画分析等は 5〜15 秒）
の実行中は沈黙し「動いているのか？」とユーザーが不安になる。MCP gateway 側から進捗を
直接 Slack に投稿し、ツール完了後に削除する（appears-and-disappears）。

設計（絶対制約）:
  - env フラグ ENABLE_PROGRESS_NOTIFY（既定 OFF）でゲート。OFF では一切送信しない。
  - fail-open: 進捗の送信/削除失敗はツール実行を一切阻害しない（例外を投げない）。
  - 宛先: _user_context.channel_id 優先 → 無ければ slack_user_id 宛 DM（open_dm）。
    どちらも無ければスキップ（DM 依頼で channel_id が無いケースは正常系）。
  - 内部情報（ツール名・パラメータ・user_id）はメッセージに一切含めない（G3/G7）。
  - 進捗は chat.delete で消す（chat.update だと OpenClaw の最終回答と二重になる）。
  - 「重い」ツールにだけ出す（軽い/未知ツールはスキップ＝点滅回避）。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog

from teamagent.adapters.slack_client import SlackClient

logger = structlog.get_logger(__name__)

# 進捗の Slack 往復は短くタイムアウトする。Slack がハング/レート制限でも、ここで打ち切って
# 本処理（ツールの実回答）を遅延させない（fail-open だけでなく fail-fast・レビュー指摘）。
_PROGRESS_TIMEOUT_S = 2.5

# 進捗を出す価値のある「重い」ツールと、その利用者向け文言（内部メカニズムは出さない）。
# ここに無いツール（軽い/未知）は進捗を出さない＝短時間ツールでの点滅を避ける。
# ⚠️ メール/カルテ等の個人機微ツール（mail_*/clientkarte/morning_digest）は【入れない】:
# 進捗を post_message（＝チャンネル可視）で出すと、公開chで「この人がメール操作中」を数秒
# ブロードキャストしてしまい、結果を ephemeral 化して秘匿している方針と逆行する（レビュー指摘）。
_PROGRESS_MESSAGES: dict[str, str] = {
    "search": "📂 資料を検索しています…",
    "workspace_search": "📂 ワークスペースを検索しています…",
    "knowledge_deliver": "📚 ナレッジを取得しています…",
    "video_analysis": "🎬 動画を分析しています（少し時間がかかります）…",
    "video_algorithm": "🎬 動画のアルゴリズム分析中（少し時間がかかります）…",
    "tiktok_search": "🔍 TikTok を検索しています…",
    "tiktok_acquire": "🔍 TikTok の動画を取得しています…",
    "proposal_draft": "📝 提案を作成しています…",
    "proposal_deck": "📝 提案資料を作成しています…",
    "run_agent": "⏳ 調べています…",
}


class ProgressHandle:
    """送信済み進捗メッセージの参照（後で削除するため channel/ts と client を保持）。"""

    __slots__ = ("channel", "client", "ts")

    def __init__(self, client: SlackClient, channel: str, ts: str) -> None:
        self.client = client
        self.channel = channel
        self.ts = ts


def enabled() -> bool:
    """ENABLE_PROGRESS_NOTIFY=1/true/yes のときのみ進捗を送る（既定 OFF）。"""
    return os.environ.get("ENABLE_PROGRESS_NOTIFY", "").strip().lower() in {"1", "true", "yes"}


async def _resolve_channel(slack: SlackClient, raw: dict[str, Any], request_id: str) -> str | None:
    """進捗の投稿先を決める: channel_id 優先 → 無ければ slack_user_id 宛 DM を開く。"""
    channel = raw.get("channel_id")
    if isinstance(channel, str) and channel:
        return channel
    uid = raw.get("slack_user_id")
    if isinstance(uid, str) and uid:
        return await slack.open_dm(uid, request_id=request_id)
    return None


async def send_progress(
    tool: str, user_context: dict[str, Any] | None, *, request_id: str
) -> ProgressHandle | None:
    """進捗メッセージを送信し ProgressHandle を返す。fail-open（失敗時 None・例外を投げない）。"""
    if not enabled():
        return None
    msg = _PROGRESS_MESSAGES.get(tool)
    if msg is None:  # 進捗を出さないツール（軽い/未知）はスキップ
        return None
    raw = user_context or {}
    try:
        slack = SlackClient.from_env()
    except Exception as e:
        logger.warning("progress_client_failed", request_id=request_id, error=type(e).__name__)
        return None
    thread_ts = raw.get("thread_ts")
    thread_ts = thread_ts if isinstance(thread_ts, str) and thread_ts else None

    async def _do() -> ProgressHandle | None:
        channel = await _resolve_channel(slack, raw, request_id)
        if not channel:
            return None  # 宛先不明（channel_id も slack_user_id も無い）→ スキップ
        result = await slack.post_message(
            channel=channel, text=msg, request_id=request_id, thread_ts=thread_ts
        )
        ts = getattr(result, "ts", "") or ""
        if not (getattr(result, "ok", False) and ts):
            return None
        logger.info("progress_sent", request_id=request_id, tool=tool)
        return ProgressHandle(client=slack, channel=channel, ts=ts)

    try:
        # タイムアウトで打ち切り（Slack ハング時に本処理を遅延させない）。
        return await asyncio.wait_for(_do(), timeout=_PROGRESS_TIMEOUT_S)
    except Exception as e:  # TimeoutError も Exception 派生＝ここで捕捉（CancelledError は非捕捉）
        logger.warning("progress_send_failed", request_id=request_id, error=type(e).__name__)
        return None


async def clear_progress(handle: ProgressHandle | None, *, request_id: str) -> None:
    """進捗メッセージを削除する（本回答は OpenClaw が別送するため進捗は消す）。fail-open。"""
    if handle is None:
        return
    try:
        await asyncio.wait_for(
            handle.client.delete_message(
                channel=handle.channel, ts=handle.ts, request_id=request_id
            ),
            timeout=_PROGRESS_TIMEOUT_S,
        )
    except Exception as e:  # TimeoutError も Exception 派生＝ここで捕捉（CancelledError は非捕捉）
        logger.warning("progress_clear_failed", request_id=request_id, error=type(e).__name__)
