"""publish の権威ゲート: USE_PROPOSAL_DECK_PUBLISH OFF なら入力 True でも公開しない。"""

import pytest

from teamagent.skills.proposal_deck.skill import ProposalDeckSkill


@pytest.fixture()
def skill() -> ProposalDeckSkill:
    return ProposalDeckSkill()


def _publish(skill: ProposalDeckSkill, *, publish_artifact: bool | None) -> str | None:
    return skill._publish_if_enabled(
        "/tmp/nonexistent.pptx",
        "テスト製品",
        "req-test",
        publish_artifact=publish_artifact,
    )


def test_gate_off_blocks_forced_publish(
    skill: ProposalDeckSkill, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("USE_PROPOSAL_DECK_PUBLISH", raising=False)
    assert _publish(skill, publish_artifact=True) is None


def test_gate_off_blocks_default_publish(
    skill: ProposalDeckSkill, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("USE_PROPOSAL_DECK_PUBLISH", raising=False)
    assert _publish(skill, publish_artifact=None) is None


def test_gate_on_still_honors_explicit_false(
    skill: ProposalDeckSkill, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USE_PROPOSAL_DECK_PUBLISH", "1")
    assert _publish(skill, publish_artifact=False) is None
