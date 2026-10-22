"""Quality gates use request metadata rather than model generated metadata."""

import json
from datetime import date

import pytest
from pydantic import ValidationError

from teamagent.skills.proposal_deck.contract import ComposerOutput
from teamagent.skills.proposal_deck.schema import ProposalDeckInput
from teamagent.skills.proposal_deck.skill import ProposalDeckSkill
from tests.skills.proposal_deck.test_evidence_input import _full_composer_json


@pytest.mark.parametrize("business_unit", [None, "BU1", "BU2"])
def test_request_metadata_overrides_composer_and_survives_worker_json(
    business_unit: str | None,
) -> None:
    data = json.loads(_full_composer_json())
    data.update(
        product_sector="美容",
        proposal_date="モデルの不正日付",
        agency_route="不正な値",
        business_unit="モデルの不正な値",
    )
    metadata = {"business_unit": business_unit} if business_unit else {}
    request = ProposalDeckInput(
        product_name="ハンバーグ",
        goal="認知",
        target_persona="家庭",
        product_sector="食品",
        proposal_date=date(2026, 10, 22),
        **metadata,
    )
    composer = ProposalDeckSkill._finalize_composer(data, request)
    payload = json.loads(composer.model_dump_json())
    assert payload["product_sector"] == "食品"
    assert payload["proposal_date"] == "2026-10-22"
    assert payload["agency_route"] is True
    assert payload["business_unit"] == (business_unit or "BU2")
    assert ComposerOutput.model_validate_json(composer.model_dump_json()).business_unit == (
        business_unit or "BU2"
    )
    assert ComposerOutput.model_validate_json(composer.model_dump_json()).proposal_date == (
        date(2026, 10, 22)
    )


def test_business_unit_defaults_and_boundary_validation() -> None:
    assert ComposerOutput.model_validate_json(_full_composer_json()).business_unit == "BU2"
    with pytest.raises(ValidationError, match="business_unit"):
        ProposalDeckInput(
            product_name="食品", goal="認知", target_persona="家庭", business_unit="BU3"
        )
    data = json.loads(_full_composer_json())
    data["business_unit"] = "BU3"
    with pytest.raises(ValidationError, match="business_unit"):
        ComposerOutput.model_validate(data)
