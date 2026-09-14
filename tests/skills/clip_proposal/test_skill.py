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
    JobSlots,
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


class _RecordingDeliverer:
    """配達先を **呼び出し側から引数で** 受け取る deliverer（本番の実体と同じ形）。

    deliverer が ctx から channel を自分で決められないことを型で固定する。
    """

    def __init__(self, *, delivered: bool = True) -> None:
        self.delivered = delivered
        self.calls: list[tuple[str, str, str]] = []

    def __call__(
        self,
        path: str,
        comment: str,
        ctx: SkillContext,
        target: str,
        channel_id: str,
        thread_ts: str,
    ) -> bool:
        self.calls.append((target, channel_id, thread_ts))
        return self.delivered


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
    monkeypatch.setenv("USE_CLIP_PROPOSAL_TOOLS", "1")
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
        {
            "identity_verified": True,
            "channel_id": "C_TEAM",
            "thread_ts": "1757000000.000100",
        }
    )
    assert (target, channel, thread_ts) == ("thread", "C_TEAM", "1757000000.000100")


@pytest.mark.parametrize(
    "metadata",
    [
        {"identity_verified": True, "channel_id": "C_TEAM"},
        {"identity_verified": True, "channel_id": "C_TEAM", "thread_ts": ""},
        {"identity_verified": True, "thread_ts": "1757000000.000100"},
        {},
    ],
)
def test_missing_thread_ts_falls_back_to_dm(metadata: dict[str, Any]) -> None:
    target, channel, thread_ts = resolve_delivery_target(metadata)
    assert target == "dm"
    assert channel == ""
    assert thread_ts == ""


@pytest.mark.parametrize("verified", [False, None, "true", 1])
def test_unverified_metadata_never_reaches_a_channel(verified: Any) -> None:
    """``identity_verified`` が True 以外なら、値が入っていても DM へ倒す。

    ``mcp_gateway/server.py:482`` は ``identity_verified=True`` を立てる経路でだけ
    ``channel_id`` を ``verified_caller`` から取る。未検証の metadata の channel_id は
    外殻（＝LLM の申告）由来でありうるので、配達先に昇格させない。
    """

    target, channel, thread_ts = resolve_delivery_target(
        {
            "identity_verified": verified,
            "channel_id": "C_TEAM",
            "thread_ts": "1757000000.000100",
        }
    )
    assert (target, channel, thread_ts) == ("dm", "", "")


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
    job_slots: JobSlots | None = None,
) -> ClipProposalSubmitSkill:
    return ClipProposalSubmitSkill(
        store=store,  # type: ignore[arg-type]
        quota=quota or DailyQuota(),
        active_jobs=active_jobs or ActiveJobIndex(),
        job_slots=job_slots or JobSlots(limit=8),
        analyzer=analyzer,
        deck_builder=lambda analysis, out_dir, request_id: f"{out_dir}/clip.pptx",
        deliverer=deliverer or _RecordingDeliverer(),
        thread_launcher=launcher or _InlineLauncher(),
    )


def test_submit_answers_immediately_with_the_client_name_echoed() -> None:
    store = _MemoryStore()
    skill = _submit_skill(store, analyzer=_StubAnalyzer())
    output = skill.run(ClipProposalSubmitInput(client_name="〇〇製作所"), _ctx())
    assert output.status == "queued"
    assert output.job_id.startswith("clp_")
    assert "「〇〇製作所」" in output.message
    assert store.rows[output.job_id]["status"] == "done"


def test_submit_records_only_a_fingerprint_and_never_the_transcript() -> None:
    store = _MemoryStore()
    skill = _submit_skill(store, analyzer=_StubAnalyzer())
    output = skill.run(ClipProposalSubmitInput(client_name="〇〇製作所"), _ctx())
    summary = json.loads(store.rows[output.job_id]["request_summary"])
    assert summary["kind"] == CLIP_JOB_KIND
    assert summary["requester"] == requester_fingerprint(_ME)
    assert _ME not in json.dumps(summary)
    result = json.loads(store.rows[output.job_id]["result_json"])
    assert "transcript" not in json.dumps(result)


def test_submit_result_carries_every_notice() -> None:
    store = _MemoryStore()
    skill = _submit_skill(store, analyzer=_StubAnalyzer())
    output = skill.run(ClipProposalSubmitInput(client_name="〇〇製作所"), _ctx())
    result = json.loads(store.rows[output.job_id]["result_json"])
    assert has_all_required_notices("\n".join(result["notices"]))
    assert has_all_required_notices(result["message"])


def test_partial_result_when_some_clips_were_dropped() -> None:
    store = _MemoryStore()
    skill = _submit_skill(store, analyzer=_StubAnalyzer(clip_count=7))
    output = skill.run(ClipProposalSubmitInput(client_name="〇〇製作所"), _ctx())
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
    first = skill.run(ClipProposalSubmitInput(client_name="〇〇製作所", file_id="F1"), _ctx())
    second = skill.run(ClipProposalSubmitInput(client_name="〇〇製作所", file_id="F1"), _ctx())

    assert second.status == "queued"
    assert second.job_id == first.job_id
    assert len(store.rows) == 1  # 2 本目のジョブを作らない
    assert quota.snapshot(requester_fingerprint(_ME))[0] == 1  # 枠も消費しない
    assert "もう一度送っていただく必要はありません" in second.message


def test_concurrent_submits_of_the_same_material_create_one_job() -> None:
    """検査と確保が分かれていると、同時 submit で 2 本作ってしまう（枠も費用も倍）。"""

    store = _MemoryStore()
    quota = DailyQuota()
    index = ActiveJobIndex()
    launcher = _BlockingLauncher()
    lock = threading.Lock()
    skill = _submit_skill(
        store, analyzer=_StubAnalyzer(), quota=quota, launcher=launcher, active_jobs=index
    )
    outputs: list[Any] = []
    barrier = threading.Barrier(4)

    def submit() -> None:
        barrier.wait(timeout=5)
        result = skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx())
        with lock:
            outputs.append(result)

    threads = [threading.Thread(target=submit) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(outputs) == 4
    assert len({output.job_id for output in outputs}) == 1
    assert len(store.rows) == 1
    assert quota.snapshot(requester_fingerprint(_ME))[0] == 1


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
        ClipProposalSubmitInput(client_name="〇〇製作所"), _ctx()
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


# ---------------------------------------------------------------------------
# 既定 OFF を構造で効かせる（@register していないことに頼らない）
# ---------------------------------------------------------------------------


def test_submit_refuses_when_the_flag_is_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """便C で registry へ載せた瞬間に env フラグ抜きで走ってしまう抜けを塞ぐ。"""

    monkeypatch.delenv("USE_CLIP_PROPOSAL_TOOLS", raising=False)
    store = _MemoryStore()
    analyzer = _StubAnalyzer()
    skill = _submit_skill(store, analyzer=analyzer)
    with pytest.raises(PermissionError):
        skill.run(ClipProposalSubmitInput(), _ctx())
    assert store.rows == {}
    assert analyzer.calls == 0


def test_status_refuses_when_the_flag_is_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_CLIP_PROPOSAL_TOOLS", raising=False)
    with pytest.raises(PermissionError):
        ClipProposalStatusSkill(store=_MemoryStore()).run(  # type: ignore[arg-type]
            ClipProposalStatusInput(), _ctx()
        )


def test_the_flag_gate_runs_before_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF のときは名簿外の人にも「名簿の話」をしない（存在を匂わせない）。"""

    monkeypatch.delenv("USE_CLIP_PROPOSAL_TOOLS", raising=False)
    with pytest.raises(PermissionError) as excinfo:
        _submit_skill(_MemoryStore(), analyzer=_StubAnalyzer()).run(
            ClipProposalSubmitInput(), _ctx(verified=False)
        )
    assert "USE_CLIP_PROPOSAL_TOOLS" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 重複判定の鍵（素材が特定できない依頼を握り潰さない）
# ---------------------------------------------------------------------------


def test_two_requests_without_a_material_are_not_merged() -> None:
    """最も自然な入口（動画を貼って「切り抜き提案作って」）の失敗モード。

    file_id は任意（省略時はスレッド内の最新の本人アップロードを使う）・client_name も
    任意。鍵が潰れると 2 本目はジョブも背景タスクも作られないまま「受け付けています」
    とだけ返り、利用者は永久に待つ。
    """

    store = _MemoryStore()
    launcher = _BlockingLauncher()
    skill = _submit_skill(
        store,
        analyzer=_StubAnalyzer(),
        quota=DailyQuota(),
        launcher=launcher,
        active_jobs=ActiveJobIndex(),
    )
    first = skill.run(ClipProposalSubmitInput(), _ctx(thread_ts="1757000000.000100"))
    second = skill.run(ClipProposalSubmitInput(), _ctx(thread_ts="1757999999.000200"))

    assert first.status == "queued"
    assert second.status == "queued"
    assert first.job_id != second.job_id
    assert len(store.rows) == 2
    assert len(launcher.pending) == 2  # 背景タスクも 2 本


def test_a_second_video_for_the_same_client_is_not_a_duplicate() -> None:
    """client_name は素材の識別子ではない（同じ得意先の 2 本目を握り潰さない）。"""

    store = _MemoryStore()
    launcher = _BlockingLauncher()
    skill = _submit_skill(
        store,
        analyzer=_StubAnalyzer(),
        quota=DailyQuota(),
        launcher=launcher,
        active_jobs=ActiveJobIndex(),
    )
    first = skill.run(ClipProposalSubmitInput(client_name="〇〇製作所", file_id="F1"), _ctx())
    second = skill.run(ClipProposalSubmitInput(client_name="〇〇製作所", file_id="F2"), _ctx())
    assert first.job_id != second.job_id
    assert len(store.rows) == 2


def test_the_same_material_in_a_different_thread_is_a_different_request() -> None:
    store = _MemoryStore()
    launcher = _BlockingLauncher()
    skill = _submit_skill(
        store,
        analyzer=_StubAnalyzer(),
        quota=DailyQuota(),
        launcher=launcher,
        active_jobs=ActiveJobIndex(),
    )
    first = skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx(thread_ts="1757000000.000100"))
    second = skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx(thread_ts="1757999999.000200"))
    assert first.job_id != second.job_id


def test_dedupe_key_includes_the_thread_and_never_collapses() -> None:
    fingerprint = requester_fingerprint(_ME)
    bare = ActiveJobIndex.key(fingerprint, ClipProposalSubmitInput())
    assert bare == ""  # 素材もスレッドも無い ＝ 重複判定の対象外

    a = ActiveJobIndex.key(fingerprint, ClipProposalSubmitInput(), thread_ts="1.1")
    b = ActiveJobIndex.key(fingerprint, ClipProposalSubmitInput(), thread_ts="2.2")
    assert a != b
    named = ActiveJobIndex.key(
        fingerprint, ClipProposalSubmitInput(client_name="〇〇製作所"), thread_ts="1.1"
    )
    assert named == a  # client_name は鍵に入らない


# ---------------------------------------------------------------------------
# 同時実行のアドミッション（description が約束する busy の実体）
# ---------------------------------------------------------------------------


def test_submit_says_busy_only_because_a_real_queue_exists() -> None:
    """description が宣言する busy が、実在の順番待ちに裏打ちされていること。"""

    store = _MemoryStore()
    launcher = _BlockingLauncher()
    skill = _submit_skill(
        store,
        analyzer=_StubAnalyzer(),
        quota=DailyQuota(),
        launcher=launcher,
        active_jobs=ActiveJobIndex(),
        job_slots=JobSlots(limit=1),
    )
    first = skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx())
    second = skill.run(ClipProposalSubmitInput(file_id="F2"), _ctx())

    assert first.status == "queued"
    assert second.status == "busy"
    assert second.job_id  # 待っている間も status で引ける
    assert "もう一度送っていただく必要はありません" in second.message
    assert len(store.rows) == 2  # 断らずジョブは作る
    assert len(launcher.pending) == 2  # 背景タスクも起きている（自動着手の実体）


def test_the_busy_promise_matches_the_tool_description() -> None:
    """実在しない挙動を LLM へ約束しない（description とコードの一致）。"""

    description = ClipProposalSubmitSkill.description
    assert "busy" in description
    assert "順番待ち" in description
    # busy を名乗るからには、順番待ちの器と着手の待ち合わせが実在すること。
    slots = JobSlots(limit=1)
    assert slots.enqueue() == 0
    assert slots.enqueue() == 1
    assert slots.start(timeout=1) is True
    assert slots.start(timeout=0.05) is False  # limit=1 なので 2 本目は待たされる
    slots.finish()
    assert slots.start(timeout=1) is True


def test_concurrency_is_bounded_while_jobs_are_running() -> None:
    """走行本数が上限を超えない（18MB proxy × 20 本同時でメモリを飛ばさない）。"""

    slots = JobSlots(limit=2)
    peak = 0
    lock = threading.Lock()
    running = 0
    release = threading.Event()

    def worker() -> None:
        nonlocal running, peak
        slots.enqueue()
        slots.start()
        with lock:
            running += 1
            peak = max(peak, running)
        release.wait(timeout=5)
        with lock:
            running -= 1
        slots.finish()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    threading.Event().wait(0.2)
    release.set()
    for thread in threads:
        thread.join(timeout=10)
    assert peak <= 2


def test_a_cancelled_submission_gives_its_queue_place_back() -> None:
    """ジョブ作成に失敗したら順番待ちの席も返す（席だけ食い潰さない）。"""

    class _BrokenStore(_MemoryStore):
        def create_job(self, job_id: str, request_summary: dict[str, Any]) -> None:
            raise RuntimeError("db down")

    slots = JobSlots(limit=1)
    skill = _submit_skill(
        _BrokenStore(),
        analyzer=_StubAnalyzer(),
        quota=DailyQuota(),
        launcher=_BlockingLauncher(),
        active_jobs=ActiveJobIndex(),
        job_slots=slots,
    )
    assert skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx()).status == "failed"
    assert slots.snapshot() == (0, 0)


# ---------------------------------------------------------------------------
# 配達先は _deliver が決めて引数で渡す
# ---------------------------------------------------------------------------


def test_delivery_target_is_decided_by_the_skill_not_the_deliverer() -> None:
    deliverer = _RecordingDeliverer()
    skill = _submit_skill(_MemoryStore(), analyzer=_StubAnalyzer(), deliverer=deliverer)
    skill.run(ClipProposalSubmitInput(), _ctx(channel_id="C_TEAM"))
    assert deliverer.calls == [("thread", "C_TEAM", "1757000000.000100")]


def test_a_channel_without_a_thread_is_never_handed_to_the_deliverer() -> None:
    """C/G 始まりのチャンネルへ直投稿する経路を開かない。"""

    deliverer = _RecordingDeliverer()
    skill = _submit_skill(_MemoryStore(), analyzer=_StubAnalyzer(), deliverer=deliverer)
    skill.run(ClipProposalSubmitInput(), _ctx(channel_id="C_TEAM", thread_ts=""))
    assert deliverer.calls == [("dm", "", "")]
    _target, channel, _thread = deliverer.calls[0]
    assert not channel.startswith(("C", "G"))


def test_the_result_records_the_target_the_skill_chose() -> None:
    store = _MemoryStore()
    deliverer = _RecordingDeliverer()
    output = _submit_skill(store, analyzer=_StubAnalyzer(), deliverer=deliverer).run(
        ClipProposalSubmitInput(), _ctx(channel_id="C_TEAM", thread_ts="")
    )
    result = json.loads(store.rows[output.job_id]["result_json"])
    assert result["delivery_target"] == "dm"
    assert result["slack_delivered"] is True


# ---------------------------------------------------------------------------
# 失敗経路でも実課金分を日次カウンタへ積む
# ---------------------------------------------------------------------------


class _ChargingAnalyzer:
    """2 コール課金してからパースで落ちる解析（本番の出力崩れを再現）。"""

    cost_cap_usd = 5.0

    def __init__(self, *, spent_usd: float = 0.80) -> None:
        self.spent_usd = spent_usd
        self.calls = 0

    def run_for_request(self, input: Any, ctx: SkillContext) -> Any:
        from teamagent.skills.clip_proposal.analysis import ClipAnalysisError

        self.calls += 1
        raise ClipAnalysisError("CLIP_PLAN_INVALID", spent_usd=self.spent_usd, calls=2)


def test_failed_jobs_still_charge_the_daily_cost_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N 本失敗したら spent は N × 実額。ここが 0 のままだと費用 cap が死ぬ。"""

    monkeypatch.setenv("CLIP_PROPOSAL_MAX_JOBS_PER_DAY", "50")
    quota = DailyQuota()
    fingerprint = requester_fingerprint(_ME)
    for _ in range(3):
        store = _MemoryStore()
        skill = _submit_skill(
            store,
            analyzer=_ChargingAnalyzer(spent_usd=0.80),
            quota=quota,
            active_jobs=ActiveJobIndex(),
        )
        output = skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx())
        assert store.rows[output.job_id]["status"] == "failed"
    assert quota.snapshot(fingerprint)[2] == pytest.approx(2.40)


def test_the_daily_cost_cap_fires_after_enough_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIP_DAILY_COST_CAP_USD", "1.5")
    monkeypatch.setenv("CLIP_PROPOSAL_MAX_JOBS_PER_DAY", "50")
    quota = DailyQuota()
    for _ in range(2):
        skill = _submit_skill(
            _MemoryStore(),
            analyzer=_ChargingAnalyzer(spent_usd=0.80),
            quota=quota,
            active_jobs=ActiveJobIndex(),
        )
        skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx())

    deferred = _submit_skill(
        _MemoryStore(), analyzer=_StubAnalyzer(), quota=quota, active_jobs=ActiveJobIndex()
    ).run(ClipProposalSubmitInput(file_id="F9"), _ctx())
    assert deferred.status == "deferred"
    assert "費用の上限" in deferred.message


def test_a_failure_that_charged_nothing_does_not_move_the_counter() -> None:
    quota = DailyQuota()
    skill = _submit_skill(
        _MemoryStore(),
        analyzer=_StubAnalyzer(raises=RuntimeError("CLIP_TEMPLATE_UNAVAILABLE")),
        quota=quota,
    )
    skill.run(ClipProposalSubmitInput(), _ctx())
    assert quota.snapshot(requester_fingerprint(_ME))[2] == 0.0


class _BlockingAnalyzer:
    """解析の最中で待たせる（走行本数の門が背景側に効いているかを見る）。"""

    cost_cap_usd = 1.0

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self._lock = threading.Lock()
        self.running = 0
        self.peak = 0

    def run_for_request(self, input: Any, ctx: SkillContext) -> Any:
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        self.entered.set()
        self.release.wait(timeout=5)
        with self._lock:
            self.running -= 1
        return sample_analysis(client_name=input.client_name)


def test_the_background_job_waits_for_a_slot_before_it_analyses() -> None:
    """受付側で busy を返すだけでは不十分（実処理が本当に待つこと）。

    ``_run_background`` がスロットを取らずに走り出すと、18MB の proxy を載せた解析が
    上限を無視して同時に走る。mcp は desiredCount=1 なのでメモリ枯渇でタスクごと落ち、
    走行中の全ジョブが失われる（日次枠は消費済みで戻らない）。
    """

    analyzer = _BlockingAnalyzer()
    threads: list[threading.Thread] = []

    def launcher(target: Callable[[], None], name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        threads.append(thread)

    skill = _submit_skill(
        _MemoryStore(),
        analyzer=analyzer,
        quota=DailyQuota(),
        launcher=launcher,
        active_jobs=ActiveJobIndex(),
        job_slots=JobSlots(limit=1),
    )
    first = skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx())
    second = skill.run(ClipProposalSubmitInput(file_id="F2"), _ctx())
    assert first.status == "queued"
    assert second.status == "busy"

    assert analyzer.entered.wait(timeout=5)
    threading.Event().wait(0.3)  # 2 本目が割り込む余地を与える
    assert analyzer.peak == 1, "走行スロットを取らずに解析へ入っている"

    analyzer.release.set()
    for thread in threads:
        thread.join(timeout=5)
    assert analyzer.peak == 1


def test_a_store_failure_mid_job_still_gives_the_slot_back() -> None:
    """mark_running（DB）が落ちても走行スロットを返す。

    返さないと走行枠が 1 つずつ永久に減り、積み重なるとすべての依頼が
    空かないスロットを待ち続けて mcp 再起動まで止まる。
    """

    class _FlakyStore(_MemoryStore):
        def mark_running(self, job_id: str) -> bool:
            raise RuntimeError("db down")

    slots = JobSlots(limit=1)
    store = _FlakyStore()
    skill = _submit_skill(
        store,
        analyzer=_StubAnalyzer(),
        quota=DailyQuota(),
        active_jobs=ActiveJobIndex(),
        job_slots=slots,
    )
    skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx())
    assert slots.snapshot() == (0, 0)

    # 次の依頼は待たされずに着手できる（枠が食い潰されていない）。
    healthy = _submit_skill(
        _MemoryStore(),
        analyzer=_StubAnalyzer(),
        quota=DailyQuota(),
        active_jobs=ActiveJobIndex(),
        job_slots=slots,
    )
    assert healthy.run(ClipProposalSubmitInput(file_id="F2"), _ctx()).status == "queued"


def test_a_dead_store_does_not_wedge_the_slot_on_the_failure_path() -> None:
    """解析も mark_failed も落ちる回（台帳ごと落ちている）でもスロットを返す。"""

    class _DeadStore(_MemoryStore):
        def mark_failed(
            self, job_id: str, error_code: str, *, expected_statuses: tuple[str, ...] = ()
        ) -> bool:
            raise RuntimeError("db down")

    slots = JobSlots(limit=1)
    skill = _submit_skill(
        _DeadStore(),
        analyzer=_StubAnalyzer(raises=RuntimeError("CLIP_TEMPLATE_UNAVAILABLE")),
        quota=DailyQuota(),
        active_jobs=ActiveJobIndex(),
        job_slots=slots,
    )
    skill.run(ClipProposalSubmitInput(file_id="F1"), _ctx())
    assert slots.snapshot() == (0, 0)
