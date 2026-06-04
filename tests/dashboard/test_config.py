"""DashboardConfig（env パース）のテスト。"""

from __future__ import annotations

from teamagent.dashboard.config import load_config


def test_parses_and_normalizes_emails() -> None:
    cfg = load_config({"DASHBOARD_ALLOWED_EMAILS": " A@X.com , b@x.com ,, "})
    assert cfg.allowed_emails == frozenset({"a@x.com", "b@x.com"})


def test_hd_lowercased_and_optional() -> None:
    assert load_config({"DASHBOARD_ALLOWED_HD": "VectorInc.co.jp"}).allowed_hd == "vectorinc.co.jp"
    assert load_config({}).allowed_hd is None


def test_dev_bypass_and_cookie_secure_flags() -> None:
    cfg = load_config({"DASHBOARD_DEV_BYPASS": "1", "DASHBOARD_COOKIE_SECURE": "true"})
    assert cfg.dev_bypass is True
    assert cfg.cookie_secure is True
    # dev_bypass 中は auth は無効扱い
    assert cfg.auth_enabled is False


def test_auth_enabled_requires_client_id_and_no_bypass() -> None:
    assert load_config({"DASHBOARD_GOOGLE_CLIENT_ID": "cid"}).auth_enabled is True
    assert load_config({}).auth_enabled is False
    assert (
        load_config({"DASHBOARD_GOOGLE_CLIENT_ID": "cid", "DASHBOARD_DEV_BYPASS": "1"}).auth_enabled
        is False
    )


def test_session_secret_from_env_or_random() -> None:
    cfg = load_config({"DASHBOARD_SESSION_SECRET": "abc"})
    assert cfg.session_secret == b"abc"
    # 未設定ならランダム（32バイト）
    assert len(load_config({}).session_secret) == 32
