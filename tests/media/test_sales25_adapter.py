from __future__ import annotations

import json
from typing import Any

import pytest

from teamagent.adapters.media_job import MediaJobClient, MediaJobError
from teamagent.media.contracts import MediaJobRequest


def _evidence(*, accepted: bool = True, empty: bool = False) -> dict[str, bytes]:
    videos = (
        []
        if empty
        else [
            {
                "id": "7000001",
                "url": "https://www.tiktok.com/@anonymous/photo/7000001",
                "rank_display": 1,
                "stats": {
                    "playCount": None,
                    "diggCount": 0,
                    "commentCount": 1,
                    "shareCount": 2,
                    "collectCount": 3,
                },
                "missingFields": ["stats.playCount"],
            }
        ]
    )
    raw = {
        "query": "synthetic",
        "fetched_at": "2026-10-23T01:02:03Z",
        "ok": accepted,
        "status": ("observed_zero" if empty else "ok") if accepted else "failed",
        "error_code": ("TIKTOK_TRULY_EMPTY" if empty else None) if accepted else "TIKTOK_CAPTCHA",
        "videos": videos,
        "sessions": 1,
        "order_basis": "search_display_order",
        "unfiltered": True,
        "missingness_preserved": True,
        "stop_reason": "exhausted" if accepted else "refused",
        "diagnostics": {"captchaDetected": not accepted, "cdnDenied": False},
        "owner_id": "untrusted-page-owner",
    }
    status = {
        "query": "synthetic",
        "accepted": accepted,
        "status": "partial" if accepted else "failed",
        "target": 100,
        "actual": len(videos),
        "shortfall": 100 - len(videos),
        "stop_reason": raw["stop_reason"],
        "diagnostics": raw["diagnostics"],
        "raw_ref": "sales25-raw.json",
    }
    return {
        "posts.json": b'{"posts":[]}',
        "sales25-raw.json": json.dumps(raw).encode(),
        "acquisition-status.json": json.dumps(status).encode(),
    }


def _client(
    monkeypatch: pytest.MonkeyPatch, artifacts: dict[str, bytes]
) -> tuple[MediaJobClient, list[MediaJobRequest]]:
    client = MediaJobClient(
        queue_url="https://sqs.example.invalid/media.fifo",
        table="media-jobs",
        bucket="teamagent-media-test",
        kms_key_id="test-key",
        clock=lambda: 1_000,
    )
    sent: list[MediaJobRequest] = []

    def run_sync(request: MediaJobRequest, **kwargs: Any) -> tuple[Any, Any]:
        sent.append(request)
        assert kwargs["timeout_s"] == 780
        assert "capture_result" in kwargs
        return artifacts, {}

    monkeypatch.setattr(client, "run_sync", run_sync)
    return client, sent


@pytest.mark.parametrize("accepted,empty", [(True, False), (True, True), (False, False)])
def test_sales25_returns_lossless_evidence_once_and_binds_trusted_owner(
    monkeypatch: pytest.MonkeyPatch, accepted: bool, empty: bool
) -> None:
    artifacts = _evidence(accepted=accepted, empty=empty)
    client, sent = _client(monkeypatch, artifacts)
    result = client.search_tiktok_sales25(
        "synthetic", owner_id="owner-anonymous", request_fingerprint="sales25-test"
    )
    assert len(sent) == 1
    assert sent[0].operation.model_dump(mode="json")["edition"] == "sales25"
    assert sent[0].operation.model_dump(mode="json")["n_per_kw"] == 100
    assert sent[0].deadline_epoch_s == 1_780
    expected = json.loads(artifacts["sales25-raw.json"])
    expected["owner_id"] = "owner-anonymous"
    assert result["raw"] == expected
    assert result["acquisition_status"] == json.loads(artifacts["acquisition-status.json"])
    assert result["acquisition_status"]["accepted"] is accepted
    assert result["media_job_id"] == sent[0].job_id


@pytest.mark.parametrize("missing", ["sales25-raw.json", "acquisition-status.json"])
def test_sales25_never_falls_back_to_lossy_posts_when_evidence_missing(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    artifacts = _evidence()
    del artifacts[missing]
    client, sent = _client(monkeypatch, artifacts)
    with pytest.raises(MediaJobError, match="SALES25_EVIDENCE_INVALID"):
        client.search_tiktok_sales25(
            "synthetic", owner_id="owner-anonymous", request_fingerprint="sales25-test"
        )
    assert len(sent) == 1


@pytest.mark.parametrize(
    "artifact,field,value",
    [
        ("acquisition-status.json", "actual", 0),
        ("acquisition-status.json", "target", True),
        ("acquisition-status.json", "accepted", 1),
        ("acquisition-status.json", "status", "complete"),
        ("sales25-raw.json", "sessions", 2),
        ("sales25-raw.json", "query", "other-query"),
        ("sales25-raw.json", "diagnostics", {"captchaDetected": True}),
        ("sales25-raw.json", "diagnostics", {"cdnDenied": True}),
    ],
)
def test_sales25_rejects_contradictory_acceptance_evidence(
    monkeypatch: pytest.MonkeyPatch, artifact: str, field: str, value: Any
) -> None:
    artifacts = _evidence()
    data = json.loads(artifacts[artifact])
    data[field] = value
    artifacts[artifact] = json.dumps(data).encode()
    client, _sent = _client(monkeypatch, artifacts)
    with pytest.raises(MediaJobError, match="SALES25_EVIDENCE_INVALID"):
        client.search_tiktok_sales25(
            "synthetic", owner_id="owner-anonymous", request_fingerprint="sales25-test"
        )


@pytest.mark.parametrize("owner,count", [("", 100), ("  ", 100), ("owner", 101)])
def test_sales25_rejects_invalid_input_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, owner: str, count: int
) -> None:
    client, sent = _client(monkeypatch, _evidence())
    with pytest.raises(ValueError):
        client.search_tiktok_sales25(
            "synthetic", owner_id=owner, max_videos=count, request_fingerprint="sales25-test"
        )
    assert sent == []


def test_sales25_job_identity_separates_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    client, sent = _client(monkeypatch, _evidence())
    for owner in ("owner-one", "owner-two"):
        client.search_tiktok_sales25(
            "synthetic", owner_id=owner, request_fingerprint="same-request"
        )
    assert sent[0].job_id != sent[1].job_id


def test_sales25_rejected_envelope_cannot_return_adoptable_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = _evidence(accepted=False)
    raw = json.loads(artifacts["sales25-raw.json"])
    raw["status"] = "ok"
    artifacts["sales25-raw.json"] = json.dumps(raw).encode()
    client, _sent = _client(monkeypatch, artifacts)
    with pytest.raises(MediaJobError, match="SALES25_EVIDENCE_INVALID"):
        client.search_tiktok_sales25(
            "synthetic", owner_id="owner-anonymous", request_fingerprint="sales25-test"
        )
