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


class _FakeStore:
    """has() だけを持つ最小トークンストア（連携済み判定の注入用）。"""

    def __init__(self, connected: bool) -> None:
        self._connected = connected

    def has(self, _user_email: str) -> bool:
        return self._connected


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
    # Google 未連携（store 注入）→ URL 生成に進み、redirect 未設定で失敗するはず。
    skill = OAuthConnectSkill(google_store=_FakeStore(False))
    with pytest.raises(ValueError, match="OAUTH_REDIRECT_URI"):
        skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))


def test_issues_personal_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # Slack 未設定・Google 未連携 → Google のみのリンク。
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SLACK_OAUTH_REDIRECT_URI", raising=False)
    skill = OAuthConnectSkill(google_store=_FakeStore(False))
    out = skill.run(OAuthConnectInput(), _ctx("Taro@VectorInc.co.jp"))
    assert out.url is not None
    assert "accounts.google.com" in out.url
    assert out.url in out.message  # 案内文に URL を含む
    assert out.slack_url is None
    assert "あなた専用" in out.message  # 誤共有抑止の文言
    assert out.user_email_masked == "T***@VectorInc.co.jp"  # mask（lower 化はしない）


def test_issues_google_and_slack_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    # 両方未連携 → Google＋Slack の dual-link（① / ②）を返す。
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(False))
    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))
    assert out.url is not None and "accounts.google.com" in out.url
    assert out.slack_url is not None and "slack.com/oauth" in out.slack_url
    assert "① Google" in out.message
    assert "② Slack" in out.message
    assert out.url in out.message  # Google URL を含む
    assert out.slack_url in out.message  # Slack URL を含む


def test_only_slack_when_google_already_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Google 連携済み・Slack 未連携 → Slack リンクだけ（Google URL は出さない）。
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(google_store=_FakeStore(True), slack_store=_FakeStore(False))
    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))
    assert out.url is None  # Google は連携済みなので生成しない
    assert out.slack_url is not None and "slack.com/oauth" in out.slack_url
    assert "Slack を連携" in out.message
    assert out.slack_url in out.message
    assert "Google は連携済み" in out.message or "Google" in out.message
    assert "① Google" not in out.message  # 単一なので番号を振らない


def test_only_google_when_slack_already_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Slack 連携済み・Google 未連携 → Google リンクだけ。
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(True))
    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))
    assert out.url is not None and "accounts.google.com" in out.url
    assert out.slack_url is None
    assert "Google を連携" in out.message


def test_both_connected_returns_no_links(monkeypatch: pytest.MonkeyPatch) -> None:
    # 両方連携済み → リンクを出さず「連携済み」と返す。
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(google_store=_FakeStore(True), slack_store=_FakeStore(True))
    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))
    assert out.url is None
    assert out.slack_url is None
    assert "連携済み" in out.message
    assert "http" not in out.message  # URL を一切含まない


def test_conn_check_failure_is_failopen(monkeypatch: pytest.MonkeyPatch) -> None:
    # store.has() が例外 → 未連携扱い（リンクを出す）で連携フローを塞がない。
    class _BoomStore:
        def has(self, _e: str) -> bool:
            raise RuntimeError("db down")

    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(google_store=_BoomStore(), slack_store=_BoomStore())
    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))
    assert out.url is not None  # 判定不能でもリンクは出る
    assert out.slack_url is not None


def test_slack_omitted_when_redirect_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # SLACK_OAUTH_REDIRECT_URI 未設定なら slack_url=None・Google のみ（後方互換）。
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SLACK_OAUTH_REDIRECT_URI", raising=False)
    skill = OAuthConnectSkill(google_store=_FakeStore(False))
    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))
    assert out.slack_url is None
    assert out.url is not None and out.url in out.message
    assert "Google を連携" in out.message
