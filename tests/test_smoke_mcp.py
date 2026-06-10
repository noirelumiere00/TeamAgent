"""scripts/smoke_mcp.py の純ロジック（check_*）を外部I/O無しで検証する。

network 部分（_run）は post-deploy 実行のため対象外。重い依存(httpx/mcp/anyio)は smoke 側で遅延 import
しているので、本テストはモジュール import だけで pure helper を呼べる。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType


def _load_smoke() -> ModuleType:
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "smoke_mcp.py"
    spec = importlib.util.spec_from_file_location("smoke_mcp", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["smoke_mcp"] = mod  # @dataclass の module 解決(sys.modules参照)のため登録してから exec
    spec.loader.exec_module(mod)
    return mod


_smoke = _load_smoke()


def test_healthz_check() -> None:
    assert _smoke.check_healthz(200).ok is True
    assert _smoke.check_healthz(500).ok is False
    assert _smoke.check_healthz(404).ok is False


def test_unauthorized_check() -> None:
    assert _smoke.check_unauthorized(401).ok is True  # bearer 無は 401 が正
    assert _smoke.check_unauthorized(200).ok is False  # 通ってしまうのは NG
    assert _smoke.check_unauthorized(403).ok is False


def test_tool_exposure_ok_for_knowledge_only() -> None:
    c = _smoke.check_tool_exposure(["search", "clientkarte", "proposal_draft", "proposal_review"])
    assert c.ok is True


def test_tool_exposure_fails_on_missing_knowledge() -> None:
    c = _smoke.check_tool_exposure(["search", "clientkarte"])  # proposal_* 欠落
    assert c.ok is False
    assert "MISSING_KNOWLEDGE" in c.detail


def test_tool_exposure_fails_on_leaked_per_user() -> None:
    # per-user(本人OAuth) が露出していたら NG（会社共有モデルで出してはいけない）
    c = _smoke.check_tool_exposure(
        ["search", "clientkarte", "proposal_draft", "proposal_review", "mail_constraints"]
    )
    assert c.ok is False
    assert "LEAKED_PER_USER" in c.detail


def test_company_scoped_ok_within_domain() -> None:
    c = _smoke.check_company_scoped(["vectorinc.co.jp"], ["vectorinc.co.jp"])
    assert c.ok is True


def test_company_scoped_fails_on_outside_domain() -> None:
    # 会社外ドメインの doc が混ざったら NG
    c = _smoke.check_company_scoped(["vectorinc.co.jp", "evil.com"], ["vectorinc.co.jp"])
    assert c.ok is False
    assert "evil.com" in c.detail


def test_summarize_returns_false_if_any_fail(capsys: object) -> None:
    checks = [_smoke.check_healthz(200), _smoke.check_healthz(500)]
    assert _smoke.summarize(checks) is False
