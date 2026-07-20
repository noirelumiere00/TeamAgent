from __future__ import annotations

import hashlib
import json

import pytest

from teamagent.adapters import tiktok_task_store as task_store_module
from teamagent.adapters.tiktok_task_store import TikTokTaskStore
from teamagent.media.contracts import (
    MediaArtifact,
    MediaJobRequest,
    MediaJobResult,
    S3ObjectRef,
)

_BUCKET = "teamagent-media-test"
_JOB_ID = "tk_0123456789ab"


def _ref(name: str, body: bytes) -> S3ObjectRef:
    return S3ObjectRef(
        bucket=_BUCKET,
        key=f"media-jobs/{_JOB_ID}/output/{name}",
        version_id="version-1",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        content_type="application/json" if name.endswith(".json") else "video/mp4",
    )


class _FakeMediaClient:
    def __init__(self, result: MediaJobResult | None = None) -> None:
        self.result = result
        self.submitted: MediaJobRequest | None = None
        self.get_deadlines: list[int] = []
        self.download_deadlines: list[int] = []
        self.presign_deadlines: list[int] = []

    def submit(self, request: MediaJobRequest) -> str:
        self.submitted = request
        return request.job_id

    def get_result(
        self,
        job_id: str,
        *,
        deadline_epoch_s: int,
        expected_audit_principal_hash: str | None = None,
    ) -> MediaJobResult | None:
        assert job_id == _JOB_ID
        assert expected_audit_principal_hash == "a" * 64
        self.get_deadlines.append(deadline_epoch_s)
        return self.result

    def download(self, ref: S3ObjectRef, *, deadline_epoch_s: int) -> bytes:
        assert ref.key.endswith("/manifest.json")
        self.download_deadlines.append(deadline_epoch_s)
        return json.dumps({"items": [{"pid": "p1", "kw": "coffee"}]}).encode()

    def presign_get(
        self,
        ref: S3ObjectRef,
        *,
        deadline_epoch_s: int,
        expires_s: int,
    ) -> str:
        assert expires_s == 600
        self.presign_deadlines.append(deadline_epoch_s)
        return f"https://s3.example.invalid/{ref.key}"


def _store(monkeypatch: pytest.MonkeyPatch, client: _FakeMediaClient) -> TikTokTaskStore:
    monkeypatch.setenv("MEDIA_TASK_QUEUE", "https://sqs.example.invalid/media.fifo")
    monkeypatch.setenv("MEDIA_JOBS_TABLE", "media-jobs")
    monkeypatch.setenv("MEDIA_JOB_BUCKET", _BUCKET)
    store = TikTokTaskStore()
    monkeypatch.setattr(store, "_session", lambda: object())
    monkeypatch.setattr(store, "_client", lambda _session: client)
    return store


def test_submit_fixes_one_absolute_deadline_before_request_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(task_store_module.time, "time", lambda: 1_000.9)
    client = _FakeMediaClient()
    store = _store(monkeypatch, client)

    assert store.submit(
        {
            "job_id": _JOB_ID,
            "request_fingerprint": "semantic-request",
            "keywords": ["coffee"],
            "n_per_kw": 1,
            "videos_per_kw": 1,
            "sort": "save_rate",
        }
    )
    assert client.submitted is not None
    assert client.submitted.created_at_epoch_s == 1_000
    assert client.submitted.deadline_epoch_s == 1_900


def test_done_status_reuses_one_deadline_for_read_download_and_presign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(task_store_module.time, "time", lambda: 2_000.9)
    manifest = _ref("manifest.json", b"manifest")
    video = _ref("video-p1", b"video")
    thumb = _ref("thumb-p1", b"thumb")
    result = MediaJobResult(
        job_id=_JOB_ID,
        status="done",
        artifacts=(
            MediaArtifact(name="manifest.json", object=manifest),
            MediaArtifact(name="video-p1", object=video),
            MediaArtifact(name="thumb-p1", object=thumb),
        ),
        metadata={"s3_prefix": f"media-jobs/{_JOB_ID}/"},
    )
    client = _FakeMediaClient(result)
    store = _store(monkeypatch, client)

    status = store.get_status(_JOB_ID, audit_principal_hash="a" * 64)

    assert status is not None
    assert status["status"] == "done"
    assert status["videos"][0]["url"].endswith(video.key)
    assert client.get_deadlines == [2_030]
    assert client.download_deadlines == [2_030]
    assert client.presign_deadlines
    assert set(client.presign_deadlines) == {2_030}
