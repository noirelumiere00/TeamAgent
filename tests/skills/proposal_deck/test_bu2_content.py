"""BU2 inputs, deterministic arithmetic, and Composer trust boundary."""

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from teamagent.media.proposal_bu2 import BU2Content, bu2_auxiliary_values
from teamagent.skills.proposal_deck.contract import ComposerOutput
from teamagent.skills.proposal_deck.schema import ProposalDeckInput
from teamagent.skills.proposal_deck.skill import ProposalDeckSkill
from tests.skills.proposal_deck.test_evidence_input import _full_composer_json


def bu2_payload() -> dict[str, Any]:
    return {
        "plans": [
            {
                "name": "Trial",
                "count": 2,
                "views": 100001,
                "views_kind": "保証",
                "unit_price_yen": "2.5",
                "period": "4週間",
                "assumption": "合成の料金条件",
            }
        ],
        "estimate_terms": {
            "net_pricing": True,
            "management_fee_rate": "0.10",
            "production_cost_included": False,
        },
        "simulation": {
            "views": 100001,
            "contacts": "4",
            "understanding_rate": "0.6",
            "assumption": "仮定: 4回接触、理解率60%",
            "rounding": "floor",
        },
        "hearing_items": [
            "目的とKPIを確認したい",
            "ご予算を確認したい",
            "実施時期を確認したい",
            "確認体制を伺いたい",
        ],
    }


def test_bu2_decimal_money_and_reach_use_explicit_assumptions() -> None:
    content = BU2Content.model_validate(bu2_payload())
    values = bu2_auxiliary_values(content)
    assert len(values) == 33
    assert content.plans[0].unit_price_yen == Decimal("2.5")
    assert values["PB-BU2-PLAN-TRIAL-AMOUNT"] == "250,003 円"
    assert values["PB-BU2-PLAN-TRIAL-UNIT-PRICE"] == "2.5円/再生"
    assert values["PB-BU2-PLAN-TRIAL-VIEWS"] == "100,001回（保証）"
    assert values["PB-BU2-SIM-REACH"] == "25,000人（推定）"
    assert values["PB-BU2-SIM-RESULT"] == "15,000人（試算）"
    assert values["PB-BU2-SIM-RATE"] == "60%（仮定）"
    assert (
        values["PB-BU2-ESTIMATE-NOTES"] == "※ネット表記\n※管理費 10% 別途\n※制作費別\n※税・管理費別"
    )
    assert "PB-BU2-HEARING" not in values
    assert values["PB-BU2-HEARING-1"] == "1. 目的とKPIを確認したい"
    assert bu2_auxiliary_values(json.loads(content.model_dump_json())) == values


def test_missing_data_is_review_text_not_invented_financial_terms() -> None:
    values = bu2_auxiliary_values({})
    assert all("要確認" in value for value in values.values() if value)
    assert "10%" not in values["PB-BU2-ESTIMATE-NOTES"]
    assert values["PB-BU2-ESTIMATE-NOTES"].splitlines()[-1] == "※税・管理費別"
    assert "4回" not in values["PB-BU2-SIM-CONTACTS"]
    values = bu2_auxiliary_values({"estimate_terms": {"net_pricing": True}})
    assert values["PB-BU2-ESTIMATE-NOTES"].splitlines()[0] == "※ネット表記"
    assert values["PB-BU2-ESTIMATE-NOTES"].count("要確認") == 2


@pytest.mark.parametrize("field", ["views", "contacts", "understanding_rate", "assumption"])
def test_simulation_has_no_standard_assumptions(field: str) -> None:
    payload = bu2_payload()
    del payload["simulation"][field]
    with pytest.raises(ValidationError):
        BU2Content.model_validate(payload)


@pytest.mark.parametrize(
    "patch",
    [
        {
            "simulation": {
                "views": 1,
                "contacts": 0,
                "understanding_rate": "0.6",
                "assumption": "仮定",
            }
        },
        {
            "simulation": {
                "views": 1,
                "contacts": 4,
                "understanding_rate": "1.01",
                "assumption": "仮定",
            }
        },
        {
            "simulation": {
                "views": 1,
                "contacts": 4,
                "understanding_rate": "0.6",
                "assumption": "  ",
            }
        },
        {
            "simulation": {
                "views": True,
                "contacts": 4,
                "understanding_rate": "0.6",
                "assumption": "仮定",
            }
        },
        {"plans": [{"name": "Trial", "views": 10, "unit_price_yen": "NaN", "assumption": "仮定"}]},
        {"plans": [{"name": "Trial", "assumption": "仮定"}] * 2},
        {"plans": [{"name": "Trial", "count": -1, "assumption": "仮定"}]},
        {"plans": [{"name": "Trial", "views": 10}]},
        {"estimate_terms": {"management_fee_rate": "1.1"}},
        {"hearing_items": ["項目"] * 3},
        {"hearing_items": ["項目"] * 7},
        {"hearing_items": ["  "] * 4},
        {"hearing_items": ["長" * 81] * 4},
    ],
)
def test_bu2_input_bounds(patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BU2Content.model_validate(patch)


def test_composer_cannot_override_bu2_input_and_95_contract_is_unchanged() -> None:
    request = ProposalDeckInput(
        product_name="合成食品", goal="認知", target_persona="家庭", **bu2_payload()
    )
    data = json.loads(_full_composer_json())
    data.update(plans="malformed model data", simulation="invented", hearing_items=["創作"])
    composer = ProposalDeckSkill._finalize_composer(data, request)
    roundtrip = ComposerOutput.model_validate_json(composer.model_dump_json())
    assert len(roundtrip.placeholders) == 95
    assert roundtrip.plans == request.plans
    assert roundtrip.simulation == request.simulation
    assert roundtrip.hearing_items == request.hearing_items
    assert roundtrip.estimate_terms == request.estimate_terms
    assert bu2_auxiliary_values(roundtrip) == bu2_auxiliary_values(request)


def test_forbidden_name_in_bu2_free_text_is_rejected() -> None:
    payload = bu2_payload()
    payload["hearing_items"][0] = "秘密の商品名について伺いたい"
    with pytest.raises(ValidationError, match="forbidden_output_terms"):
        ProposalDeckInput(
            product_name="合成食品",
            goal="認知",
            target_persona="家庭",
            forbidden_output_terms=["秘密の商品名"],
            **payload,
        )


def test_bu2_inputs_survive_length_repair_and_forced_skip() -> None:
    from unittest.mock import MagicMock

    from teamagent.skills.base import SkillContext
    from tests.skills.proposal_deck.test_proposal_deck_skill import _resp

    payload = bu2_payload()
    request = ProposalDeckInput(
        product_name="合成食品",
        goal="認知",
        target_persona="家庭",
        max_repair=0,
        forced_skipped_ids=[41],
        **payload,
    )
    data = json.loads(_full_composer_json())
    data["placeholders"]["61"] = "長" * 81
    data.update(plans="invalid", simulation="invalid", hearing_items=["モデルの創作"])
    bedrock = MagicMock()
    bedrock.converse.return_value = _resp(json.dumps(data))
    composer, _, warnings = ProposalDeckSkill(bedrock=bedrock)._compose(request, SkillContext())
    assert warnings and composer.placeholders[61] == "長" * 18
    assert "切り詰め" in warnings[0]
    assert 41 not in composer.placeholders
    assert any(item.id == 41 for item in composer.skipped_placeholders)
    assert composer.plans == request.plans
    assert composer.simulation == request.simulation
    assert composer.hearing_items == request.hearing_items
    assert composer.estimate_terms == request.estimate_terms
    assert bedrock.converse.call_count == 1


def test_plan_source_and_money_basis_are_preserved_for_slide_notes() -> None:
    from teamagent.media.proposal_bu2 import bu2_source_notes

    notes = bu2_source_notes(bu2_payload())
    assert "PB-BU2-PLAN-TRIAL-AMOUNT" in notes
    assert notes["PB-BU2-HEARING-6"] == "入力4件のため6番枠未使用"
    assert "合成の料金条件" in notes["PB-BU2-PLAN-TRIAL-AMOUNT"]
    assert "税・管理費別、円単位四捨五入" in notes["PB-BU2-PLAN-TRIAL-AMOUNT"]


@pytest.mark.parametrize("item_count", [4, 5, 6])
def test_hearing_slots_keep_numbers_with_text_and_leave_unused_slots_empty(item_count: int) -> None:
    items = [f"確認項目{index}" for index in range(1, item_count + 1)]
    values = bu2_auxiliary_values({"hearing_items": items})
    for index in range(1, 7):
        value = values[f"PB-BU2-HEARING-{index}"]
        if index <= item_count:
            assert value == f"{index}. 確認項目{index}"
        else:
            assert value == ""


def test_zero_hearing_uses_only_first_slot_and_notes_the_unused_five() -> None:
    from teamagent.media.proposal_bu2 import bu2_source_notes

    values = bu2_auxiliary_values({})
    assert "要確認" in values["PB-BU2-HEARING-1"]
    assert all(values[f"PB-BU2-HEARING-{i}"] == "" for i in range(2, 7))
    notes = bu2_source_notes({})
    assert "PB-BU2-HEARING-1" not in notes
    assert notes["PB-BU2-HEARING-6"] == "入力0件のため6番枠未使用"
