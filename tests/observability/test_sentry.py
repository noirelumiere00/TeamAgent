"""observability/sentry.py のユニットテスト。

検証ポイント：
- DSN 未設定なら init_sentry() は False を返し副作用なし
- scrub_value がシークレット / PII を redact し、長文を truncate する
- before_send が event 全体（extra / breadcrumbs / message / exception）を再帰スクラブ
- request_id が tag に昇格される
- capture_skill_exception / capture_event_exception が初期化前は no-op
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.observability import sentry as sentry_mod
from teamagent.observability.sentry import (
    _MAX_FIELD_LEN,
    before_send,
    capture_event_exception,
    capture_skill_exception,
    init_sentry,
    is_initialized,
    scrub_value,
)


@pytest.fixture(autouse=True)
def _reset_sentry_state() -> Any:
    """各テスト前後で init フラグを戻す。"""
    sentry_mod._reset_for_tests()
    yield
    sentry_mod._reset_for_tests()


# -----------------------------------------------------------
# テストフィクスチャ — GitHub Push Protection に引っかからないよう
# シークレットっぽい文字列は **実行時に組み立てる**（リテラル直書きしない）
# -----------------------------------------------------------
_FAKE_SLACK_BOT = "xox" + "b-" + "FAKE" * 5 + "-DO-NOT-USE"
_FAKE_SLACK_APP = "xa" + "pp-" + "FAKE" * 5 + "-NOT-REAL"
_FAKE_ANTHROPIC = "sk-" + "ant-" + "FAKE" * 8  # 32 chars, > 20 required
_FAKE_AWS = "AK" + "IA" + "TESTFAKE12345678"  # AKIA + ちょうど 16 文字 → 完全マッチ
_FAKE_GOOGLE = "AI" + "za" + "Fake" + "X" * 33  # AIza + 37 文字
_FAKE_SENTRY_DSN = "https://" + "deadbeef" * 4 + "@o0.ingest.sentry.io/0"


# -----------------------------------------------------------
# init_sentry
# -----------------------------------------------------------
def test_init_sentry_no_op_when_dsn_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """DSN 未設定なら何もせず False を返す。"""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert init_sentry() is False
    assert is_initialized() is False


def test_init_sentry_no_op_when_dsn_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """DSN が空白文字列でも no-op。"""
    monkeypatch.setenv("SENTRY_DSN", "   ")
    assert init_sentry() is False


def test_init_sentry_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """既に init 済みなら True を返してスキップ。"""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    sentry_mod._INITIALIZED = True
    try:
        assert init_sentry() is True
    finally:
        sentry_mod._reset_for_tests()


# -----------------------------------------------------------
# scrub_value — シークレットマスク
# -----------------------------------------------------------
def test_scrub_redacts_slack_bot_token() -> None:
    raw = f"token={_FAKE_SLACK_BOT}"
    assert "xox" + "b-" not in scrub_value(raw)
    assert "[REDACTED_SECRET]" in scrub_value(raw)


def test_scrub_redacts_slack_app_token() -> None:
    assert "xa" + "pp-" not in scrub_value(_FAKE_SLACK_APP)


def test_scrub_redacts_anthropic_key() -> None:
    raw = f"auth: {_FAKE_ANTHROPIC}"
    assert "sk-" + "ant-" not in scrub_value(raw)


def test_scrub_redacts_aws_access_key() -> None:
    raw = f"{_FAKE_AWS} in config"
    assert "AK" + "IA" not in scrub_value(raw)


def test_scrub_redacts_google_api_key() -> None:
    out = scrub_value(_FAKE_GOOGLE)
    assert "AI" + "za" not in out


def test_scrub_redacts_private_key_block() -> None:
    raw = (
        "header\n-----BEGIN RSA PRIVATE KEY-----\nABCDEFG\nXYZ\n"
        "-----END RSA PRIVATE KEY-----\nfooter"
    )
    out = scrub_value(raw)
    assert "PRIVATE KEY" not in out
    assert "header" in out
    assert "footer" in out


# -----------------------------------------------------------
# scrub_value — PII
# -----------------------------------------------------------
def test_scrub_redacts_email() -> None:
    assert "[REDACTED_PII]" in scrub_value("contact taro@example.co.jp now")


def test_scrub_redacts_jp_phone() -> None:
    assert "[REDACTED_PII]" in scrub_value("call 03-1234-5678 today")


# -----------------------------------------------------------
# scrub_value — 構造化対応
# -----------------------------------------------------------
def test_scrub_handles_nested_dict_and_list() -> None:
    obj = {
        "user": {"email": "a@b.com", "token": _FAKE_SLACK_BOT},
        "hits": [{"text": _FAKE_AWS}],
        "safe": "hello",
    }
    out = scrub_value(obj)
    assert out["user"]["email"] == "[REDACTED_PII]"
    assert out["user"]["token"] == "[REDACTED_SECRET]"
    assert out["hits"][0]["text"] == "[REDACTED_SECRET]"
    assert out["safe"] == "hello"


def test_scrub_truncates_long_text() -> None:
    raw = "x" * (_MAX_FIELD_LEN + 500)
    out = scrub_value(raw)
    assert isinstance(out, str)
    assert len(out) < len(raw)
    assert "[TRUNCATED:" in out


def test_scrub_passes_through_non_string_primitives() -> None:
    assert scrub_value(42) == 42
    assert scrub_value(3.14) == 3.14
    assert scrub_value(True) is True
    assert scrub_value(None) is None


def test_scrub_handles_tuple() -> None:
    out = scrub_value(("safe", _FAKE_SLACK_BOT))
    assert isinstance(out, tuple)
    assert out[0] == "safe"
    assert "[REDACTED_SECRET]" in out[1]


# -----------------------------------------------------------
# before_send — Sentry イベント整形
# -----------------------------------------------------------
def test_before_send_scrubs_extra() -> None:
    event: dict[str, Any] = {
        "extra": {
            "query": f"{_FAKE_AWS} が出た",
            "request_id": "req-abc123",
        }
    }
    out = before_send(event, {})
    assert out is not None
    assert "AK" + "IA" not in out["extra"]["query"]
    # request_id は tag に昇格
    assert out["tags"]["request_id"] == "req-abc123"


def test_before_send_scrubs_breadcrumbs() -> None:
    event: dict[str, Any] = {
        "breadcrumbs": {
            "values": [
                {
                    "message": f"called with {_FAKE_SLACK_BOT}",
                    "data": {"k": _FAKE_ANTHROPIC},
                },
                {"message": "all good"},
            ]
        }
    }
    out = before_send(event, {})
    assert out is not None
    crumbs = out["breadcrumbs"]["values"]
    assert "xox" + "b-" not in crumbs[0]["message"]
    assert "sk-" + "ant-" not in crumbs[0]["data"]["k"]
    assert crumbs[1]["message"] == "all good"


def test_before_send_scrubs_message_string() -> None:
    event: dict[str, Any] = {"message": "error 03-1234-5678 detected"}
    out = before_send(event, {})
    assert out is not None
    assert "[REDACTED_PII]" in out["message"]


def test_before_send_scrubs_message_dict_formatted() -> None:
    event: dict[str, Any] = {
        "message": {"formatted": "user a@b.com failed", "message": "user %s failed"}
    }
    out = before_send(event, {})
    assert out is not None
    assert "[REDACTED_PII]" in out["message"]["formatted"]


def test_before_send_scrubs_exception_value() -> None:
    event: dict[str, Any] = {
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": f"leaked {_FAKE_SLACK_BOT} in trace",
                },
            ]
        }
    }
    out = before_send(event, {})
    assert out is not None
    assert "xox" + "b-" not in out["exception"]["values"][0]["value"]


def test_before_send_promotes_request_id_to_tag() -> None:
    event: dict[str, Any] = {"extra": {"request_id": "req-xyz"}}
    out = before_send(event, {})
    assert out is not None
    assert out["tags"]["request_id"] == "req-xyz"


def test_before_send_handles_missing_sections() -> None:
    """空のイベントでも例外を投げず通すこと。"""
    assert before_send({}, {}) is not None


# -----------------------------------------------------------
# capture_* — DSN 未設定時は no-op
# -----------------------------------------------------------
def test_capture_skill_exception_noop_when_not_initialized() -> None:
    # init していない状態でも例外を投げない
    capture_skill_exception(RuntimeError("x"), request_id="r", skill="search")


def test_capture_event_exception_noop_when_not_initialized() -> None:
    capture_event_exception(RuntimeError("x"), event_type="t")


# -----------------------------------------------------------
# 実 init（ダミー DSN + before_send で送信ブロック）
# -----------------------------------------------------------
def test_init_with_dummy_dsn_actually_initializes(monkeypatch: pytest.MonkeyPatch) -> None:
    """ダミー DSN を渡したら init は成功して is_initialized=True になる。

    送信を実際にしないため、テスト中だけ before_send を `None 返し` でモンキーパッチして
    全イベントをドロップする。テスト終了時にクライアントを明示クローズしてプロセスを綺麗に。
    """
    import sentry_sdk

    # before_send を「常に None を返す」に差し替えて Sentry サーバへの送信を完全ブロック
    monkeypatch.setattr(
        "teamagent.observability.sentry.before_send", lambda event, hint: None
    )
    monkeypatch.setenv("SENTRY_DSN", _FAKE_SENTRY_DSN)
    monkeypatch.setenv("APP_ENV", "test")

    assert init_sentry() is True
    assert is_initialized() is True

    # 実 init 後、capture 経路を呼んでも例外を投げない（before_send=None でドロップ）
    capture_skill_exception(RuntimeError("dummy"), request_id="req-test", skill="search")
    capture_event_exception(RuntimeError("dummy2"), event_type="slack:test")

    # テスト終了時にクライアントを閉じてプロセス終了時の flush 警告を回避
    client = sentry_sdk.get_client()
    if client is not None:
        client.close(timeout=0.0)
