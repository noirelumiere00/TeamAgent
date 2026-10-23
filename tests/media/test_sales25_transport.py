"""Selected-media sender stays separate from legacy search and public tool defaults."""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.adapters.media_job import MediaJobClient, MediaJobError
from teamagent.media.contracts import MediaJobRequest, Sales25MediaOperation, TikTokAcquireOperation


@pytest.mark.parametrize("missing", [False, True])
def test_selected_media_sender_requires_both_artifacts(
    monkeypatch: pytest.MonkeyPatch, missing: bool
) -> None:
    client = MediaJobClient(queue_url="queue", table="jobs", bucket="media-test", clock=lambda: 100)
    operation = Sales25MediaOperation(
        kind="sales25_media",
        post_id="9000000010",
        url="https://www.tiktok.com/@synthetic/video/9000000010",
        media_type="video",
    )
    artifacts = {"media-manifest.json": b"{}", "media-bundle.zip": b"synthetic"}
    if missing:
        artifacts.pop("media-bundle.zip")
    calls: list[MediaJobRequest] = []

    def run_sync(
        request: MediaJobRequest, **kwargs: Any
    ) -> tuple[dict[str, bytes], dict[str, Any]]:
        assert kwargs["timeout_s"] == 900
        calls.append(request)
        return artifacts, {}

    monkeypatch.setattr(client, "run_sync", run_sync)
    if missing:
        with pytest.raises(MediaJobError, match="ARTIFACT_MISSING"):
            client.acquire_sales25_media(operation, request_fingerprint="synthetic-owner:run:post")
    else:
        assert (
            client.acquire_sales25_media(operation, request_fingerprint="synthetic-owner:run:post")
            == artifacts
        )
    assert len(calls) == 1
    assert calls[0].operation == operation
    assert calls[0].deadline_epoch_s - calls[0].created_at_epoch_s == 900


@pytest.mark.parametrize("flag", [None, "", "0", "false"])
def test_disabled_flag_does_not_add_selected_media_keys_to_search(
    monkeypatch: pytest.MonkeyPatch, flag: str | None
) -> None:
    monkeypatch.delenv("USE_OMIYAGE_SALES25", raising=False)
    if flag is not None:
        monkeypatch.setenv("USE_OMIYAGE_SALES25", flag)
    request = TikTokAcquireOperation(kind="tiktok_acquire", keywords=("synthetic",))
    assert request.model_dump(mode="json") == {
        "kind": "tiktok_acquire",
        "search_type": "keyword",
        "keywords": ["synthetic"],
        "n_per_kw": 10,
        "videos_per_kw": 2,
        "sort": "display",
        "artifact_mode": "full",
        "max_video_bytes": 30 * 1024 * 1024,
        "client": {"client": None, "client_short": None, "competitors": [], "industry": None},
    }
