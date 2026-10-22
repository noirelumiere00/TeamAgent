"""Builder carries existing sector and one fixed creation date into composer input."""

from datetime import date
from pathlib import Path

import pytest

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


def test_builder_passes_existing_sector_and_creation_date(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_builder_test(monkeypatch, media_configured=False)
    deck = _CapturingDeck()
    builder = ProposalBuilderSkill(
        search=object(),
        deck=deck,  # type: ignore[arg-type]
        account_db_path="unused.xlsx",
    )
    builder.run(_input(_research(sector="食品"), proposal_date="2026-10-22"), SkillContext())
    assert deck.inputs[0].product_sector == "食品"
    assert deck.inputs[0].proposal_date == date(2026, 10, 22)
    assert deck.inputs[0].auxiliary_placeholders["PB-DATETIME"] == "2026年10月22日"
    assert deck.inputs[0].auxiliary_placeholders["PB-MONTH"] == "2026年09月"


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
