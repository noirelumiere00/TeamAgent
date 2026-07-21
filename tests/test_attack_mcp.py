"""scripts/attack_mcp.py の純ロジックを検証する。

network 部（_run）は実DB＋live MCP のため対象外。重い依存(anyio/mcp)は attack_mcp 側で遅延 import。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType


def _load_attack() -> ModuleType:
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "attack_mcp.py"
    spec = importlib.util.spec_from_file_location("attack_mcp", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["attack_mcp"] = mod  # @dataclass の module 解決のため登録してから exec
    spec.loader.exec_module(mod)
    return mod


_atk = _load_attack()


def test_attack_payloads_cover_spoof_fields() -> None:
    # gateway が破棄すべき4フィールドが vector 群で網羅されている
    keys = {k for p in _atk.ATTACK_PAYLOADS.values() for k in p}
    assert {"user_role", "user_email", "user_groups", "identity_verified"} <= keys
    assert "baseline" in _atk.ATTACK_PAYLOADS  # 比較基準が在る


def test_build_user_context_payload_overrides() -> None:
    ctx = _atk.build_user_context("U123", {"user_role": "admin"})
    assert ctx == {"slack_user_id": "U123", "user_role": "admin"}
    # bad_slack_uid: payload が slack_user_id を上書きできる
    ctx2 = _atk.build_user_context("U123", {"slack_user_id": "evil"})
    assert ctx2["slack_user_id"] == "evil"


def test_caller_rejection_check_pass_and_fail() -> None:
    good = '{"error":"Caller authorization failed.","code":"CALLER_IDENTITY_REJECTED"}'
    assert _atk.check_caller_rejected("admin_role", good).ok is True
    assert _atk.check_caller_rejected("admin_role", '{"hits":[]}').ok is False
    assert _atk.check_caller_rejected("admin_role", "not-json").ok is False


def test_summarize_false_if_any_fail() -> None:
    checks = [
        _atk.check_caller_rejected(
            "baseline",
            '{"error":"Caller authorization failed.","code":"CALLER_IDENTITY_REJECTED"}',
        ),
        _atk.check_caller_rejected("admin_role", '{"hits":[]}'),
    ]
    assert _atk.summarize(checks) is False


def _validate_no_dns(url: str) -> object:
    # 単体テストは非ネットワーク（check_dns=False＝DNS解決を伴わない安価検査）。
    from teamagent.adapters.url_guard import validate_scrape_url

    return validate_scrape_url(url, check_dns=False)


def test_url_guard_blocks_ssrf_payloads() -> None:
    # IMDS/localhost/private/部分文字列bypass/scheme/userinfo/非許可 が全て弾かれる。
    for name in (
        "imds",
        "localhost",
        "private_10",
        "substr_bypass",
        "scheme_file",
        "userinfo",
        "nonallowed",
    ):
        chk = _atk.check_url_guard_blocks(name, _atk.SSRF_URL_PAYLOADS[name], _validate_no_dns)
        assert chk.ok, f"{name} NOT blocked: {chk.detail}"


def test_url_guard_allows_known_domains() -> None:
    for url in _atk.ALLOWED_URL_SAMPLES:
        assert _atk.check_url_guard_allows("ok", url, _validate_no_dns).ok
