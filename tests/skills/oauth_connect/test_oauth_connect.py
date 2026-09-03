"""oauth_connect Skill の単体テスト（外部 I/O 無し・env と OAuthConsentFlow を制御）。"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from teamagent.adapters.slack_oauth_flow import expected_bind_tag, verify_state_detailed
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
_VERIFIED_SLACK_USER_ID = "U0123456789"
_VERIFIED_SLACK_TEAM_ID = "T0123456789"


class _FakeStore:
    """has() だけを持つ最小トークンストア（連携済み判定の注入用）。"""

    def __init__(self, connected: bool) -> None:
        self._connected = connected

    def has(self, _user_email: str) -> bool:
        return self._connected


class _FakeSlackIdentityStore:
    """保存済み uid を平文 token の復号なしで返す SlackTokenStore テストダブル。"""

    def __init__(self, slack_user_id: str | None) -> None:
        self._slack_user_id = slack_user_id

    def slack_user_id(self, _user_email: str) -> str | None:
        return self._slack_user_id

    def has(self, _user_email: str) -> bool:
        return self._slack_user_id is not None


def _ctx(
    user_email: str | None,
    *,
    slack_user_id: str | None = _VERIFIED_SLACK_USER_ID,
    slack_team_id: str | None = _VERIFIED_SLACK_TEAM_ID,
) -> SkillContext:
    meta = {"user_email": user_email} if user_email is not None else {}
    if slack_user_id is not None:
        meta["verified_slack_user_id"] = slack_user_id
    if slack_team_id is not None:
        meta["verified_slack_team_id"] = slack_team_id
    return SkillContext(request_id="r", user_id="U1", metadata=meta)


def _slack_state(url: str) -> str:
    values = parse_qs(urlparse(url).query).get("state")
    assert values and len(values) == 1
    return values[0]


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


def test_no_slack_link_when_verified_uid_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LEGACY metadata では Slack state を無束縛で発行せず、Google と代替導線は残す。"""
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(False))

    out = skill.run(
        OAuthConnectInput(),
        _ctx("taro@vectorinc.co.jp", slack_user_id=None, slack_team_id=None),
    )

    assert out.url is not None and out.url in out.message
    assert out.slack_url is None
    assert "Slack で Aico に『連携』と話しかけてください" in out.message


def test_no_slack_link_when_verified_team_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """検証済み team ID が欠けても Slack URL は fail-closed、Google は維持する。"""
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(False))

    out = skill.run(
        OAuthConnectInput(),
        _ctx("taro@vectorinc.co.jp", slack_team_id=None),
    )

    assert out.url is not None and out.url in out.message
    assert out.slack_url is None
    assert "Slack で Aico に『連携』と話しかけてください" in out.message


def test_connected_user_gets_no_link_when_uid_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存済み uid が検証済み caller と一致すれば従来どおり連携済み扱い。"""
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(
        google_store=_FakeStore(True),
        slack_store=_FakeSlackIdentityStore(_VERIFIED_SLACK_USER_ID),
    )

    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))

    assert out.url is None
    assert out.slack_url is None
    assert "連携済み" in out.message


def test_mismatched_uid_user_gets_a_bound_relink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """誤紐付け行は連携済みで隠さず、現在の検証済み caller に束縛した再連携 URL を出す。"""
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(
        google_store=_FakeStore(True),
        slack_store=_FakeSlackIdentityStore("U9999999999"),
    )

    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))

    assert out.url is None
    assert out.slack_url is not None
    assert "連携し直す" in out.message
    detailed = verify_state_detailed(_slack_state(out.slack_url))
    assert detailed is not None
    assert detailed.bind_tag == expected_bind_tag(
        _VERIFIED_SLACK_TEAM_ID,
        _VERIFIED_SLACK_USER_ID,
    )


def test_empty_stored_uid_user_gets_a_bound_relink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNIQUE/本人照合の壁の外にいる空 uid の既存行も再連携対象にする。"""
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(
        google_store=_FakeStore(True),
        slack_store=_FakeSlackIdentityStore(""),
    )

    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))

    assert out.slack_url is not None
    assert "連携し直す" in out.message
    detailed = verify_state_detailed(_slack_state(out.slack_url))
    assert detailed is not None
    assert detailed.bind_tag == expected_bind_tag(
        _VERIFIED_SLACK_TEAM_ID,
        _VERIFIED_SLACK_USER_ID,
    )


def test_llm_supplied_uid_is_ignored_in_favor_of_verified_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入力 extra に偽 uid を積まれても state は metadata の検証済み uid に束縛される。"""
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    attacker_uid = "U9999999999"
    forged_input = OAuthConnectInput.model_validate({"slack_user_id": attacker_uid})
    assert "slack_user_id" not in OAuthConnectInput.model_json_schema()["properties"]
    assert "slack_team_id" not in OAuthConnectInput.model_json_schema()["properties"]
    skill = OAuthConnectSkill(google_store=_FakeStore(True), slack_store=_FakeStore(False))

    out = skill.run(forged_input, _ctx("taro@vectorinc.co.jp"))

    assert out.slack_url is not None
    detailed = verify_state_detailed(_slack_state(out.slack_url))
    assert detailed is not None
    assert detailed.bind_tag == expected_bind_tag(
        _VERIFIED_SLACK_TEAM_ID,
        _VERIFIED_SLACK_USER_ID,
    )
    assert detailed.bind_tag != expected_bind_tag(_VERIFIED_SLACK_TEAM_ID, attacker_uid)


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


# ---------------------------------------------------------------------------
# スコープ対応の再連携（2026-07-13: 既連携ユーザーが v0.3 追加スコープを持たない問題）
# ---------------------------------------------------------------------------


class _ScopedStore:
    """scopes() を持つトークンストア（スコープ対応判定の注入用）。"""

    def __init__(self, scopes: tuple[str, ...] | None) -> None:
        self._scopes = scopes

    def scopes(self, _user_email: str) -> tuple[str, ...] | None:
        return self._scopes

    def has(self, _user_email: str) -> bool:
        return self._scopes is not None


def _full_scopes() -> tuple[str, ...]:
    from teamagent.adapters.google_oauth_flow import WORKSPACE_SCOPES

    return WORKSPACE_SCOPES


def test_scope_upgrade_issues_reconnect_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """旧スコープ（calendar.events 無し）の既連携ユーザーには再連携リンクを出す。"""
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SLACK_OAUTH_REDIRECT_URI", raising=False)
    old_scopes = tuple(s for s in _full_scopes() if not s.endswith("calendar.events"))
    skill = OAuthConnectSkill(google_store=_ScopedStore(old_scopes))
    out = skill.run(OAuthConnectInput(), _ctx("user@example.com"))
    assert out.url is not None, "スコープ不足なら再連携リンクを出す"
    assert "再連携" in out.message


def test_full_scopes_returns_connected_without_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """全スコープ保持ならリンクを出さず連携済み扱い。"""
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SLACK_OAUTH_REDIRECT_URI", raising=False)
    skill = OAuthConnectSkill(google_store=_ScopedStore(_full_scopes()))
    out = skill.run(OAuthConnectInput(), _ctx("user@example.com"))
    assert out.url is None
    assert "連携済み" in out.message


def test_superset_scopes_is_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Google が余分な scope を追加返却しても（上位集合）連携済み扱い。"""
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SLACK_OAUTH_REDIRECT_URI", raising=False)
    extra = (*_full_scopes(), "https://www.googleapis.com/auth/drive.metadata.readonly")
    skill = OAuthConnectSkill(google_store=_ScopedStore(extra))
    out = skill.run(OAuthConnectInput(), _ctx("user@example.com"))
    assert out.url is None


def test_store_without_scopes_falls_back_to_has(monkeypatch: pytest.MonkeyPatch) -> None:
    """scopes() 未実装ストア（旧実装/テストダブル）は has() ベースへフォールバック。"""
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SLACK_OAUTH_REDIRECT_URI", raising=False)
    skill = OAuthConnectSkill(google_store=_FakeStore(connected=True))
    out = skill.run(OAuthConnectInput(), _ctx("user@example.com"))
    assert out.url is None, "has()=True かつ scopes 判定不能なら従来どおり連携済み扱い"


def test_no_row_still_issues_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """行なし（未連携）は従来どおりリンクを出す（scope対応で退行しない）。"""
    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SLACK_OAUTH_REDIRECT_URI", raising=False)
    skill = OAuthConnectSkill(google_store=_ScopedStore(None))
    out = skill.run(OAuthConnectInput(), _ctx("user@example.com"))
    assert out.url is not None
    assert "再連携" not in out.message


# ── 🔴 1 往復でリンクまで届ける（ユーザー指示 2026-08-25）────────────────────────


def test_description_forbids_asking_back_before_calling() -> None:
    """tool description は LLM が **選択時**に読む面。ここに聞き返し禁止を書いておく。

    SOUL.md だけに書くと、tool 一覧しか見ていない判断の局面で効かない。実害は
    「連携」→ 聞き返し →「リンクが欲しい」→ ようやくリンク、の 2 往復。
    """
    desc = OAuthConnectSkill.description
    assert "呼ぶ前に確認や聞き返しを挟まないこと" in desc
    assert "リンクを出しますか？" in desc
    assert "どちらですか？" in desc
    assert "まとめて 1 レスポンスで返す" in desc


def test_single_response_carries_both_links_without_a_choice_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1 レスポンスに Google と Slack の**両方**が載り、選択を促す文言が無いこと。

    「どちらを連携しますか？」と聞かせない根拠は、そもそも両方まとめて返る実装
    （`_compose_message` の ① / ②）にある。分岐質問の必要が無いことを実出力で固定する。
    """
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(False))
    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))

    # 1 レスポンスで両方のリンクが届いている＝利用者に選ばせる必要が無い。
    assert out.url is not None and out.url in out.message
    assert out.slack_url is not None and out.slack_url in out.message

    # 出力自体が聞き返し・選択要求になっていない。
    for banned in ("どちらを", "どちらの", "選んでください", "どれを", "しますか？"):
        assert banned not in out.message, f"案内文が聞き返しになっている: {banned}"


# ── path 形式リンク（USE_OAUTH_START_LINKS・既定 OFF）───────────────────────────
# @Aico の LLM が長い認可 URL(?state=…) を再タイプして state を壊す事故の根治。
# フラグ ON かつ CONNECT_BASE_URL 設定時だけ connect-web の /oauth2/start/{state} 等へ差し替える。

# Google・Slack 両方未連携（連携済み無し・抑止無し）の案内文。OFF/ON でリンク以外は不変。
_EXPECTED_DUAL_MESSAGE = (
    "👋 *taro@vectorinc.co.jp* の連携リンクです（1回だけ・所要1分）。\n"
    "下のリンクは *あなた専用* です（他の人と共有しないでください）。\n"
    "開いて、表示される権限を *許可* してください:\n"
    "\n*① Google を連携*（メールの読み取り・下書き作成、カレンダー等）\n"
    "{google}\n"
    "\n*② Slack を連携*（本人としての検索・チャンネル巡回）\n"
    "{slack}\n"
    "\n「✅ 連携が完了しました」が出れば成功です。あとは話しかけるだけ。"
)
_BASE = "https://connect.example.com"


def _google_state(url: str) -> str:
    values = parse_qs(urlparse(url).query).get("state")
    assert values and len(values) == 1
    return values[0]


@pytest.mark.parametrize("flag", [None, "", "0", "false", "off"])
def test_start_links_off_keeps_raw_urls_and_message_bytes(
    monkeypatch: pytest.MonkeyPatch, flag: str | None
) -> None:
    """既定 OFF（未設定/偽値）では認可 URL をそのまま返し、案内文はバイト単位で従来どおり。"""
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("CONNECT_BASE_URL", _BASE)  # 土台があってもフラグ OFF なら使わない
    if flag is None:
        monkeypatch.delenv("USE_OAUTH_START_LINKS", raising=False)
    else:
        monkeypatch.setenv("USE_OAUTH_START_LINKS", flag)
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(False))

    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))

    assert out.url is not None and out.url.startswith("https://accounts.google.com/o/oauth2/auth?")
    assert out.slack_url is not None
    assert out.slack_url.startswith("https://slack.com/oauth/v2/authorize?")
    assert "/oauth2/start/" not in out.message and "/slack/oauth/start/" not in out.message
    assert out.message == _EXPECTED_DUAL_MESSAGE.format(google=out.url, slack=out.slack_url)


@pytest.mark.parametrize("base", [_BASE, _BASE + "/", "  " + _BASE + "//  "])
def test_start_links_on_replaces_links_with_path_only(
    monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    """ON かつ CONNECT_BASE_URL 設定時は path リンク（query 無し）。末尾スラッシュ/空白は正規化。"""
    from teamagent.adapters.google_oauth_flow import verify_state as google_verify_state

    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("USE_OAUTH_START_LINKS", "1")
    monkeypatch.setenv("CONNECT_BASE_URL", base)
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(False))

    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))

    assert out.url is not None and out.slack_url is not None
    assert out.url.startswith(f"{_BASE}/oauth2/start/")
    assert out.slack_url.startswith(f"{_BASE}/slack/oauth/start/")
    for link in (out.url, out.slack_url):
        assert "?" not in link and "&" not in link  # query 無し・path のみ
        assert "//oauth2" not in link and "//slack" not in link  # 末尾スラッシュ二重化なし
    # path の state はそのまま検証可能（署名・本人・Slack 束縛が保たれている）。
    google_state = out.url.rsplit("/", 1)[1]
    assert google_verify_state(google_state) == "taro@vectorinc.co.jp"
    detailed = verify_state_detailed(out.slack_url.rsplit("/", 1)[1])
    assert detailed is not None and detailed.email == "taro@vectorinc.co.jp"
    assert detailed.bind_tag == expected_bind_tag(_VERIFIED_SLACK_TEAM_ID, _VERIFIED_SLACK_USER_ID)
    # 案内文は認可 URL を一切含まず、リンク以外は OFF 時と同一。
    assert "accounts.google.com" not in out.message
    assert "slack.com/oauth" not in out.message
    assert out.message == _EXPECTED_DUAL_MESSAGE.format(google=out.url, slack=out.slack_url)


def test_start_links_on_uses_the_same_state_as_the_authorization_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """path に載る state は authorization_url が返した state そのもの（別発行ではない）。"""
    from teamagent.adapters.google_oauth_flow import OAuthConsentFlow

    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SLACK_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.setenv("USE_OAUTH_START_LINKS", "true")
    monkeypatch.setenv("CONNECT_BASE_URL", _BASE)
    seen: dict[str, str] = {}
    original = OAuthConsentFlow.authorization_url

    def _spy(self: OAuthConsentFlow, user_email: str, **kw: str) -> tuple[str, str]:
        url, state = original(self, user_email, **kw)
        seen["url"], seen["state"] = url, state
        return url, state

    monkeypatch.setattr(OAuthConsentFlow, "authorization_url", _spy)
    out = OAuthConnectSkill(google_store=_FakeStore(False)).run(
        OAuthConnectInput(), _ctx("taro@vectorinc.co.jp")
    )
    assert out.url == f"{_BASE}/oauth2/start/{seen['state']}"
    assert _google_state(seen["url"]) == seen["state"]


def test_start_links_on_without_base_url_falls_back_to_raw_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ON でも CONNECT_BASE_URL が無ければ壊れた相対リンクを出さず、認可 URL を直接返す。"""
    from structlog.testing import capture_logs

    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("USE_OAUTH_START_LINKS", "1")
    monkeypatch.setenv("CONNECT_BASE_URL", "   ")
    skill = OAuthConnectSkill(google_store=_FakeStore(False), slack_store=_FakeStore(False))

    with capture_logs() as logs:
        out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))

    assert out.url is not None and out.url.startswith("https://accounts.google.com/")
    assert out.slack_url is not None and out.slack_url.startswith("https://slack.com/")
    assert "/oauth2/start/" not in out.message
    events = {e["event"]: e for e in logs}
    assert events["oauth_connect_start_links_prereq_missing"]["missing"] == "CONNECT_BASE_URL"
    assert events["oauth_connect_url_issued"]["start_links"] is False


def test_issued_log_reports_start_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """oauth_connect_url_issued に start_links が載る（ON=True / OFF=False）。"""
    from structlog.testing import capture_logs

    for k, v in _OAUTH_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SLACK_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.setenv("CONNECT_BASE_URL", _BASE)

    monkeypatch.setenv("USE_OAUTH_START_LINKS", "1")
    with capture_logs() as on_logs:
        OAuthConnectSkill(google_store=_FakeStore(False)).run(
            OAuthConnectInput(), _ctx("taro@vectorinc.co.jp")
        )
    monkeypatch.delenv("USE_OAUTH_START_LINKS")
    with capture_logs() as off_logs:
        OAuthConnectSkill(google_store=_FakeStore(False)).run(
            OAuthConnectInput(), _ctx("taro@vectorinc.co.jp")
        )

    on = [e for e in on_logs if e["event"] == "oauth_connect_url_issued"]
    off = [e for e in off_logs if e["event"] == "oauth_connect_url_issued"]
    assert on and on[-1]["start_links"] is True
    assert off and off[-1]["start_links"] is False


def test_start_links_on_slack_only_when_google_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google 連携済み・Slack 未連携 → Slack だけ path リンク（Google は出さない）。"""
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("USE_OAUTH_START_LINKS", "1")
    monkeypatch.setenv("CONNECT_BASE_URL", _BASE)
    skill = OAuthConnectSkill(google_store=_FakeStore(True), slack_store=_FakeStore(False))

    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))

    assert out.url is None
    assert out.slack_url is not None and out.slack_url.startswith(f"{_BASE}/slack/oauth/start/")
    assert out.slack_url in out.message
    assert "Slack を連携" in out.message


def test_start_links_on_both_connected_emits_no_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """両方連携済みならフラグ ON でもリンク（path 形式含む）を一切出さない。"""
    for k, v in {**_OAUTH_ENV, **_SLACK_ENV}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("USE_OAUTH_START_LINKS", "1")
    monkeypatch.setenv("CONNECT_BASE_URL", _BASE)
    skill = OAuthConnectSkill(google_store=_FakeStore(True), slack_store=_FakeStore(True))

    out = skill.run(OAuthConnectInput(), _ctx("taro@vectorinc.co.jp"))

    assert out.url is None and out.slack_url is None
    assert "http" not in out.message
