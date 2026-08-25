#!/usr/bin/env python3
"""Fargate Scheduled Task: Aico 主要経路の合成カナリア（柱3・2026-06-22 事故対策）。

「壊れても自動で気づかない」を潰す。1時間ごとに主要経路を合成的に叩き、壊れていたら
構造化ログ `canary_health_result overall=false` ＋ exit 非0 を出す（CloudWatch metric filter→
alarm→SNS で約1時間以内に自動通知＝ユーザー申告を待たない）。

主検査 = **identity 解決（Slack user_id → 本人 email）**。これは per-user 機能（連携/メール/
朝ダイジェスト）すべての前提となる単一共有依存で、bot token 失効・Slack API 変更・rate limit
で壊れると**全機能が無音で死ぬ**。最も軽量（Slack API 1 コール・embedder 不要）かつ高価値。
将来 oauth_connect URL 生成・search 疎通も足せる枠組み（evaluate_canary に集約）。

書込みは一切しない（read-only な resolve のみ）。SLACK_BOT_TOKEN が要る。
"""

from __future__ import annotations

import asyncio
import os
import sys

import structlog

logger = structlog.get_logger(__name__)

# 既定カナリア対象＝管理者（必ず resolve できる社内ユーザー）。env で差し替え可。
_CANARY_USER_DEFAULT = "U09CX1CCBLN"


def evaluate_canary(results: dict[str, bool]) -> bool:
    """全チェック合格なら True（空 dict は False＝何も検査できていない＝異常）。純関数。"""
    return bool(results) and all(results.values())


async def _check_identity_resolve(slack_user_id: str) -> bool:
    """Slack user_id → 本人 email を本番と同じ resolver で解決できるか。"""
    from teamagent.mcp_gateway.server import build_slack_identity_resolver

    resolver = build_slack_identity_resolver()
    if resolver is None:
        logger.warning("canary_resolver_unavailable", reason="no_slack_bot_token")
        return False
    try:
        identity = await resolver(slack_user_id)
    except Exception as exc:
        logger.warning("canary_identity_resolve_exception", error=type(exc).__name__)
        return False
    email = getattr(identity, "email", None) if identity is not None else None
    return bool(email)


async def _run() -> dict[str, bool]:
    """主要経路を合成的に叩いて check 名→合否の dict を返す（I/O 部・タスク内でのみ実行）。"""
    uid = (
        os.environ.get("CANARY_SLACK_USER_ID", _CANARY_USER_DEFAULT).strip() or _CANARY_USER_DEFAULT
    )
    results: dict[str, bool] = {}
    results["identity_resolve"] = await _check_identity_resolve(uid)
    return results


def main() -> int:
    results = asyncio.run(_run())
    overall = evaluate_canary(results)
    logger.info(
        "canary_health_result",
        overall=overall,
        **{f"check_{name}": ok for name, ok in results.items()},
    )
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
