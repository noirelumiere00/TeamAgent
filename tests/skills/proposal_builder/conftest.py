"""従来の Skill 単体契約は通知 OFF。署名付き受け入れテストは明示 ON。"""

import pytest


@pytest.fixture(autouse=True)
def legacy_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_LONG_JOB_NOTIFY", "0")


@pytest.fixture(autouse=True)
def fresh_job_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """同時実行の枠はプロセス共有なので、テストごとに作り直す（起動役が target を走らせない試験がある）。"""
    from teamagent.skills.proposal_builder import skill as proposal_skill

    monkeypatch.setattr(proposal_skill, "_job_slots", None)
    monkeypatch.setattr(proposal_skill, "_job_slots_limit", 0)
    monkeypatch.setenv("PROPOSAL_MAX_CONCURRENT_JOBS", "8")
