"""submit の同時実行アドミッション制御（C4）。

固定する不変量:
  1. 上限までは通常どおり queued
  2. 上限超過は **ジョブを作らず** busy（台帳にも増えない・retry_after を伴う）
  3. 走行中ジョブが終われば枠は戻る（release 漏れで永久 busy にしない）
  4. 背景スレッドの起動・台帳書込に失敗しても枠は戻る
  5. 上限は OMIYAGE_MAX_CONCURRENT_JOBS で変えられ、共有インスタンスで効く
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest

from teamagent.adapters.proposal_job_store import ProposalJobStore
from teamagent.adapters.tiktok_scraper import TikTokScrapeError
from teamagent.skills.base import SkillContext
from teamagent.skills.omiyage_report.schema import OmiyageReportSubmitInput
from teamagent.skills.omiyage_report.skill import (
    JobAdmission,
    OmiyageReportSubmitSkill,
    _configured_max_concurrent_jobs,
    reset_job_admission,
)


class _ManualLauncher:
    """target を保持するだけで走らせない（submit の即答部分だけを見る）。"""

    def __init__(self) -> None:
        self.targets: list[Callable[[], None]] = []

    def __call__(self, target: Callable[[], None], name: str) -> None:
        self.targets.append(target)

    def run_one(self) -> None:
        self.targets.pop(0)()


def _offline_searcher(*args: Any, **kwargs: Any) -> Any:
    """全軸失敗で背景実行を即終了させる（ネットワークにも Slack にも触らない）。"""

    raise TikTokScrapeError("TIKTOK_SEARCH_FAILED")


def _input(brand: str = "ブランドA") -> OmiyageReportSubmitInput:
    return OmiyageReportSubmitInput(
        brand=brand,
        competitors=["競合B"],
        keywords=["シャンプー"],
    )


def _skill(
    *,
    store: ProposalJobStore,
    launcher: Any,
    admission: JobAdmission | None = None,
) -> OmiyageReportSubmitSkill:
    return OmiyageReportSubmitSkill(
        store=store,
        thread_launcher=launcher,
        admission=admission,
        searcher=_offline_searcher,
        heartbeat_seconds=0,
    )


def _ctx() -> SkillContext:
    return SkillContext(request_id="req-admission", user_id="u@example.com", metadata={})


def test_accepts_up_to_the_limit_then_returns_busy_without_creating_a_job() -> None:
    memory: dict[str, dict[str, Any]] = {}
    store = ProposalJobStore(table_name="", memory=memory)
    launcher = _ManualLauncher()
    skill = _skill(store=store, launcher=launcher, admission=JobAdmission(2))

    first = skill.run(_input("A"), _ctx())
    second = skill.run(_input("B"), _ctx())
    third = skill.run(_input("C"), _ctx())

    assert [first.status, second.status] == ["queued", "queued"]
    assert third.status == "busy"
    assert third.job_id == ""
    assert third.retry_after_seconds > 0
    assert "2件" in third.message
    assert "ジョブは作成していません" in third.message
    assert "omiyage_report_status" in third.message
    # 3 本目はジョブを作っていない＝台帳は 2 行のまま。
    assert len(memory) == 2
    assert len(launcher.targets) == 2


def test_finished_job_returns_its_slot() -> None:
    memory: dict[str, dict[str, Any]] = {}
    store = ProposalJobStore(table_name="", memory=memory)
    launcher = _ManualLauncher()
    skill = _skill(store=store, launcher=launcher, admission=JobAdmission(1))

    assert skill.run(_input("A"), _ctx()).status == "queued"
    assert skill.run(_input("B"), _ctx()).status == "busy"

    launcher.run_one()  # 1 本目の背景実行が終わる（全軸失敗で終わっても枠は返る）
    assert [row["status"] for row in memory.values()] == ["failed"]

    assert skill.run(_input("C"), _ctx()).status == "queued"


def test_thread_start_failure_returns_the_slot() -> None:
    store = ProposalJobStore(table_name="", memory={})

    def _broken_launcher(target: Callable[[], None], name: str) -> None:
        raise RuntimeError("cannot start thread")

    admission = JobAdmission(1)
    skill = _skill(store=store, launcher=_broken_launcher, admission=admission)

    failed = skill.run(_input("A"), _ctx())

    assert failed.status == "failed"
    # 枠が返っていなければここは False になる。
    assert admission.try_acquire() is True


def test_ledger_write_failure_returns_the_slot() -> None:
    class _BrokenStore(ProposalJobStore):
        def create_job(self, job_id: str, request_summary: dict[str, Any]) -> None:
            raise RuntimeError("ledger down")

    admission = JobAdmission(1)
    skill = _skill(
        store=_BrokenStore(table_name="", memory={}),
        launcher=_ManualLauncher(),
        admission=admission,
    )

    with pytest.raises(RuntimeError):
        skill.run(_input("A"), _ctx())

    assert admission.try_acquire() is True


def test_limit_is_configurable_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMIYAGE_MAX_CONCURRENT_JOBS", raising=False)
    assert _configured_max_concurrent_jobs() == 3

    monkeypatch.setenv("OMIYAGE_MAX_CONCURRENT_JOBS", "5")
    assert _configured_max_concurrent_jobs() == 5
    assert JobAdmission().limit == 5

    # 範囲外はクランプされる（0 本や無制限を env から作れない）。
    monkeypatch.setenv("OMIYAGE_MAX_CONCURRENT_JOBS", "0")
    assert _configured_max_concurrent_jobs() == 1
    monkeypatch.setenv("OMIYAGE_MAX_CONCURRENT_JOBS", "999")
    assert _configured_max_concurrent_jobs() == 16


def test_admission_is_shared_across_skill_instances() -> None:
    """本番は tool 呼び出しごとに Skill を作り直す＝共有インスタンスでなければ効かない。"""

    reset_job_admission(1)
    store = ProposalJobStore(table_name="", memory={})
    launcher = _ManualLauncher()

    first = _skill(store=store, launcher=launcher).run(_input("A"), _ctx())
    second = _skill(store=store, launcher=launcher).run(_input("B"), _ctx())

    assert first.status == "queued"
    assert second.status == "busy"


def test_release_is_bounded_by_the_limit() -> None:
    """二重 release は静かに上限を緩めず ValueError で顕在化する。"""

    admission = JobAdmission(1)
    assert admission.try_acquire() is True
    admission.release()
    with pytest.raises(ValueError):
        admission.release()


def test_concurrent_submits_admit_exactly_the_limit() -> None:
    """同時 submit でも通るのは上限本数ちょうど（カウンタの競合で緩まない）。"""

    store = ProposalJobStore(table_name="", memory={})
    admission = JobAdmission(2)
    launcher = _ManualLauncher()
    gate = threading.Barrier(4, timeout=10)
    statuses: list[str] = []
    lock = threading.Lock()

    def _submit(index: int) -> None:
        gate.wait()
        out = _skill(store=store, launcher=launcher, admission=admission).run(
            _input(f"brand-{index}"), _ctx()
        )
        with lock:
            statuses.append(out.status)

    threads = [threading.Thread(target=_submit, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(statuses) == ["busy", "busy", "queued", "queued"]
