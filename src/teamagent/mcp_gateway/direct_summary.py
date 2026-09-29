"""検索上位チェックの結果を、mcp が会話へ直接投稿する（Aico に文面を組み直させない）。

フラグは USE_DIRECT_SUMMARY_POST（既定 OFF＝今と同じ）。

背景（2026-09-28 本番）: SOUL.md に「slack_summary をそのまま返す」を入れ（#456）、MCP の返却を
slack_summary などの数欄に絞っても（#462 の mcp_relay_fields）、Aico（Haiku）は文面を自分の
見出し（「勝ち筋」など）で組み直し、詳細レポートの URL を落とした
（本文に「上記リンク」とだけ残った）。
モデルへの指示では止まらないので、文面は mcp が Slack へ直接出し、Aico には「投稿済み」だけを返す。

仕組み:
- 対象（フラグ ON・allowlist 内・署名検証済み・1 対 1 DM）なら、skill の完了後に結果を
  Block Kit（``search_surface_check/slack_render.py``）で依頼元の DM へ投稿する。経路は動画分析の
  切り離しと同じ（``detached_jobs.post_to_origin_status``＝1 回の再試行）。Block Kit が描けない・
  弾かれたときは ``slack_summary`` の文字だけ（エスケープ・裸 URL の ``<URL>`` 化）で出し直す。
- 届いた（または届いた可能性がある＝タイムアウト）なら、Aico には中身の無い「投稿済み」の payload を
  返す。言い換えの材料（集計・URL）を渡さない。
- 届かなかったら、今までどおりの返却に戻す（結果が消えないことを優先する）。

env（どれも TD の env で変えられる）:
- ``USE_DIRECT_SUMMARY_POST``: 既定 OFF。
- ``DIRECT_SUMMARY_POST_ALLOWED_EMAILS``: カンマ区切り。**空なら誰にも適用しない**。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import structlog
from mcp.types import TextContent

from teamagent.mcp_gateway import detached_jobs
from teamagent.skills._shared.slack_blocks import RichMessage, render_or_none
from teamagent.skills._shared.slack_mrkdwn import markdown_bold_to_mrkdwn

logger = structlog.get_logger(__name__)

ENABLED_ENV = "USE_DIRECT_SUMMARY_POST"
ALLOWED_EMAILS_ENV = "DIRECT_SUMMARY_POST_ALLOWED_EMAILS"

# 直接投稿の対象ツール（いまは検索上位チェックだけ）。
DIRECT_TOOLS = frozenset({"search_surface_check"})

POSTED = "posted"
UNCERTAIN = "uncertain"
FAILED = "failed"

# Aico に返す文（SOUL の「slack_summary をそのまま返す」規約に乗っても短い一言で終わるようにする）。
POSTED_TEXT = "結果はこの会話に投稿しました（上のメッセージをご覧ください）。"
UNCERTAIN_TEXT = (
    "結果をこの会話に投稿しました。届いていない場合は、同じ依頼をもう一度送ってください。"
)
_AICO_NOTE = (
    "結果の本文は mcp がこの会話へ直接投稿済み。言い換え・要約・補足・表は書かず、"
    "slack_summary の一文だけを返すこと。"
)


@dataclass(frozen=True)
class DirectPolicy:
    """直接投稿を使うかどうかの決まり（env から読む）。既定は「使わない」。"""

    enabled: bool = False
    allowed_emails: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> DirectPolicy:
        return cls(
            enabled=(os.environ.get(ENABLED_ENV) or "").strip().lower() in {"1", "true", "yes"},
            allowed_emails=frozenset(
                e.strip().lower()
                for e in os.environ.get(ALLOWED_EMAILS_ENV, "").split(",")
                if e.strip()
            ),
        )


def load_policy() -> DirectPolicy:
    """呼び出しごとに env を読む（TD 差し替えだけで段階を進められる。テストの差し替え口）。"""
    return DirectPolicy.from_env()


def decide(
    policy: DirectPolicy,
    *,
    tool: str,
    verified_caller: Any,
    metadata: dict[str, Any],
) -> tuple[detached_jobs.Destination | None, str]:
    """直接投稿してよいかを決める。``(宛先, 理由)``。宛先が None なら今までどおり Aico へ返す。"""
    if tool not in DIRECT_TOOLS:
        return None, "tool"
    if not policy.enabled:
        return None, "disabled"
    if verified_caller is None:
        return None, "legacy"
    if metadata.get("identity_verified") is not True:
        return None, "unverified"
    email = metadata.get("user_email")
    # 照合するのは resolver が解決した email（_user_context の申告値ではない）。
    # 空の allowlist は全員拒否。
    if not isinstance(email, str) or email.strip().lower() not in policy.allowed_emails:
        return None, "not_allowed"
    destination = detached_jobs.destination_from_claim(verified_caller)
    if destination is None:
        return None, "no_destination"
    if not destination.is_dm:
        # 第 1 段階は 1 対 1 DM だけ（チャンネルのスレッドへの直接投稿は実機未確認）。
        return None, "not_dm"
    return destination, "ok"


def rich_message(
    data: dict[str, Any], *, skill_input: Any = None, request_id: str
) -> RichMessage | None:
    """1 段目の Block Kit 版（``search_surface_check/slack_render.py``）。

    描けない（想定外の形・描画の例外）ときは None＝今の文字だけの投稿に戻す（結果を消さない）。
    """

    def _render() -> RichMessage | None:
        from teamagent.skills.search_surface_check.schema import (
            SearchSurfaceCheckInput,
            SearchSurfaceCheckOutput,
        )
        from teamagent.skills.search_surface_check.slack_render import surface_message

        out = SearchSurfaceCheckOutput.model_validate(data)
        input = skill_input if isinstance(skill_input, SearchSurfaceCheckInput) else None
        return surface_message(out, input)

    return render_or_none(_render, request_id=request_id, kind="search_surface_check")


def deliver(
    data: dict[str, Any],
    destination: detached_jobs.Destination,
    *,
    request_id: str,
    skill_input: Any = None,
) -> str:
    """結果を依頼元の DM へ投稿する。event loop の外（thread）から呼ぶ。

    Block Kit（見出し・要点・数字の欄・1 本 1 行・文字リンク）で出す。描けない・弾かれたときは
    ``slack_summary`` の文字だけの投稿に戻す（``post_to_origin_status`` の 2 回目）。
    返り値は ``posted`` / ``uncertain``（タイムアウト＝届いた可能性あり）/ ``failed``。
    """
    summary = data.get("slack_summary")
    if not isinstance(summary, str) or not summary.strip():
        return FAILED
    rich = rich_message(data, skill_input=skill_input, request_id=request_id)
    status = detached_jobs.post_to_origin_status(
        markdown_bold_to_mrkdwn(summary), destination, request_id=request_id, rich=rich
    )
    logger.info(
        "direct_summary_posted", request_id=request_id, status=status, blocks=rich is not None
    )
    return status


def posted_response(status: str) -> list[TextContent]:
    """Aico への返却（中身の無い「投稿済み」）。集計・URL は載せない。"""
    text = UNCERTAIN_TEXT if status == UNCERTAIN else POSTED_TEXT
    payload = {
        "status": "posted",
        "delivered": status == POSTED,
        "slack_summary": text,
        "note": _AICO_NOTE,
    }
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


__all__ = [
    "ALLOWED_EMAILS_ENV",
    "DIRECT_TOOLS",
    "ENABLED_ENV",
    "FAILED",
    "POSTED",
    "POSTED_TEXT",
    "UNCERTAIN",
    "UNCERTAIN_TEXT",
    "DirectPolicy",
    "decide",
    "deliver",
    "load_policy",
    "posted_response",
    "rich_message",
]
