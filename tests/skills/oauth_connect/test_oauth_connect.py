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
