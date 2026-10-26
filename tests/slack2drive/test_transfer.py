"""Fake wire-level Slack/Drive failures, bounded streaming and idempotency."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from teamagent.adapters.slack_file_guard import SlackFileGuardError
from teamagent.slack2drive.transfer import CHUNK, DRIVE_API, DRIVE_ID, Transfer, TransferError


class Body(httpx.SyncByteStream):
    def __init__(self, data: bytes, *, broken: bool = False) -> None:
        self.data = data
        self.broken = broken
        self.yielded = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for start in range(0, len(self.data), 65536):
            if self.broken and start >= 65536:
                raise httpx.ReadError("simulated interrupted download")
            block = self.data[start : start + 65536]
            self.yielded += len(block)
            yield block

    def close(self) -> None:
        self.closed = True


class FakeApis:
    def __init__(self, data: bytes = b"%PDF-1.7\nsmall test document") -> None:
        self.data = data
        self.file: dict[str, Any] = {
            "id": "FTEST123",
            "mode": "hosted",
            "size": len(data),
            "mimetype": "application/pdf",
            "url_private_download": "https://files.slack.com/files-pri/T/F/document.pdf",
        }
        self.row = {"slack_file_id": "FTEST123", "drive_filename": "client_report.pdf"}
        self.requests: list[httpx.Request] = []
        self.streams: list[Body] = []
        self.files: dict[str, dict[str, Any]] = {}
        self.inventory_pages: dict[str, dict[str, Any]] | None = None
        self.info_status = 200
        self.info_error = ""
        self.download_status = 200
        self.download_redirect = ""
        self.download_broken = False
        self.second_data: bytes | None = None
        self.upload_status = 200
        self.upload_location = "https://www.googleapis.com/upload/drive/v3/files?upload_id=fake"
        self.uploaded = bytearray()
        self.chunk_sizes: list[int] = []
        self.upload_body_reads: list[int] = []
        self.lose_chunk_response = False
        self.rate_limit_chunk = False
        self.partial_chunk = False
        self.bad_checksum = False
        self.info_retries = 0
        self.download_retries = 0
        self.drive_retries = 0
        self.drive_retry_status = 429
        self.drive_retry_reason = "rateLimitExceeded"
        self.retry_after = "0"
        self.delays: list[float] = []
        self.saved: list[dict[str, Any]] = []
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle))
        self.transfer = Transfer(
            self.client,
            "test-slack-credential",
            lambda: {"Authorization": "Bearer test-google-credential"},
            sleep=self.delays.append,
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "slack.com":
            if self.info_retries:
                self.info_retries -= 1
                return httpx.Response(429, headers={"Retry-After": self.retry_after})
            body = (
                {"ok": False, "error": self.info_error}
                if self.info_error
                else {"ok": True, "file": self.file}
            )
            return httpx.Response(self.info_status, json=body)
        if request.url.host in {"files.slack.com", "slack-files.com"}:
            if self.download_retries:
                self.download_retries -= 1
                return httpx.Response(429, headers={"Retry-After": self.retry_after})
            if request.url.host == "files.slack.com" and self.download_redirect:
                return httpx.Response(302, headers={"Location": self.download_redirect})
            data = self.second_data if self.streams and self.second_data is not None else self.data
            stream = Body(data, broken=self.download_broken)
            self.streams.append(stream)
            return httpx.Response(
                self.download_status, headers={"Content-Type": "application/pdf"}, stream=stream
            )
        assert request.url.host == "www.googleapis.com", "unexpected outbound host"
        if self.drive_retries:
            self.drive_retries -= 1
            return httpx.Response(
                self.drive_retry_status,
                headers={"Retry-After": self.retry_after},
                json={"error": {"errors": [{"reason": self.drive_retry_reason}]}},
            )
        if request.method == "POST":
            assert not self.uploaded
            assert json.loads(request.content)["id"] == "reserved-id"
            return httpx.Response(self.upload_status, headers={"Location": self.upload_location})
        if request.method == "PUT":
            content_range = request.headers["Content-Range"]
            if content_range.startswith("bytes */"):
                return self.progress()
            if self.rate_limit_chunk:
                self.rate_limit_chunk = False
                return httpx.Response(429, headers={"Retry-After": "0"})
            self.chunk_sizes.append(len(request.content))
            self.upload_body_reads.append(self.streams[-1].yielded)
            start, end = content_range.split(" ")[1].split("/")[0].split("-")
            assert int(start) == len(self.uploaded), "chunk was sent twice"
            assert int(end) == len(self.uploaded) + len(request.content) - 1
            if self.partial_chunk:
                self.partial_chunk = False
                self.uploaded.extend(request.content[: len(request.content) // 2])
                return self.progress()
            self.uploaded.extend(request.content)
            if len(self.uploaded) == len(self.data):
                metadata = self.metadata()
                if self.bad_checksum:
                    metadata["sha256Checksum"] = "0" * 64
                self.files["reserved-id"] = metadata
            if self.lose_chunk_response:
                self.lose_chunk_response = False
                raise httpx.ReadError("simulated lost upload response", request=request)
            return self.progress()
        if request.url.path.endswith("/generateIds"):
            return httpx.Response(200, json={"ids": ["reserved-id"]})
        if str(request.url).split("?")[0] == DRIVE_API:
            if self.inventory_pages is not None:
                return httpx.Response(
                    200, json=self.inventory_pages[request.url.params.get("pageToken", "")]
                )
            return httpx.Response(200, json={"files": list(self.files.values())})
        file_id = request.url.path.rsplit("/", 1)[-1]
        if file_id == "folder-id":
            return httpx.Response(
                200,
                json={
                    "id": "folder-id",
                    "name": "99_一次倉庫",
                    "mimeType": "application/vnd.google-apps.folder",
                    "driveId": DRIVE_ID,
                    "trashed": False,
                    "capabilities": {"canAddChildren": True},
                },
            )
        return (
            httpx.Response(200, json=self.files[file_id])
            if file_id in self.files
            else httpx.Response(404)
        )

    def progress(self) -> httpx.Response:
        if len(self.uploaded) == len(self.data):
            return httpx.Response(200, json=self.files["reserved-id"])
        headers = {"Range": f"bytes=0-{len(self.uploaded) - 1}"} if self.uploaded else {}
        return httpx.Response(308, headers=headers)

    def metadata(self, **overrides: Any) -> dict[str, Any]:
        return {
            "id": "reserved-id",
            "name": self.row["drive_filename"],
            "size": str(len(self.data)),
            "sha256Checksum": hashlib.sha256(self.data).hexdigest(),
            "driveId": DRIVE_ID,
            "mimeType": "application/pdf",
            "trashed": False,
            "appProperties": {"slack_file_id": "FTEST123"},
            **overrides,
        }

    def run(
        self,
        *,
        commit: bool = False,
        state: dict[str, Any] | None = None,
        inventory: list[dict[str, Any]] | None = None,
        max_bytes: int = 50_000_000,
    ) -> dict[str, Any]:
        if state is None:
            state = {}
        self.transfer.one(
            self.row,
            state,
            inventory or [],
            "folder-id",
            lambda: self.saved.append(dict(state)),
            commit=commit,
            max_bytes=max_bytes,
        )
        return state


@pytest.fixture
def api() -> Iterator[FakeApis]:
    fake = FakeApis()
    try:
        yield fake
    finally:
        fake.client.close()


def test_dry_run_hashes_stream_but_never_writes_or_reserves_id(api: FakeApis) -> None:
    state = api.run()
    assert state["status"] == "would_upload"
    assert state["sha256"] == hashlib.sha256(api.data).hexdigest()
    assert all(request.method == "GET" for request in api.requests)
    assert not any(request.url.path.endswith("/generateIds") for request in api.requests)
    assert not api.saved
    assert len(api.streams) == 1 and api.streams[0].closed


def test_commit_checkpoints_reserved_id_before_create_and_checks_server_hash(api: FakeApis) -> None:
    state = api.run(commit=True)
    assert state["status"] == "saved"
    assert state["created"] is True
    assert api.saved == [{**state, "status": "uploading"}]
    assert bytes(api.uploaded) == api.data
    assert len(api.streams) == 2 and all(stream.closed for stream in api.streams)
    create = next(request for request in api.requests if request.method == "POST")
    assert json.loads(create.content)["parents"] == ["folder-id"]
    assert create.url.params["supportsAllDrives"] == "true"
    assert create.headers["X-Upload-Content-Length"] == str(len(api.data))


@pytest.mark.parametrize("stage", ["info", "download", "upload"])
@pytest.mark.parametrize("status", [401, 403, 404])
def test_http_denials_fail_closed(api: FakeApis, stage: str, status: int) -> None:
    setattr(api, f"{stage}_status", status)
    with pytest.raises(TransferError, match=f"^http_{status}$"):
        api.run(commit=True)
    assert not api.uploaded


@pytest.mark.parametrize("error", ["file_not_found", "missing_scope", "invalid_auth"])
def test_slack_logical_denial_uses_fixed_error_code(api: FakeApis, error: str) -> None:
    api.info_error = error
    with pytest.raises(TransferError, match=rf"^slack_{error}$"):
        api.run(commit=True)
    assert len(api.requests) == 1


def test_unknown_slack_error_does_not_leak_upstream_message(api: FakeApis) -> None:
    api.info_error = "untrusted upstream text"
    with pytest.raises(TransferError, match=r"^slack_files_info_denied$"):
        api.run(commit=True)


@pytest.mark.parametrize("stage", ["info", "download"])
def test_slack_429_retries_then_streams(api: FakeApis, stage: str) -> None:
    setattr(api, f"{stage}_retries", 2)
    assert api.run()["status"] == "would_upload"
    assert api.delays == [0, 0]


def test_drive_429_retries_id_reservation_without_duplicate_create(api: FakeApis) -> None:
    api.drive_retries = 2
    assert api.run(commit=True)["status"] == "saved"
    assert api.delays == [0, 0]
    assert sum(request.method == "POST" for request in api.requests) == 1


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded"])
def test_drive_403_rate_limit_response_retries(api: FakeApis, reason: str) -> None:
    api.drive_retries = 2
    api.drive_retry_status = 403
    api.drive_retry_reason = reason
    assert api.run(commit=True)["status"] == "saved"
    assert api.delays == [0, 0]


def test_drive_403_permission_denial_does_not_retry(api: FakeApis) -> None:
    api.drive_retries = 2
    api.drive_retry_status = 403
    api.drive_retry_reason = "insufficientFilePermissions"
    with pytest.raises(TransferError, match=r"^http_403$"):
        api.run(commit=True)
    assert not api.delays and not api.uploaded


@pytest.mark.parametrize("retry_after", ["61", "-1", "not-a-delay"])
def test_unbounded_or_invalid_retry_delay_is_rejected(api: FakeApis, retry_after: str) -> None:
    api.info_retries = 1
    api.retry_after = retry_after
    with pytest.raises(TransferError, match=r"retry_later|invalid_retry_after"):
        api.run()
    assert not api.delays


def test_rate_limit_exhaustion_is_bounded(api: FakeApis) -> None:
    api.info_retries = 99
    with pytest.raises(TransferError, match=r"^slack_retry_exhausted$"):
        api.run()
    assert len(api.requests) == 4


def test_interrupted_download_closes_stream_without_writing() -> None:
    api = FakeApis(b"a" * (CHUNK + 1))
    api.download_broken = True
    with api.client, pytest.raises(httpx.ReadError):
        api.run(commit=True)
    assert all(stream.closed for stream in api.streams)
    assert not api.saved and not api.uploaded


@pytest.mark.parametrize("failure", ["lose_chunk_response", "rate_limit_chunk"])
def test_upload_queries_acknowledged_range_before_retry(failure: str) -> None:
    api = FakeApis(b"a" * (CHUNK + 100))
    setattr(api, failure, True)
    with api.client:
        assert api.run(commit=True)["status"] == "saved"
    assert bytes(api.uploaded) == api.data
    assert api.chunk_sizes == [CHUNK, 100]
    assert any(
        request.headers.get("Content-Range", "").startswith("bytes */") for request in api.requests
    )


def test_final_chunk_lost_response_is_recovered_without_resending(api: FakeApis) -> None:
    api.lose_chunk_response = True
    assert api.run(commit=True)["status"] == "saved"
    assert api.chunk_sizes == [len(api.data)]


def test_partial_308_upload_resumes_at_server_acknowledged_offset() -> None:
    api = FakeApis(b"a" * (CHUNK + 100))
    api.partial_chunk = True
    with api.client:
        assert api.run(commit=True)["status"] == "saved"
    assert bytes(api.uploaded) == api.data
    assert api.chunk_sizes == [CHUNK, CHUNK // 2, 100]


def test_streams_bounded_chunks_without_reading_whole_second_download() -> None:
    api = FakeApis(b"a" * (CHUNK * 3 + 17))
    with api.client:
        api.run(commit=True)
    assert api.chunk_sizes == [CHUNK, CHUNK, CHUNK, 17]
    assert api.upload_body_reads == [CHUNK * 2, CHUNK * 3, len(api.data), len(api.data)]
    assert max(api.chunk_sizes) == CHUNK


def test_duplicate_bytes_reuses_existing_file_even_with_another_name(api: FakeApis) -> None:
    state = api.run(commit=True, inventory=[api.metadata(id="old-id", name="another.pdf")])
    assert state["status"] == "reused" and state["drive_id"] == "old-id"
    assert state["created"] is False
    assert not api.saved and not api.uploaded


def test_same_name_different_bytes_requires_review_without_overwrite(api: FakeApis) -> None:
    with pytest.raises(TransferError, match=r"^same_name_needs_review$"):
        api.run(commit=True, inventory=[api.metadata(sha256Checksum="0" * 64)])
    assert not api.saved and all(request.method == "GET" for request in api.requests)


def test_manifest_existing_reference_must_be_verified(api: FakeApis) -> None:
    api.row["existing_drive_id"] = "old-id"
    api.files["old-id"] = api.metadata(id="old-id", sha256Checksum="0" * 64)
    with pytest.raises(TransferError, match=r"^existing_reference_unverified$"):
        api.run(commit=True)
    assert not api.saved


def test_source_change_between_downloads_prevents_final_upload_commit() -> None:
    api = FakeApis(b"a" * (CHUNK + 17))
    api.second_data = b"b" * len(api.data)
    with api.client, pytest.raises(TransferError, match=r"^source_changed_between_downloads$"):
        api.run(commit=True)
    assert len(api.uploaded) == CHUNK
    assert not api.files


def test_second_download_with_trailing_bytes_never_commits_even_when_expected_hash_matches() -> (
    None
):
    api = FakeApis(b"a" * CHUNK)
    api.second_data = api.data + b"unexpected trailing bytes"
    with api.client, pytest.raises(TransferError, match=r"^download_size_overflow$"):
        api.run(commit=True)
    assert not api.uploaded and not api.files


def test_server_checksum_mismatch_never_marks_saved(api: FakeApis) -> None:
    api.bad_checksum = True
    state: dict[str, Any] = {}
    with pytest.raises(TransferError, match=r"^uploaded_checksum_mismatch$"):
        api.run(commit=True, state=state)
    assert state["status"] == "uploading"


def test_reserved_id_after_lost_success_recovers_without_new_upload(api: FakeApis) -> None:
    state = {"drive_id": "reserved-id", "created": True, "status": "uploading"}
    api.files["reserved-id"] = api.metadata()
    result = api.run(commit=True, state=state)
    assert result["status"] == "saved"
    assert not api.saved and not api.uploaded
    assert all(request.method == "GET" for request in api.requests)


@pytest.mark.parametrize(
    "overrides", [{"driveId": "another-drive"}, {"appProperties": {"slack_file_id": "OTHER"}}]
)
def test_reserved_id_recovery_requires_migration_ownership(
    api: FakeApis, overrides: dict[str, Any]
) -> None:
    state = {"drive_id": "reserved-id", "created": True, "status": "uploading"}
    api.files["reserved-id"] = api.metadata(**overrides)
    with pytest.raises(TransferError, match=r"^checkpoint_drive_identity_changed$"):
        api.run(commit=True, state=state)
    assert state["status"] == "uploading"
    assert not api.saved and not api.uploaded


def test_uncommitted_reserved_id_is_reused_for_restart(api: FakeApis) -> None:
    state = {"drive_id": "reserved-id", "created": True, "status": "uploading"}
    assert api.run(commit=True, state=state)["status"] == "saved"
    assert not any(request.url.path.endswith("/generateIds") for request in api.requests)


def test_missing_verified_file_requires_review_not_recreation(api: FakeApis) -> None:
    state = {"drive_id": "reserved-id", "created": True, "verified": True}
    with pytest.raises(TransferError, match=r"^completed_drive_file_missing$"):
        api.run(commit=True, state=state)
    assert not api.saved


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.invalid/stolen",
        "https://slack-files.com.attacker.invalid/file",
        "http://slack-files.com/file",
    ],
)
def test_hostile_redirect_is_never_requested(api: FakeApis, url: str) -> None:
    api.download_redirect = url
    with pytest.raises(SlackFileGuardError):
        api.run(commit=True)
    assert {request.url.host for request in api.requests} == {"slack.com", "files.slack.com"}


def test_allowed_cdn_redirect_drops_slack_authorization(api: FakeApis) -> None:
    api.download_redirect = "https://slack-files.com/files-pri-safe/file.pdf"
    assert api.run()["status"] == "would_upload"
    cdn = next(request for request in api.requests if request.url.host == "slack-files.com")
    origin = next(request for request in api.requests if request.url.host == "files.slack.com")
    assert "Authorization" not in cdn.headers
    assert origin.headers["Authorization"] == "Bearer test-slack-credential"


def test_hostile_upload_location_is_rejected_before_sending_google_token(api: FakeApis) -> None:
    api.upload_location = "https://attacker.invalid/upload"
    with pytest.raises(TransferError, match=r"^invalid_upload_location$"):
        api.run(commit=True)
    assert not any(request.method == "PUT" for request in api.requests)


@pytest.mark.parametrize("size", [0, -1, True, "25", 50_000_000])
def test_invalid_or_over_limit_size_never_downloads(api: FakeApis, size: object) -> None:
    api.file["size"] = size
    with pytest.raises(TransferError, match=r"^size_not_allowed$"):
        api.run(commit=True)
    assert not api.streams


def test_mime_must_match_approved_extension(api: FakeApis) -> None:
    api.file["mimetype"] = "application/x-executable"
    with pytest.raises(TransferError, match=r"^mime_not_allowed$"):
        api.run(commit=True)
    assert not api.streams


@pytest.mark.parametrize("difference", [-1, 1])
def test_truncated_or_excess_body_never_creates(api: FakeApis, difference: int) -> None:
    api.file["size"] = len(api.data) + difference
    with pytest.raises(TransferError, match=r"^download_size_(overflow|mismatch)$"):
        api.run(commit=True)
    assert not api.saved


def test_manifest_checksum_is_enforced_before_writes(api: FakeApis) -> None:
    api.row["sha256"] = "0" * 64
    with pytest.raises(TransferError, match=r"^manifest_checksum_mismatch$"):
        api.run(commit=True)
    assert not api.saved


def test_stop_prevents_every_network_request(api: FakeApis) -> None:
    api.transfer.stopped = lambda: True
    with pytest.raises(TransferError, match=r"^stopped$"):
        api.run(commit=True)
    assert not api.requests


def test_destination_requires_exact_writable_shared_drive_folder(api: FakeApis) -> None:
    api.transfer.destination("folder-id")
    with pytest.raises(TransferError, match=r"^destination_is_drive_root$"):
        api.transfer.destination(DRIVE_ID)
    assert len(api.requests) == 1


def test_inventory_reads_all_visible_shared_drives_and_every_page(api: FakeApis) -> None:
    api.inventory_pages = {
        "": {"files": [api.metadata(id="one")], "nextPageToken": "page2"},
        "page2": {"files": [api.metadata(id="two", driveId="another-drive")]},
    }
    assert [file["id"] for file in api.transfer.inventory()] == ["one", "two"]
    assert [request.url.params["pageToken"] for request in api.requests] == ["", "page2"]
    assert all(request.url.params["corpora"] == "allDrives" for request in api.requests)
    assert all(
        request.url.params["includeItemsFromAllDrives"] == "true" for request in api.requests
    )


def test_incomplete_inventory_cannot_be_used_for_deduplication(api: FakeApis) -> None:
    api.inventory_pages = {"": {"files": [], "incompleteSearch": True}}
    with pytest.raises(TransferError, match=r"^incomplete_drive_search$"):
        api.transfer.inventory()


def test_inventory_repeated_page_token_cannot_loop_forever(api: FakeApis) -> None:
    api.inventory_pages = {
        "": {"files": [], "nextPageToken": "repeated"},
        "repeated": {"files": [], "nextPageToken": "repeated"},
    }
    with pytest.raises(TransferError, match=r"^repeated_drive_page$"):
        api.transfer.inventory()
    assert len(api.requests) == 2
