from __future__ import annotations

import json
import socket

import pytest
from pydantic import ValidationError

from teamagent.media.contracts import (
    MAX_JOB_BODY_BYTES,
    FrameOperation,
    S3ObjectRef,
    SlidesOperation,
    make_job_request,
    parse_job_request,
)
from teamagent.media.security import MediaSsrfError, validate_acquire_url


def _ref() -> S3ObjectRef:
    return S3ObjectRef(
        bucket="teamagent-media-test",
        key="media-jobs/mj_0123456789abcdef01234567/input/source.bin",
        sha256="a" * 64,
        size=4,
        content_type="video/mp4",
    )


def test_job_payload_is_strict_hashed_bounded_and_idempotent() -> None:
    operation = FrameOperation(
        kind="frame",
        source=_ref(),
        timecodes=(0.0, 1.5),
        width=320,
    )
    first = make_job_request(
        operation=operation,
        output_bucket="teamagent-media-test",
        request_fingerprint="req-1:frame",
        now_epoch_s=100,
        timeout_s=300,
    )
    second = make_job_request(
        operation=operation,
        output_bucket="teamagent-media-test",
        request_fingerprint="req-1:frame",
        now_epoch_s=100,
        timeout_s=300,
    )

    assert first == second
    assert first.job_id.startswith("mj_")
    assert len(first.to_json_bytes()) < MAX_JOB_BODY_BYTES
    assert parse_job_request(first.to_json_bytes()) == first

    tampered = json.loads(first.to_json_bytes())
    tampered["operation"]["width"] = 640
    with pytest.raises(ValidationError, match="payload_sha256 mismatch"):
        parse_job_request(json.dumps(tampered))


def test_job_contract_rejects_extra_fields_traversal_and_unbounded_deadline() -> None:
    with pytest.raises(ValidationError):
        S3ObjectRef(
            bucket="teamagent-media-test",
            key="media-jobs/x/input/../secret",
            sha256="b" * 64,
            size=1,
            content_type="text/plain",
        )

    with pytest.raises(ValidationError):
        SlidesOperation(
            kind="slides",
            html=_ref(),
            selector="section .slide",
            width=1280,
            height=720,
            device_scale_factor=2,
        )

    operation = FrameOperation(kind="frame", source=_ref(), timecodes=(0.0,))
    with pytest.raises(ValueError, match="timeout_s"):
        make_job_request(
            operation=operation,
            output_bucket="teamagent-media-test",
            request_fingerprint="req",
            now_epoch_s=100,
            timeout_s=901,
        )


def test_acquire_ssrf_guard_allows_only_public_youtube_tiktok_instagram() -> None:
    def public_resolver(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.10", 443))]

    # RFC 5737 documentation ranges are classified private/reserved by Python, so use a real
    # globally-routable DNS address in the injected deterministic result.
    def globally_routable(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    del public_resolver
    assert (
        validate_acquire_url(
            "https://www.youtube.com/watch?v=abc",
            resolver=globally_routable,
        )
        == "https://www.youtube.com/watch?v=abc"
    )
    assert (
        validate_acquire_url(
            "https://www.tiktok.com/@u/video/1",
            resolver=globally_routable,
        )
        == "https://www.tiktok.com/@u/video/1"
    )
    assert (
        validate_acquire_url(
            "https://www.instagram.com/reel/abc/",
            resolver=globally_routable,
        )
        == "https://www.instagram.com/reel/abc/"
    )

    with pytest.raises(MediaSsrfError, match="DOMAIN_BLOCKED"):
        validate_acquire_url("https://example.com/video", resolver=globally_routable)

    def private_resolver(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]

    with pytest.raises(MediaSsrfError, match="PRIVATE_ADDRESS"):
        validate_acquire_url(
            "https://www.youtube.com/watch?v=abc",
            resolver=private_resolver,
        )
