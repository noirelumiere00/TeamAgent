from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from teamagent.media.contracts import TikTokAcquireOperation
from teamagent.media.deadline import DeadlineBudget
from teamagent.media.operations import _tiktok_acquire


def _operation(capture: bool = False) -> TikTokAcquireOperation:
    values: dict[str, Any] = {
        "kind": "tiktok_acquire",
        "keywords": ("coffee",),
        "n_per_kw": 2,
        "videos_per_kw": 0,
        "artifact_mode": "metadata_only",
    }
    if capture:
        values["first_visit_capture"] = True
    return TikTokAcquireOperation(**values)


def test_capture_false_operation_serialization_is_identical() -> None:
    assert "first_visit_capture" not in _operation().model_dump(mode="json")
    assert _operation(True).model_dump(mode="json")["first_visit_capture"] is True


@pytest.mark.parametrize(
    "code,status", [("TIKTOK_TRULY_EMPTY", "observed_zero"), ("TIKTOK_BOT_WALL", "failed")]
)
def test_worker_capture_keeps_zero_response_before_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str, status: str
) -> None:
    node, scraper = tmp_path / "node", tmp_path / "search.mjs"
    node.touch()
    scraper.touch()
    monkeypatch.setenv("TIKTOK_NODE_BIN", str(node))
    monkeypatch.setenv("TIKTOK_SCRAPER_PATH", str(scraper))
    commands: list[list[str]] = []
    payload = {"ok": False, "errorCode": code, "videos": [], "diag": {"pagesFetched": 1}}

    def fake_process(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 2, json.dumps(payload).encode(), b"")

    monkeypatch.setattr("teamagent.media.operations._run_process", fake_process)
    result = _tiktok_acquire(_operation(True), tmp_path, DeadlineBudget(200, clock=lambda: 100))
    raw = next(
        artifact for artifact in result.artifacts if artifact.name == "first-visit-capture.json"
    )
    captures = json.loads(raw.path.read_text())["attempts"]
    assert captures[0]["status"] == status
    assert captures[0]["raw_response"] == payload
    assert "--first-visit-capture" in commands[0]
    assert json.loads(result.artifacts[0].path.read_text())["posts"] == []


@pytest.mark.parametrize("source_capture", [False, True])
def test_worker_capture_supports_both_scraper_shapes_without_changing_legacy_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_capture: bool
) -> None:
    node, scraper = tmp_path / "node", tmp_path / "search.mjs"
    node.touch()
    scraper.touch()
    monkeypatch.setenv("TIKTOK_NODE_BIN", str(node))
    monkeypatch.setenv("TIKTOK_SCRAPER_PATH", str(scraper))
    payload: dict[str, Any] = {
        "ok": True,
        "type": "keyword",
        "videos": [
            {
                "id": "12345678",
                "url": "https://www.tiktok.com/@u/video/12345678",
                "stats": {"playCount": 0, "diggCount": 7},
            }
        ],
    }
    if source_capture:
        payload["firstVisitCapture"] = {
            "missingness_preserved": True,
            "posts": [
                {
                    "provider_id": "12345678",
                    "plays": None,
                    "likes": 0,
                    "isAd": True,
                    "mediaType": "photo",
                    "imageCount": 3,
                    "missingFields": ["stats.playCount"],
                }
            ],
        }
    monkeypatch.setattr(
        "teamagent.media.operations._run_process",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps(payload).encode(), b""
        ),
    )
    before = _tiktok_acquire(_operation(), tmp_path, DeadlineBudget(200, clock=lambda: 100))
    before_bytes = before.artifacts[0].path.read_bytes()
    assert [artifact.name for artifact in before.artifacts] == ["posts.json"]
    after = _tiktok_acquire(_operation(True), tmp_path, DeadlineBudget(200, clock=lambda: 100))
    assert after.artifacts[0].path.read_bytes() == before_bytes
    assert after.metadata == before.metadata
    sidecar = next(a for a in after.artifacts if a.name == "first-visit-capture.json")
    attempt = json.loads(sidecar.path.read_bytes())["attempts"][0]
    assert attempt["raw_response"] == payload
    if source_capture:
        assert attempt["posts"] == payload["firstVisitCapture"]["posts"]
        assert attempt["missingness_preserved"] is True
    else:
        row = attempt["posts"][0]
        assert (row["provider_id"], row["kw"], row["rank_display"]) == ("12345678", "coffee", 1)
        assert (row["plays"], row["likes"]) == (0, 7)
        assert "stats.playCount" in row["unknownFields"]
        assert row["missingFields"] == []
        assert attempt["missingness_preserved"] is False
    assert attempt["shortfalls"] == [
        {
            "kw": "coffee",
            "requested": 2,
            "actual": 1,
            "reason": "MEDIA_TIKTOK_RESULT_SHORTFALL",
        }
    ]


def test_capture_sidecar_upload_failure_keeps_successful_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from teamagent.media.contracts import make_job_request
    from teamagent.media.operations import OperationOutput, ProducedArtifact
    from teamagent.media.worker import run_job
    from tests.media.test_worker import _Backend

    class Backend(_Backend):
        def upload_artifact(self, request: Any, lease: Any, artifact: Any) -> Any:
            if artifact.name == "first-visit-capture.json":
                raise OSError("capture S3 write failed")
            return super().upload_artifact(request, lease, artifact)

    def execute(*_args: Any, **kwargs: Any) -> OperationOutput:
        path = kwargs["workdir"] / "posts.json"
        path.write_bytes(b'{"posts":[]}')
        return OperationOutput(
            (
                ProducedArtifact("posts.json", path, "application/json"),
                ProducedArtifact("first-visit-capture.json", path, "application/json"),
            ),
            {},
        )

    monkeypatch.setattr("teamagent.media.worker.execute_operation", execute)
    request = make_job_request(
        operation=_operation(True),
        output_bucket="teamagent-media-test",
        request_fingerprint="capture-failure",
        now_epoch_s=100,
        timeout_s=300,
    )
    result = run_job(
        request, Backend(), temp_root=tmp_path, now_epoch_s=101, owner="worker", clock=lambda: 101
    )
    assert result.status == "done"
    assert [artifact.name for artifact in result.artifacts] == ["posts.json"]


def test_capture_sidecar_download_failure_keeps_legacy_artifact() -> None:
    from teamagent.adapters.media_job import MediaJobError
    from teamagent.media.contracts import MediaArtifact, MediaJobResult, make_job_request
    from tests.media.test_adapter import _LifecycleClient, _ref

    request = make_job_request(
        operation=_operation(True),
        output_bucket="teamagent-media-test",
        request_fingerprint="capture-download",
        now_epoch_s=100,
        timeout_s=300,
    )
    post_ref = _ref(request.job_id)
    capture_ref = post_ref.model_copy(update={"key": post_ref.key + "-capture"})

    class Client(_LifecycleClient):
        def download(self, ref: Any, *, deadline_epoch_s: int) -> bytes:
            if ref == capture_ref:
                raise MediaJobError("MEDIA_ARTIFACT_SHA256_INVALID")
            return super().download(ref, deadline_epoch_s=deadline_epoch_s)

    client = Client(
        MediaJobResult(
            job_id=request.job_id,
            status="done",
            artifacts=(
                MediaArtifact(name="posts.json", object=post_ref),
                MediaArtifact(name="first-visit-capture.json", object=capture_ref),
            ),
        )
    )
    artifacts, _metadata = client.run_sync(request)
    assert artifacts == {"posts.json": b"artifact"}


def test_media_client_capture_keeps_job_and_immutable_object_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.adapters.media_job import MediaJobClient
    from teamagent.media.contracts import MediaArtifact, MediaJobResult
    from tests.media.test_adapter import _ref

    client = MediaJobClient(
        queue_url="queue", table="jobs", bucket="teamagent-media-test", clock=lambda: 100
    )
    requests: list[Any] = []
    posts = [{"provider_id": "12345678", "url": "https://www.tiktok.com/@u/video/12345678"}]
    attempt = {
        "query": "coffee",
        "status": "ok",
        "posts": [{"plays": None, "likes": 0, "isAd": True}],
    }

    def run_sync(request: Any, **kwargs: Any) -> tuple[dict[str, bytes], dict[str, Any]]:
        requests.append(request)
        if hook := kwargs.get("capture_result"):
            hook(
                MediaJobResult(
                    job_id=request.job_id,
                    status="done",
                    metadata={"extra_provider_marker": "preserved"},
                    artifacts=(
                        MediaArtifact(name="first-visit-capture.json", object=_ref(request.job_id)),
                    ),
                )
            )
        return {
            "posts.json": json.dumps({"posts": posts}).encode(),
            "first-visit-capture.json": json.dumps({"attempts": [attempt]}).encode(),
        }, {}

    monkeypatch.setattr(client, "run_sync", run_sync)
    assert client.search_tiktok("coffee", request_fingerprint="same-request") == posts
    captured: list[dict[str, Any]] = []
    assert (
        client.search_tiktok("coffee", request_fingerprint="same-request", capture=captured.append)
        == posts
    )
    assert requests[0].job_id != requests[1].job_id
    assert "first_visit_capture" not in requests[0].operation.model_dump(mode="json")
    assert requests[1].operation.first_visit_capture is True
    assert captured[0]["media_job_id"] == requests[1].job_id
    assert captured[0]["posts"] == attempt["posts"]
    assert captured[0]["media_metadata"] == {"extra_provider_marker": "preserved"}
    assert captured[0]["media_result"]["status"] == "done"
    assert captured[0]["artifacts"][0]["object"] == _ref(requests[1].job_id).model_dump(mode="json")


@pytest.mark.parametrize("sidecar", [None, b'{"attempts":[]}', b"broken", b'{"attempts":[1]}'])
@pytest.mark.parametrize(
    "reason,status",
    [
        ("MEDIA_TIKTOK_RESULT_SHORTFALL", "ok"),
        ("MEDIA_TIKTOK_TRULY_EMPTY", "observed_zero"),
        ("MEDIA_TIKTOK_BOT_WALL", "failed"),
    ],
)
def test_media_client_falls_back_to_posts_when_optional_capture_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, sidecar: bytes | None, reason: str, status: str
) -> None:
    from teamagent.adapters.media_job import MediaJobClient
    from teamagent.media.contracts import MediaArtifact, MediaJobResult
    from tests.media.test_adapter import _ref

    client = MediaJobClient(
        queue_url="queue", table="jobs", bucket="teamagent-media-test", clock=lambda: 100
    )
    posts = (
        [
            {
                "provider_id": "12345678",
                "url": "https://www.tiktok.com/@u/video/12345678",
                "kw": "coffee",
                "rank_display": 7,
                "plays": 0,
                "likes": 8,
            }
        ]
        if status == "ok"
        else []
    )
    shortfalls = [{"kw": "coffee", "requested": 10, "actual": len(posts), "reason": reason}]
    requests: list[Any] = []

    def run_sync(request: Any, **kwargs: Any) -> tuple[dict[str, bytes], dict[str, Any]]:
        requests.append(request)
        kwargs["capture_result"](
            MediaJobResult(
                job_id=request.job_id,
                status="done",
                artifacts=(MediaArtifact(name="posts.json", object=_ref(request.job_id)),),
            )
        )
        artifacts = {"posts.json": json.dumps({"posts": posts, "shortfalls": shortfalls}).encode()}
        if sidecar is not None:
            artifacts["first-visit-capture.json"] = sidecar
        return artifacts, {
            "shortfalls": shortfalls,
            "search_types": {"coffee": "keyword(fallback)"},
        }

    monkeypatch.setattr(client, "run_sync", run_sync)
    captured: list[dict[str, Any]] = []
    assert (
        client.search_tiktok("coffee", request_fingerprint="fallback", capture=captured.append)
        == posts
    )
    assert len(captured) == 1
    record = captured[0]
    assert record["status"] == status
    assert record["query"] == "coffee"
    assert record["search_type"] == "keyword(fallback)"
    assert record["shortfalls"] == shortfalls
    assert record["media_job_id"] == requests[0].job_id
    assert record["artifacts"][0]["object"] == _ref(requests[0].job_id).model_dump(mode="json")
    assert record["missingness_preserved"] is False
    assert record["raw_response"]["posts"] == posts
    if posts:
        row = record["posts"][0]
        assert row["rank_display"] == 7
        assert row["plays"] == 0
        assert row["likes"] == 8
        assert "stats.playCount" in row["unknownFields"]
        assert "stats.diggCount" not in row["unknownFields"]
        assert row["missingFields"] == []
    else:
        assert record["posts"] == []


def test_worker_capture_write_failure_keeps_legacy_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node, scraper = tmp_path / "node", tmp_path / "search.mjs"
    node.touch()
    scraper.touch()
    monkeypatch.setenv("TIKTOK_NODE_BIN", str(node))
    monkeypatch.setenv("TIKTOK_SCRAPER_PATH", str(scraper))
    payload = {"ok": True, "type": "keyword", "videos": []}
    monkeypatch.setattr("teamagent.media.operations._node_json", lambda *_args, **_kwargs: payload)
    original_write = Path.write_text

    def write(path: Path, data: str, **kwargs: Any) -> int:
        if path.name == "first-visit-capture.json":
            raise OSError("capture filesystem full")
        return original_write(path, data, **kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    result = _tiktok_acquire(_operation(True), tmp_path, DeadlineBudget(200, clock=lambda: 100))
    assert [artifact.name for artifact in result.artifacts] == ["posts.json"]
    assert json.loads(result.artifacts[0].path.read_text())["posts"] == []


def test_worker_capture_works_without_core_adapter_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    node, scraper = tmp_path / "node", tmp_path / "search.mjs"
    node.touch()
    scraper.touch()
    monkeypatch.setenv("TIKTOK_NODE_BIN", str(node))
    monkeypatch.setenv("TIKTOK_SCRAPER_PATH", str(scraper))
    payload = {"ok": True, "type": "keyword", "videos": []}
    monkeypatch.setattr(
        "teamagent.media.operations._run_process",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps(payload).encode(), b""
        ),
    )
    original_import = builtins.__import__

    def runtime_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith(("teamagent.adapters", "structlog")):
            raise ImportError("not installed in media-runtime")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", runtime_import)
    result = _tiktok_acquire(_operation(True), tmp_path, DeadlineBudget(200, clock=lambda: 100))
    sidecar = next(
        artifact for artifact in result.artifacts if artifact.name == "first-visit-capture.json"
    )
    assert json.loads(sidecar.path.read_text())["attempts"][0]["raw_response"] == payload
