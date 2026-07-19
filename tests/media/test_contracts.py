from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from teamagent.media.contracts import (
    MAX_JOB_BODY_BYTES,
    FrameOperation,
    S3ObjectRef,
    SlidesOperation,
    make_job_request,
    parse_job_request,
    semantic_request_sha256,
)
from teamagent.media.operations import _ffmpeg_input
from teamagent.media.security import (
    MediaSsrfError,
    public_dns_only,
    validate_acquire_url,
)


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


def test_semantic_request_hash_is_stable_across_delayed_timestamp_envelopes() -> None:
    operation = FrameOperation(
        kind="frame",
        source=_ref(),
        timecodes=(0.0, 1.5),
        width=320,
    )
    first = make_job_request(
        operation=operation,
        output_bucket="teamagent-media-test",
        request_fingerprint="req-1:delayed",
        now_epoch_s=100,
        timeout_s=300,
    )
    delayed = make_job_request(
        operation=operation,
        output_bucket="teamagent-media-test",
        request_fingerprint="req-1:delayed",
        now_epoch_s=220,
        timeout_s=300,
    )

    assert first.idempotency_key == delayed.idempotency_key
    assert first.idempotency_key == semantic_request_sha256(operation, "req-1:delayed")
    assert first.job_id == delayed.job_id
    assert first.created_at_epoch_s != delayed.created_at_epoch_s
    assert first.deadline_epoch_s != delayed.deadline_epoch_s
    assert first.payload_sha256 != delayed.payload_sha256


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


def test_job_is_expired_at_the_exact_absolute_deadline() -> None:
    request = make_job_request(
        operation=FrameOperation(kind="frame", source=_ref(), timecodes=(0.0,)),
        output_bucket="teamagent-media-test",
        request_fingerprint="exact-deadline",
        now_epoch_s=100,
        timeout_s=300,
    )

    with pytest.raises(ValueError, match="deadline exceeded"):
        request.assert_not_expired(now_epoch_s=request.deadline_epoch_s)


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
    assert (
        validate_acquire_url(
            "https://instagr.am/p/abc/",
            resolver=globally_routable,
        )
        == "https://instagr.am/p/abc/"
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


@pytest.mark.parametrize(
    "address",
    (
        "0.0.0.0",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "192.0.2.1",
        "198.18.0.1",
        "224.0.0.1",
        "255.255.255.255",
        "::",
        "::1",
        "::ffff:127.0.0.1",
        "2001:db8::1",
        "fc00::1",
        "fe80::1",
        "ff02::1",
    ),
)
def test_ssrf_guard_rejects_every_non_global_address(address: str) -> None:
    def resolver(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]

    with pytest.raises(MediaSsrfError, match="PRIVATE_ADDRESS"):
        validate_acquire_url(
            "https://www.youtube.com/watch?v=abc",
            resolver=resolver,
        )


def test_ssrf_guard_rejects_mixed_public_and_private_dns_answers() -> None:
    def resolver(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 443)),
        ]

    with pytest.raises(MediaSsrfError, match="PRIVATE_ADDRESS"):
        validate_acquire_url(
            "https://www.youtube.com/watch?v=abc",
            resolver=resolver,
        )


def test_connect_boundary_dns_guard_revalidates_every_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def rebound(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        nonlocal calls
        calls += 1
        address = "8.8.8.8" if calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", rebound)
    with public_dns_only():
        assert socket.getaddrinfo("www.youtube.com", 443)
        with pytest.raises(MediaSsrfError, match="DNS_PRIVATE_ADDRESS"):
            socket.getaddrinfo("www.youtube.com", 443)


def test_ffmpeg_staged_input_protocol_contract_blocks_network_protocols() -> None:
    assert _ffmpeg_input(Path("/tmp/staged.m3u8")) == [
        "-protocol_whitelist",
        "file,pipe",
        "-protocol_blacklist",
        "http,https,tcp,tls,udp,rtp,ftp,gopher,sftp,ssh",
        "-i",
        "/tmp/staged.m3u8",
    ]
