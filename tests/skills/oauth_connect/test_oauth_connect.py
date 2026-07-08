"""oauth_connect Skill の単体テスト（外部 I/O 無し・env と OAuthConsentFlow を制御）。"""

from __future__ import annotations

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.oauth_connect.schema import OAuthConnectInput
from teamagent.skills.oauth_connect.skill import OAuthConnectSkill

# make_state は OAUTH_STATE_SECRET、_flow() は CONNECT_GOOGLE_CLIENT_ID/SECRET を要する。
_OAUTH_ENV = {
    "OAUTH_REDIRECT_URI": "https://connect.example.com/oauth2/callback",
    "OAUTH_STATE_SECRET": "test-state-secret-0123456789",
    "CONNECT_GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
    "CONNECT_GOOGLE_CLIENT_SECRET": "test-secret",
}

# Slack 個人連携(xoxp) の認可URL生成に要する env。
# SlackOAuthConsentFlow.authorization_url は CONNECT_SLACK_CLIENT_ID と
# SLACK_OAUTH_STATE_SECRET（make_state）を読む。
_SLACK_ENV = {
    "SLACK_OAUTH_REDIRECT_URI": "https://connect.example.com/slack/oauth/callback",
    "CONNECT_SLACK_CLIENT_ID": "123456789.987654321",
    "SLACK_OAUTH_STATE_SECRET": "test-slack-state-secret-0123456789",
}


def _ctx(user_email: str | None) -> SkillContext:
    meta = {"user_email": user_email} if user_email is not None else {}
    return SkillContext(request_id="r", user_id="U1", metadata=meta)


def test_fail_closed_when_user_email_missing() -> None:
    skill = OAuthConnectSkill()
    with pytest.raises(PermissionError, match="user_email"):
        skill.run(OAuthConnectInput(), _ctx(None))


def test_fail_closed_when_user_email_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill()
    with pytest.raises(PermissionError):
        skill.run(OAuthConnectInput(), _ctx("   "))


def test_missing_redirect_uri_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OAUTH_REDIRECT_URI", raising=False)
    skill = OAuthConnectSkill()
    with pytest.raises(ValueError, match="OAUTH_REDIRECT_URI"):
        skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))


def test_issues_personal_url(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill()
    out = skill.run(OAuthConnectInput(), _ctx("Taro@VectorInc.co.jp"))
    assert "accounts.google.com" in out.url
    assert out.url in out.message  # 案内文に URL を含む
    assert "あなた専用" in out.message  # 誤共有抑止の文言
    assert out.user_email_masked == "T***@VectorInc.co.jp"  # mask（lower 化はしない）


def test_issues_google_and_slack_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    # SLACK_OAUTH_REDIRECT_URI 設定時は Google＋Slack の dual-link を返す。
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill()
    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))
    assert "accounts.google.com" in out.url
    assert out.slack_url is not None
    assert "slack.com/oauth" in out.slack_url
    assert "① Google" in out.message
    assert "② Slack" in out.message
    assert out.url in out.message  # Google URL を含む
    assert out.slack_url in out.message  # Slack URL を含む


def test_slack_omitted_when_redirect_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # SLACK_OAUTH_REDIRECT_URI 未設定なら slack_url=None・Google のみ（後方互換）。
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SLACK_OAUTH_REDIRECT_URI", raising=False)
    skill = OAuthConnectSkill()
    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))
    assert out.slack_url is None
    assert "① Google" in out.message
    assert "Slack 連携は現在未設定" in out.message
