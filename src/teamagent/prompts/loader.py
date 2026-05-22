"""Prompt ファイル読み込みヘルパー。

importlib.resources で teamagent.prompts パッケージ内の .md を読む。
コードに prompt を hard-code しないため必ずこれ経由で取得すること。
"""

from __future__ import annotations

from importlib.resources import files


def load_prompt(skill: str, version: str, name: str) -> str:
    """`src/teamagent/prompts/<skill>/<version>/<name>.md` を読み込む。

    Args:
        skill: Skill 名（例 "search"）
        version: バージョン（例 "v1"）
        name: ファイル名（拡張子なし、例 "system"）

    Returns:
        Markdown ファイルの文字列
    """
    path = files("teamagent.prompts") / skill / version / f"{name}.md"
    return path.read_text(encoding="utf-8")
