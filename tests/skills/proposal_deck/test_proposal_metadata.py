"""Quality gates use request metadata rather than model generated metadata."""

import json
from datetime import date

from teamagent.skills.proposal_deck.contract import ComposerOutput
from teamagent.skills.proposal_deck.schema import ProposalDeckInput
from teamagent.skills.proposal_deck.skill import ProposalDeckSkill
from tests.skills.proposal_deck.test_evidence_input import _full_composer_json


def test_request_metadata_overrides_composer_and_survives_worker_json() -> None:
    data = json.loads(_full_composer_json())
    data.update(product_sector="美容", proposal_date="モデルの不正日付", agency_route="不正な値")
    request = ProposalDeckInput(
        product_name="ハンバーグ",
        goal="認知",
        target_persona="家庭",
        product_sector="食品",
        proposal_date=date(2026, 10, 22),
    )
    composer = ProposalDeckSkill._finalize_composer(data, request)
    payload = json.loads(composer.model_dump_json())
    assert payload["product_sector"] == "食品"
    assert payload["proposal_date"] == "2026-10-22"
    assert payload["agency_route"] is False
    assert ComposerOutput.model_validate_json(composer.model_dump_json()).proposal_date == (
        date(2026, 10, 22)
    )
