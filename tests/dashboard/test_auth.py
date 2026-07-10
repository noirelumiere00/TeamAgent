"""管理画面 認証のテスト（HMAC セッション・allowlist・id_token 検証）。"""

from __future__ import annotations

from typing import Any

from teamagent.dashboard.auth import (
    authenticate_id_token,
    check_allowed,
    make_session,
    verify_session,
)
from teamagent.dashboard.config import DashboardConfig

_SECRET = b"unit-test-secret-key"


def _cfg(**kw: Any) -> DashboardConfig:
    base = {
        "allowed_emails": frozenset({"owner@vectorinc.co.jp"}),
        "allowed_hd": "vectorinc.co.jp",
        "google_client_id": "client-123.apps.googleusercontent.com",
        "session_secret": _SECRET,
        "dev_bypass": False,
    }
    base.update(kw)
    return DashboardConfig(**base)  # type: ignore[arg-type]


# ---- セッション ------------------------------------------------
def test_session_roundtrip() -> None:
    s = make_session("owner@vectorinc.co.jp", _SECRET, ttl_s=3600, now=1000)
    assert verify_session(s, _SECRET, now=1500) == "owner@vectorinc.co.jp"


def test_session_rejects_tampered_signature() -> None:
    s = make_session("owner@vectorinc.co.jp", _SECRET, ttl_s=3600, now=1000)
    assert verify_session(s + "x", _SECRET, now=1500) is None


def test_session_rejects_wrong_secret() -> None:
    s = make_session("owner@vectorinc.co.jp", _SECRET, ttl_s=3600, now=1000)
    assert verify_session(s, b"other-secret", now=1500) is None


def test_session_rejects_expired() -> None:
    s = make_session("owner@vectorinc.co.jp", _SECRET, ttl_s=10, now=1000)
    assert verify_session(s, _SECRET, now=2000) is None


def test_session_rejects_malformed() -> None:
    assert verify_session("not-a-session", _SECRET, now=1) is None
    assert verify_session("", _SECRET, now=1) is None


def test_session_payload_tamper_breaks_signature() -> None:
    """payload を書き換えると署名が合わず None（他人になりすませない）。"""
    s = make_session("owner@vectorinc.co.jp", _SECRET, ttl_s=3600, now=1000)
    raw_b64, sig_b64 = s.split(".", 1)
    import base64
    import json

    payload = json.loads(base64.urlsafe_b64decode(raw_b64 + "=="))
    payload["email"] = "attacker@evil.com"
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    assert verify_session(f"{forged}.{sig_b64}", _SECRET, now=1500) is None


# ---- allowlist / hd / verified ---------------------------------
def test_check_allowed_happy() -> None:
    claims = {"email": "Owner@vectorinc.co.jp", "email_verified": True, "hd": "vectorinc.co.jp"}
    ok, email = check_allowed(claims, _cfg())
    assert ok is True
    assert email == "owner@vectorinc.co.jp"  # 正規化される


def test_check_allowed_rejects_unverified() -> None:
    claims = {"email": "owner@vectorinc.co.jp", "email_verified": False, "hd": "vectorinc.co.jp"}
    ok, _ = check_allowed(claims, _cfg())
    assert ok is False


def test_check_allowed_rejects_wrong_hd() -> None:
    claims = {"email": "owner@vectorinc.co.jp", "email_verified": True, "hd": "gmail.com"}
    ok, _ = check_allowed(claims, _cfg())
    assert ok is False


def test_check_allowed_rejects_not_in_allowlist() -> None:
    claims = {"email": "stranger@vectorinc.co.jp", "email_verified": True, "hd": "vectorinc.co.jp"}
    ok, email = check_allowed(claims, _cfg())
    assert ok is False
    assert email == "stranger@vectorinc.co.jp"  # 監査用に返す


def test_check_allowed_email_verified_string_true() -> None:
    """email_verified が文字列 'true' でも通す。"""
    claims = {"email": "owner@vectorinc.co.jp", "email_verified": "true", "hd": "vectorinc.co.jp"}
    ok, _ = check_allowed(claims, _cfg())
    assert ok is True


# ---- allowed_hd_opens_domain（会社ドメイン全体に開放・connect-web 全社共有） ----
def test_hd_opens_domain_allows_any_verified_company_user() -> None:
    """開放モード: allowlist 外でも hd 一致＋email_verified なら許可（全社共有）。"""
    claims = {"email": "yu-aoki@vectorinc.co.jp", "email_verified": True, "hd": "vectorinc.co.jp"}
    ok, email = check_allowed(claims, _cfg(allowed_hd_opens_domain=True))
    assert ok is True
    assert email == "yu-aoki@vectorinc.co.jp"


def test_hd_opens_domain_still_allows_allowlisted() -> None:
    claims = {"email": "owner@vectorinc.co.jp", "email_verified": True, "hd": "vectorinc.co.jp"}
    ok, _ = check_allowed(claims, _cfg(allowed_hd_opens_domain=True))
    assert ok is True


def test_hd_opens_domain_rejects_other_domain() -> None:
    """開放モードでも別ドメイン(hd不一致)かつ allowlist 外は拒否（詐称防止）。"""
    claims = {"email": "someone@gmail.com", "email_verified": True, "hd": "gmail.com"}
    ok, _ = check_allowed(claims, _cfg(allowed_hd_opens_domain=True))
    assert ok is False


def test_hd_opens_domain_rejects_missing_hd() -> None:
    """開放モードでも hd クレーム無し（非 Workspace）かつ allowlist 外は拒否。"""
    claims = {"email": "consumer@gmail.com", "email_verified": True}
    ok, _ = check_allowed(claims, _cfg(allowed_hd_opens_domain=True))
    assert ok is False


def test_hd_opens_domain_rejects_unverified() -> None:
    """開放モードでも email_verified 未確認は拒否。"""
    claims = {"email": "yu-aoki@vectorinc.co.jp", "email_verified": False, "hd": "vectorinc.co.jp"}
    ok, _ = check_allowed(claims, _cfg(allowed_hd_opens_domain=True))
    assert ok is False


# ---- id_token 検証 ---------------------------------------------
def test_authenticate_id_token_with_injected_verifier() -> None:
    def verifier(token: str, client_id: str) -> dict[str, Any]:
        assert token == "tok"
        assert client_id == "client-123.apps.googleusercontent.com"
        return {"email": "owner@vectorinc.co.jp", "email_verified": True, "hd": "vectorinc.co.jp"}

    ok, email = authenticate_id_token("tok", _cfg(), verifier=verifier)
    assert ok is True
    assert email == "owner@vectorinc.co.jp"


def test_authenticate_id_token_verifier_raises_is_denied() -> None:
    def verifier(token: str, client_id: str) -> dict[str, Any]:
        raise ValueError("invalid token")

    ok, email = authenticate_id_token("bad", _cfg(), verifier=verifier)
    assert ok is False
    assert email is None


def test_authenticate_id_token_no_client_id_denied() -> None:
    ok, email = authenticate_id_token("tok", _cfg(google_client_id=None))
    assert ok is False
    assert email is None
