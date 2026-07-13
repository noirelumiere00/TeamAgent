"""Ingest 失敗を #ops Slack channel に webhook で通知する最小ヘルパ。

設計方針:
- ingest pipeline は同期処理（_run_kind の except で呼ばれる）ため、本ヘルパも同期 API。
- webhook URL が未設定なら **完全 no-op**（dev/test/手動実行で誤って通知しないため）。
- dry_run=True なら **常に no-op**（DB 書き込みを伴わない検証実行で外部通知しない）。
- 通知自体の失敗は logger.warning だけで握りつぶす（リトライしない＝重複通知ループ回避）。
  失敗の事実は journalctl / structlog から後追い可能。
- 例外型/メッセージから粗く分類（OAuth/RateLimit/Embedding/DB/Network/Unknown）して
  Slack block の Type に乗せる＝対応者が当たり判定をしやすい。

Usage:
    alerter = IngestOpsAlerter.from_env()           # OPS_SLACK_WEBHOOK_URL を見る
    alerter.send_ingest_failure(
        kind="gdrive", exc=err, request_id="req-1",
        spec_repr="folder=12345", dry_run=False,
    )
"""

from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


class AlertType(StrEnum):
    OAUTH_EXPIRED = "oauth_expired"
    RATE_LIMITED = "rate_limited"
    EMBEDDING_FAILED = "embedding_failed"
    DB_ERROR = "db_error"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"


def classify_exception(exc: BaseException) -> AlertType:
    """例外型＆メッセージから AlertType を推測（ML 不要・確実な単語マッチのみ）.

    返り値の優先順位は AlertType の定義順（OAuth→Rate→Embedding→DB→Network→Unknown）。
    """
    exc_name = type(exc).__name__
    exc_str = str(exc).lower()

    if (
        "oauth" in exc_str
        or "unauthorized" in exc_str
        or "invalid_grant" in exc_str
        or "token has been expired" in exc_str
        or "401" in exc_str
    ):
        return AlertType.OAUTH_EXPIRED
    if (
        "rate_limit" in exc_str
        or "rate limit" in exc_str
        or "too_many_requests" in exc_str
        or "429" in exc_str
        or exc_name == "RateLimitError"
    ):
        return AlertType.RATE_LIMITED
    if "embed" in exc_str or "e5" in exc_str:
        return AlertType.EMBEDDING_FAILED
    if (
        "database" in exc_str
        or "psycopg" in exc_str
        or exc_name in ("DataError", "IntegrityError", "OperationalError")
    ):
        return AlertType.DB_ERROR
    if (
        "timeout" in exc_str
        or "connection" in exc_str
        or exc_name in ("ConnectError", "TimeoutException", "ConnectionError")
    ):
        return AlertType.NETWORK_ERROR
    return AlertType.UNKNOWN


@dataclass(frozen=True)
class IngestOpsAlerter:
    """Slack Incoming Webhook で #ops に ingest 失敗を通知する。"""

    webhook_url: str | None
    timeout_s: float = 5.0

    @classmethod
    def from_env(cls) -> IngestOpsAlerter:
        """OPS_SLACK_WEBHOOK_URL を読んで初期化（未設定なら no-op alerter）."""
        return cls(webhook_url=os.environ.get("OPS_SLACK_WEBHOOK_URL") or None)

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def send_ingest_failure(
        self,
        *,
        kind: str,
        exc: BaseException,
        request_id: str,
        spec_repr: str = "",
        dry_run: bool = False,
    ) -> bool:
        """1 source 失敗を webhook 経由で通知。送信成功なら True、未送信/失敗なら False.

        dry_run=True / webhook 未設定 / 通知 POST 失敗 のいずれも False を返すが、
        いずれもログ出力のみで例外は伝播させない（pipeline 続行を阻害しない）。
        """
        if dry_run:
            logger.info(
                "ops_alert_skipped_dry_run",
                request_id=request_id,
                kind=kind,
            )
            return False
        if not self.enabled:
            logger.info(
                "ops_alert_skipped_disabled",
                request_id=request_id,
                kind=kind,
                reason="OPS_SLACK_WEBHOOK_URL not set",
            )
            return False

        alert_type = classify_exception(exc)
        error_msg = f"{type(exc).__name__}: {str(exc)[:500]}"
        timestamp = _dt.datetime.now(_dt.UTC).isoformat()
        title = f":rotating_light: Ingest {kind} failed: {alert_type.value}"

        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": title}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Kind*\n{kind}"},
                    {"type": "mrkdwn", "text": f"*Type*\n{alert_type.value}"},
                    {"type": "mrkdwn", "text": f"*Request ID*\n`{request_id}`"},
                    {"type": "mrkdwn", "text": f"*Time*\n{timestamp}"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Error*\n```{error_msg}```"},
            },
        ]
        if spec_repr:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"*Spec:* `{spec_repr[:200]}`"}],
                }
            )

        try:
            resp = httpx.post(
                str(self.webhook_url),
                json={"blocks": blocks, "text": title},
                timeout=self.timeout_s,
            )
            ok = resp.status_code == 200
            logger.info(
                "ops_alert_sent",
                request_id=request_id,
                kind=kind,
                alert_type=alert_type.value,
                status=resp.status_code,
                ok=ok,
            )
            return ok
        except Exception:
            logger.warning(
                "ops_alert_send_failed",
                request_id=request_id,
                kind=kind,
                alert_type=alert_type.value,
                exc_info=True,
            )
            return False

    def send_freshness_warning(
        self,
        *,
        stale: list[Any],
        request_id: str,
        dry_run: bool = False,
    ) -> bool:
        """取り込み鮮度の警告を webhook 通知（stale=StaleSource のリスト）。

        「ある source_type の取り込みが N 日以上遅れている（or 1件も無い）」を検知した
        ときに ops へ出す。dry_run / webhook 未設定 / stale 空 なら no-op（False）。
        通知失敗は握りつぶす（ingest 続行を阻害しない）。
        """
        if dry_run or not self.enabled or not stale:
            return False
        timestamp = _dt.datetime.now(_dt.UTC).isoformat()
        srcs = ", ".join(getattr(s, "source_type", "?") for s in stale)
        title = f":warning: Ingest freshness alert: {srcs} が古い/未取り込み"
        lines = "\n".join(
            f"• *{getattr(s, 'source_type', '?')}*: {getattr(s, 'reason', '')}" for s in stale
        )
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": ":warning: 取り込み鮮度アラート"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{lines}\n\n共有された資料が検索に載っていない可能性があります。"
                        f"ingest の稼働状況（EventBridge ルール / 手動 run のソース指定）を確認してください。"
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"*Request ID* `{request_id}`  *Time* {timestamp}"}
                ],
            },
        ]
        try:
            resp = httpx.post(
                str(self.webhook_url),
                json={"blocks": blocks, "text": title},
                timeout=self.timeout_s,
            )
            ok = resp.status_code == 200
            logger.info(
                "ops_freshness_alert_sent",
                request_id=request_id,
                stale_count=len(stale),
                status=resp.status_code,
                ok=ok,
            )
            return ok
        except Exception:
            logger.warning("ops_freshness_alert_failed", request_id=request_id, exc_info=True)
            return False


__all__ = ["AlertType", "IngestOpsAlerter", "classify_exception"]
