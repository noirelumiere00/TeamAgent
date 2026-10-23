"""The MCP-visible instructions must not invite invented customer pricing."""

import pytest

from teamagent.mcp_gateway.server import list_tool_defs
from teamagent.orchestrator.tools import tool_from_skill
from teamagent.skills.proposal_builder.skill import (
    ProposalBuilderSkill,
    ProposalBuilderSubmitSkill,
)


@pytest.fixture(params=[ProposalBuilderSkill, ProposalBuilderSubmitSkill])
def tool(request):
    return list_tool_defs([tool_from_skill(request.param)])[0]


def _assert_request_only(description: str) -> None:
    for rule in ("利用者が明示した値だけ", "無ければ省略", "推測・相場・例の数値を入れない"):
        assert rule in description


def test_tool_description_requires_user_supplied_bu2_values(tool) -> None:
    _assert_request_only(tool.description)
    assert "指定した資料" in tool.description


@pytest.mark.parametrize(
    "model,fields",
    [
        ("BU2Plan", ["count", "views", "unit_price_yen", "period", "assumption"]),
        (
            "BU2EstimateTerms",
            ["net_pricing", "management_fee_rate", "production_cost_included"],
        ),
        ("BU2Simulation", ["views", "contacts", "understanding_rate", "assumption"]),
    ],
)
def test_nested_bu2_descriptions_survive_mcp_schema_projection(tool, model, fields) -> None:
    properties = tool.inputSchema["$defs"][model]["properties"]
    for field in fields:
        _assert_request_only(properties[field].get("description", ""))


def test_bu2_container_descriptions_allow_omission_without_filling_assumptions(tool) -> None:
    properties = tool.inputSchema["properties"]
    for field in ("plans", "estimate_terms", "simulation"):
        _assert_request_only(properties[field].get("description", ""))
    assert "一つでも未指定ならsimulation全体を省略" in properties["simulation"]["description"]
