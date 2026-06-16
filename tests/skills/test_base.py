"""SkillRegistry のユニットテスト。"""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import BaseModel

from teamagent.skills.base import BaseSkill, SkillContext, SkillRegistry, register


class DummyIn(BaseModel):
    text: str


class DummyOut(BaseModel):
    upper: str


def test_register_and_get_skill() -> None:
    """@register でレジストリに入り、get() で取り出せること。"""
    SkillRegistry._clear()

    @register
    class DummySkill(BaseSkill[DummyIn, DummyOut]):
        name: ClassVar[str] = "dummy"
        description: ClassVar[str] = "test dummy"
        input_schema: ClassVar[type[BaseModel]] = DummyIn
        output_schema: ClassVar[type[BaseModel]] = DummyOut

        def run(self, input: DummyIn, ctx: SkillContext) -> DummyOut:
            return DummyOut(upper=input.text.upper())

    assert "dummy" in SkillRegistry.list_all()
    cls = SkillRegistry.get("dummy")
    skill = cls()
    out = skill.run(DummyIn(text="hello"), SkillContext())
    assert isinstance(out, DummyOut)
    assert out.upper == "HELLO"


def test_base_skill_meta_defaults_and_override() -> None:
    """任意メタ（version/owner/required_scope/audit_tag）の既定値と上書き（実行基盤#17,#23）。"""
    SkillRegistry._clear()

    class DefaultSkill(BaseSkill[DummyIn, DummyOut]):
        name: ClassVar[str] = "meta_default"
        description: ClassVar[str] = "d"
        input_schema: ClassVar[type[BaseModel]] = DummyIn
        output_schema: ClassVar[type[BaseModel]] = DummyOut

        def run(self, input: DummyIn, ctx: SkillContext) -> DummyOut:
            return DummyOut(upper=input.text.upper())

    # 既定（後方互換・既存スキルは未指定でこの値）
    assert DefaultSkill.version == "1.0"
    assert DefaultSkill.owner == ""
    assert DefaultSkill.required_scope == ()
    assert DefaultSkill.audit_tag == ""

    class ScopedSkill(DefaultSkill):
        name: ClassVar[str] = "meta_scoped"
        version: ClassVar[str] = "2.1"
        owner: ClassVar[str] = "sales-platform"
        required_scope: ClassVar[tuple[str, ...]] = ("knowledge.read",)
        audit_tag: ClassVar[str] = "rag"

    assert ScopedSkill.required_scope == ("knowledge.read",)
    assert ScopedSkill.owner == "sales-platform"


def test_register_duplicate_name_raises() -> None:
    """同じ name を 2 回登録すると ValueError。"""
    SkillRegistry._clear()

    @register
    class A(BaseSkill[DummyIn, DummyOut]):
        name: ClassVar[str] = "duplicate"
        description: ClassVar[str] = "first"
        input_schema: ClassVar[type[BaseModel]] = DummyIn
        output_schema: ClassVar[type[BaseModel]] = DummyOut

        def run(self, input: DummyIn, ctx: SkillContext) -> DummyOut:
            return DummyOut(upper=input.text)

    with pytest.raises(ValueError, match="重複"):

        @register
        class B(BaseSkill[DummyIn, DummyOut]):
            name: ClassVar[str] = "duplicate"
            description: ClassVar[str] = "second"
            input_schema: ClassVar[type[BaseModel]] = DummyIn
            output_schema: ClassVar[type[BaseModel]] = DummyOut

            def run(self, input: DummyIn, ctx: SkillContext) -> DummyOut:
                return DummyOut(upper=input.text)


def test_get_unknown_raises() -> None:
    """未登録の Skill を get するとき KeyError。"""
    SkillRegistry._clear()
    with pytest.raises(KeyError, match="未登録"):
        SkillRegistry.get("nonexistent")


def test_context_request_id_auto_generated() -> None:
    """SkillContext は request_id を自動生成する。"""
    ctx = SkillContext()
    assert ctx.request_id.startswith("req-")
    assert len(ctx.request_id) == 16  # "req-" + 12 hex


def test_context_logger_binds_fields() -> None:
    """bind_logger は request_id / skill / user_id を持つロガーを返す。"""
    ctx = SkillContext(user_id="u-42")
    log = ctx.bind_logger("my_skill")
    # structlog の bound logger は _context dict を持つ
    bound = log._context  # type: ignore[attr-defined]
    assert bound["skill"] == "my_skill"
    assert bound["user_id"] == "u-42"
    assert bound["request_id"] == ctx.request_id
