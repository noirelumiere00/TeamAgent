"""import の向き（仕様 v3 §2-4・§5 T31）。

video_algorithm → search_surface_check.video_structure は許す。search_surface_check が
video_algorithm から import してよいのは schema・frames・evidence（葉のモジュール）だけ。
新しい Python で 1 つずつ import し、循環しないこと・逆向きの依存が無いことを確かめる。
壊し方: search_surface_check 側から report / facts を import させる → 赤。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

_FRESH = """
import importlib, json, sys
importlib.import_module({module!r})
print(json.dumps(sorted(m for m in sys.modules if m.startswith("teamagent.skills."))))
"""


def _loaded_after_import(module: str) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _FRESH.format(module=module)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    import json

    loaded: list[str] = json.loads(proc.stdout.strip().splitlines()[-1])
    return loaded


@pytest.mark.parametrize(
    "module",
    [
        "teamagent.skills.video_algorithm.slides",
        "teamagent.skills.video_algorithm.facts",
        "teamagent.skills.video_algorithm.evidence",
        "teamagent.skills.video_algorithm.analysis",
        "teamagent.skills.video_algorithm.synthesis",
        # サムネ（一覧の表紙）の読み取りと事実（単独で import でき、evidence は葉のまま）
        "teamagent.skills.video_algorithm.cover_facts",
        "teamagent.skills.video_algorithm.cover_read",
        "teamagent.skills.search_surface_check.video_structure",
        "teamagent.skills.search_surface_check.video_chapter",
    ],
)
def test_each_module_imports_alone(module: str) -> None:
    assert module in _loaded_after_import(module)


def test_evidence_is_a_leaf() -> None:
    loaded = _loaded_after_import("teamagent.skills.video_algorithm.evidence")
    assert not any(m.startswith("teamagent.skills.search_surface_check") for m in loaded)
    heavy = {
        "facts",
        "analysis",
        "report",
        "slides",
        "synthesis",
        "skill",
        "cover_facts",
        "cover_read",
    }
    assert not any(m.rsplit(".", 1)[-1] in heavy for m in loaded)


def test_search_surface_check_does_not_pull_video_algorithm_rendering() -> None:
    for module in (
        "teamagent.skills.search_surface_check.video_structure",
        "teamagent.skills.search_surface_check.video_chapter",
        "teamagent.skills.search_surface_check.video_notes",
    ):
        loaded = _loaded_after_import(module)
        va = {m for m in loaded if m.startswith("teamagent.skills.video_algorithm.")}
        allowed = {
            "teamagent.skills.video_algorithm.schema",
            "teamagent.skills.video_algorithm.frames",
            "teamagent.skills.video_algorithm.evidence",
        }
        assert va <= allowed, (module, sorted(va - allowed))


def test_cover_read_does_not_pull_rendering_or_the_skill() -> None:
    """表紙の読み取り（Gemini を呼ぶ層）は描画・skill・統合を読み込まない。"""
    loaded = _loaded_after_import("teamagent.skills.video_algorithm.cover_read")
    heavy = {"report", "slides", "synthesis", "skill", "analysis"}
    assert not any(m.rsplit(".", 1)[-1] in heavy for m in loaded)
