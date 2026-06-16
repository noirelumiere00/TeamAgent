"""Skill 基底クラスとレジストリ。

CLAUDE.md 6-bis ルール準拠：
- Pydantic v2 で I/O を固定
- 構造化ログ + request_id 伝播
- 3層分離（Skill / Adapter / Runtime）の Skill 層

Usage:
    class MySkillInput(BaseModel):
        query: str

    class MySkillOutput(BaseModel):
        answer: str

    @register("my_skill")
    class MySkill(BaseSkill[MySkillInput, MySkillOutput]):
        input_schema = MySkillInput
        output_schema = MySkillOutput
        description = "..."

        def run(self, input: MySkillInput, ctx: SkillContext) -> MySkillOutput:
            ...
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, TypeVar

import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

TInput = TypeVar("TInput", bound=BaseModel)
TOutput = TypeVar("TOutput", bound=BaseModel)


@dataclass
class SkillContext:
    """Skill 実行コンテキスト。

    Skill ごとに作成され、request_id を含む構造化ログを出す責任を持つ。
    """

    request_id: str = field(default_factory=lambda: f"req-{uuid.uuid4().hex[:12]}")
    user_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def bind_logger(self, skill_name: str) -> Any:
        """request_id / skill / user_id をバインド済みのロガーを返す。

        structlog の型ヒントが未整備で BoundLogger を返さないため Any としている。
        """
        return logger.bind(
            request_id=self.request_id,
            skill=skill_name,
            user_id=self.user_id,
        )


class BaseSkill(ABC, Generic[TInput, TOutput]):
    """すべての Skill が継承する基底クラス。

    サブクラスは以下のクラス変数を必ず定義する：
    - name: 一意な Skill 名（snake_case 推奨）
    - description: 何をする Skill か（Registry の自己説明用）
    - input_schema: Pydantic v2 BaseModel のサブクラス
    - output_schema: Pydantic v2 BaseModel のサブクラス

    任意メタ（既定安全・後方互換／spec matrix 実行基盤#17・#23）:
    - version: SemVer 風の版（既定 "1.0"）。register() がログに出す。
    - owner: 担当（チーム/個人。空可）。
    - required_scope: このスキルが要求する権限スコープのタプル。空＝制約なし。
      MCP 境界（mcp_gateway.server.dispatch_tool）が将来この値で照合する足場
      （現状は空既定で no-op＝従来挙動）。openclaw.config.json5 の toolFilter
      手書きとの二重定義をここへ寄せていく出発点。
    - audit_tag: 監査ログ用の分類タグ（空可）。
    """

    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[type[BaseModel]]
    output_schema: ClassVar[type[BaseModel]]
    # 任意メタ（未指定でも動く＝既存スキル非改変で安全）
    version: ClassVar[str] = "1.0"
    owner: ClassVar[str] = ""
    required_scope: ClassVar[tuple[str, ...]] = ()
    audit_tag: ClassVar[str] = ""

    @abstractmethod
    def run(self, input: TInput, ctx: SkillContext) -> TOutput:
        """Skill のメインロジック。

        Pydantic で検証済みの input と、request_id を持つ ctx を受ける。
        例外を投げた場合は runtime 層でキャッチして CloudWatch にログされる。
        生入力をエラーログに含めないこと（CLAUDE.md 6-bis Don't）。
        """
        ...


class SkillRegistry:
    """Skill のレジストリ。インポート時に @register デコレータで自動登録される。"""

    _skills: ClassVar[dict[str, type[BaseSkill[Any, Any]]]] = {}

    @classmethod
    def register(cls, skill_cls: type[BaseSkill[Any, Any]]) -> type[BaseSkill[Any, Any]]:
        """Skill クラスを登録する。重複名は許容しない。"""
        if not hasattr(skill_cls, "name"):
            raise ValueError(f"{skill_cls.__name__} に name が定義されていません")
        if skill_cls.name in cls._skills:
            raise ValueError(f"Skill 名 {skill_cls.name!r} が重複しています")
        cls._skills[skill_cls.name] = skill_cls
        logger.debug(
            "skill_registered",
            skill=skill_cls.name,
            version=getattr(skill_cls, "version", "1.0"),
            owner=getattr(skill_cls, "owner", ""),
            required_scope=list(getattr(skill_cls, "required_scope", ())),
        )
        return skill_cls

    @classmethod
    def get(cls, name: str) -> type[BaseSkill[Any, Any]]:
        """名前から Skill クラスを取り出す。"""
        if name not in cls._skills:
            raise KeyError(f"Skill {name!r} は未登録です。登録済み: {list(cls._skills)}")
        return cls._skills[name]

    @classmethod
    def list_all(cls) -> list[str]:
        """登録済み Skill 名を返す。"""
        return sorted(cls._skills.keys())

    @classmethod
    def _clear(cls) -> None:
        """テスト用：レジストリをクリアする。本番では呼ばないこと。"""
        cls._skills.clear()


def register(skill_cls: type[BaseSkill[Any, Any]]) -> type[BaseSkill[Any, Any]]:
    """Skill 登録用のショートカットデコレータ。"""
    return SkillRegistry.register(skill_cls)
