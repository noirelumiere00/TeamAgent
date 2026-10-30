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
    request = ProposalDeckInput.model_validate(
        {
            "product_name": "ハンバーグ",
            "goal": "認知",
            "target_persona": "家庭",
            "product_sector": "食品",
            "proposal_date": date(2026, 10, 22),
            **metadata,
        }
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
        ProposalDeckInput.model_validate(
            {
                "product_name": "食品",
                "goal": "認知",
                "target_persona": "家庭",
                "business_unit": "BU3",
            }
        )
    data = json.loads(_full_composer_json())
    data["business_unit"] = "BU3"
    with pytest.raises(ValidationError, match="business_unit"):
        ComposerOutput.model_validate(data)


@pytest.mark.parametrize("model_date", ["未定", ""])
@pytest.mark.parametrize("posting_date", [None, date(2027, 1, 10)])
def test_deterministic_fields_override_model_before_first_validation(
    model_date: str,
    posting_date: date | None,
) -> None:
    data = json.loads(_full_composer_json())
    data.update(
        posting_start_date=model_date,
        auxiliary_placeholders={"invalid-model-key": ""},
        template_profile="invalid-model-profile",
        evidence_images={"invalid-model-id": []},
    )
    request = ProposalDeckInput(
        product_name="合成商材",
        goal="認知",
        target_persona="家庭",
        posting_start_date=posting_date,
        auxiliary_placeholders={"PB-MONTH": "未定"},
    )
    composer = ProposalDeckSkill._finalize_composer(data, request)
    assert composer.posting_start_date == posting_date
    assert composer.auxiliary_placeholders == request.auxiliary_placeholders
    assert composer.template_profile == request.template_profile
    assert composer.evidence_images == request.evidence_images


@pytest.mark.parametrize("value", ["未定", "", "2050-13-31"])
def test_invalid_request_posting_date_is_still_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="posting_start_date"):
        ProposalDeckInput.model_validate(
            {
                "product_name": "合成商材",
                "goal": "認知",
                "target_persona": "家庭",
                "posting_start_date": value,
            }
        )
