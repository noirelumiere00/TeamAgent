"""名乗り（Aico）が 4 つの真実源で一致していることの契約テスト。

改名（2026-08）は **openclaw イメージへ焼き込まれる 3 ファイル**と、
それを実機照合する built-image 契約テストの期待値の計 4 箇所に散っている:

1. ``infra/openclaw/openclaw.config.json5`` の ``agents.list[].identity.name``
   — OpenClaw が Control UI の ``assistantName`` として配る値。
2. ``infra/openclaw/IDENTITY.md`` の ``- Name:`` — identity-file パーサが読む真実源。
3. ``infra/openclaw/SOUL.md`` — ペルソナが「名前を尋ねられたら」返す名前。
4. ``tests/scripts/test_openclaw_runtime_image.py`` の ``assistantName`` 期待値
   — 実イメージに対する照合（``OPENCLAW_RUNTIME_TEST_IMAGE`` 未設定時は skip）。

4 を実行できない環境（CI の既定）では、1〜3 のどれか 1 つだけ改名して残りが取り残されても
誰も気づかない。``identity.name`` と ``IDENTITY.md`` の一致を縛るテストは**存在しなかった**
ので、ここで固定する。

⚠️ 改名するときは、このテストが赤くなった 4 箇所を**同じコミットで**揃えること
（片側だけ載せ替えると「名乗りは新名・エラー文は旧名」の混在になる）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_OPENCLAW = _ROOT / "infra" / "openclaw"

ASSISTANT_NAME = "Aico"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_openclaw_config_identity_name() -> None:
    config = _read(_OPENCLAW / "openclaw.config.json5")
    match = re.search(r"identity:\s*\{\s*name:\s*\"([^\"]+)\"", config)
    assert match is not None, "agents.list[].identity.name が見つからない"
    assert match.group(1) == ASSISTANT_NAME


def test_identity_file_name_matches_config() -> None:
    identity = _read(_OPENCLAW / "IDENTITY.md")
    match = re.search(r"^-\s*Name:\s*(.+?)\s*$", identity, flags=re.MULTILINE)
    assert match is not None, "IDENTITY.md の `- Name:` 行が見つからない"
    assert match.group(1) == ASSISTANT_NAME


def test_soul_declares_the_same_name() -> None:
    soul = _read(_OPENCLAW / "SOUL.md")
    assert f"「{ASSISTANT_NAME}」と答える" in soul


def test_built_image_contract_expects_the_same_name() -> None:
    """実イメージ照合テストの期待値が真実源から取り残されていないこと。

    このテスト自身は built image を起動しない（skip される側の期待値だけを見る）。
    """
    contract = _read(_ROOT / "tests" / "scripts" / "test_openclaw_runtime_image.py")
    assert f'assistantName: "{ASSISTANT_NAME}"' in contract


@pytest.mark.parametrize(
    "legacy",
    ["NewsTV AI", "AiLa", "TeamAgent"],
)
def test_legacy_names_only_survive_as_declared_aliases(legacy: str) -> None:
    """旧称は「旧称です」と断った行にだけ残っていてよい（名乗り行に混ざらない）。"""
    for path in (_OPENCLAW / "IDENTITY.md", _OPENCLAW / "SOUL.md"):
        for line in _read(path).splitlines():
            if legacy not in line:
                continue
            assert "旧称" in line, f"{path.name} に旧称が説明なしで残っている: {line!r}"
