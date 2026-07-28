"""video_algorithm の quota / timeout再発話キャッシュ配線テスト。"""

from __future__ import annotations

from threading import Event
from typing import Any

import pytest

from teamagent.adapters.gemini_client import GeminiResponse
from teamagent.adapters.quota_store import QuotaResult
from teamagent.adapters.video_algorithm_cache import (
    VideoAlgorithmCacheLease,
    VideoAlgorithmCacheLeaseLostError,
    VideoAlgorithmCacheLeaseUnavailableError,
    VideoAlgorithmResultCache,
)
from teamagent.runtime.slack_bot import SkillDispatcher
from teamagent.skills.base import SkillContext
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    VideoAlgorithmInput,
    VideoAlgorithmOutput,
    VideoMeta,
    VideoVSEOAnalysis,
)
from teamagent.skills.video_algorithm.skill import VideoAlgorithmSkill, _LeaseHeartbeat

ME = "me@vectorinc.co.jp"


@pytest.fixture(autouse=True)
def _guards_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_QUOTA_ENABLED", raising=False)
    monkeypatch.delenv("ANALYSIS_CACHE_ENABLED", raising=False)
    monkeypatch.setenv("TEAMAGENT_LOCAL_MEDIA_RUNTIME", "true")


class _FakeGemini:
    model_id = "gemini-2.5-flash"

    def __init__(self) -> None:
        self.video_calls = 0

    def analyze_video_bytes(self, **kwargs: Any) -> GeminiResponse:
        self.video_calls += 1
        return GeminiResponse(
            text="```json\n{}\n```",
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.01,
            model_id=self.model_id,
            latency_ms=1,
        )

    def generate_text(self, *args: Any, **kwargs: Any) -> GeminiResponse:
        return GeminiResponse(
            text="```json\n{}\n```",
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.01,
            model_id=self.model_id,
            latency_ms=1,
        )


def _metas(count: int) -> list[VideoMeta]:
    return [VideoMeta(rank=i, url=f"https://example.invalid/{i}") for i in range(1, count + 1)]


def _skill(
    tmp_path: Any,
    gemini: _FakeGemini,
    *,
    count: int = 1,
    result_cache: VideoAlgorithmResultCache | None = None,
) -> VideoAlgorithmSkill:
    metas = _metas(count)
    return VideoAlgorithmSkill(
        gemini=gemini,  # type: ignore[arg-type]
        searcher=lambda query, limit, request_id: metas[:limit],
        downloader=lambda url: (b"video", "video/mp4"),
        proxy=lambda data, mime: (data, mime),
        report_dir=str(tmp_path),
        result_cache=result_cache,
    )


def _input(*, max_videos: int = 1) -> VideoAlgorithmInput:
    return VideoAlgorithmInput(
        query="新宿 ランチ",
        max_videos=max_videos,
        board_size=5,
        outputs=["report"],
    )


def _ctx(request_id: str = "r") -> SkillContext:
    return SkillContext(request_id=request_id, metadata={"user_email": ME})


def test_quota_disabled_is_complete_noop(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VIDEO_QUOTA_ENABLED 既定falseでは store生成すらせず従来処理だけを行う。"""

    import teamagent.adapters.quota_store as quota_store

    def _must_not_construct(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("quota store must stay inactive")

    monkeypatch.setattr(quota_store.VideoQuotaStore, "__init__", _must_not_construct)
    gemini = _FakeGemini()
    output = _skill(tmp_path, gemini).run(_input(), _ctx())

    assert gemini.video_calls == 1
    assert len(output.videos) == 1


def test_quota_preconsumes_each_attempted_batch_including_backfill(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失敗した初回2本 + バックフィル1本 = 実際に開始した3本を事前計数する。"""

    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    import teamagent.adapters.quota_store as quota_store

    consumed: list[int] = []

    def _consume(
        self: Any,
        email: str,
        count: int,
        *,
        request_id: str,
    ) -> QuotaResult:
        assert email == ME
        consumed.append(count)
        return QuotaResult(allowed=True, used=sum(consumed), limit=20)

    monkeypatch.setattr(quota_store.VideoQuotaStore, "try_consume", _consume)
    gemini = _FakeGemini()
    skill = _skill(tmp_path, gemini, count=3)

    def _analyze(meta: VideoMeta, **kwargs: Any) -> AnalyzedVideo:
        # rank1 の失敗も試行として計数され、rank3 がバックフィルされる。
        analysis = None if meta.rank == 1 else VideoVSEOAnalysis()
        return AnalyzedVideo(meta=meta, analysis=analysis, error=None if analysis else "failed")

    monkeypatch.setattr(skill, "_analyze_one", _analyze)
    output = skill.run(_input(max_videos=2), _ctx())

    assert consumed == [2, 1]
    assert sum(consumed) == 3
    assert [video.meta.rank for video in output.videos] == [2, 3]


def test_quota_block_stops_before_analysis(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    import teamagent.adapters.quota_store as quota_store

    monkeypatch.setattr(
        quota_store.VideoQuotaStore,
        "try_consume",
        lambda self, email, count, *, request_id: QuotaResult(
            allowed=False,
            used=20,
            limit=20,
        ),
    )
    gemini = _FakeGemini()
    skill = _skill(tmp_path, gemini)

    with pytest.raises(RuntimeError, match="VIDEO_QUOTA_EXCEEDED"):
        skill.run(_input(), _ctx())
    assert gemini.video_calls == 0


class _FakeS3:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self._version = 0

    def get_object(self, Bucket: str, Key: str) -> Any:  # noqa: N803 - boto3 naming
        if Key not in self.store:
            raise type("NoSuchKey", (Exception,), {})()
        body = self.store[Key]
        return {
            "Body": type("_Body", (), {"read": lambda self2: body})(),
            "ETag": self.etags.get(Key, ""),
        }

    def put_object(self, Bucket: str, Key: str, Body: bytes, **kwargs: Any) -> None:  # noqa: N803
        if kwargs.get("IfNoneMatch") == "*" and Key in self.store:
            error = type("PreconditionFailed", (Exception,), {})()
            error.response = {"Error": {"Code": "PreconditionFailed"}}  # type: ignore[attr-defined]
            raise error
        if expected := kwargs.get("IfMatch"):
            if self.etags.get(Key) != expected:
                error = type("PreconditionFailed", (Exception,), {})()
                error.response = {  # type: ignore[attr-defined]
                    "Error": {"Code": "PreconditionFailed"}
                }
                raise error
        self._version += 1
        self.store[Key] = Body
        self.etags[Key] = f'"v{self._version}"'


class _TransientResultS3(_FakeS3):
    """最初のresult writeだけ一過性失敗させ、lease I/Oは通す。"""

    def __init__(self) -> None:
        super().__init__()
        self.result_failures = 1

    def put_object(self, Bucket: str, Key: str, Body: bytes, **kwargs: Any) -> None:  # noqa: N803
        if self.result_failures and Key.endswith(".json") and not Key.endswith(".lease.json"):
            self.result_failures -= 1
            raise RuntimeError("temporary result write outage")
        super().put_object(Bucket=Bucket, Key=Key, Body=Body, **kwargs)


def test_result_cache_hit_skips_quota_and_gemini(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    import teamagent.adapters.quota_store as quota_store

    consumed: list[int] = []

    def _consume(
        self: Any,
        email: str,
        count: int,
        *,
        request_id: str,
    ) -> QuotaResult:
        consumed.append(count)
        return QuotaResult(allowed=True, used=sum(consumed), limit=20)

    monkeypatch.setattr(quota_store.VideoQuotaStore, "try_consume", _consume)
    gemini = _FakeGemini()
    cache = VideoAlgorithmResultCache(bucket="b", client=_FakeS3(), ttl_seconds=60)
    skill = _skill(tmp_path, gemini, result_cache=cache)

    first = skill.run(_input(), _ctx("r1"))
    second = skill.run(_input(), _ctx("r2"))

    assert first.total_cost_usd == 0.01
    assert second.total_cost_usd == 0.0
    assert second.report_html_path is None
    assert gemini.video_calls == 1
    assert consumed == [1]


def test_active_result_cache_lease_stops_retry_before_quota_and_gemini(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    gemini = _FakeGemini()
    cache = VideoAlgorithmResultCache(bucket="b", client=_FakeS3(), lease_seconds=600)
    input_obj = _input()
    cache_key = cache.cache_key(
        query=input_obj.query,
        max_videos=input_obj.max_videos,
        prompt_version="v1",
        model_id=gemini.model_id,
        board_size=input_obj.board_size,
        outputs=input_obj.outputs,
        kw_set=input_obj.kw_set,
        client_name=input_obj.client_name,
        acquire_job_id=input_obj.acquire_job_id,
        search_volume=input_obj.search_volume,
        requester=ME,
    )
    assert cache.acquire_lease(cache_key, request_id="original") is not None

    import teamagent.adapters.quota_store as quota_store

    monkeypatch.setattr(
        quota_store.VideoQuotaStore,
        "__init__",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("active retry must not construct quota store")
        ),
    )
    with pytest.raises(RuntimeError, match="VIDEO_ALGORITHM_IN_PROGRESS"):
        _skill(tmp_path, gemini, result_cache=cache).run(input_obj, _ctx("retry"))
    assert gemini.video_calls == 0


def test_cache_unavailable_fails_closed_before_quota_and_gemini(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")

    class _UnavailableS3:
        def get_object(self, **kwargs: Any) -> Any:
            raise RuntimeError("s3 unavailable")

        def put_object(self, **kwargs: Any) -> Any:
            raise RuntimeError("s3 unavailable")

    import teamagent.adapters.quota_store as quota_store

    monkeypatch.setattr(
        quota_store.VideoQuotaStore,
        "__init__",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unavailable cache must stop before quota")
        ),
    )
    gemini = _FakeGemini()
    cache = VideoAlgorithmResultCache(bucket="b", client=_UnavailableS3())

    with pytest.raises(RuntimeError, match="VIDEO_ALGORITHM_CACHE_UNAVAILABLE"):
        _skill(tmp_path, gemini, result_cache=cache).run(_input(), _ctx())
    assert gemini.video_calls == 0


def test_paid_core_result_write_retries_without_second_quota_or_gemini(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """課金後の最初のresult writeがtransientでも同じlease内でcoreを保存する。"""

    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    import teamagent.adapters.quota_store as quota_store

    consumed: list[int] = []

    def _consume(
        self: Any,
        email: str,
        count: int,
        *,
        request_id: str,
    ) -> QuotaResult:
        consumed.append(count)
        return QuotaResult(allowed=True, used=sum(consumed), limit=20)

    monkeypatch.setattr(quota_store.VideoQuotaStore, "try_consume", _consume)
    gemini = _FakeGemini()
    s3 = _TransientResultS3()
    cache = VideoAlgorithmResultCache(bucket="b", client=s3)

    output = _skill(tmp_path, gemini, result_cache=cache).run(_input(), _ctx())

    assert output.total_cost_usd == 0.01
    assert gemini.video_calls == 1
    assert consumed == [1]
    assert s3.result_failures == 0


def test_lost_lease_stops_before_backfill_quota(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    import teamagent.adapters.quota_store as quota_store

    consumed: list[int] = []

    def _consume(
        self: Any,
        email: str,
        count: int,
        *,
        request_id: str,
    ) -> QuotaResult:
        consumed.append(count)
        return QuotaResult(allowed=True, used=sum(consumed), limit=20)

    monkeypatch.setattr(quota_store.VideoQuotaStore, "try_consume", _consume)
    gemini = _FakeGemini()
    cache = VideoAlgorithmResultCache(bucket="b", client=_FakeS3())
    skill = _skill(tmp_path, gemini, count=3, result_cache=cache)
    analyzed_ranks: list[int] = []

    def _analyze(meta: VideoMeta, **kwargs: Any) -> AnalyzedVideo:
        analyzed_ranks.append(meta.rank)
        analysis = None if meta.rank == 1 else VideoVSEOAnalysis()
        return AnalyzedVideo(meta=meta, analysis=analysis, error=None if analysis else "failed")

    monkeypatch.setattr(skill, "_analyze_one", _analyze)
    original_renew = cache.renew_lease
    ownership_checks = 0

    def _lose_before_backfill(lease: Any, *, request_id: str) -> None:
        nonlocal ownership_checks
        ownership_checks += 1
        if ownership_checks == 2:
            raise VideoAlgorithmCacheLeaseLostError("taken over")
        original_renew(lease, request_id=request_id)

    monkeypatch.setattr(cache, "renew_lease", _lose_before_backfill)

    with pytest.raises(RuntimeError, match="VIDEO_ALGORITHM_LEASE_LOST"):
        skill.run(_input(max_videos=2), _ctx())
    assert consumed == [2]
    assert analyzed_ranks == [1, 2]


def test_heartbeat_thread_recovers_one_transient_failure_at_paid_boundary() -> None:
    """background失敗をloss扱いせず、同期renew成功後に処理を継続できる。"""

    class _TransientCache:
        lease_heartbeat_seconds = 0.01
        lease_retry_seconds = 10.0

        def __init__(self) -> None:
            self.renew_calls = 0
            self.first_failure = Event()

        def renew_lease(self, lease: Any, *, request_id: str) -> None:
            self.renew_calls += 1
            if self.renew_calls == 1:
                self.first_failure.set()
                raise VideoAlgorithmCacheLeaseUnavailableError("temporary")

    cache = _TransientCache()
    heartbeat = _LeaseHeartbeat(
        cache,  # type: ignore[arg-type]
        VideoAlgorithmCacheLease(object_key="lease", token="token", generation=1),
        "request",
    )
    heartbeat.start()
    assert cache.first_failure.wait(timeout=1)

    # threadは長いretry待ち中だが、課金後境界の同期renewが即時に回復確認する。
    heartbeat.assert_owned()
    heartbeat.close()

    assert cache.renew_calls == 2
    assert not heartbeat._thread.is_alive()


def test_paid_core_survives_artifact_failure_without_second_analysis(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    import teamagent.adapters.quota_store as quota_store

    consumed: list[int] = []

    def _consume(
        self: Any,
        email: str,
        count: int,
        *,
        request_id: str,
    ) -> QuotaResult:
        consumed.append(count)
        return QuotaResult(allowed=True, used=sum(consumed), limit=20)

    monkeypatch.setattr(quota_store.VideoQuotaStore, "try_consume", _consume)
    gemini = _FakeGemini()
    cache = VideoAlgorithmResultCache(bucket="b", client=_FakeS3(), ttl_seconds=60)
    skill = _skill(tmp_path, gemini, result_cache=cache)

    def _analyze_with_cover(meta: VideoMeta, **kwargs: Any) -> AnalyzedVideo:
        gemini.video_calls += 1
        return AnalyzedVideo(
            meta=meta,
            analysis=VideoVSEOAnalysis(),
            cover_data_uri="data:image/jpeg;base64,LIGHTWEIGHT",
            cost_usd=0.01,
            model_id=gemini.model_id,
        )

    monkeypatch.setattr(skill, "_analyze_one", _analyze_with_cover)
    monkeypatch.setattr(
        skill,
        "_publisher",
        lambda path, **kwargs: "https://example.invalid/original-report",
    )
    input_obj = VideoAlgorithmInput(
        query="新宿 ランチ",
        max_videos=1,
        board_size=5,
        outputs=["pptx"],
    )
    monkeypatch.setattr(
        skill,
        "_build_proposal_outputs",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pptx failed")),
    )

    with pytest.raises(RuntimeError, match="pptx failed"):
        skill.run(input_obj, _ctx("r1"))
    assert gemini.video_calls == 1
    assert consumed == [1]

    retried_covers: list[str] = []

    def _capture_retry_cover(out: VideoAlgorithmOutput, *args: Any, **kwargs: Any) -> None:
        retried_covers.append(out.videos[0].cover_data_uri)

    monkeypatch.setattr(skill, "_build_proposal_outputs", _capture_retry_cover)
    monkeypatch.setattr(
        skill,
        "_write_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("checkpointed report must not be regenerated")
        ),
    )
    retried = skill.run(input_obj, _ctx("r2"))
    assert retried.total_cost_usd == 0.0
    assert retried.report_url == "https://example.invalid/original-report"
    assert retried_covers == ["data:image/jpeg;base64,LIGHTWEIGHT"]
    assert gemini.video_calls == 1
    assert consumed == [1]


def test_quota_enabled_fails_closed_without_resolved_email(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    gemini = _FakeGemini()

    with pytest.raises(RuntimeError, match="VIDEO_QUOTA_IDENTITY_REQUIRED"):
        _skill(tmp_path, gemini).run(_input(), SkillContext(request_id="no-email"))
    assert gemini.video_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("quota_enabled", [False, True])
async def test_socket_mode_resolves_email_only_when_quota_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
    quota_enabled: bool,
) -> None:
    if quota_enabled:
        monkeypatch.setenv("VIDEO_QUOTA_ENABLED", "1")
    else:
        monkeypatch.delenv("VIDEO_QUOTA_ENABLED", raising=False)

    contexts: list[SkillContext] = []
    resolver_calls: list[str | None] = []

    class _FakeSkill:
        def run(self, input: VideoAlgorithmInput, ctx: SkillContext) -> VideoAlgorithmOutput:
            contexts.append(ctx)
            return VideoAlgorithmOutput(query=input.query, slack_summary="ok")

        def cleanup_output(self, output: VideoAlgorithmOutput) -> None:
            return None

    async def _resolve(user_id: str | None) -> str:
        resolver_calls.append(user_id)
        return ME

    dispatcher = object.__new__(SkillDispatcher)
    monkeypatch.setattr(dispatcher, "get_video_algorithm_skill", lambda: _FakeSkill())
    monkeypatch.setattr(dispatcher, "_resolve_user_email", _resolve)

    result = await dispatcher.run_video_algorithm("新宿 ランチ", "r", "U123")

    assert result == "ok"
    assert resolver_calls == (["U123"] if quota_enabled else [])
    assert contexts[0].metadata == ({"user_email": ME} if quota_enabled else {})
