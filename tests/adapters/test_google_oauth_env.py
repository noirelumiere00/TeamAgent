"""Shared ECS OAuth secret expansion; no credential values reach logs."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

from teamagent.adapters import google_oauth_env as oauth

ENV_NAMES = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN")
PAYLOAD = {
    "client_id": "client-id",
    "client_secret": "client-secret",
    "refresh_token": "refresh-token",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GOOGLE_OAUTH_JSON", *ENV_NAMES):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(oauth, "logger", MagicMock())


def test_expand_google_oauth_json_populates_three_env_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_JSON", json.dumps(PAYLOAD))
    oauth.expand_google_oauth_json()
    assert [os.environ[name] for name in ENV_NAMES] == list(PAYLOAD.values())


def test_expand_google_oauth_json_preserves_existing_nonempty_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_JSON", json.dumps(PAYLOAD))
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "explicit-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")
    oauth.expand_google_oauth_json()
    assert os.environ["GOOGLE_CLIENT_ID"] == "explicit-id"
    assert os.environ["GOOGLE_CLIENT_SECRET"] == "client-secret"
    assert os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"] == "refresh-token"


@pytest.mark.parametrize("raw", [None, "", " "])
def test_expand_google_oauth_json_missing_is_noop(
    monkeypatch: pytest.MonkeyPatch, raw: str | None
) -> None:
    if raw is not None:
        monkeypatch.setenv("GOOGLE_OAUTH_JSON", raw)
    oauth.expand_google_oauth_json()
    assert all(name not in os.environ for name in ENV_NAMES)
    oauth.logger.warning.assert_not_called()


@pytest.mark.parametrize(
    "raw", ['{"client_secret":"do-not-log",broken', "[]", "null", '"do-not-log"']
)
def test_expand_google_oauth_json_invalid_input_is_safe(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("GOOGLE_OAUTH_JSON", raw)
    oauth.expand_google_oauth_json()
    assert all(name not in os.environ for name in ENV_NAMES)
    oauth.logger.warning.assert_called_once()
    assert "do-not-log" not in str(oauth.logger.mock_calls)


def test_expand_google_oauth_json_skips_missing_or_non_string_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "GOOGLE_OAUTH_JSON",
        json.dumps({"client_id": "client-id", "client_secret": {"invalid": "secret"}}),
    )
    oauth.expand_google_oauth_json()
    assert os.environ["GOOGLE_CLIENT_ID"] == "client-id"
    assert "GOOGLE_CLIENT_SECRET" not in os.environ
    assert "GOOGLE_OAUTH_REFRESH_TOKEN" not in os.environ
