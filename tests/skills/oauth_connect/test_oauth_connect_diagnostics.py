"""oauth_connect の失敗/警告経路に診断行（CONNECT-I02 / I03 / L01）が付くことのテスト。

正常経路（リンク発行のみ・連携済み）の案内文は従来とバイト単位で同一のまま（既存テスト
test_start_links_off_keeps_raw_urls_and_message_bytes が固定）。ここでは
- fail-closed（no_user_email）→ PermissionError に CONNECT-I02 ＋ 検証済み Slack user ID
- リンク生成失敗（url_failed / OAUTH_REDIRECT_URI 無し）→ ValueError に CONNECT-L01
- Slack リンク抑止（slack_url_suppressed）/ 生成失敗（slack_url_failed）→ message に CONNECT-L01
- Slack 再連携要（slack_rebind_needed）→ message に CONNECT-I03
- 診断行に state / URL / 素のメールが無い
を固定する。
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from teamagent.connect_diagnostics import ADMIN_FORWARD_HINT
from teamagent.skills.base import SkillContext
from teamagent.skills.oauth_connect.schema import OAuthConnectInput
from teamagent.skills.oauth_connect.skill import OAuthConnectSkill

_OAUTH_ENV = {
    "OAUTH_REDIRECT_URI": "https://connect.example.com/oauth2/callback",
    "OAUTH_STATE_SECRET": "test-state-secret-0123456789",
    "CONNECT_GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
    "CONNECT_GOOGLE_CLIENT_SECRET": "test-secret",
}
_SLACK_ENV = {
    "SLACK_OAUTH_REDIRECT_URI": "https://connect.example.com/slack/oauth/callback",
    "CONNECT_SLACK_CLIENT_ID": "123456789.987654321",
    "SLACK_OAUTH_STATE_SECRET": "test-slack-state-secret-0123456789",
}
_UID = "U0123456789"
_TEAM = "T0123456789"
_EMAIL = "taro@vectorinc.co.jp"
_DIAG_RE = re.compile(r"診断: (CONNECT-[A-Z]\d\d[a-c]?) \d{4}-\d\d-\d\d \d\d:\d\d JST (.+)$", re.M)


class _FakeStore:
    def __init__(self, connected: bool) -> None:
        self._connected = connected

    def has(self, _user_email: str) -> bool:
        return self._connected


class _FakeSlackIdentityStore:
    def __init__(self, slack_user_id: str | None) -> None:
        self._slack_user_id = slack_user_id

    def slack_user_id(self, _user_email: str) -> str | None:
        return self._slack_user_id

    def has(self, _user_email: str) -> bool:
        return self._slack_user_id is not None


def _ctx(
    user_email: str | None,
    *,
    slack_user_id: str | None = _UID,
    slack_team_id: str | None = _TEAM,
) -> SkillContext:
    meta: dict[str, Any] = {"user_email": user_email} if user_email is not None else {}
    if slack_user_id is not None:
        meta["verified_slack_user_id"] = slack_user_id
    if slack_team_id is not None:
        meta["verified_slack_team_id"] = slack_team_id
    return SkillContext(request_id="req-diag-1", user_id="U1", metadata=meta)


def _env(monkeypatch: pytest.MonkeyPatch, *envs: dict[str, str]) -> None:
    for env in envs:
        for k, v in env.items():
            monkeypatch.setenv(k, v)


def _diag(text: str) -> tuple[str, str]:
    m = _DIAG_RE.search(text)
    assert m, text
    assert ADMIN_FORWARD_HINT in text
    return m.group(1), m.group(2)


def test_fail_closed_no_user_email_is_i02_with_slack_user_id() -> None:
    skill = OAuthConnectSkill()
    with pytest.raises(PermissionError) as ei:
        skill.run(OAuthConnectInput(), _ctx(None))
    code, tail = _diag(str(ei.value))
    assert code == "CONNECT-I02"
    assert tail == f"{_UID} req-diag-1"


def test_fail_closed_without_slack_user_id_has_placeholder() -> None:
    skill = OAuthConnectSkill()
    with pytest.raises(PermissionError) as ei:
        skill.run(OAuthConnectInput(), _ctx(None, slack_user_id=None, slack_team_id=None))
    code, tail = _diag(str(ei.value))
    assert code == "CONNECT-I02"
    assert tail == "- req-diag-1"


def test_missing_redirect_uri_is_l01(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OAUTH_REDIRECT_URI", raising=False)
    skill = OAuthConnectSkill(google_store=_FakeStore(False))
    with pytest.raises(ValueError, match="OAUTH_REDIRECT_URI") as ei:
        skill.run(OAuthConnectInput(), _ctx(_EMAIL))
    code, tail = _diag(str(ei.value))
    assert code == "CONNECT-L01"
    assert tail == "t***@vectorinc.co.jp req-diag-1"
    assert "taro@" not in str(ei.value)


def test_google_url_failed_is_l01(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, _OAUTH_ENV)
    monkeypatch.delenv("CONNECT_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("CONNECT_GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    skill = OAuthConnectSkill(google_store=_FakeStore(False))
    with pytest.raises(ValueError, match="連携リンクの生成に失敗") as ei:
        skill.run(OAuthConnectInput(), _ctx(_EMAIL))
    code, tail = _diag(str(ei.value))
    assert code == "CONNECT-L01"
    assert tail == "t***@vectorinc.co.jp req-diag-1"


def test_slack_url_suppressed_is_l01_in_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, _OAUTH_ENV, _SLACK_ENV)
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(False))
    out = skill.run(OAuthConnectInput(), _ctx(_EMAIL, slack_user_id=None, slack_team_id=None))
    assert out.url is not None and out.slack_url is None
    code, tail = _diag(out.message)
    assert code == "CONNECT-L01"
    assert tail == "t***@vectorinc.co.jp req-diag-1"
    # 診断行は本文（リンク）の後ろ
    assert out.message.index(out.url) < out.message.index("診断: CONNECT-L01")


def test_slack_url_failed_is_l01_in_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, _OAUTH_ENV, _SLACK_ENV)
    monkeypatch.delenv("CONNECT_SLACK_CLIENT_ID", raising=False)  # Slack URL 生成が例外になる
    monkeypatch.delenv("SLACK_CLIENT_ID", raising=False)
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(False))
    out = skill.run(OAuthConnectInput(), _ctx(_EMAIL))
    assert out.url is not None and out.slack_url is None
    assert "Slack の連携リンクは発行できませんでした" in out.message
    assert _diag(out.message)[0] == "CONNECT-L01"


def test_slack_url_failed_with_google_connected_is_l01(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, _OAUTH_ENV, _SLACK_ENV)
    monkeypatch.delenv("CONNECT_SLACK_CLIENT_ID", raising=False)
    monkeypatch.delenv("SLACK_CLIENT_ID", raising=False)
    skill = OAuthConnectSkill(google_store=_FakeStore(True), slack_store=_FakeStore(False))
    out = skill.run(OAuthConnectInput(), _ctx(_EMAIL))
    assert out.url is None and out.slack_url is None
    assert "Google は連携済み" in out.message
    assert "Slack の連携リンクを発行できませんでした" in out.message
    assert _diag(out.message)[0] == "CONNECT-L01"


def test_slack_rebind_needed_is_i03_in_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, _OAUTH_ENV, _SLACK_ENV)
    skill = OAuthConnectSkill(
        google_store=_FakeStore(True), slack_store=_FakeSlackIdentityStore("U9999999999")
    )
    out = skill.run(OAuthConnectInput(), _ctx(_EMAIL))
    assert out.slack_url is not None
    assert "連携し直す" in out.message
    code, tail = _diag(out.message)
    assert code == "CONNECT-I03"
    assert tail == "t***@vectorinc.co.jp req-diag-1"
    assert out.message.index(out.slack_url) < out.message.index("診断: CONNECT-I03")


def test_happy_path_has_no_diag(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, _OAUTH_ENV, _SLACK_ENV)
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(False))
    out = skill.run(OAuthConnectInput(), _ctx(_EMAIL))
    assert out.url and out.slack_url
    assert "診断:" not in out.message
    both = OAuthConnectSkill(google_store=_FakeStore(True), slack_store=_FakeStore(True))
    assert "診断:" not in both.run(OAuthConnectInput(), _ctx(_EMAIL)).message


def test_diag_never_contains_state_or_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, _OAUTH_ENV, _SLACK_ENV)
    skill = OAuthConnectSkill(
        google_store=_FakeStore(True), slack_store=_FakeSlackIdentityStore("U9999999999")
    )
    out = skill.run(OAuthConnectInput(), _ctx(_EMAIL))
    assert out.slack_url is not None
    m = _DIAG_RE.search(out.message)
    assert m
    line = m.group(0)
    assert "http" not in line and "state=" not in line and "taro@" not in line
