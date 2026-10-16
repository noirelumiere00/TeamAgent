"""Opt-in private snapshots; capture errors never interrupt the omiyage report."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from teamagent.media.tiktok_capture import _capture_record

if TYPE_CHECKING:
    from teamagent.adapters.tiktok_scraper import TikTokSearchResult

CaptureUploader = Callable[[str, bytes, str], dict[str, Any]]


def capture_enabled() -> bool:
    return os.environ.get("FIRST_VISIT_CAPTURE", "").strip().lower() in {"1", "true", "yes"}


def upload_capture(key: str, body: bytes, content_type: str) -> dict[str, Any]:
    """Use the existing report bucket, without ACLs or shareable URLs."""
    bucket = os.environ.get("OMIYAGE_DECK_PLAN_BUCKET", "")
    if not bucket:
        raise ValueError("OMIYAGE_DECK_PLAN_BUCKET is required for first-visit capture")
    import boto3

    full_key = os.environ.get("OMIYAGE_DECK_PLAN_PREFIX", "omiyage/deck-plans/") + key
    response = boto3.client("s3").put_object(
        Bucket=bucket, Key=full_key, Body=body, ContentType=content_type, IfNoneMatch="*"
    )
    return {
        "bucket": bucket,
        "key": full_key,
        "version_id": response.get("VersionId"),
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
        "content_type": content_type,
    }


class CaptureSession:
    def __init__(
        self,
        request: Mapping[str, Any],
        request_id: str,
        owner_user_id: str | None,
        axes: Sequence[tuple[str, str, str]],
        requested: int,
        *,
        uploader: CaptureUploader,
        log: Any,
        omiyage_job_id: str | None = None,
    ) -> None:
        self._uploader = uploader
        self._log = log
        capture_id = uuid.uuid4().hex
        self._prefix = f"{datetime.now(UTC):%Y-%m-%d}/{request_id}/first-visit/{capture_id}"
        self._snapshot: dict[str, Any] = {
            "schema_version": 1,
            "capture_id": capture_id,
            "request_id": request_id,
            "omiyage_job_id": omiyage_job_id,
            "owner_user_id": owner_user_id,
            "request": dict(request),
            "axes": [
                {
                    "axis_id": f"axis_{i:02d}",
                    "role": role,
                    "label": label,
                    "query": query,
                    "requested": requested,
                    "attempts": [],
                }
                for i, (role, label, query) in enumerate(axes, 1)
            ],
        }
        self._relevance: list[dict[str, Any]] = []
        self._revision = 0

    def attempt_count(self, index: int) -> int:
        return len(self._snapshot["axes"][index]["attempts"])

    def record(self, index: int, evidence: Mapping[str, Any]) -> None:
        try:
            self._snapshot["axes"][index]["attempts"].append(copy.deepcopy(dict(evidence)))
            body = self._bytes(self._snapshot)
            sha = hashlib.sha256(body).hexdigest()
            ref = self._uploader(f"{self._prefix}/capture-{sha}.json", body, "application/json")
            self._revision += 1
            # Each revision is immutable, including when the report later fails.
            manifest = {
                "schema_version": 1,
                "capture": ref,
                "request_id": self._snapshot["request_id"],
                "omiyage_job_id": self._snapshot["omiyage_job_id"],
                "owner_user_id": self._snapshot["owner_user_id"],
            }
            self._uploader(
                f"{self._prefix}/{self._revision:04d}/capture-manifest.json",
                self._bytes(manifest),
                "application/json",
            )
            self._log.info(
                "first_visit_capture_saved",
                capture_id=self._snapshot["capture_id"],
                revision=self._revision,
                capture_ref=ref,
                omiyage_job_id=self._snapshot["omiyage_job_id"],
            )
        except Exception as exc:
            self._log.warning("first_visit_capture_save_failed", error_type=type(exc).__name__)

    def fallback(
        self,
        index: int,
        *,
        result: TikTokSearchResult | None = None,
        error_code: str | None = None,
    ) -> None:
        axis = self._snapshot["axes"][index]
        if result is not None:
            posts = [
                {
                    "provider_id": video.id,
                    "url": video.url,
                    "kw": result.query,
                    "rank_display": rank,
                    "title": video.desc,
                    "hashtags": list(video.hashtags),
                    "account_id": video.author.unique_id,
                    "account_name": video.author.nickname,
                    "followers": video.author.follower_count,
                    "cover_url": video.cover_url,
                    "create_time": video.create_time,
                    "duration": video.duration,
                    "plays": video.play_count,
                    "likes": video.digg_count,
                    "comments": video.comment_count,
                    "shares": video.share_count,
                    "saves": video.collect_count,
                }
                for rank, video in enumerate(result.videos, 1)
            ]
            self.record(
                index,
                _capture_record(
                    {"ok": True, "type": result.search_type, "posts": posts},
                    query=result.query,
                    search_type="keyword",
                    requested=axis["requested"],
                ),
            )
            return
        self.record(
            index,
            {
                "query": axis["query"],
                "requested": axis["requested"],
                "search_type": "keyword",
                "status": "failed" if error_code else "unknown",
                "error_code": error_code,
                "acquired_on": None,
                "attempted_on": datetime.now(UTC).isoformat(),
                "order_basis": None,
                "sessions": None,
                "stop_reason": error_code or "capture_unavailable",
                "unfiltered": True,
                "missingness_preserved": False,
                "posts": [],
                "shortfalls": [],
            },
        )

    def record_relevance(self, index: int, *, checked: bool, excluded_ids: Sequence[str]) -> None:
        try:
            axis = self._snapshot["axes"][index]
            self._relevance.append(
                {
                    "axis_id": axis["axis_id"],
                    "checked": checked,
                    "excluded": [
                        {"id": vid, "reason": "unrelated_to_requested_category"}
                        for vid in excluded_ids
                    ],
                }
            )
            self._uploader(
                f"{self._prefix}/relevance-{index + 1:02d}/relevance.json",
                self._bytes({"axes": self._relevance}),
                "application/json",
            )
        except Exception as exc:
            self._log.warning("first_visit_capture_save_failed", error_type=type(exc).__name__)

    @staticmethod
    def _bytes(value: Any) -> bytes:
        return (
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        ).encode()
