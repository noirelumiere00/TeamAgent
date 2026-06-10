"""scripts/attack_mcp.py の純ロジック（check_* / build_user_context / ATTACK_PAYLOADS）を検証する。

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


def test_results_identical_pass_and_fail() -> None:
    assert _atk.check_results_identical("admin_role", "RESULT", "RESULT").ok is True
    bad = _atk.check_results_identical("admin_role", "RESULT", "RESULT+leak")
    assert bad.ok is False
    assert "DIFFERS" in bad.detail


def test_no_outsider_pass_and_fail() -> None:
    assert _atk.check_no_outsider("admin_role", "company docs only", "OUTSIDER_TOK").ok is True
    leaked = _atk.check_no_outsider("admin_role", "...OUTSIDER_TOK...", "OUTSIDER_TOK")
    assert leaked.ok is False
    assert "leaked" in leaked.detail
    # 空 needle は誤検知しない
    assert _atk.check_no_outsider("admin_role", "anything", "").ok is True


def test_summarize_false_if_any_fail() -> None:
    checks = [
        _atk.check_no_outsider("baseline", "ok", "TOK"),
        _atk.check_results_identical("admin_role", "a", "b"),
    ]
    assert _atk.summarize(checks) is False
