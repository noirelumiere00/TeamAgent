"""video_algorithm の TTL 付き結果キャッシュ契約（外部I/Oなし）。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from teamagent.adapters.video_algorithm_cache import (
    VideoAlgorithmCacheLeaseHeldError,
    VideoAlgorithmCacheLeaseLostError,
    VideoAlgorithmResultCache,
)
from teamagent.skills.video_algorithm.schema import (
    AnalyzedVideo,
    FrameShot,
    VideoAlgorithmOutput,
    VideoMeta,
)


class _PreconditionFailedError(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "PreconditionFailed"}}


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
            raise _PreconditionFailedError
        if expected := kwargs.get("IfMatch"):
            if self.etags.get(Key) != expected:
                raise _PreconditionFailedError
        self._version += 1
        self.store[Key] = Body
        self.etags[Key] = f'"v{self._version}"'


class _ResultRaceS3(_FakeS3):
    """結果read後・CAS write直前に別ownerの結果を差し込む。"""

    def __init__(self) -> None:
        super().__init__()
        self.race_key: str | None = None
        self.winner_body: bytes | None = None

    def put_object(self, Bucket: str, Key: str, Body: bytes, **kwargs: Any) -> None:  # noqa: N803
        if Key == self.race_key and kwargs.get("IfMatch") and self.winner_body is not None:
            self.race_key = None
            self._version += 1
            self.store[Key] = self.winner_body
            self.etags[Key] = f'"v{self._version}"'
        super().put_object(Bucket=Bucket, Key=Key, Body=Body, **kwargs)


def _key(**overrides: Any) -> str:
    args: dict[str, Any] = {
        "query": "新宿 ランチ",
        "max_videos": 5,
        "prompt_version": "v1",
        "model_id": "gemini-2.5-flash",
        "board_size": 30,
        "outputs": ["report", "slides"],
        "kw_set": ["新宿 ランチ", "渋谷 ランチ"],
    }
    args.update(overrides)
    return VideoAlgorithmResultCache.cache_key(**args)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("query", "渋谷 ランチ"),
        ("max_videos", 6),
        ("prompt_version", "v2"),
        ("model_id", "gemini-3"),
        ("board_size", 20),
        ("outputs", ["report"]),
        ("kw_set", ["新宿 ランチ"]),
    ],
)
def test_cache_key_includes_every_required_dimension(field: str, value: Any) -> None:
    assert _key(**{field: value}) != _key()
    assert len(_key()) == 64


def test_cache_key_also_separates_optional_result_inputs() -> None:
    base = _key()
    assert _key(client_name="顧客A") != base
    assert _key(search_volume=1200) != base
    assert _key(acquire_job_id="tk_0123456789ab", requester="a@example.com") != base
    # acquire成果物は同じ job_id でも所有者境界を跨いで再利用しない。
    assert _key(acquire_job_id="tk_0123456789ab", requester="a@example.com") != _key(
        acquire_job_id="tk_0123456789ab", requester="b@example.com"
    )


def test_result_roundtrip_has_mandatory_ttl_and_drops_local_path() -> None:
    now = [1000.0]
    s3 = _FakeS3()
    cache = VideoAlgorithmResultCache(
        bucket="b",
        client=s3,
        ttl_seconds=60,
        clock=lambda: now[0],
    )
    output = VideoAlgorithmOutput(
        query="新宿 ランチ",
        report_html_path="/tmp/request/report.html",
        report_url="https://example.invalid/report",
        total_cost_usd=0.12,
    )
    key = _key()
    lease = cache.acquire_lease(key, request_id="lease")
    assert lease is not None

    cache.put(
        key,
        output=output.model_dump(mode="json"),
        stage="complete",
        lease=lease,
        request_id="r1",
    )
    raw = json.loads(s3.store[f"analysis-cache/video-algorithm/{key}.json"].decode("utf-8"))
    assert raw["schema_version"] == 3
    assert raw["stage"] == "complete"
    assert raw["lease_generation"] == 1
    assert raw["created_at_epoch_s"] == 1000
    assert raw["expires_at_epoch_s"] == 1060
    assert raw["output"]["report_html_path"] is None

    hit = cache.get(key, request_id="r2")
    assert hit is not None
    assert hit.output["query"] == output.query
    assert hit.output["report_url"] == output.report_url
    assert hit.output["report_html_path"] is None

    now[0] = 1060.0
    assert cache.get(key, request_id="r3") is None


def test_missing_ttl_is_never_a_hit() -> None:
    s3 = _FakeS3()
    cache = VideoAlgorithmResultCache(
        bucket="b",
        prefix="analysis-cache/video-algorithm/",
        client=s3,
        clock=lambda: 1000.0,
    )
    key = _key()
    s3.store[f"analysis-cache/video-algorithm/{key}.json"] = json.dumps(
        {
            "schema_version": 3,
            "stage": "complete",
            "lease_generation": 1,
            "output": VideoAlgorithmOutput(query="x").model_dump(mode="json"),
        }
    ).encode("utf-8")

    assert cache.get(key, request_id="r") is None


def test_result_cache_drops_large_media_but_keeps_lightweight_cover_for_artifact_retry() -> None:
    s3 = _FakeS3()
    cache = VideoAlgorithmResultCache(bucket="b", client=s3)
    output = VideoAlgorithmOutput(
        query="x",
        videos=[
            AnalyzedVideo(
                meta=VideoMeta(rank=1),
                frames=[FrameShot(sec=1.0, caption="場面", data_uri="data:image/jpeg;base64,AAA")],
                video_data_uri="data:video/mp4;base64,BBB",
                cover_data_uri="data:image/jpeg;base64,CCC",
            )
        ],
    )
    lease = cache.acquire_lease(_key(), request_id="lease")
    assert lease is not None

    cache.put(
        _key(),
        output=output.model_dump(mode="json"),
        stage="paid_core",
        lease=lease,
        request_id="r",
    )

    payload = json.loads(s3.store[f"analysis-cache/video-algorithm/{_key()}.json"].decode("utf-8"))
    cached_video = payload["output"]["videos"][0]
    assert cached_video["video_data_uri"] == ""
    assert cached_video["cover_data_uri"] == "data:image/jpeg;base64,CCC"
    assert cached_video["frames"][0]["data_uri"] == ""
    assert cached_video["frames"][0]["caption"] == "場面"


def test_lease_blocks_competing_run_and_supports_release_takeover() -> None:
    now = [1000.0]
    s3 = _FakeS3()
    cache = VideoAlgorithmResultCache(
        bucket="b",
        client=s3,
        lease_seconds=600,
        clock=lambda: now[0],
    )
    key = _key()

    first = cache.acquire_lease(key, request_id="r1")
    assert first is not None
    assert first.generation == 1
    with pytest.raises(VideoAlgorithmCacheLeaseHeldError):
        cache.acquire_lease(key, request_id="r2")

    cache.release_lease(first, request_id="r1")
    second = cache.acquire_lease(key, request_id="r2")
    assert second is not None
    assert second.token != first.token
    assert second.generation == 2


def test_expired_lease_is_replaced_with_etag_compare_and_swap() -> None:
    now = [1000.0]
    cache = VideoAlgorithmResultCache(
        bucket="b",
        client=_FakeS3(),
        lease_seconds=301,
        clock=lambda: now[0],
    )
    first = cache.acquire_lease(_key(), request_id="r1")
    assert first is not None

    now[0] = 1301.0
    second = cache.acquire_lease(_key(), request_id="r2")
    assert second is not None
    assert second.token != first.token
    assert second.generation == 2


def test_lease_client_initialization_failure_returns_fail_closed_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = VideoAlgorithmResultCache(bucket="b")

    def _fail() -> Any:
        raise RuntimeError("no credentials")

    monkeypatch.setattr(cache, "_ensure_client", _fail)
    assert cache.acquire_lease(_key(), request_id="r") is None


def test_heartbeat_renewal_extends_active_lease() -> None:
    now = [1000.0]
    cache = VideoAlgorithmResultCache(
        bucket="b",
        client=_FakeS3(),
        lease_seconds=301,
        clock=lambda: now[0],
    )
    lease = cache.acquire_lease(_key(), request_id="r1")
    assert lease is not None

    now[0] = 1200.0
    cache.renew_lease(lease, request_id="r1")
    now[0] = 1302.0
    with pytest.raises(VideoAlgorithmCacheLeaseHeldError):
        cache.acquire_lease(_key(), request_id="r2")


def test_result_etag_cas_fences_takeover_after_ownership_check() -> None:
    s3 = _ResultRaceS3()
    cache = VideoAlgorithmResultCache(
        bucket="b",
        client=s3,
        clock=lambda: 1000.0,
    )
    key = _key()
    lease = cache.acquire_lease(key, request_id="r1")
    assert lease is not None
    assert cache.put(
        key,
        output=VideoAlgorithmOutput(query="old-core").model_dump(mode="json"),
        stage="paid_core",
        lease=lease,
        request_id="r1",
    )

    result_key = f"analysis-cache/video-algorithm/{key}.json"
    winner = json.loads(s3.store[result_key].decode("utf-8"))
    winner["stage"] = "complete"
    winner["lease_generation"] = 2
    winner["lease_token"] = "new-owner"
    winner["output"]["query"] = "new"
    s3.race_key = result_key
    s3.winner_body = json.dumps(winner, separators=(",", ":")).encode("utf-8")

    # cache.put 内のownership readは旧ownerのまま成功する。その直後の結果ETag CASが
    # takeoverを検出できなければ、この旧結果が新世代を上書きしてしまう。
    with pytest.raises(VideoAlgorithmCacheLeaseLostError):
        cache.put(
            key,
            output=VideoAlgorithmOutput(query="stale").model_dump(mode="json"),
            stage="complete",
            lease=lease,
            request_id="r1",
        )
    hit = cache.get(key, request_id="read")
    assert hit is not None
    assert hit.stage == "complete"
    assert hit.output["query"] == "new"


def test_complete_result_cannot_downgrade_to_paid_core_in_same_generation() -> None:
    cache = VideoAlgorithmResultCache(bucket="b", client=_FakeS3())
    key = _key()
    lease = cache.acquire_lease(key, request_id="r")
    assert lease is not None
    assert cache.put(
        key,
        output=VideoAlgorithmOutput(query="complete").model_dump(mode="json"),
        stage="complete",
        lease=lease,
        request_id="r",
    )
    assert cache.put(
        key,
        output=VideoAlgorithmOutput(query="downgrade").model_dump(mode="json"),
        stage="paid_core",
        lease=lease,
        request_id="r",
    )

    hit = cache.get(key, request_id="read")
    assert hit is not None
    assert hit.stage == "complete"
    assert hit.output["query"] == "complete"
