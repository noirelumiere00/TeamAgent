"""83 枚提案書の同時実行の上限（PROPOSAL_MAX_CONCURRENT_JOBS・既定 2）。

全員公開（10-14 小俣さん決定・動画チェックの Gemini 切替の後）の前提。固定すること:
- 枠が埋まっていれば job row も thread も作らずに断る（即答・内部語なし）
- 枠は thread が終わったら返る。thread の起動に失敗・受付の途中で落ちたときも返る（二重には返さない）
- 枠はプロセス共有（skill のインスタンスが呼び出しごとに作られても上限を共有する）
変異: acquire を外す・thread の finally で返さない・起動失敗で返さない、でそれぞれ赤。
"""

from __future__ import annotations

import threading
from datetime import date
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.skills.base import SkillContext
from teamagent.skills.proposal_builder import skill as proposal_skill
from teamagent.skills.proposal_builder.schema import ProposalBuilderSubmitInput
from teamagent.skills.proposal_builder.skill import ALLOWED_EMAILS_ENV, ProposalBuilderSubmitSkill

ME = "s-komata@vectorinc.co.jp"


class _Launcher:
    """本体は走らせずに預かる（run_all で後から走らせる）。fail=True なら起動に失敗する。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.targets: list[Any] = []

    def __call__(self, target: Any, name: str) -> None:
        if self.fail:
            raise RuntimeError("thread を起動できない")
        self.targets.append(target)

    def run_all(self) -> None:
        targets, self.targets = self.targets, []
        for target in targets:
            target()


def _skill(
    launcher: _Launcher,
    memory: dict[str, dict[str, Any]],
    slots: tuple[threading.BoundedSemaphore, int] | None,
    store: ProposalJobStore | None = None,
) -> ProposalBuilderSubmitSkill:
    skill = ProposalBuilderSubmitSkill(
        builder_factory=lambda: None,  # type: ignore[arg-type,return-value]
        store=store or ProposalJobStore(table_name="", memory=memory),
        thread_launcher=launcher,
        input_validator=lambda _input: None,
        heartbeat_seconds=0,
        retry_after_seconds=30,
        job_slots=slots,
    )
    # 本体（調査・生成）は走らせない。枠の出し入れだけを見る。
    skill._run_background = lambda *a, **k: None  # type: ignore[method-assign]
    return skill


def _input() -> ProposalBuilderSubmitInput:
    return ProposalBuilderSubmitInput(
        gemini_json={"A": {"client_name": "テスト社"}}, posting_start_date=date(2026, 9, 1)
    )


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="pb-cap",
        user_id=ME,
        metadata={
            "user_email": ME,
            "identity_verified": True,
            "channel_id": "D1",
            "thread_ts": "1.2",
        },
    )


@pytest.fixture(autouse=True)
def allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ALLOWED_EMAILS_ENV, ME)


def test_full_slots_refuse_without_job_or_thread() -> None:
    launcher, memory = _Launcher(), {}
    skill = _skill(launcher, memory, (threading.BoundedSemaphore(1), 1))
    first = skill.run(_input(), _ctx())
    second = skill.run(_input(), _ctx())
    assert first.status == "queued" and len(memory) == 1 and len(launcher.targets) == 1
    assert second.status == "failed" and second.job_id == "" and second.retry_after_seconds == 0
    assert "作成中" in second.message and "job" not in second.message.lower()
    assert len(memory) == 1 and len(launcher.targets) == 1  # 断った回は何も作らない


def test_slot_returns_when_the_job_ends() -> None:
    launcher, memory = _Launcher(), {}
    skill = _skill(launcher, memory, (threading.BoundedSemaphore(1), 1))
    assert skill.run(_input(), _ctx()).status == "queued"
    launcher.run_all()  # 本体が終わる（finally で枠を返す）
    assert skill.run(_input(), _ctx()).status == "queued"


def test_launch_failure_returns_the_slot_once() -> None:
    sem = threading.BoundedSemaphore(1)
    failing = _skill(_Launcher(fail=True), {}, (sem, 1))
    assert failing.run(_input(), _ctx()).status == "failed"
    ok_launcher = _Launcher()
    assert _skill(ok_launcher, {}, (sem, 1)).run(_input(), _ctx()).status == "queued"
    # 二重に返していれば BoundedSemaphore が ValueError になる（ここまで来れば一度だけ）
    ok_launcher.run_all()


def test_store_failure_returns_the_slot() -> None:
    class _BrokenStore(ProposalJobStore):
        def create_job(self, *a: Any, **k: Any) -> None:  # type: ignore[override]
            raise RuntimeError("DynamoDB に書けない")

    sem = threading.BoundedSemaphore(1)
    broken = _skill(_Launcher(), {}, (sem, 1), store=_BrokenStore(table_name="", memory={}))
    with pytest.raises(RuntimeError):
        broken.run(_input(), _ctx())
    assert _skill(_Launcher(), {}, (sem, 1)).run(_input(), _ctx()).status == "queued"


def test_slots_are_shared_across_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPOSAL_MAX_CONCURRENT_JOBS", "1")
    monkeypatch.setattr(proposal_skill, "_job_slots", None)
    a = _skill(_Launcher(), {}, None)
    b = _skill(_Launcher(), {}, None)
    assert a.run(_input(), _ctx()).status == "queued"
    assert b.run(_input(), _ctx()).status == "failed"


@pytest.mark.parametrize(("raw", "expected"), [(None, 2), ("3", 3), ("0", 1), ("99", 8), ("x", 2)])
def test_limit_from_env(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int) -> None:
    if raw is None:
        monkeypatch.delenv("PROPOSAL_MAX_CONCURRENT_JOBS", raising=False)
    else:
        monkeypatch.setenv("PROPOSAL_MAX_CONCURRENT_JOBS", raw)
    assert proposal_skill._configured_max_concurrent_jobs() == expected
