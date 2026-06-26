"""connect-web /mail/draft ルートのテスト（朝ダイジェスト『下書きを作成』URLボタンの着地点）。

実 HMAC トークン（encode→decode を通す）で検証し、下書き生成本体は FakeMorning でモック。
Gmail/Bedrock/DB には触れない。
"""

from __future__ import annotations

import urllib.parse as _u
from typing import Any

import pytest
from fastapi.testclient import TestClient

from teamagent.connect_web.app import create_app
from teamagent.skills.morning_digest.draft_token import encode_draft_token

OWNER = "s-komata@vectorinc.co.jp"


class _FakeMorning:
    def __init__(self, result: dict[str, Any], **_kw: Any) -> None:
        self._r = result

    def generate_draft_for_thread(self, thread_id: str, requester: str, ctx: Any) -> dict[str, Any]:
        return self._r


def _client(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> TestClient:
    monkeypatch.setenv("MAIL_ACTION_HMAC_SECRET", "sec")
    import teamagent.skills.morning_digest.skill as ms

    monkeypatch.setattr(ms, "MorningDigestSkill", lambda **kw: _FakeMorning(result))
    app = create_app(store=object(), redirect_uri="https://connect.example/oauth2/callback")
    return TestClient(app)


def test_mail_draft_creates_and_redirects_to_gmail(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(monkeypatch, {"created": True, "error": None})
    tok = encode_draft_token("thrA", OWNER)
    r = c.get(f"/mail/draft?t={_u.quote(tok)}&u={_u.quote(OWNER)}", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert "#all/thrA" in loc  # その案件スレッド（下書きインライン）
    assert f"authuser={OWNER}" in loc  # 本人アカウント固定


def test_mail_draft_already_also_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(monkeypatch, {"created": False, "already": True})
    tok = encode_draft_token("thrA", OWNER)
    r = c.get(f"/mail/draft?t={_u.quote(tok)}&u={_u.quote(OWNER)}", follow_redirects=False)
    assert r.status_code == 302


def test_mail_draft_invalid_token_400(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(monkeypatch, {"created": False})
    r = c.get("/mail/draft?t=bad.token&u=x@y.com", follow_redirects=False)
    assert r.status_code == 400
    assert "無効" in r.text


def test_mail_draft_owner_mismatch_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # 別人所有のトークンに本人 u を付けても decode が None（HMAC owner 照合）。
    c = _client(monkeypatch, {"created": True})
    tok = encode_draft_token("thrA", "someone-else@x.com")
    r = c.get(f"/mail/draft?t={_u.quote(tok)}&u={_u.quote(OWNER)}", follow_redirects=False)
    assert r.status_code == 400


def test_mail_draft_error_shows_page(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(monkeypatch, {"created": False, "error": "not_connected"})
    tok = encode_draft_token("thrB", OWNER)
    r = c.get(f"/mail/draft?t={_u.quote(tok)}&u={_u.quote(OWNER)}", follow_redirects=False)
    assert r.status_code == 200
    assert "連携" in r.text
