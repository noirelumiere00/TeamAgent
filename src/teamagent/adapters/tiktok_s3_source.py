"""Owner-bound reader for immutable TikTok acquisition artifacts.

Downstream skills accept a job ID, never an arbitrary S3 prefix.  The job row is
read with the caller-derived audit principal hash, and every artifact download
uses the exact S3 VersionId recorded in the fenced result manifest.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

import structlog

from teamagent.adapters.media_job import MediaJobClient, MediaJobError
from teamagent.media.contracts import S3ObjectRef

logger = structlog.get_logger(__name__)

_JOB_ID = re.compile(r"^(?:mj_[0-9a-f]{24}|tk_[0-9a-f]{12})$")
_READ_DEADLINE_SECONDS = 30


def media_audit_principal_hash(requested_by: object) -> str:
    """Return the single audit-owner representation shared by media skills."""

    return hashlib.sha256(str(requested_by or "unknown").encode("utf-8")).hexdigest()


class TikTokS3Source:
    """Read one caller-owned acquisition job through its immutable result."""

    def __init__(
        self,
        job_id: str,
        *,
        audit_principal_hash: str,
        client: MediaJobClient | None = None,
        clock: Any = time.time,
    ) -> None:
        if not _JOB_ID.fullmatch(job_id):
            raise ValueError("TikTok acquisition job ID is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", audit_principal_hash):
            raise ValueError("TikTok acquisition audit principal is invalid")
        self._job_id = job_id
        self._audit_principal_hash = audit_principal_hash
        self._client = client or MediaJobClient()
        self._clock = clock
        self._posts: list[dict[str, Any]] | None = None
        self._url_to_ref: dict[str, S3ObjectRef] | None = None

    def _download_json(self, ref: S3ObjectRef, *, deadline_epoch_s: int) -> Any:
        raw = self._client.download(ref, deadline_epoch_s=deadline_epoch_s)
        if len(raw) > ref.size:
            raise MediaJobError("MEDIA_ARTIFACT_INTEGRITY_FAILED")
        return json.loads(raw.decode("utf-8"))

    def _ensure_loaded(self) -> None:
        if self._posts is not None:
            return
        deadline_epoch_s = int(self._clock()) + _READ_DEADLINE_SECONDS
        result = self._client.get_result(
            self._job_id,
            deadline_epoch_s=deadline_epoch_s,
            expected_audit_principal_hash=self._audit_principal_hash,
        )
        if result is None or result.status != "done":
            raise MediaJobError("MEDIA_TIKTOK_ACQUIRE_RESULT_NOT_READY")
        artifacts = {artifact.name: artifact.object for artifact in result.artifacts}
        posts_ref = artifacts.get("posts.json")
        if posts_ref is None:
            raise MediaJobError("MEDIA_TIKTOK_POSTS_ARTIFACT_MISSING")
        data = self._download_json(posts_ref, deadline_epoch_s=deadline_epoch_s)
        if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
            raise MediaJobError("MEDIA_TIKTOK_POSTS_ARTIFACT_INVALID")
        self._posts = [item for item in data["posts"] if isinstance(item, dict)]

        url_to_ref: dict[str, S3ObjectRef] = {}
        manifest_ref = artifacts.get("manifest.json")
        if manifest_ref is not None:
            try:
                manifest = self._download_json(manifest_ref, deadline_epoch_s=deadline_epoch_s)
                items = manifest.get("items", []) if isinstance(manifest, dict) else []
                for item in items:
                    if not isinstance(item, dict) or not item.get("downloaded"):
                        continue
                    pid = str(item.get("pid") or "")
                    url = str(item.get("tiktok_url") or "")
                    video_ref = artifacts.get(f"video-{pid}")
                    if url and video_ref is not None:
                        url_to_ref[url] = video_ref
            except Exception as exc:
                logger.info("tiktok_s3_manifest_skipped", error=type(exc).__name__)
        self._url_to_ref = url_to_ref

    def posts(self, n: int | None = None) -> list[dict[str, Any]]:
        """Return immutable normalized posts in display-rank order."""

        self._ensure_loaded()
        posts = self._posts or []
        return posts[:n] if n else posts

    def download(self, url: str) -> tuple[bytes, str]:
        """Download the exact VersionId mapped to a manifest-owned TikTok URL."""

        self._ensure_loaded()
        ref = (self._url_to_ref or {}).get(url)
        if ref is None:
            raise FileNotFoundError(f"no downloaded video for URL in owned manifest: {url[:60]}")
        deadline_epoch_s = int(self._clock()) + _READ_DEADLINE_SECONDS
        return self._client.download(ref, deadline_epoch_s=deadline_epoch_s), ref.content_type


__all__ = ["TikTokS3Source", "media_audit_principal_hash"]
