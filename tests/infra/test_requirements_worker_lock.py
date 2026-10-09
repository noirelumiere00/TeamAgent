"""EC2 worker 用 requirements-worker.lock の依存集合契約。

deploy_to_ec2.sh は ``pip install --require-hashes -r requirements-worker.lock`` で
worker の venv を組むため、この lock に残った依存はそのまま EC2 へ配られる。
lock は uv.lock から ``uv export`` した射影を外科的に保守しているので、
uv.lock から消えた依存が worker lock にだけ取り残されないことを固定する。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER_LOCK = ROOT / "requirements-worker.lock"
UV_LOCK = ROOT / "uv.lock"

_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", re.MULTILINE)


def _normalize(name: str) -> str:
    # PEP 503 の正規化（pip / uv と同じく区切り記号と大文字小文字を同一視する）
    return re.sub(r"[-_.]+", "-", name).lower()


def _worker_packages() -> set[str]:
    text = WORKER_LOCK.read_text(encoding="utf-8")
    return {_normalize(name) for name in _REQUIREMENT.findall(text)}


def _uv_lock_packages() -> set[str]:
    lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    return {_normalize(package["name"]) for package in lock["package"]}


def test_worker_lock_is_parsed() -> None:
    # 正規表現の取りこぼしで下の契約が空集合のまま通らないようにする
    assert len(_worker_packages()) > 100


def test_worker_lock_has_no_bundled_claude_agent_sdk() -> None:
    # claude-agent-sdk は Bun/JS runtime と Claude CLI を同梱するため core から外した
    # （pyproject.toml の dependencies 冒頭・uv.lock は test_dockerfile_teamagent_mcp が固定）。
    # worker lock にだけ 0.2.87 が残り、未使用の同梱 CLI が EC2 へ配られていた。
    assert "claude-agent-sdk" not in _worker_packages()


def test_worker_lock_packages_are_subset_of_uv_lock() -> None:
    orphaned = _worker_packages() - _uv_lock_packages()

    assert orphaned == set(), f"uv.lock に無い依存が worker lock に残っている: {sorted(orphaned)}"
