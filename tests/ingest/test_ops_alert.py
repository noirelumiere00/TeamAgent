"""ops_alert.py のテスト — 例外分類・no-op 動作・webhook POST 動作。"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from teamagent.ingest.ops_alert import (
    AlertType,
    IngestOpsAlerter,
    classify_exception,
)


# -----------------------------------------------------------
# classify_exception
# -----------------------------------------------------------
@pytest.mark.parametrize(
    "exc, expected",
    [
        (Exception("OAuth token has been expired"), AlertType.OAUTH_EXPIRED),
        (Exception("HTTP 401 unauthorized"), AlertType.OAUTH_EXPIRED),
        (Exception("invalid_grant"), AlertType.OAUTH_EXPIRED),
        (Exception("rate_limit_exceeded"), AlertType.RATE_LIMITED),
        (Exception("HTTP 429 too_many_requests"), AlertType.RATE_LIMITED),
        (Exception("embedding model failed"), AlertType.EMBEDDING_FAILED),
        (Exception("LocalE5Embedder dimension mismatch (e5)"), AlertType.EMBEDDING_FAILED),
        (Exception("psycopg.OperationalError: connection refused"), AlertType.DB_ERROR),
        (Exception("database is unavailable"), AlertType.DB_ERROR),
        (Exception("read timeout"), AlertType.NETWORK_ERROR),
        (Exception("connection reset by peer"), AlertType.NETWORK_ERROR),
        (Exception("Some random error"), AlertType.UNKNOWN),
    ],
)
def test_classify_exception(exc: Exception, expected: AlertType) -> None:
    assert classify_exception(exc) == expected


# -----------------------------------------------------------
# IngestOpsAlerter.from_env / enabled
# -----------------------------------------------------------
def test_from_env_no_webhook_returns_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPS_SLACK_WEBHOOK_URL", raising=False)
    alerter = IngestOpsAlerter.from_env()
    assert alerter.enabled is False
    assert alerter.webhook_url is None


def test_from_env_empty_webhook_returns_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPS_SLACK_WEBHOOK_URL", "")
    alerter = IngestOpsAlerter.from_env()
    assert alerter.enabled is False


def test_from_env_with_webhook_returns_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPS_SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/X/Y/Z")
    alerter = IngestOpsAlerter.from_env()
    assert alerter.enabled is True


# -----------------------------------------------------------
# send_ingest_failure: dry_run / disabled は no-op (False)
# -----------------------------------------------------------
def test_send_skipped_when_dry_run() -> None:
    alerter = IngestOpsAlerter(webhook_url="https://hooks.slack.com/services/X/Y/Z")
    with patch("teamagent.ingest.ops_alert.httpx.post") as mock_post:
        ok = alerter.send_ingest_failure(
            kind="slack",
            exc=Exception("anything"),
            request_id="req-1",
            dry_run=True,
        )
    assert ok is False
    mock_post.assert_not_called()  # dry-run では一切 POST しない


def test_send_skipped_when_disabled() -> None:
    alerter = IngestOpsAlerter(webhook_url=None)
    with patch("teamagent.ingest.ops_alert.httpx.post") as mock_post:
        ok = alerter.send_ingest_failure(
            kind="slack",
            exc=Exception("anything"),
            request_id="req-1",
            dry_run=False,
        )
    assert ok is False
    mock_post.assert_not_called()


# -----------------------------------------------------------
# send_ingest_failure: POST が 200 で True、POST 失敗で False（例外伝播しない）
# -----------------------------------------------------------
def test_send_posts_to_webhook_and_returns_true_on_200() -> None:
    alerter = IngestOpsAlerter(webhook_url="https://hooks.slack.com/services/X/Y/Z")
    with patch("teamagent.ingest.ops_alert.httpx.post") as mock_post:
        mock_post.return_value.status_code = 200
        ok = alerter.send_ingest_failure(
            kind="gdrive",
            exc=Exception("OAuth expired"),
            request_id="req-1",
            spec_repr="folder=12345",
            dry_run=False,
        )
    assert ok is True
    assert mock_post.call_count == 1
    posted = mock_post.call_args.kwargs["json"]
    # block 構造に AlertType と request_id が含まれる
    assert any(AlertType.OAUTH_EXPIRED.value in str(b) for b in posted["blocks"])
    assert any("req-1" in str(b) for b in posted["blocks"])


def test_send_returns_false_on_post_exception() -> None:
    alerter = IngestOpsAlerter(webhook_url="https://hooks.slack.com/services/X/Y/Z")
    with patch("teamagent.ingest.ops_alert.httpx.post", side_effect=RuntimeError("net down")):
        ok = alerter.send_ingest_failure(
            kind="gdrive",
            exc=Exception("anything"),
            request_id="req-1",
            dry_run=False,
        )
    # 例外を握りつぶし False を返す（pipeline 続行を阻害しない）
    assert ok is False


def test_send_returns_false_on_post_non200() -> None:
    alerter = IngestOpsAlerter(webhook_url="https://hooks.slack.com/services/X/Y/Z")
    with patch("teamagent.ingest.ops_alert.httpx.post") as mock_post:
        mock_post.return_value.status_code = 500
        ok = alerter.send_ingest_failure(
            kind="slack",
            exc=Exception("anything"),
            request_id="req-1",
            dry_run=False,
        )
    assert ok is False
