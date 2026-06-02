"""adapters/google_auth.py のユニットテスト。

OAuth 強制 / スコープ解決 / Credentials 構築のロジックを検証する
（実際のトークンリフレッシュ＝ネットワークは発生しない）。
"""

from __future__ import annotations

import pytest

from teamagent.adapters.google_auth import (
    SHARED_OAUTH_SCOPES,
    build_oauth_credentials,
    force_oauth_enabled,
    resolve_oauth_scopes,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("no", False),
    ],
)
def test_force_oauth_enabled(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("GOOGLE_FORCE_OAUTH", value)
    assert force_oauth_enabled() is expected


def test_force_oauth_disabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_FORCE_OAUTH", raising=False)
    assert force_oauth_enabled() is False


def test_resolve_oauth_scopes_returns_preferred_without_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_OAUTH_SCOPES", raising=False)
    assert resolve_oauth_scopes(SHARED_OAUTH_SCOPES) == list(SHARED_OAUTH_SCOPES)


def test_resolve_oauth_scopes_override_comma_and_space(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GOOGLE_OAUTH_SCOPES",
        "https://www.googleapis.com/auth/drive.file, https://www.googleapis.com/auth/gmail.modify",
    )
    assert resolve_oauth_scopes(("ignored",)) == [
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/gmail.modify",
    ]


def test_resolve_oauth_scopes_blank_override_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_SCOPES", "   ")
    assert resolve_oauth_scopes(("a", "b")) == ["a", "b"]


def test_build_oauth_credentials_none_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k in ("GOOGLE_OAUTH_REFRESH_TOKEN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert build_oauth_credentials(SHARED_OAUTH_SCOPES) is None


def test_build_oauth_credentials_builds_with_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "rt-xyz")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.delenv("GOOGLE_OAUTH_SCOPES", raising=False)

    from google.oauth2.credentials import Credentials

    creds = build_oauth_credentials(SHARED_OAUTH_SCOPES)
    assert isinstance(creds, Credentials)
    assert creds.refresh_token == "rt-xyz"
    assert "https://www.googleapis.com/auth/drive.readonly" in creds.scopes
