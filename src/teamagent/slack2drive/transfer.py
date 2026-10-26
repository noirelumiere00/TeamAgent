"""Bounded Slack → Drive transfer; no file bodies or credentials in checkpoints."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx

from teamagent.adapters.slack_file_guard import (
    is_external_file,
    is_followable_redirect,
    validate_slack_file_redirect,
    validate_slack_file_url,
)

DRIVE_ID = "0AMqN_WOwh8uLUk9PVA"
CHUNK = 1024 * 1024  # Drive requires multiples of 256 KiB except the last chunk.
FIELDS = "id,name,mimeType,size,sha256Checksum,driveId,trashed,appProperties"
MIMES = {
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
}
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"


class TransferError(Exception):
    """Only fixed codes, never upstream exception text/URLs/response bodies."""


def require(ok: bool, code: str) -> None:
    if not ok:
        raise TransferError(code)


def retryable(response: httpx.Response) -> bool:
    if response.status_code == 429 or response.status_code >= 500:
        return True
    if response.status_code == 403:
        try:
            return any(
                item.get("reason") in ("rateLimitExceeded", "userRateLimitExceeded")
                for item in response.json().get("error", {}).get("errors", [])
            )
        except (ValueError, AttributeError, TypeError):
            return False
    return False


class Transfer:
    def __init__(
        self,
        http: httpx.Client,
        slack_token: str,
        drive_headers: Callable[[], dict[str, str]],
        *,
        sleep: Callable[[float], None] = time.sleep,
        stopped: Callable[[], bool] = lambda: False,
    ) -> None:
        self.http = http
        self.slack_token = slack_token
        self.drive_headers = drive_headers
        self.sleep = sleep
        self.stopped = stopped

    def check_stop(self) -> None:
        require(not self.stopped(), "stopped")

    def pause(self, response: httpx.Response, attempt: int) -> None:
        try:
            delay = float(response.headers.get("Retry-After", str(2**attempt)))
        except ValueError:
            raise TransferError("invalid_retry_after") from None
        require(0 <= delay <= 60, "retry_later")
        self.check_stop()
        self.sleep(delay)

    def drive(self, method: str, url: str = DRIVE_API, **kwargs: Any) -> httpx.Response:
        extra_headers = kwargs.pop("headers", {})
        for attempt in range(4):
            self.check_stop()
            response = self.http.request(
                method,
                url,
                headers={**self.drive_headers(), **extra_headers},
                follow_redirects=False,
                **kwargs,
            )
            if not retryable(response):
                return response
            self.pause(response, attempt)
        raise TransferError("drive_retry_exhausted")

    @staticmethod
    def status(response: httpx.Response, allowed: tuple[int, ...] = (200,)) -> None:
        require(response.status_code in allowed, f"http_{response.status_code}")

    def metadata(self, file_id: str) -> dict[str, Any] | None:
        require(bool(re.fullmatch(r"[A-Za-z0-9_-]+", file_id)), "invalid_drive_id")
        response = self.drive(
            "GET", f"{DRIVE_API}/{file_id}", params={"supportsAllDrives": "true", "fields": FIELDS}
        )
        if response.status_code == 404:
            return None
        self.status(response)
        return dict(response.json())

    def destination(self, folder_id: str) -> None:
        require(folder_id != DRIVE_ID, "destination_is_drive_root")
        require(bool(re.fullmatch(r"[A-Za-z0-9_-]+", folder_id)), "invalid_folder_id")
        response = self.drive(
            "GET",
            f"{DRIVE_API}/{folder_id}",
            params={
                "supportsAllDrives": "true",
                "fields": "id,name,mimeType,driveId,trashed,capabilities(canAddChildren)",
            },
        )
        self.status(response)
        folder = response.json()
        require(
            folder.get("id") == folder_id
            and folder.get("name") == "99_一次倉庫"
            and folder.get("mimeType") == "application/vnd.google-apps.folder"
            and folder.get("driveId") == DRIVE_ID
            and folder.get("trashed") is False
            and folder.get("capabilities", {}).get("canAddChildren") is True,
            "destination_not_writable_99_folder",
        )

    def inventory(self) -> list[dict[str, Any]]:
        # user corpus omits unopened shared-drive files; incomplete allDrives must fail closed.
        files: list[dict[str, Any]] = []
        page = ""
        seen: set[str] = set()
        while True:
            response = self.drive(
                "GET",
                params={
                    "q": "trashed = false",
                    "corpora": "allDrives",
                    "spaces": "drive",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                    "pageSize": 1000,
                    "pageToken": page,
                    "fields": f"nextPageToken,incompleteSearch,files({FIELDS})",
                },
            )
            self.status(response)
            body = response.json()
            require(not body.get("incompleteSearch"), "incomplete_drive_search")
            files.extend(body["files"])
            page = body.get("nextPageToken", "")
            if not page:
                return files
            require(page not in seen, "repeated_drive_page")
            seen.add(page)

    def info(self, file_id: str) -> dict[str, Any]:
        for attempt in range(4):
            self.check_stop()
            response = self.http.get(
                "https://slack.com/api/files.info",
                params={"file": file_id},
                headers={"Authorization": f"Bearer {self.slack_token}"},
                follow_redirects=False,
            )
            if response.status_code == 429 or response.status_code >= 500:
                self.pause(response, attempt)
                continue
            self.status(response)
            body = response.json()
            if body.get("error") == "ratelimited":
                self.pause(response, attempt)
                continue
            if body.get("ok") is not True:
                code = body.get("error")
                if code in (
                    "file_not_found",
                    "missing_scope",
                    "invalid_auth",
                    "token_revoked",
                    "not_authed",
                    "access_denied",
                ):
                    raise TransferError(f"slack_{code}")
                raise TransferError("slack_files_info_denied")
            file: dict[str, Any] = body["file"]
            require(
                file.get("id") == file_id and not is_external_file(file), "not_hosted_slack_file"
            )
            return file
        raise TransferError("slack_retry_exhausted")

    @contextmanager
    def download(self, file: dict[str, Any]) -> Iterator[httpx.Response]:
        url = validate_slack_file_url(file.get("url_private_download", ""))
        headers = {"Authorization": f"Bearer {self.slack_token}"}
        for hop in range(2):
            retry = False
            for attempt in range(4):
                self.check_stop()
                with self.http.stream(
                    "GET", url, headers=headers, follow_redirects=False
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        self.pause(response, attempt)
                        continue
                    if hop == 0 and is_followable_redirect(response.status_code):
                        url = validate_slack_file_redirect(response.headers.get("Location", ""))
                        headers = {}  # Signed CDN URL must never receive the Slack token.
                        retry = True
                        break
                    self.status(response)
                    content_type = response.headers.get("Content-Type", "").split(";")[0]
                    require(
                        content_type in (file["mimetype"], "application/octet-stream"),
                        "download_mime_mismatch",
                    )
                    require(
                        response.headers.get("Content-Encoding", "identity") == "identity",
                        "encoded_body",
                    )
                    yield response
                    return
            require(retry, "download_retry_exhausted")
        raise TransferError("download_redirect_exhausted")

    def chunks(self, file: dict[str, Any]) -> Iterator[bytes]:
        size = file["size"]
        count = 0
        pending = b""
        with self.download(file) as response:
            for chunk in response.iter_raw(CHUNK):
                self.check_stop()
                count += len(chunk)
                require(count <= size, "download_size_overflow")
                if pending:
                    yield pending
                pending = chunk
        require(count == size, "download_size_mismatch")
        if pending:
            yield pending  # Validate EOF before Drive can commit the final chunk.

    def digest(self, file: dict[str, Any]) -> str:
        digest = hashlib.sha256()
        for chunk in self.chunks(file):
            digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def matches(file: dict[str, Any], size: int, sha256: str) -> bool:
        return (
            not file.get("trashed", False)
            and str(file.get("size")) == str(size)
            and file.get("sha256Checksum") == sha256
        )

    def upload_chunk(self, url: str, chunk: bytes, offset: int, size: int) -> None:
        end = offset + len(chunk)
        cursor = offset
        for attempt in range(4):
            self.check_stop()
            try:
                response = self.http.put(
                    url,
                    headers={
                        **self.drive_headers(),
                        "Content-Range": f"bytes {cursor}-{end - 1}/{size}",
                        "Content-Type": "application/octet-stream",
                    },
                    content=chunk[cursor - offset :],
                    follow_redirects=False,
                )
            except httpx.TransportError:
                response = None
            if response is None or retryable(response):
                if response is not None:
                    self.pause(response, attempt)
                # A lost response may have committed the chunk: query before resending.
                response = self.drive(
                    "PUT", url, headers={"Content-Range": f"bytes */{size}"}, content=b""
                )
            if response.status_code in (200, 201):
                require(end == size, "upload_completed_early")
                return
            self.status(response, (308,))
            acknowledged = response.headers.get("Range", "")
            match = re.fullmatch(r"bytes=0-(\d+)", acknowledged)
            require(not acknowledged or match is not None, "upload_range_mismatch")
            received = int(match[1]) + 1 if match else 0
            require(cursor <= received <= end, "upload_range_mismatch")
            if received == end and end < size:
                return
            require(received < end, "upload_not_finalized")
            cursor = received
        raise TransferError("upload_retry_exhausted")

    def upload(
        self, file: dict[str, Any], row: dict[str, Any], state: dict[str, Any], folder_id: str
    ) -> dict[str, Any]:
        response = self.drive(
            "POST",
            UPLOAD_API,
            headers={
                "X-Upload-Content-Type": file["mimetype"],
                "X-Upload-Content-Length": str(file["size"]),
            },
            params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": FIELDS},
            json={
                "id": state["drive_id"],
                "name": row["drive_filename"],
                "mimeType": file["mimetype"],
                "parents": [folder_id],
                "appProperties": {"slack_file_id": row["slack_file_id"]},
            },
        )
        self.status(response, (200, 201))
        url = response.headers.get("Location", "")
        parsed = urlsplit(url)
        require(
            parsed.scheme == "https"
            and parsed.netloc == "www.googleapis.com"
            and parsed.path == "/upload/drive/v3/files",
            "invalid_upload_location",
        )
        # Session URLs are bearer capabilities: keep only in memory, never in the ledger.
        digest = hashlib.sha256()
        offset = 0
        for chunk in self.chunks(file):
            digest.update(chunk)
            if offset + len(chunk) == file["size"]:
                require(digest.hexdigest() == state["sha256"], "source_changed_between_downloads")
            self.upload_chunk(url, chunk, offset, file["size"])
            offset += len(chunk)
        uploaded = self.metadata(state["drive_id"])
        require(
            uploaded is not None
            and self.matches(uploaded, file["size"], state["sha256"])
            and uploaded.get("driveId") == DRIVE_ID,
            "uploaded_checksum_mismatch",
        )
        assert uploaded is not None
        return uploaded

    def one(
        self,
        row: dict[str, Any],
        state: dict[str, Any],
        inventory: list[dict[str, Any]],
        folder_id: str,
        save: Callable[[], None],
        *,
        commit: bool = False,
        max_bytes: int = 50_000_000,
    ) -> None:
        file = self.info(row["slack_file_id"])
        size = file.get("size")
        require(type(size) is int and 0 < size < max_bytes, "size_not_allowed")
        assert isinstance(size, int)
        require(row.get("size_bytes") in (None, "", size, str(size)), "manifest_size_mismatch")
        extension = "." + row["drive_filename"].rsplit(".", 1)[-1].lower()
        require(MIMES.get(extension) == file.get("mimetype"), "mime_not_allowed")
        sha256 = self.digest(file)
        require(row.get("sha256") in (None, "", sha256), "manifest_checksum_mismatch")
        require(state.get("sha256") in (None, sha256), "checkpoint_source_changed")
        state.update(sha256=sha256, size=size)
        if state.get("drive_id"):
            existing = self.metadata(state["drive_id"])
            if existing:
                require(self.matches(existing, size, sha256), "checkpoint_drive_changed")
                if state.get("created"):
                    require(
                        existing.get("driveId") == DRIVE_ID
                        and existing.get("appProperties", {}).get("slack_file_id")
                        == row["slack_file_id"],
                        "checkpoint_drive_identity_changed",
                    )
                state.update(status="saved" if state.get("created") else "reused", error="")
                return
            require(not state.get("verified"), "completed_drive_file_missing")
        existing_id = row.get("existing_drive_id")
        if existing_id:
            existing = self.metadata(existing_id)
            require(
                existing is not None and self.matches(existing, size, sha256),
                "existing_reference_unverified",
            )
            state.update(status="reused", drive_id=existing_id, created=False, error="")
            return
        for existing in inventory:
            if self.matches(existing, size, sha256):
                state.update(status="reused", drive_id=existing["id"], created=False, error="")
                return
        require(
            not any(
                f.get("name") in (row["drive_filename"], row.get("original_filename"))
                for f in inventory
            ),
            "same_name_needs_review",
        )
        if not commit:
            state.update(status="would_upload", error="")
            return
        if not state.get("drive_id"):
            response = self.drive(
                "GET", f"{DRIVE_API}/generateIds", params={"count": 1, "space": "drive"}
            )
            self.status(response)
            state.update(drive_id=response.json()["ids"][0], created=True)
        state.update(status="uploading", error="")
        save()  # Persist the reserved ID and source hash before any Drive create.
        uploaded = self.upload(file, row, state, folder_id)
        inventory.append(uploaded)
        state.update(status="saved", error="")
