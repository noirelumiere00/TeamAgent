"""submit / status の門番と非同期境界。

本番の失敗モードを再現する:
- ``identity_verified`` が無い（OC 申告だけの呼び出し）
- ``CLIP_PROPOSAL_USERS`` 未設定・空文字（＝全員拒否でなければ全社に開く事故）
- ``thread_ts`` が無い（チャンネル直投稿になる）
- 他人の ``job_id`` で status を引く
- 日次上限に当たる
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

import pytest

from teamagent.skills.base import SkillContext
from teamagent.skills.clip_proposal.limits import DailyQuota
from teamagent.skills.clip_proposal.notices import has_all_required_notices
from teamagent.skills.clip_proposal.schema import (
    ClipProposalStatusInput,
    ClipProposalSubmitInput,
)
from teamagent.skills.clip_proposal.skill import (
    CLIP_JOB_KIND,
    ActiveJobIndex,
    ClipProposalStatusSkill,
    ClipProposalSubmitSkill,
    allowed_users,
    enabled,
    new_clip_job_id,
    requester_fingerprint,
    resolve_delivery_target,
    verify_requester,
)

from .fixtures import sample_analysis

_ME = "komata@example.com"
_OTHER = "someone@example.com"


class _MemoryStore:
    """ProposalJobStore の最小フェイク（本番と同じ「行が無い」形を返す）。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def create_job(self, job_id: str, request_summary: dict[str, Any]) -> None:
        self.rows[job_id] = {
            "status": "queued",
            "request_summary": json.dumps(request_summary, ensure_ascii=False),
        }

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.rows.get(job_id)

    def mark_running(self, job_id: str) -> bool:
        self.rows[job_id]["status"] = "running"
        return True

    def mark_done(self, job_id: str, result_json: str) -> bool:
        self.rows[job_id].update(status="done", result_json=result_json)
        return True

    def mark_failed(
        self, job_id: str, error_code: str, *, expected_statuses: tuple[str, ...] = ()
    ) -> bool:
        # 本番の ProposalJobStore は失敗コードを ``error_code`` 列に置く。
        # ここを ``failure_code`` にすると、実装の読み違いをテストが隠してしまう。
        self.rows[job_id].update(status="failed", error_code=error_code)
        return True


class _InlineLauncher:
    """daemon thread の代わりにその場で走らせる（非同期境界の検査は別テスト）。"""

    def __init__(self) -> None:
        self.launched = 0

    def __call__(self, target: Callable[[], None], name: str) -> None:
        self.launched += 1
        target()


class _StubAnalyzer:
    cost_cap_usd = 1.0

    def __init__(self, *, clip_count: int = 10, raises: Exception | None = None) -> None:
        self.clip_count = clip_count
        self.raises = raises
        self.calls = 0

    def run_for_request(self, input: Any, ctx: SkillContext) -> Any:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return sample_analysis(clip_count=self.clip_count, client_name=input.client_name)


@pytest.fixture(autouse=True)
def _roster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIP_PROPOSAL_USERS", _ME)
    monkeypatch.delenv("CLIP_PROPOSAL_MAX_JOBS_PER_DAY", raising=False)
    monkeypatch.delenv("CLIP_PROPOSAL_MAX_JOBS_PER_DAY_TOTAL", raising=False)


def _ctx(
    *,
    email: str = _ME,
    verified: bool = True,
    slack_user_id: str = "U_ME",
    channel_id: str = "D_ME",
    thread_ts: str = "1757000000.000100",
) -> SkillContext:
    return SkillContext(
        request_id="req-clip-1",
        user_id=slack_user_id,
        metadata={
            "identity_verified": verified,
            "verified_slack_user_id": slack_user_id,
            "user_email": email,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
        },
    )


# ---------------------------------------------------------------------------
# 既定 OFF
# ---------------------------------------------------------------------------


def test_feature_is_off_unless_the_flag_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_CLIP_PROPOSAL_TOOLS", raising=False)
    assert not enabled()
    monkeypatch.setenv("USE_CLIP_PROPOSAL_TOOLS", "true")
    assert enabled()


def test_skills_are_not_registered_yet() -> None:
    """MCP 露出と OC の 4 点セットは便C。台帳を動かさないので registry へ載せない。"""

    from teamagent.skills.base import SkillRegistry

    assert "clip_proposal_submit" not in SkillRegistry.list_all()
    assert "clip_proposal_status" not in SkillRegistry.list_all()


# ---------------------------------------------------------------------------
# 本人限定（安全装置）
# ---------------------------------------------------------------------------


def test_unverified_identity_is_refused() -> None:
    with pytest.raises(PermissionError):
        verify_requester(_ctx(verified=False))


def test_missing_verified_slack_user_id_is_refused() -> None:
    with pytest.raises(PermissionError):
        verify_requester(_ctx(slack_user_id=""))


@pytest.mark.parametrize("roster", ["", "   ", ","])
def test_empty_roster_denies_everyone(monkeypatch: pytest.MonkeyPatch, roster: str) -> None:
    monkeypatch.setenv("CLIP_PROPOSAL_USERS", roster)
    assert allowed_users() == frozenset()
    with pytest.raises(PermissionError):
        verify_requester(_ctx())


def test_unset_roster_denies_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIP_PROPOSAL_USERS", raising=False)
    with pytest.raises(PermissionError):
        verify_requester(_ctx())


def test_user_outside_the_roster_is_refused() -> None:
    with pytest.raises(PermissionError):
        verify_requester(_ctx(email=_OTHER))


def test_fingerprint_does_not_leak_the_address() -> None:
    fingerprint = requester_fingerprint(_ME)
    assert _ME not in fingerprint
    assert fingerprint != requester_fingerprint(_OTHER)
    assert fingerprint == requester_fingerprint(" Komata@Example.com ")


# ---------------------------------------------------------------------------
# 配達先（thread_ts 無しは本人 DM 固定へ倒す）
# ---------------------------------------------------------------------------


def test_thread_delivery_needs_both_channel_and_thread_ts() -> None:
    target, channel, thread_ts = resolve_delivery_target(
        {"channel_id": "C_TEAM", "thread_ts": "1757000000.000100"}
    )
    assert (target, channel, thread_ts) == ("thread", "C_TEAM", "1757000000.000100")


@pytest.mark.parametrize(
    "metadata",
    [
        {"channel_id": "C_TEAM"},
        {"channel_id": "C_TEAM", "thread_ts": ""},
        {"thread_ts": "1757000000.000100"},
        {},
    ],
)
def test_missing_thread_ts_falls_back_to_dm(metadata: dict[str, Any]) -> None:
    target, channel, thread_ts = resolve_delivery_target(metadata)
    assert target == "dm"
    assert channel == ""
    assert thread_ts == ""


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


def _submit_skill(
    store: _MemoryStore,
    *,
    analyzer: Any = None,
    quota: DailyQuota | None = None,
    launcher: Any = None,
    deliverer: Any = None,
    active_jobs: ActiveJobIndex | None = None,
) -> ClipProposalSubmitSkill:
    return ClipProposalSubmitSkill(
        store=store,  # type: ignore[arg-type]
        quota=quota or DailyQuota(),
        active_jobs=active_jobs or ActiveJobIndex(),
        analyzer=analyzer,
        deck_builder=lambda analysis, out_dir, request_id: f"{out_dir}/clip.pptx",
        deliverer=deliverer or (lambda path, comment, ctx: (True, "thread")),
        thread_launcher=launcher or _InlineLauncher(),
    )


def test_submit_answers_immediately_with_the_client_name_echoed() -> None:
    store = _MemoryStore()
    skill = _submit_skill(store, analyzer=_StubAnalyzer())
    output = skill.run(ClipProposalSubmitInput(client_name="初田製作所"), _ctx())
    assert output.status == "queued"
    assert output.job_id.startswith("clp_")
    assert "「初田製作所」" in output.message
    assert store.rows[output.job_id]["status"] == "done"


def test_submit_records_only_a_fingerprint_and_never_the_transcript() -> None:
    store = _MemoryStore()
    skill = _submit_skill(store, analyzer=_StubAnalyzer())
    output = skill.run(ClipProposalSubmitInput(client_name="初田製作所"), _ctx())
    summary = json.loads(store.rows[output.job_id]["request_summary"])
    assert summary["kind"] == CLIP_JOB_KIND
    assert summary["requester"] == requester_fingerprint(_ME)
    assert _ME not in json.dumps(summary)
    result = json.loads(store.rows[output.job_id]["result_json"])
    assert "transcript" not in json.dumps(result)


def test_submit_result_carries_every_notice() -> None:
    store = _MemoryStore()
    skill = _submit_skill(store, analyzer=_StubAnalyzer())
    output = skill.run(ClipProposalSubmitInput(client_name="初田製作所"), _ctx())
    result = json.loads(store.rows[output.job_id]["result_json"])
    assert has_all_required_notices("\n".join(result["notices"]))
    assert has_all_required_notices(result["message"])


def test_partial_result_when_some_clips_were_dropped() -> None:
    store = _MemoryStore()
    skill = _submit_skill(store, analyzer=_StubAnalyzer(clip_count=7))
    output = skill.run(ClipProposalSubmitInput(client_name="初田製作所"), _ctx())
    result = json.loads(store.rows[output.job_id]["result_json"])
    assert result["status"] == "partial"
    assert result["clip_count"] == 7


def test_daily_limit_defers_without_creating_a_job() -> None:
    store = _MemoryStore()
    quota = DailyQuota()
    skill = _submit_skill(store, analyzer=_StubAnalyzer(), quota=quota)
    for _ in range(2):
        assert skill.run(ClipProposalSubmitInput(), _ctx()).status == "queued"
    created = len(store.rows)
    output = skill.run(ClipProposalSubmitInput(), _ctx())
    assert output.status == "deferred"
    assert output.job_id == ""
    assert len(store.rows) == created  # ジョブを作っていない
    assert "小俣さん" in output.message


def test_failed_job_does_not_give_the_daily_slot_back() -> None:
    store = _MemoryStore()
    quota = DailyQuota()
    skill = _submit_skill(
        store, analyzer=_StubAnalyzer(raises=RuntimeError("CLIP_TEMPLATE_UNAVAILABLE")), quota=quota
    )
    first = skill.run(ClipProposalSubmitInput(), _ctx())
    assert store.rows[first.job_id]["status"] == "failed"
    skill.run(ClipProposalSubmitInput(), _ctx())
    assert skill.run(ClipProposalSubmitInput(), _ctx()).status == "deferred"


def test_failure_code_is_a_safe_marker() -> None:
    store = _MemoryStore()
    skill = _submit_skill(
        store,
        analyzer=_StubAnalyzer(
            raises=RuntimeError("boom at /srv/secret/path CLIP_TEMPLATE_UNAVAILABLE")
        ),
    )
    output = skill.run(ClipProposalSubmitInput(), _ctx())
    assert store.rows[output.job_id]["error_code"] == "CLIP_TEMPLATE_UNAVAILABLE"


def test_submit_refuses_a_user_outside_the_roster() -> None:
    skill = _submit_skill(_MemoryStore(), analyzer=_StubAnalyzer())
    with pytest.raises(PermissionError):
        skill.run(ClipProposalSubmitInput(), _ctx(email=_OTHER))


def test_busy_output_never_asks_for_a_resubmission() -> None:
    skill = _submit_skill(_MemoryStore(), analyzer=_StubAnalyzer())
    output = skill.busy_output(position=2, wait_minutes=30)
    assert output.status == "busy"
    assert output.retry_after_seconds >= 1800
    assert "もう一度送っていただく必要はありません" in output.message


def test_submit_runs_the_pipeline_off_the_request_thread() -> None:
    """受付は即答で、実処理は別スレッド（本番は daemon thread）。"""

    store = _MemoryStore()
    seen: list[str] = []

    def launcher(target: Callable[[], None], name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        thread.join(timeout=5)
        seen.append(name)

    skill = _submit_skill(store, analyzer=_StubAnalyzer(), launcher=launcher)
    output = skill.run(ClipProposalSubmitInput(), _ctx())
    assert seen == [f"clip-proposal-{output.job_id}"]


def test_failed_status_reads_the_stores_error_code_column() -> None:
    """``ProposalJobStore`` は ``error_code`` 列に書く（``failure_code`` ではない）。"""

    store = _MemoryStore()
    submitted = _submit_skill(
        store, analyzer=_StubAnalyzer(raises=RuntimeError("CLIP_TEMPLATE_UNAVAILABLE"))
    ).run(ClipProposalSubmitInput(), _ctx())
    status = ClipProposalStatusSkill(store=store).run(  # type: ignore[arg-type]
        ClipProposalStatusInput(job_id=submitted.job_id), _ctx()
    )
    assert status.status == "failed"
    assert status.error_code == "CLIP_TEMPLATE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# 重複 submit
# ---------------------------------------------------------------------------


class _BlockingLauncher:
    """背景処理を走らせずに保持する（走行中の重複 submit を再現する）。"""

    def __init__(self) -> None:
        self.pending: list[Callable[[], None]] = []

    def __call__(self, target: Callable[[], None], name: str) -> None:
        self.pending.append(target)

    def drain(self) -> None:
        for target in self.pending:
            target()
        self.pending.clear()


def test_duplicate_submit_returns_the_same_job_without_burning_a_slot() -> None:
    store = _MemoryStore()
    quota = DailyQuota()
    index = ActiveJobIndex()
    launcher = _BlockingLauncher()
    skill = _submit_skill(
        store, analyzer=_StubAnalyzer(), quota=quota, launcher=launcher, active_jobs=index
    )
    first = skill.run(ClipProposalSubmitInput(client_name="初田製作所", file_id="F1"), _ctx())
    second = skill.run(ClipProposalSubmitInput(client_name="初田製作所", file_id="F1"), _ctx())

    assert second.status == "queued"
    assert second.job_id == first.job_id
    assert len(store.rows) == 1  # 2 本目のジョブを作らない
    assert quota.snapshot(requester_fingerprint(_ME))[0] == 1  # 枠も消費しない
    assert "もう一度送っていただく必要はありません" in second.message


def test_a_different_material_is_not_treated_as_a_duplicate() -> None:
    store = _MemoryStore()
    launcher = _BlockingLauncher()
    skill = _submit_skill(
        store,
        analyzer=_StubAnalyzer(),
        quota=DailyQuota(),
        launcher=launcher,
        active_jobs=ActiveJobIndex(),
    )
    first = skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx())
    second = skill.run(ClipProposalSubmitInput(file_id="F2"), _ctx())
    assert first.job_id != second.job_id
    assert len(store.rows) == 2


def test_the_dedupe_slot_is_released_when_the_job_ends() -> None:
    store = _MemoryStore()
    index = ActiveJobIndex()
    launcher = _BlockingLauncher()
    skill = _submit_skill(
        store, analyzer=_StubAnalyzer(), quota=DailyQuota(), launcher=launcher, active_jobs=index
    )
    first = skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx())
    launcher.drain()
    second = skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx())
    assert second.job_id != first.job_id


def test_the_dedupe_slot_is_released_even_when_the_job_fails() -> None:
    store = _MemoryStore()
    launcher = _BlockingLauncher()
    skill = _submit_skill(
        store,
        analyzer=_StubAnalyzer(raises=RuntimeError("CLIP_TEMPLATE_UNAVAILABLE")),
        quota=DailyQuota(),
        launcher=launcher,
        active_jobs=ActiveJobIndex(),
    )
    first = skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx())
    launcher.drain()
    second = skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx())
    assert second.job_id != first.job_id


def test_another_user_with_the_same_file_id_is_not_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIP_PROPOSAL_USERS", f"{_ME},{_OTHER}")
    store = _MemoryStore()
    launcher = _BlockingLauncher()
    skill = _submit_skill(
        store,
        analyzer=_StubAnalyzer(),
        quota=DailyQuota(),
        launcher=launcher,
        active_jobs=ActiveJobIndex(),
    )
    mine = skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx())
    theirs = skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx(email=_OTHER))
    assert mine.job_id != theirs.job_id


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_returns_the_result_for_the_owner() -> None:
    store = _MemoryStore()
    submitted = _submit_skill(store, analyzer=_StubAnalyzer()).run(
        ClipProposalSubmitInput(client_name="初田製作所"), _ctx()
    )
    status = ClipProposalStatusSkill(store=store).run(  # type: ignore[arg-type]
        ClipProposalStatusInput(job_id=submitted.job_id), _ctx()
    )
    assert status.status == "done"
    assert status.result_status == "ready"
    assert status.clip_count == 10


def test_status_hides_even_the_existence_of_someone_elses_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _MemoryStore()
    submitted = _submit_skill(store, analyzer=_StubAnalyzer()).run(
        ClipProposalSubmitInput(), _ctx()
    )
    monkeypatch.setenv("CLIP_PROPOSAL_USERS", f"{_ME},{_OTHER}")
    status = ClipProposalStatusSkill(store=store).run(  # type: ignore[arg-type]
        ClipProposalStatusInput(job_id=submitted.job_id), _ctx(email=_OTHER)
    )
    assert status.status == "not_found"
    assert status.job_id == ""  # 存在も漏らさない
    assert status.error_code == "JOB_NOT_FOUND"


def test_status_without_a_job_id_never_reaches_someone_elses_job() -> None:
    store = _MemoryStore()
    _submit_skill(store, analyzer=_StubAnalyzer()).run(ClipProposalSubmitInput(), _ctx())
    lookups: list[str] = []

    def lookup(fingerprint: str) -> str | None:
        lookups.append(fingerprint)
        return None

    status = ClipProposalStatusSkill(store=store, recent_lookup=lookup).run(  # type: ignore[arg-type]
        ClipProposalStatusInput(), _ctx()
    )
    assert lookups == [requester_fingerprint(_ME)]
    assert status.status == "not_found"


def test_status_rejects_a_foreign_job_id_shape() -> None:
    with pytest.raises(ValueError):
        ClipProposalStatusInput(job_id="omy_" + "0" * 32)


def test_job_ids_are_unique_and_prefixed() -> None:
    ids = {new_clip_job_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(job_id.startswith("clp_") for job_id in ids)
