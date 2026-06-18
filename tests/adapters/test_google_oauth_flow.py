"""OAuth 同意フローの state 署名テスト（CSRF対策・課金0）。

実 Google 認可は W1 後だが、CSRF の要となる state 署名/検証は stdlib のみで完結＝決定的に検証。
"""

from __future__ import annotations

import base64

import pytest

from teamagent.adapters.google_oauth_flow import (
    WORKSPACE_READONLY_SCOPES,
    connect_client_id_secret,
    make_state,
    verify_state,
)

_SECRET = b"unit-test-secret"

_CONNECT_ENV_VARS = (
    "CONNECT_GOOGLE_CLIENT_ID",
    "CONNECT_GOOGLE_CLIENT_SECRET",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
)


def test_make_verify_state_roundtrip() -> None:
    state = make_state("S-Komata@Vectorinc.co.jp ", secret=_SECRET)
    assert verify_state(state, secret=_SECRET) == "s-komata@vectorinc.co.jp"  # 正規化


def test_verify_state_rejects_wrong_secret() -> None:
    state = make_state("a@x.com", secret=_SECRET)
    assert verify_state(state, secret=b"attacker-secret") is None  # 別鍵では検証失敗


def test_verify_state_rejects_garbage() -> None:
    assert verify_state("not-valid-base64-!!!", secret=_SECRET) is None
    assert verify_state("", secret=_SECRET) is None


def test_verify_state_rejects_tampered_email() -> None:
    """email 部分を書き換えても署名が一致しない（なりすまし防止）。"""
    state = make_state("a@x.com", secret=_SECRET)
    raw = base64.urlsafe_b64decode(state.encode()).decode()
    _, sig = raw.rsplit(".", 1)
    tampered = base64.urlsafe_b64encode(f"evil@x.com.{sig}".encode()).decode()
    assert verify_state(tampered, secret=_SECRET) is None


def test_workspace_scopes_are_all_readonly() -> None:
    assert len(WORKSPACE_READONLY_SCOPES) == 7
    assert all(s.endswith(".readonly") for s in WORKSPACE_READONLY_SCOPES)


@pytest.fixture
def _clean_oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect / 共有 OAuth の env を一旦全削除してから各テストで個別に設定する。"""
    for var in _CONNECT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_connect_client_prefers_connect_specific_env(
    monkeypatch: pytest.MonkeyPatch, _clean_oauth_env: None
) -> None:
    """connect 専用(Web型)が共有 GOOGLE_*(Desktop型)より優先される＝client 不一致の根治。"""
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_ID", "web.apps.googleusercontent.com")
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_SECRET", "GOCSPX-web")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "desktop.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-desktop")
    assert connect_client_id_secret() == ("web.apps.googleusercontent.com", "GOCSPX-web")


def test_connect_client_falls_back_to_shared_google_env(
    monkeypatch: pytest.MonkeyPatch, _clean_oauth_env: None
) -> None:
    """CONNECT_* 未設定なら GOOGLE_* にフォールバック（後方互換・ローカル開発）。"""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "shared.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-shared")
    assert connect_client_id_secret() == ("shared.apps.googleusercontent.com", "GOCSPX-shared")


def test_connect_client_falls_back_per_variable(
    monkeypatch: pytest.MonkeyPatch, _clean_oauth_env: None
) -> None:
    """id/secret は変数ごとに独立してフォールバックする（本番=id平文+secretのみSM の構成に対応）。"""
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_ID", "web.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-shared")
    assert connect_client_id_secret() == ("web.apps.googleusercontent.com", "GOCSPX-shared")


def test_connect_client_none_when_unset(
    monkeypatch: pytest.MonkeyPatch, _clean_oauth_env: None
) -> None:
    """何も無ければ (None, None)＝_flow() が明示エラーを出す前提。"""
    assert connect_client_id_secret() == (None, None)
