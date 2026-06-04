"""オーケストレーター用 ToolSpec — 既存 Skill を「ツール」として束ねる薄い層.

3層分離維持: ここは runtime 寄りのオーケストレーション層。Skill を呼ぶだけで
adapter は直叩きしない。各 Skill の `input_schema` をそのまま LLM ツールの
JSON schema に使える（Pydantic v2）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from teamagent.skills.base import BaseSkill

SkillFactory = Callable[[], BaseSkill[Any, Any]]


@dataclass(frozen=True)
class ToolSpec:
    """LLM に提示する 1 ツール = 1 Skill。"""

    name: str
    description: str
    skill_cls: type[BaseSkill[Any, Any]]
    factory: SkillFactory | None = None
    """本番 Skill は依存注入が要るため factory を渡せる。PoC fixture は引数なし生成。"""

    @property
    def input_schema(self) -> type[BaseModel]:
        return self.skill_cls.input_schema

    def json_schema(self) -> dict[str, Any]:
        """LLM の tool 定義に渡す入力スキーマ（Bedrock toolConfig / SDK tool 共通で使える）。"""
        return self.skill_cls.input_schema.model_json_schema()

    def instantiate(self) -> BaseSkill[Any, Any]:
        return self.factory() if self.factory is not None else self.skill_cls()


def tool_from_skill(
    skill_cls: type[BaseSkill[Any, Any]], factory: SkillFactory | None = None
) -> ToolSpec:
    """登録済み Skill から ToolSpec を作る（name/description は Skill のクラス変数を使用）。"""
    return ToolSpec(
        name=skill_cls.name,
        description=skill_cls.description,
        skill_cls=skill_cls,
        factory=factory,
    )


__all__ = ["SkillFactory", "ToolSpec", "tool_from_skill"]
