"""Builder carries existing sector and one fixed creation date into composer input."""

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder.schema import ProposalBuilderStatusInput
from teamagent.skills.proposal_builder.skill import ProposalBuilderSkill, ProposalBuilderStatusSkill
from teamagent.skills.proposal_deck.schema import ProposalDeckInput, ProposalDeckOutput
from tests.skills.proposal_builder.test_research_delivery_recovery import _build_job
from tests.skills.proposal_builder.test_tiktok_enrichment import (
    _CapturingDeck,
    _configure_builder_test,
    _input,
    _research,
)


@pytest.mark.parametrize("business_unit", [None, "BU1", "BU2"])
def test_builder_passes_existing_sector_and_creation_date(
    monkeypatch: pytest.MonkeyPatch, business_unit: str | None
) -> None:
    _configure_builder_test(monkeypatch, media_configured=False)
    deck = _CapturingDeck()
    builder = ProposalBuilderSkill(
        search=object(),
        deck=deck,  # type: ignore[arg-type]
        account_db_path="unused.xlsx",
    )
    metadata = {"business_unit": business_unit} if business_unit else {}
    request = _input(_research(sector="食品"), proposal_date="2026-10-22", **metadata)
    request = type(request).model_validate_json(request.model_dump_json())
    builder.run(request, SkillContext())
    assert deck.inputs[0].product_sector == "食品"
    assert deck.inputs[0].proposal_date == date(2026, 10, 22)
    assert deck.inputs[0].business_unit == (business_unit or "BU2")
    assert deck.inputs[0].auxiliary_placeholders["PB-DATETIME"] == "2026年10月22日"
    assert deck.inputs[0].auxiliary_placeholders["PB-MONTH"] == "2026年09月"


def test_builder_rejects_unknown_business_unit() -> None:
    with pytest.raises(ValidationError, match="business_unit"):
        _input(_research(), business_unit="BU3")


def test_quality_warning_reaches_completion_without_blocking_ready_proposal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = _CapturingDeck.run
    warning = "19枚目: 警告: 本文500字超過 本文が501字あります"

    def run(
        self: _CapturingDeck, input: ProposalDeckInput, ctx: SkillContext
    ) -> ProposalDeckOutput:
        return original(self, input, ctx).model_copy(update={"quality_warnings": [warning]})

    monkeypatch.setattr(_CapturingDeck, "run", run)
    job, ctx, target, _slack, store, _posts = _build_job(
        monkeypatch, tmp_path, ready=True, deferred=False
    )
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert state.status == "done" and state.proposal_status == "ready"
    assert warning in state.result_message and warning in state.warnings
    assert "proposal_quality_warnings" not in state.verification_issues
    target.discard()


def test_builder_transfers_bu2_inputs_through_json_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.skills.proposal_deck.test_bu2_content import bu2_payload

    _configure_builder_test(monkeypatch, media_configured=False)
    deck = _CapturingDeck()
    builder = ProposalBuilderSkill(search=object(), deck=deck, account_db_path="unused.xlsx")
    request = _input(_research(sector="食品"), **bu2_payload())
    request = type(request).model_validate_json(request.model_dump_json())
    builder.run(
        request,
        SkillContext(
            metadata={"request_text": "2本、100001回、2.5円/回、4週間、4回接触、60%、10%"}
        ),
    )
    assert deck.inputs[0].plans == request.plans
    assert deck.inputs[0].simulation == request.simulation
    assert deck.inputs[0].hearing_items == request.hearing_items
    assert deck.inputs[0].estimate_terms == request.estimate_terms


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("request_text", [None, "100001回、理解率60%"])
def test_builder_filters_sync_and_background_inputs(
    monkeypatch: pytest.MonkeyPatch, background: bool, request_text: str | None
) -> None:
    from teamagent.adapters.proposal_job_store import ProposalJobStore
    from teamagent.skills.proposal_builder.schema import ProposalBuilderSubmitInput
    from teamagent.skills.proposal_builder.skill import ProposalBuilderSubmitSkill
    from tests.skills.proposal_builder.test_async_jobs import _ctx
    from tests.skills.proposal_deck.test_bu2_content import bu2_payload

    _configure_builder_test(monkeypatch, media_configured=False)
    monkeypatch.setenv("PROPOSAL_BUILDER_ALLOWED_EMAILS", "*")
    deck = _CapturingDeck()
    builder = ProposalBuilderSkill(search=object(), deck=deck, account_db_path="unused.xlsx")
    request = _input(_research(), **bu2_payload())
    ctx = _ctx()
    if request_text is not None:
        ctx.metadata["request_text"] = request_text
    if background:
        pending = []
        submit = ProposalBuilderSubmitSkill(
            builder_factory=lambda: builder,
            store=ProposalJobStore(table_name="", memory={}),
            thread_launcher=lambda target, _name: pending.append(target),
            input_validator=lambda _: None,
            heartbeat_seconds=0,
        )
        accepted = submit.run(ProposalBuilderSubmitInput.model_validate(request.model_dump()), ctx)
        assert accepted.status == "queued"
        # 後続の会話や呼出しで metadata が変わっても job の原文は変わらない。
        ctx.metadata["request_text"] = "2本、2.5円、4回、10%"
        pending.pop()()
    else:
        builder.run(request, ctx)
    result = deck.inputs[0]
    assert result.plans[0].count is None
    assert result.plans[0].unit_price_yen is None
    assert result.plans[0].period == ""
    assert result.estimate_terms.management_fee_rate is None
    assert result.simulation is not None
    assert result.simulation.contacts is None
    assert result.simulation.views == (100001 if request_text else None)
    assert result.simulation.understanding_rate == (
        request.simulation.understanding_rate if request_text else None
    )


@pytest.mark.parametrize("client", [None, "クライアント", "指定宛名"])
@pytest.mark.parametrize("confidential", [False, True])
def test_client_falls_back_to_research_brand_and_keeps_confidentiality(
    monkeypatch: pytest.MonkeyPatch, client: str | None, confidential: bool
) -> None:
    _configure_builder_test(monkeypatch, media_configured=False)
    deck = _CapturingDeck()
    builder = ProposalBuilderSkill(search=object(), deck=deck, account_db_path="unused.xlsx")  # type: ignore[arg-type]
    brand = "合成ブランド"
    kwargs = {"client_name": client} if client else {}
    builder.run(
        _input(_research(brand=brand), confidential_product_name=confidential, **kwargs),
        SkillContext(),
    )
    actual = deck.inputs[0].auxiliary_placeholders["PB-CLIENT-NAME"]
    if client == "指定宛名":
        assert actual == client
    elif confidential:
        assert actual and brand not in actual and actual != "クライアント"
    else:
        assert actual == brand
