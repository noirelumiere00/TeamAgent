"""run_canary_health の純判定ロジックのテスト（I/O 無し）。"""

from __future__ import annotations

import importlib.util
import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "run_canary_health.py"


def _load() -> object:
    spec = importlib.util.spec_from_file_location("run_canary_health", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load()


def test_all_pass_is_healthy() -> None:
    assert m.evaluate_canary({"identity_resolve": True}) is True
    assert m.evaluate_canary({"identity_resolve": True, "search": True}) is True


def test_any_fail_is_unhealthy() -> None:
    assert m.evaluate_canary({"identity_resolve": False}) is False
    assert m.evaluate_canary({"identity_resolve": True, "search": False}) is False


def test_empty_is_unhealthy() -> None:
    # 何も検査できていない＝異常扱い（カナリア自身の起動失敗を見逃さない）
    assert m.evaluate_canary({}) is False
