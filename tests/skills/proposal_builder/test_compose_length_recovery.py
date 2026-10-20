"""本番の2枠超過を、生成から非同期完成通知までフェイクで再現する。"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from teamagent.mcp_gateway.server import _format_proposal_completion
from teamagent.skills.proposal_builder.schema import ProposalBuilderStatusInput
from teamagent.skills.proposal_builder.skill import ProposalBuilderStatusSkill
from teamagent.skills.proposal_deck.skill import ProposalDeckSkill
from tests.skills.proposal_builder.test_research_delivery_recovery import _build_job
from tests.skills.proposal_builder.test_tiktok_enrichment import _CapturingDeck
from tests.skills.proposal_deck.test_proposal_deck_skill import _full_composer_json, _resp


@pytest.mark.parametrize("deferred", [False, True])
def test_two_oversized_slots_complete_and_are_listed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deferred: bool
) -> None:
    payload = json.loads(_full_composer_json())
    payload["placeholders"].update({"87": "あ" * 21, "90": "い" * 23})
    original = payload["placeholders"].copy()
    bedrock = MagicMock(model_id="jp.anthropic.claude-sonnet-4-6")
    bedrock.converse.return_value = _resp(json.dumps(payload, ensure_ascii=False))
    composer = ProposalDeckSkill(bedrock=bedrock)
    captured = {}
    fake_run = _CapturingDeck.run

    def run(self, input, ctx):
        # ここでは文字数の復旧と通知配線を検証。根拠・守秘検証は専用テストで担保する。
        deck, cost, warnings = composer._compose(
            input.model_copy(update={"enforce_provenance": False}), ctx
        )
        captured.update(deck.placeholders)
        return fake_run(self, input, ctx).model_copy(
            update={"total_cost_usd": cost, "length_warnings": warnings}
        )

    monkeypatch.setattr(_CapturingDeck, "run", run)
    job, ctx, target, _slack, store, _ = _build_job(
        monkeypatch, tmp_path, deferred=deferred, ready=True
    )
    state = ProposalBuilderStatusSkill(store=store).run(ProposalBuilderStatusInput(job_id=job), ctx)
    assert state.status == "done"
    assert state.proposal_status == "ready"
    assert bedrock.converse.call_count == 3
    for pid in (87, 90):
        assert 1 <= len(captured[pid]) <= 18
        assert "要確認" in captured[pid]
        assert any(f"{{{pid}}}" in warning for warning in state.warnings)
        assert f"{{{pid}}}" in _format_proposal_completion(state)
    assert {str(pid): text for pid, text in captured.items() if pid not in (87, 90)} == {
        pid: text for pid, text in original.items() if pid not in ("87", "90")
    }
    target.discard()


def test_completion_lists_all_adjustments_even_with_long_result_message() -> None:
    from types import SimpleNamespace

    warnings = [f"文字数調整 {{{pid}}}: 要確認の既定文に置換" for pid in range(60, 93)]
    completion = _format_proposal_completion(
        SimpleNamespace(
            status="done",
            result_message="調査結果" * 1000 + "\n" + "\n".join(warnings),
            warnings=warnings,
            proposal_status="ready",
            filled_count=95,
            skipped_count=0,
            pptx_url=None,
        )
    )
    assert all(completion.count(warning) == 1 for warning in warnings)
