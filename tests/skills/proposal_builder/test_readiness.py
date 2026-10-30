"""描画検査のerror・不能・必須欄不足はreadyにしない。"""

from pathlib import Path
from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder.schema import ProposalBuilderStatusInput
from teamagent.skills.proposal_builder.skill import ProposalBuilderStatusSkill
from teamagent.skills.proposal_deck.schema import ProposalDeckInput, ProposalDeckOutput
from tests.skills.proposal_builder.test_research_delivery_recovery import _build_job
from tests.skills.proposal_builder.test_tiktok_enrichment import _CapturingDeck


@pytest.mark.parametrize(
    "metadata,issue",
    [
        ({}, "pptx_l1_unavailable"),
        ({"qa_error": "RuntimeError", "field_issues": []}, "pptx_l1_unavailable"),
        ({"deck_qa": {"error_count": 1}, "field_issues": []}, "pptx_l1_errors:1"),
        (
            {"deck_qa": {"error_count": 0}, "field_issues": ["auxiliary_missing:PB-ACCOUNTS"]},
            "auxiliary_missing:PB-ACCOUNTS",
        ),
        (
            {"deck_qa": {"error_count": 0}, "field_issues": ["required_image_missing:6:2"]},
            "required_image_missing:6:2",
        ),
        ({"deck_qa": {"error_count": 0, "warn_count": 3}, "field_issues": []}, None),
        (
            {
                "deck_qa": {"error_count": 0},
                "field_issues": [],
                "field_warnings": ["要対応: 画像未挿入 1 枠（54枚目）"],
            },
            None,
        ),
    ],
)
def test_render_inspection_controls_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    metadata: dict[str, Any],
    issue: str | None,
) -> None:
    original = _CapturingDeck.run

    def run(
        self: _CapturingDeck, input: ProposalDeckInput, ctx: SkillContext
    ) -> ProposalDeckOutput:
        return original(self, input, ctx).model_copy(
            update={
                "render_metadata": metadata,
                "quality_warnings": metadata.get("field_warnings", []),
            }
        )

    monkeypatch.setattr(_CapturingDeck, "run", run)
    job, ctx, target, slack, store, _ = _build_job(
        monkeypatch, tmp_path, deferred=False, ready=True
    )
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert state.status == "done"
    assert state.proposal_status == ("draft" if issue else "ready")
    if issue:
        assert issue in state.verification_issues
        assert any("要対応" in warning for warning in state.warnings)
        assert not any(upload["title"].endswith(".pptx") for upload in slack.uploads)
    for warning in metadata.get("field_warnings", []):
        assert warning in state.warnings
        assert warning in state.result_message
    target.discard()
