"""エージェント画面の「提案されたプロンプト」を Aico 仕様へ差し替えるビルド時パッチ（2026-10-07）。

上流 @openclaw/slack は新しいアシスタントスレッドで英語の固定 3 件を送る。Slack の提案は押すと
その文がそのまま送られるので、書き換え不要で Aico が答えられる文だけにする。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "infra/openclaw/patch-slack-home.mjs"


def _prompts() -> list[tuple[str, str]]:
    src = PATCH.read_text(encoding="utf-8")
    block = src.split("const DEFAULT_ASSISTANT_PROMPTS = [", 2)[2]
    block = block.split("];", 1)[0]
    return re.findall(r'title: "([^"]+)",\s*\\n\\t\\tmessage: "([^"]+)"', block) or re.findall(
        r'title: "([^"]+)",\s*message: "([^"]+)"', block.replace("\\t", "").replace("\\n", " ")
    )


def test_prompts_are_japanese_ready_to_send_and_within_slack_limit() -> None:
    prompts = _prompts()
    assert len(prompts) == 4  # Slack の上限は 4 件
    for title, message in prompts:
        assert re.search(r"[ぁ-んァ-ヶ一-龥]", title + message)  # 日本語
        # 押すとそのまま送られる＝利用者が書き換える前提の穴（（取引先名）や ○○）を持たない
        assert not re.search(r"[（(][^）)]*[）)]|○○|◯◯|XX", message), message
        assert len(title) <= 20 and len(message) <= 60
    assert "Aico にこう頼めます" in PATCH.read_text(encoding="utf-8")


def test_patch_applies_to_the_shipped_slack_plugin() -> None:
    """上流の実物（@openclaw/slack 2026.7.1 の dist）に当てて置換と構文を確かめる。

    任意実行: OPENCLAW_SLACK_DIST_DIR を付けたときだけ（CI では Docker ビルドの node 実行が門）。
    """
    raw = os.environ.get("OPENCLAW_SLACK_DIST_DIR")
    if not raw:
        pytest.skip("OPENCLAW_SLACK_DIST_DIR（@openclaw/slack の package/dist）が無い")
    work = Path(os.environ.get("TMPDIR", "/tmp")) / "slack-dist-patch-test"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(raw, work)
    subprocess.run(["node", str(PATCH), str(work)], check=True, capture_output=True)
    provider = next(work.glob("provider-*.js"))
    subprocess.run(["node", "--check", str(provider)], check=True)
    text = provider.read_text(encoding="utf-8")
    assert "What can you do?" not in text and "Try asking" not in text
    for title, message in _prompts():
        assert f'title: "{title}"' in text and f'message: "{message}"' in text
