"""connect-web の **start ルート**（``/oauth2/start/{state}`` ・ ``/slack/oauth/start/{state}``）の
失敗ページに診断行（``診断: CONNECT-…``）が出ることのテスト。

背景: PR #380 で callback の全失敗経路に診断行が付いたが、PR #376 で足した path 形式の
開始リンク（env ``USE_OAUTH_START_LINKS=1`` で**本番点灯済み**）は route が ``Request`` を
受けていなかったため診断行が付かなかった。利用者が最初に踏むのは start 側なので、ここが
「検証に失敗しました」だけだと管理者へは「うまくいかない」としか届かない。

固定するもの:
- start の各失敗（署名不一致 / 期限切れ / 非 base64url / 長すぎ / 束縛無し / 設定不備）で
  期待コードの診断行と転送案内が出る
- 診断行に秘匿値（state / code / token / 素のメール）が出ない・HTML エスケープされる
- 正常時（302）は診断行が出ない
- warning ログに ``request_id=`` / ``diag=`` / ``state_reason=`` が乗る

実 Google / 実 Slack / 実 DB / 実 KMS は 0。
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

import teamagent.connect_web.app as app_module
from teamagent.adapters import google_oauth_flow, slack_oauth_flow
from teamagent.adapters.google_oauth_flow import OAuthConsentFlow
from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.adapters.slack_oauth_flow import SlackOAuthConsentFlow
from teamagent.connect_diagnostics import ConnectDiag, admin_forward_hint
from teamagent.connect_web.app import _MAX_OAUTH_START_STATE_CHARS, create_app

_GOOGLE_REDIRECT = "https://connect.example.com/oauth2/callback"
_SLACK_REDIRECT = "https://connect.example.com/slack/oauth/callback"
_EMAIL = "owner@vectorinc.co.jp"
_MASKED = "o***@vectorinc.co.jp"
_SLACK_UID = "U0123456789"
_SLACK_TEAM = "T0123456789"

_DIAG_RE = re.compile(r"診断: (CONNECT-[A-Z]\d\d[a-c]?) (\d{4}-\d\d-\d\d \d\d:\d\d JST) ([^<\s]+)")

_ENV = {
    "OAUTH_STATE_SECRET": "unit-test-state-secret-0123456789",
    "CONNECT_GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
    "CONNECT_GOOGLE_CLIENT_SECRET": "test-secret",
    "OAUTH_REDIRECT_URI": _GOOGLE_REDIRECT,
    "SLACK_OAUTH_STATE_SECRET": "unit-test-slack-state-secret-0123456789",
    "CONNECT_SLACK_CLIENT_ID": "123456789.987654321",
    "SLACK_OAUTH_REDIRECT_URI": _SLACK_REDIRECT,
}


def _diag(text: str) -> tuple[str, str, str]:
    m = _DIAG_RE.search(text)
    assert m, "診断行が無い"
    return m.group(1), m.group(2), m.group(3)


def _assert_no_secrets(text: str, *, state: str) -> None:
    """秘匿値（state）と素のメールがページに出ないこと。"""
    assert state not in text
    assert _EMAIL not in text
    assert "owner@" not in text


class _FakeStore:
    def put(self, user_email: str, token: Any) -> None:  # pragma: no cover - 呼ばれない
        raise AssertionError("start ルートは保存しない")


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", raising=False)
    monkeypatch.delenv("CONNECT_SEARCH_ALLOWED_HD", raising=False)

    def exchange_fn(code: str) -> OAuthToken:  # pragma: no cover - start では呼ばれない
        return OAuthToken(refresh_token=f"rt-{code}", scopes=("a",), id_token=f"id-{code}")

    app = create_app(
        redirect_uri=_GOOGLE_REDIRECT,
        slack_redirect_uri=_SLACK_REDIRECT,
        exchange_fn=exchange_fn,
        store=_FakeStore(),
        google_state_consumer=lambda _s: True,
        slack_state_consumer=lambda _s: True,
    )
    return TestClient(app)


def _gstate(**kw: Any) -> str:
    return google_oauth_flow.make_state(_EMAIL, **kw)


def _sstate(*, bound: bool = True, **kw: Any) -> str:
    if bound:
        return slack_oauth_flow.make_state(
            _EMAIL, slack_user_id=_SLACK_UID, slack_team_id=_SLACK_TEAM, **kw
        )
    return slack_oauth_flow.make_state(_EMAIL, **kw)


def _retype_one_char(state: str) -> str:
    mid = len(state) // 2
    repl = "A" if state[mid] != "A" else "B"
    return state[:mid] + repl + state[mid + 1 :]


# ── 正常系: 診断行を出さない ────────────────────────────────────────────────────


def test_google_start_success_has_no_diag(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    _, state = OAuthConsentFlow(redirect_uri=_GOOGLE_REDIRECT).authorization_url(_EMAIL)
    r = client.get(f"/oauth2/start/{state}", follow_redirects=False)
    assert r.status_code == 302
    assert "診断:" not in r.text


def test_slack_start_success_has_no_diag(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    _, state = SlackOAuthConsentFlow(redirect_uri=_SLACK_REDIRECT).authorization_url(
        _EMAIL, slack_user_id=_SLACK_UID, slack_team_id=_SLACK_TEAM
    )
    r = client.get(f"/slack/oauth/start/{state}", follow_redirects=False)
    assert r.status_code == 302
    assert "診断:" not in r.text


# ── Google start: S01 / S02 / S06 ──────────────────────────────────────────────


def test_google_start_tampered_state_is_s01_without_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 転記型の改竄（構造無傷・HMAC 不一致）→ S01。署名未検証の email は出さない。"""
    client = _client(monkeypatch)
    tampered = _retype_one_char(_gstate())
    r = client.get(f"/oauth2/start/{tampered}", follow_redirects=False)
    assert r.status_code == 400
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.S01.value
    assert subject == "-"
    assert admin_forward_hint() in r.text
    _assert_no_secrets(r.text, state=tampered)


def test_google_start_expired_state_is_s02_with_masked_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """期限切れは S02。署名は一致しているので診断行にマスク済み本人メールが載る。"""
    client = _client(monkeypatch)
    state = _gstate(now=1, nonce="old")
    r = client.get(f"/oauth2/start/{state}", follow_redirects=False)
    assert r.status_code == 400
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.S02.value
    assert subject == _MASKED
    assert "30 分" in r.text
    _assert_no_secrets(r.text, state=state)


def test_google_start_expired_and_tampered_are_distinguished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S01 と S02 が**出し分いている**こと（片方に潰れていない）。"""
    client = _client(monkeypatch)
    expired = client.get(f"/oauth2/start/{_gstate(now=1, nonce='old')}", follow_redirects=False)
    tampered = client.get(f"/oauth2/start/{_retype_one_char(_gstate())}", follow_redirects=False)
    assert _diag(expired.text)[0] == ConnectDiag.S02.value
    assert _diag(tampered.text)[0] == ConnectDiag.S01.value


@pytest.mark.parametrize(
    ("label", "path_state"),
    [
        ("garbage", "not-a-state"),
        ("slash", "broken/with/slash"),
        ("too_long", "A" * (_MAX_OAUTH_START_STATE_CHARS + 1)),
    ],
)
def test_google_start_malformed_states_are_s01(
    monkeypatch: pytest.MonkeyPatch, label: str, path_state: str
) -> None:
    client = _client(monkeypatch)
    r = client.get(f"/oauth2/start/{path_state}", follow_redirects=False)
    assert r.status_code == 400, label
    assert _diag(r.text)[0] == ConnectDiag.S01.value, label
    assert _diag(r.text)[2] == "-", label


def test_google_start_unconfigured_is_s06_with_masked_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """認可 URL を組めない設定不備は S06（利用者操作では直らない）。"""
    client = _client(monkeypatch)
    state = _gstate()
    for name in (
        "CONNECT_GOOGLE_CLIENT_ID",
        "CONNECT_GOOGLE_CLIENT_SECRET",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    r = client.get(f"/oauth2/start/{state}", follow_redirects=False)
    assert r.status_code == 500
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.S06.value
    assert subject == _MASKED
    _assert_no_secrets(r.text, state=state)


# ── Slack start: T01 / S06 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "kind"),
    [
        ("tampered", "tampered"),
        ("expired", "expired"),
        ("garbage", "garbage"),
        ("too_long", "too_long"),
    ],
)
def test_slack_start_state_problems_are_t01(
    monkeypatch: pytest.MonkeyPatch, label: str, kind: str
) -> None:
    client = _client(monkeypatch)
    path_state = {
        "tampered": lambda: _retype_one_char(_sstate()),
        "expired": lambda: _sstate(now=1, nonce="old"),
        "garbage": lambda: "not-a-state",
        "too_long": lambda: "A" * (_MAX_OAUTH_START_STATE_CHARS + 1),
    }[kind]()
    r = client.get(f"/slack/oauth/start/{path_state}", follow_redirects=False)
    assert r.status_code == 400, label
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.T01.value, label
    assert subject == "-", label  # 署名未検証なので識別子は出さない
    _assert_no_secrets(r.text, state=path_state)


def test_slack_start_unbound_state_is_t01_with_masked_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """束縛（bind_tag）無しは T01。署名は一致しているのでマスク済み本人メールが載る。"""
    client = _client(monkeypatch)
    unbound = _sstate(bound=False)
    r = client.get(f"/slack/oauth/start/{unbound}", follow_redirects=False)
    assert r.status_code == 400
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.T01.value
    assert subject == _MASKED
    _assert_no_secrets(r.text, state=unbound)


def test_slack_start_unconfigured_is_s06(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch)
    state = _sstate()
    monkeypatch.delenv("CONNECT_SLACK_CLIENT_ID", raising=False)
    monkeypatch.delenv("SLACK_CLIENT_ID", raising=False)
    r = client.get(f"/slack/oauth/start/{state}", follow_redirects=False)
    assert r.status_code == 500
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.S06.value
    assert subject == _MASKED


# ── request_id / エスケープ / 秘匿 ─────────────────────────────────────────────


def test_start_diag_carries_request_id_from_alb_trace_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    rid = "1-68b7c0de-0123456789abcdef01234567"
    r = client.get(
        "/oauth2/start/not-a-state",
        headers={"X-Amzn-Trace-Id": f"Root={rid}"},
        follow_redirects=False,
    )
    line = _DIAG_RE.search(r.text)
    assert line is not None
    assert r.text[line.end() :].startswith(f" {rid}")


def test_start_diag_is_html_escaped_and_request_id_sanitised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """request_id ヘッダにタグを入れても素の ``<script>`` にならない（＋不正値は落とす）。"""
    client = _client(monkeypatch)
    r = client.get(
        "/oauth2/start/not-a-state",
        headers={"X-Request-Id": "<script>alert(1)</script>"},
        follow_redirects=False,
    )
    assert "<script>alert(1)</script>" not in r.text
    code, _when, subject = _diag(r.text)
    assert code == ConnectDiag.S01.value
    assert subject == "-"


def test_start_failure_page_never_reflects_state_in_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    tampered = _retype_one_char(_gstate())
    r = client.get(f"/oauth2/start/{tampered}", follow_redirects=False)
    assert "location" not in r.headers
    for name, value in r.headers.items():
        assert tampered not in value, f"header {name} に state が反射している"


# ── warning ログ（診断行 ↔ ログの突合キー）─────────────────────────────────────


class _RecordingLogger:
    """app モジュールの ``logger`` を差し替える記録器。

    structlog の ``capture_logs`` / ``LogCapture`` は使わない: 本番設定
    （``observability.logging_config``）が ``cache_logger_on_first_use=True`` なので、
    先に別テストが 1 度でもログを出していると bound logger がキャッシュされ、後から
    ``structlog.configure`` し直しても捕まらない（全体実行でだけ落ちる非決定テストになる）。
    """

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def _record(self, event: str, **kw: Any) -> None:
        self.entries.append((event, kw))

    debug = info = warning = error = exception = critical = _record

    def of(self, event: str) -> list[dict[str, Any]]:
        return [kw for name, kw in self.entries if name == event]


@pytest.fixture()
def captured_logs(monkeypatch: pytest.MonkeyPatch) -> _RecordingLogger:
    recorder = _RecordingLogger()
    monkeypatch.setattr(app_module, "logger", recorder)
    return recorder


@pytest.mark.parametrize(
    ("path", "event", "state_reason", "diag"),
    [
        ("/oauth2/start/bad!state", "connect_start_bad_state", "not_base64url", "CONNECT-S01"),
        (
            "/slack/oauth/start/bad!state",
            "connect_slack_start_bad_state",
            "not_base64url",
            "CONNECT-T01",
        ),
    ],
)
def test_start_warning_log_has_request_id_diag_and_state_reason(
    monkeypatch: pytest.MonkeyPatch,
    captured_logs: _RecordingLogger,
    path: str,
    event: str,
    state_reason: str,
    diag: str,
) -> None:
    client = _client(monkeypatch)
    rid = "1-68b7c0de-0123456789abcdef01234567"
    client.get(path, headers={"X-Amzn-Trace-Id": f"Root={rid}"}, follow_redirects=False)
    entries = captured_logs.of(event)
    assert entries, f"{event} のログが無い: {[n for n, _ in captured_logs.entries]}"
    entry = entries[-1]
    assert entry["request_id"] == rid
    assert entry["diag"] == diag
    assert entry["state_reason"] == state_reason


def test_google_start_tampered_log_says_bad_signature(
    monkeypatch: pytest.MonkeyPatch, captured_logs: _RecordingLogger
) -> None:
    """転記事故は ``state_reason=bad_signature``（runbook の切り分け語彙と揃える）。"""
    client = _client(monkeypatch)
    client.get(f"/oauth2/start/{_retype_one_char(_gstate())}", follow_redirects=False)
    entries = captured_logs.of("connect_start_bad_state")
    assert entries
    assert entries[-1]["state_reason"] == "bad_signature"
    assert entries[-1]["diag"] == ConnectDiag.S01.value


def test_google_start_expired_log_says_expired(
    monkeypatch: pytest.MonkeyPatch, captured_logs: _RecordingLogger
) -> None:
    client = _client(monkeypatch)
    client.get(f"/oauth2/start/{_gstate(now=1, nonce='old')}", follow_redirects=False)
    entries = captured_logs.of("connect_start_bad_state")
    assert entries
    assert entries[-1]["state_reason"] == "expired"
    assert entries[-1]["diag"] == ConnectDiag.S02.value


def test_slack_start_unbound_log_is_t01(
    monkeypatch: pytest.MonkeyPatch, captured_logs: _RecordingLogger
) -> None:
    client = _client(monkeypatch)
    client.get(f"/slack/oauth/start/{_sstate(bound=False)}", follow_redirects=False)
    entries = captured_logs.of("connect_slack_start_unbound_rejected")
    assert entries
    assert entries[-1]["diag"] == ConnectDiag.T01.value
    assert entries[-1]["state_reason"] == "unbound"


# ── 消費しない契約は保ったまま（S03 を start では出さない理由）─────────────────


def test_start_still_does_not_consume_state_and_never_emits_s03(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start は state を消費しない＝「使用済み」を知り得ないので S03 は callback 側だけ。

    ここが赤くなったら、start に消費記録の読み取りを足していないかを疑うこと
    （足すと「何度開いても同じ認可 URL へ飛ぶ」という PR #376 の契約が壊れる）。
    """
    calls: list[str] = []

    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    app = create_app(
        redirect_uri=_GOOGLE_REDIRECT,
        slack_redirect_uri=_SLACK_REDIRECT,
        exchange_fn=lambda code: OAuthToken(refresh_token="rt", scopes=("a",), id_token="id"),
        store=_FakeStore(),
        google_state_consumer=lambda s: (calls.append(s), True)[1],
        slack_state_consumer=lambda s: (calls.append(s), True)[1],
    )
    client = TestClient(app)
    issued_url, state = OAuthConsentFlow(redirect_uri=_GOOGLE_REDIRECT).authorization_url(_EMAIL)

    for _ in range(3):
        r = client.get(f"/oauth2/start/{state}", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == issued_url
        assert "診断:" not in r.text
    assert calls == []
