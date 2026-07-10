"""MCP 返却ペイロードの長文退避（v0.3 Task 8）— 純関数群＋S3退避。

dispatch_tool の返却直前に適用する。ペイロード全体が閾値を超えたら:
  1. 全文 JSON を非公開 S3 へ退避（署名付き URL・7日）
  2. 構造は保ったまま長い文字列フィールドだけを切り詰め（引用・出典キーは保持）
  3. トップレベルに offloaded/full_url/offload_note を付与
L0 は「切り詰め済みの構造化結果＋全文 URL」を受け取る＝Slack の長文制限を構造的に回避
しつつ、hits の引用等の機能を殺さない（丸ごと URL 化は機能退行＝監査指摘）。

安全設計:
  - **allowlist 方式**: 退避対象は会社共有ナレッジ系 tool のみ（下記 OFFLOAD_TOOLS）。
    per-user PII を返す tool（mail_* / morning_digest 等）は署名 URL が RLS/本人限定
    配信をバイパスする漏洩経路になるため **対象外**（denylist だと新 tool の追加漏れが
    事故になる。allowlist なら新 tool は明示追加まで「退避されない」だけ＝fail-safe）
  - fail-open: S3 退避失敗時は切り詰めもせず原文を返す（機能を止めない。
    その場合の Slack 側制限は OC の分割/要約に委ねる＝従来挙動）
  - URL 系フィールド（*_url/permalink/uri）は切り詰めない（リンク破壊防止）
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# 退避対象 tool（会社共有ナレッジのみ・per-user PII 系は絶対に足さないこと）。
OFFLOAD_TOOLS: frozenset[str] = frozenset(
    {
        "search",
        "clientkarte",
        "knowledge_deliver",
        "proposal_draft",
        "proposal_review",
        "tiktok_search",
        "video_analysis",
        "video_algorithm",
    }
)

# ペイロード全体（JSON 文字列長）がこれを超えたら退避＋切り詰めを発動。
_DEFAULT_MAX_CHARS = 10_000
# 切り詰め後の各文字列フィールド上限（answer 等の要約系はこの5倍まで許容）。
_DEFAULT_FIELD_CHARS = 500
_SUMMARY_KEYS = frozenset({"answer", "summary", "message", "note"})
_TRUNC_MARK = "…〔省略・全文は offload URL へ〕"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def enabled() -> bool:
    """USE_PAYLOAD_OFFLOAD=1 のときのみ発動（既定 OFF・§10 E1-2）。"""
    return os.environ.get("USE_PAYLOAD_OFFLOAD", "").strip().lower() in {"1", "true", "yes"}


def _truncate_strings(node: Any, field_chars: int) -> Any:
    """構造を保って長い文字列だけ切り詰める（dict/list を再帰・URL キーは温存）。"""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            key = str(k)
            if isinstance(v, str):
                lowered = key.lower()
                if lowered.endswith(("url", "uri", "permalink", "link")):
                    out[key] = v  # リンクは切らない
                    continue
                cap = field_chars * 5 if lowered in _SUMMARY_KEYS else field_chars
                out[key] = v if len(v) <= cap else v[:cap] + _TRUNC_MARK
            else:
                out[key] = _truncate_strings(v, field_chars)
        return out
    if isinstance(node, list):
        return [_truncate_strings(v, field_chars) for v in node]
    return node


def maybe_offload(tool: str, data: dict[str, Any], *, request_id: str) -> dict[str, Any]:
    """必要なら長文ペイロードを S3 退避し、切り詰め済み dict を返す（それ以外は原文のまま）。

    dispatch_tool 専用。呼び出し順はミドルウェア規約どおり
    「(usage記録) → **offload** → リンク注入」（注入キーを切り詰め対象にしないため注入は後）。
    """
    if not enabled() or tool not in OFFLOAD_TOOLS:
        return data
    try:
        raw = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        return data
    max_chars = _env_int("PAYLOAD_OFFLOAD_MAX_CHARS", _DEFAULT_MAX_CHARS)
    if len(raw) <= max_chars:
        return data

    from teamagent.adapters.report_publish import publish_text

    url = publish_text(
        raw,
        prefix=os.environ.get("PAYLOAD_OFFLOAD_PREFIX") or "payload-offload/",
        request_id=request_id,
    )
    if not url:
        # fail-open: 退避できないなら切り詰めもしない（引用全損より従来挙動を選ぶ）。
        logger.warning("payload_offload_failed", request_id=request_id, tool=tool)
        return data
    field_chars = _env_int("PAYLOAD_OFFLOAD_FIELD_CHARS", _DEFAULT_FIELD_CHARS)
    trimmed = _truncate_strings(data, field_chars)
    trimmed["offloaded"] = True
    trimmed["full_url"] = url
    trimmed["offload_note"] = (
        "本文が長いため全文を退避しました（リンクは7日間有効・社外共有不可）。"
        "以下の内容は要点のみの切り詰め版です。"
    )
    logger.info(
        "payload_offloaded",
        request_id=request_id,
        tool=tool,
        original_chars=len(raw),
    )
    return trimmed  # type: ignore[no-any-return]


__all__ = ["OFFLOAD_TOOLS", "enabled", "maybe_offload"]
