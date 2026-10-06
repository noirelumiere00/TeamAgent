"""短い英字のブランド名・競合名は商材の語を添えて検索する（10-05 HIS で 0 本）。"""

from __future__ import annotations

from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput
from teamagent.skills.omiyage_report.skill import OmiyageReportSubmitSkill


def _plan(**kw: object) -> list[tuple[str, str, str]]:
    skill = OmiyageReportSubmitSkill.__new__(OmiyageReportSubmitSkill)
    return skill._axis_plan(OmiyageReportSubmitInput(**kw))  # type: ignore[arg-type]


def test_short_ascii_names_get_a_category_hint() -> None:
    """変異: _name_query を素通しにすると「HIS」単体で検索して赤。"""
    plan = _plan(brand="HIS", competitors=["JTB"], keywords=["海外旅行", "航空券"])
    assert ("brand", "ブランド名「HIS」検索（「HIS 海外旅行」で検索）", "HIS 海外旅行") in plan
    assert ("competitor", "競合「JTB」検索（「JTB 海外旅行」で検索）", "JTB 海外旅行") in plan
    assert ("general", "一般KW「海外旅行」検索", "海外旅行") in plan


def test_category_wins_over_keyword_and_long_names_are_untouched() -> None:
    plan = _plan(
        brand="HIS", competitors=["エムキュア", "GABAN"], keywords=["海外旅行"], category="旅行"
    )
    assert ("brand", "ブランド名「HIS」検索（「HIS 旅行」で検索）", "HIS 旅行") in plan
    assert ("competitor", "競合「エムキュア」検索", "エムキュア") in plan
    assert ("competitor", "競合「GABAN」検索", "GABAN") in plan  # 5 文字は一般語と紛れにくい
