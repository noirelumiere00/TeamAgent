"""tiktok_acquire の投函計画（1ジョブの実行上限に収まる形への組み直し）の契約。

本番（2026-08-28〜09-29）では、既定値（10本/KW・動画2本/KW）の複数KW呼び出しと、
検索面チェックのレシピ（3KW以上は videos_per_kw=0 で先に取得）が入力検証で必ず拒否され、
通ったのは1KWだけだった。ここでは「断らずに、実際の受付境界（契約モデルと dispatcher
Lambda）を通る形で投函する」ことを確かめる。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from teamagent.adapters.tiktok_task_store import TikTokTaskStore, new_job_id
from teamagent.mcp_gateway import async_job_notify as notify
from teamagent.mcp_gateway import server
from teamagent.media.contracts import (
    TikTokAcquireOperation,
    TikTokClientConfig,
    tiktok_search_timeout_seconds,
)
from teamagent.skills._shared.long_jobs import ORIGIN_KEY, Origin
from teamagent.skills.base import SkillContext
from teamagent.skills.tiktok_acquire.plan import (
    MAX_JOBS_PER_REQUEST,
    AcquireJobPlan,
    plan_acquire_jobs,
)
from teamagent.skills.tiktok_acquire.schema import TikTokAcquireInput
from teamagent.skills.tiktok_acquire.skill import TikTokAcquireSkill

_HANDLER = (
    Path(__file__).parents[3] / "infra" / "terraform" / "lambda" / "tiktok_dispatch" / "handler.py"
)


def _ctx() -> SkillContext:
    return SkillContext(
        request_id="req-plan", user_id="U123", metadata={"user_email": "a@vectorinc.co.jp"}
    )


def _operation(job: AcquireJobPlan) -> TikTokAcquireOperation:
    """本番の TikTokTaskStore.submit と同じ組み立て（実行上限を超えると ValidationError）。"""

    return TikTokAcquireOperation(
        kind="tiktok_acquire",
        keywords=job.keywords,
        n_per_kw=job.n_per_kw,
        videos_per_kw=job.videos_per_kw,
        artifact_mode=job.artifact_mode,
        client=TikTokClientConfig(),
    )


@pytest.fixture
def dispatcher(monkeypatch: pytest.MonkeyPatch) -> Any:
    """本番 dispatcher Lambda（handler.py）を boto3 なしで読み込む。"""

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name, **_kwargs: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    spec = importlib.util.spec_from_file_location("_teamagent_plan_dispatch", _HANDLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- 計画（純関数） ---------------------------------------------------------------


def test_default_three_keywords_are_split_per_keyword_without_shrinking() -> None:
    # 既定値のまま3KW（本番で必ず拒否されていた形）。動画ありなので KW ごとに分けて並べる。
    plan = plan_acquire_jobs(["a", "b", "c"], n_per_kw=10, videos_per_kw=2)

    assert plan.jobs == (
        AcquireJobPlan(("a",), 10, 2, "full"),
        AcquireJobPlan(("b",), 10, 2, "full"),
        AcquireJobPlan(("c",), 10, 2, "full"),
    )
    assert plan.adjustments == (
        "動画も保存するので、1回の実行時間に収まるよう3件の取得に分けて並べました（本数は要求どおり）。",
    )


def test_surface_check_recipe_is_one_metadata_only_job() -> None:
    # 検索面チェックのレシピ（3KW以上は videos_per_kw=0 で先に取得）。後工程は job_id を
    # 1つしか受け取らないので、分けずに指標だけの1ジョブにする（取得本数は削らない）。
    plan = plan_acquire_jobs(["a", "b", "c"], n_per_kw=10, videos_per_kw=0)

    assert plan.jobs == (AcquireJobPlan(("a", "b", "c"), 10, 0, "metadata_only"),)
    assert len(plan.adjustments) == 1
    assert "表示順と指標だけ" in plan.adjustments[0]


def test_surface_check_upper_bound_five_keywords_at_thirty_posts_is_one_job() -> None:
    # search_surface_check の上限（5KW・max_posts_per_kw=30）でも1ジョブに収まる。
    plan = plan_acquire_jobs(["a", "b", "c", "d", "e"], n_per_kw=30, videos_per_kw=0)

    assert plan.jobs == (AcquireJobPlan(("a", "b", "c", "d", "e"), 30, 0, "metadata_only"),)


def test_ten_keywords_without_videos_are_split_evenly() -> None:
    # 指標だけでも1ジョブは7KWまで（120秒/KW）。10KW は 5+5 に均等に分ける。
    kws = [f"kw{i}" for i in range(10)]
    plan = plan_acquire_jobs(kws, n_per_kw=10, videos_per_kw=0)

    assert [job.keywords for job in plan.jobs] == [tuple(kws[:5]), tuple(kws[5:])]
    assert all(job.artifact_mode == "metadata_only" for job in plan.jobs)
    assert any("2件の取得に分けて" in note for note in plan.adjustments)


def test_single_keyword_over_limit_shrinks_posts_and_says_so() -> None:
    # 1KW でも入らない量（30本+動画2本=1860秒）は、動画本数を保って取得本数だけ縮める。
    plan = plan_acquire_jobs(["a"], n_per_kw=30, videos_per_kw=2)

    assert plan.jobs == (AcquireJobPlan(("a",), 10, 2, "full"),)
    assert plan.adjustments == (
        "1KWだけでも1回の実行時間に収まらないので、各KWの取得本数を30本→10本に減らしました。",
    )


def test_catalog_six_recipe_shrinks_videos_only_when_unavoidable() -> None:
    # SOUL のカタログ⑥（5KW・動画6本）: 動画6本は1KWでも入らない（最小でも1140秒）。
    # 入る範囲で動画を最も多く残す（取得5本・動画4本=850秒）。
    plan = plan_acquire_jobs(["a", "b", "c", "d", "e"], n_per_kw=10, videos_per_kw=6)

    assert [job.keywords for job in plan.jobs] == [("a",), ("b",), ("c",), ("d",), ("e",)]
    assert {(job.n_per_kw, job.videos_per_kw, job.artifact_mode) for job in plan.jobs} == {
        (5, 4, "full")
    }
    assert plan.adjustments == (
        "動画も保存するので、1回の実行時間に収まるよう5件の取得に分けて並べました。",
        "1KWだけでも1回の実行時間に収まらないので、各KWの取得本数を10本→5本、"
        "動画保存本数を6本→4本に減らしました。",
    )


def test_videos_only_shrink_is_reported_on_its_own() -> None:
    # 取得本数は要求どおり（5本）で動画本数だけ縮めるとき、その1点だけを書く。
    plan = plan_acquire_jobs(["a"], n_per_kw=5, videos_per_kw=6)

    assert plan.jobs == (AcquireJobPlan(("a",), 5, 4, "full"),)
    assert plan.adjustments == (
        "1KWだけでも1回の実行時間に収まらないので、各KWの動画保存本数を6本→4本に減らしました。",
    )


def test_many_keywords_with_videos_stay_within_the_parallel_job_cap() -> None:
    # 取得タスクは1本16 vCPU・Fargate 上限140 vCPU。10KW を10ジョブに分けると上限を超え、
    # dispatcher は超えた分を失敗で確定する（他の人のジョブも巻き込む）。5ジョブに詰めて、
    # 詰めるために縮めた本数を書く。
    kws = [f"kw{i}" for i in range(10)]
    plan = plan_acquire_jobs(kws, n_per_kw=10, videos_per_kw=2)

    assert MAX_JOBS_PER_REQUEST == 5
    assert [job.keywords for job in plan.jobs] == [tuple(kws[i : i + 2]) for i in range(0, 10, 2)]
    assert {(job.n_per_kw, job.videos_per_kw) for job in plan.jobs} == {(3, 1)}
    assert plan.adjustments == (
        "動画も保存するので、1回の実行時間に収まるよう5件の取得に分けて並べました。",
        "KWが多く、同時に動かせる取得は5件までなので、各KWの取得本数を10本→3本、"
        "動画保存本数を2本→1本に減らしました。",
    )


def test_request_that_fits_is_sent_as_is() -> None:
    plan = plan_acquire_jobs(["a"], n_per_kw=10, videos_per_kw=2)

    assert plan.jobs == (AcquireJobPlan(("a",), 10, 2, "full"),)
    assert plan.adjustments == ()


def test_every_request_in_the_input_domain_passes_both_admission_gates(dispatcher: Any) -> None:
    # 入力スキーマの全域（KW1〜10 × 取得1〜30 × 動画0〜10）で、組み直した各ジョブが
    # 契約モデル（core）と dispatcher Lambda（本番の受付）の両方を通ることを確かめる。
    for keyword_count in range(1, 11):
        kws = [f"kw{i}" for i in range(keyword_count)]
        for n_per_kw in range(1, 31):
            for videos_per_kw in range(11):
                TikTokAcquireInput(keywords=kws, n_per_kw=n_per_kw, videos_per_kw=videos_per_kw)
                plan = plan_acquire_jobs(kws, n_per_kw=n_per_kw, videos_per_kw=videos_per_kw)
                case = (keyword_count, n_per_kw, videos_per_kw)
                # KW は順番どおり、欠けも重複もなく、どれかのジョブに1回だけ入る。
                assert [kw for job in plan.jobs for kw in job.keywords] == kws, case
                # 1依頼で同時に動かすジョブは上限まで（Fargate vCPU 上限の保護）。
                assert len(plan.jobs) <= MAX_JOBS_PER_REQUEST, case
                for job in plan.jobs:
                    operation = _operation(job)
                    dispatcher._validate_operation(operation.model_dump(mode="json"))
                    # 要求より増やさない。動画なしは取得本数を削らない。
                    assert job.n_per_kw <= n_per_kw, case
                    assert job.videos_per_kw <= videos_per_kw, case
                    if videos_per_kw == 0:
                        assert job.n_per_kw == n_per_kw, case
                        assert job.artifact_mode == "metadata_only" or len(plan.jobs) == 1, case
                    else:
                        assert job.artifact_mode == "full", case


def test_metadata_only_cost_model_matches_search_timeout() -> None:
    # 指標だけのジョブの見積りは検索上限（n<=30 は 120秒/KW）だけ＝7KW×120=840<=870。
    assert tiktok_search_timeout_seconds(30) == 120
    assert plan_acquire_jobs([f"kw{i}" for i in range(7)], n_per_kw=30, videos_per_kw=0).jobs == (
        AcquireJobPlan(tuple(f"kw{i}" for i in range(7)), 30, 0, "metadata_only"),
    )


# ---- skill → 本番の TikTokTaskStore.submit（契約モデルで検証）------------------------


class _RecordingMediaClient:
    def __init__(self, fail_keywords: frozenset[str] = frozenset()) -> None:
        self.requests: list[Any] = []
        self._fail_keywords = fail_keywords

    def submit(self, request: Any) -> None:
        if set(request.operation.keywords) & self._fail_keywords:
            raise RuntimeError("queue unavailable")
        self.requests.append(request)


class _ProductionValidationStore(TikTokTaskStore):
    """submit の検証（TikTokAcquireOperation / make_job_request）は本番コードのまま通す。

    差し替えるのは SQS/DynamoDB へ書く MediaJobClient だけ。実行上限を超える spec は
    本番と同じく ValidationError → submit=False になる。
    """

    def __init__(self, client: _RecordingMediaClient) -> None:
        super().__init__()
        self._queue_url = "https://sqs.ap-northeast-1.amazonaws.com/123456789012/media"
        self._table = "teamagent-test-media-jobs"
        self._bucket = "teamagent-test-media-bucket"
        self._recording_client = client

    def _client(self, session: Any) -> Any:
        return self._recording_client


def test_default_three_keyword_call_is_queued_as_three_jobs() -> None:
    client = _RecordingMediaClient()
    skill = TikTokAcquireSkill(store=_ProductionValidationStore(client))

    out = skill.run(TikTokAcquireInput(keywords=["コンビニ", "スイーツ", "新作"]), _ctx())

    assert out.status == "queued"
    assert len(out.job_ids) == 3 and len(set(out.job_ids)) == 3
    assert out.job_id == out.job_ids[0]
    assert [job.keywords for job in out.jobs] == [["コンビニ"], ["スイーツ"], ["新作"]]
    assert [request.job_id for request in client.requests] == out.job_ids
    for request in client.requests:
        assert request.operation.n_per_kw == 10
        assert request.operation.videos_per_kw == 2
        assert request.operation.artifact_mode == "full"
    assert out.adjustments and "3件の取得に分けて" in out.message
    for job_id in out.job_ids:
        assert job_id not in out.message


def test_surface_check_recipe_call_is_queued_as_one_metadata_only_job() -> None:
    client = _RecordingMediaClient()
    skill = TikTokAcquireSkill(store=_ProductionValidationStore(client))

    out = skill.run(
        TikTokAcquireInput(keywords=["セブン", "ファミマ", "ローソン"], videos_per_kw=0),
        _ctx(),
    )

    assert out.status == "queued"
    assert out.job_ids == [out.job_id]
    (request,) = client.requests
    assert request.operation.keywords == ("セブン", "ファミマ", "ローソン")
    assert request.operation.n_per_kw == 10
    assert request.operation.artifact_mode == "metadata_only"
    assert out.jobs[0].artifact_mode == "metadata_only"


def test_request_that_fits_keeps_the_legacy_job_id() -> None:
    # 要求どおり1ジョブなら冪等キーは従来と同じ＝同じ依頼の再送で同じ job_id になる。
    client = _RecordingMediaClient()
    skill = TikTokAcquireSkill(store=_ProductionValidationStore(client))
    input_ = TikTokAcquireInput(keywords=["コンビニスイーツ"])

    out = skill.run(input_, _ctx())

    legacy_fingerprint = hashlib.sha256(
        json.dumps(
            {"request_id": "req-plan", "input": input_.model_dump(mode="json")},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert out.job_id == new_job_id(legacy_fingerprint)
    assert out.adjustments == []
    assert "40〜50分" in out.message
    assert "job_id" not in out.message


def test_partial_submit_failure_keeps_the_jobs_that_were_queued() -> None:
    client = _RecordingMediaClient(fail_keywords=frozenset({"b"}))
    skill = TikTokAcquireSkill(store=_ProductionValidationStore(client))

    out = skill.run(TikTokAcquireInput(keywords=["a", "b", "c"]), _ctx())

    assert out.status == "queued"
    assert [job.keywords for job in out.jobs] == [["a"], ["c"]]
    assert "KW「b」の取得は投函に失敗しました" in out.message


# ---- 完了通知: 分けたジョブの全部に見張りを付ける -----------------------------------------


def test_completion_notice_is_scheduled_for_every_split_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: list[str] = []
    monkeypatch.setattr(notify, "enabled", lambda: True)
    monkeypatch.setattr(
        notify,
        "schedule_completion_notice",
        lambda **kwargs: scheduled.append(kwargs["job_id"]),
    )

    server._schedule_async_job_notice(
        "tiktok_acquire",
        {
            "status": "queued",
            "job_id": "tk_000000000001",
            "job_ids": ["tk_000000000001", "tk_000000000002"],
        },
        {"channel_id": "C123"},
        SkillContext(metadata={ORIGIN_KEY: Origin("D12345678", None, "U12345678")}),
    )

    assert scheduled == ["tk_000000000001|tk_000000000002"]
