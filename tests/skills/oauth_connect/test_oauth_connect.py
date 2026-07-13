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


def test_message_uses_markdown_links_not_bare_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """表示契約: リンクは `- [ラベル](URL)` 形式で、生 URL の裸貼り行が無い。

    OpenClaw(@openclaw/slack) はエージェント返信を markdown→mrkdwn 変換するため、
    この形式なら Slack でラベル付き装飾リンクになる（2026-07-13 実機の
    「生URLがそのまま出て怪しく見える」オンボーディング UX 問題の再発防止）。
    """
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(False))
    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))
    # 2本とも Markdown リンク（リスト項目）として含まれる。
    assert f"]({out.url})" in out.message
    assert f"]({out.slack_url})" in out.message
    assert out.message.count("- [🔗") == 2
    # 生 URL の裸貼り行（行頭 http）が存在しない。
    for line in out.message.splitlines():
        assert not line.strip().startswith("http")


def test_single_link_message_is_markdown_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """単一リンク時も Markdown リンク形式（番号なし）で出る。"""
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SLACK_OAUTH_REDIRECT_URI", raising=False)
    skill = OAuthConnectSkill(google_store=_FakeStore(False))
    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))
    assert f"- [🔗 Google を連携する（メール・カレンダー等）]({out.url})" in out.message
    assert "①" not in out.message


def test_bold_uses_double_asterisk_not_single(monkeypatch: pytest.MonkeyPatch) -> None:
    """太字は `**…**`（標準 Markdown）で書く。

    OpenClaw の markdown→mrkdwn 変換は `**x**`→`*x*`（Slack 太字）だが、`*x*` は
    emphasis として **italic** に化ける。単独 `*` の太字へ退行すると Slack で
    見た目が崩れる（強調が効かない）ため、`**…**` を使っていることを固定する。
    """
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(False))
    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))
    assert "**あなた専用**" in out.message  # 誤共有抑止の強調（太字）
    # `**…**` を全て伏せた残りに、太字を意図した単独 `*` が残っていないこと。
    without_bold = out.message.replace("**", "")
    assert "*" not in without_bold


def test_oauth_urls_are_markdown_link_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """生成される OAuth URL が `[ラベル](URL)` の宛先として安全であることを固定する。

    markdown-it のリンク宛先は空白や非バランスな `)` で早期終端する。現行の
    Google/Slack OAuth URL は全パラメータが percent-encode 済み＋state は
    base64url（`A-Za-z0-9-_=`）で、空白・`)`・`]` を含まない。将来 URL 生成が
    変わってこれらが混入すると装飾リンクが静かに壊れる（生URLより悪い）ため、
    実生成 URL に対して不変条件をテストで検知する（本番コードは fail-safe を保つ）。
    """
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(False))
    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))
    for link in (out.url, out.slack_url):
        assert link is not None
        assert " " not in link  # 空白は宛先を終端させる
        assert ")" not in link  # 非バランスな ) は宛先を終端させる
        assert "]" not in link


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
