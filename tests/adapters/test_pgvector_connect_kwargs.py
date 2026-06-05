"""_connect_kwargs（DB接続の堅牢化 kwargs）の単体テスト（DB 非依存・純ロジック）。"""

from __future__ import annotations

import pytest

from teamagent.adapters.pgvector_client import _connect_kwargs


def test_connect_kwargs_defaults() -> None:
    """既定で connect_timeout・statement/lock/idle timeout・keepalive が入る。"""
    kw = _connect_kwargs()
    assert kw["connect_timeout"] == 5
    assert kw["keepalives"] == 1
    # サーバ側タイムアウトは libpq options 文字列で渡る
    opts = kw["options"]
    assert "statement_timeout=30000" in opts
    assert "lock_timeout=5000" in opts
    assert "idle_in_transaction_session_timeout=30000" in opts


def test_connect_kwargs_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """env でタイムアウト値を上書きできる（運用で締める/緩めるため）。"""
    monkeypatch.setenv("PG_CONNECT_TIMEOUT_S", "9")
    monkeypatch.setenv("PG_STATEMENT_TIMEOUT_MS", "12345")
    monkeypatch.setenv("PG_LOCK_TIMEOUT_MS", "777")
    kw = _connect_kwargs()
    assert kw["connect_timeout"] == 9
    assert "statement_timeout=12345" in kw["options"]
    assert "lock_timeout=777" in kw["options"]


def test_connect_kwargs_invalid_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """不正な env 値は既定にフォールバック（起動を壊さない）。"""
    monkeypatch.setenv("PG_CONNECT_TIMEOUT_S", "abc")
    assert _connect_kwargs()["connect_timeout"] == 5
