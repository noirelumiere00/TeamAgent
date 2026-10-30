"""Private library thumbnails; deliberately separate from expiring media job artifacts."""

from __future__ import annotations

import os
import re
from typing import Any

from botocore.exceptions import ClientError

THUMB_PREFIX = "video-library/thumbs/"


def thumbnail_key(platform: str, video_id: str) -> str:
    if platform not in {"tiktok", "instagram", "youtube"} or not re.fullmatch(
        r"[A-Za-z0-9_-]+", video_id
    ):
        raise ValueError("invalid_video_identity")
    return f"{THUMB_PREFIX}{platform}/{video_id}.jpg"


class VideoLibraryStorage:
    def __init__(self, *, bucket: str | None = None, client: Any = None) -> None:
        self.bucket = bucket if bucket is not None else os.environ["VIDEO_LIBRARY_S3_BUCKET"]
        if not self.bucket:
            raise ValueError("video_library_bucket_missing")
        self.client = client

    def _s3(self) -> Any:
        if self.client is None:
            import boto3

            self.client = boto3.client("s3")
        return self.client

    def put(self, platform: str, video_id: str, body: bytes) -> str:
        key = thumbnail_key(platform, video_id)
        self._s3().put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="image/jpeg")
        return key

    def get(self, platform: str, video_id: str) -> tuple[bytes, str] | None:
        try:
            obj = self._s3().get_object(Bucket=self.bucket, Key=thumbnail_key(platform, video_id))
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchKey", "404", "AccessDenied"}:
                # GetObject without ListBucket can return 403 for a missing key.
                return None
            raise
        stream = obj["Body"]
        try:
            return stream.read(), str(obj["ETag"])
        finally:
            stream.close()
