"""Legacy TikTok tool UX backed by the generic strict media-job contract.

The public skill still returns ``tk_*`` IDs and the existing output schema.  The
wire payload, idempotency, deadlines, storage integrity and short-lived
artifacts are shared with every other media operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any

import structlog

from teamagent.adapters.media_job import MediaJobClient, MediaJobError
from teamagent.media.contracts import (
    MediaJobResult,
    TikTokAcquireOperation,
    TikTokClientConfig,
    make_job_request,
)

logger = structlog.get_logger(__name__)

_DEFAULT_BUCKET = "teamagent-dev-raw-files"
_PRESIGN_S = 300
_ARTIFACT_TTL_S = 3600
_DEADLINE_S = 15 * 60


class TikTokTaskStore:
    """Compatibility adapter over the generic SQS/DynamoDB/S3 media boundary."""

    def __init__(self) -> None:
        self._region = os.environ.get("AWS_REGION") or "ap-northeast-1"
        self._queue_url = os.environ.get("MEDIA_TASK_QUEUE") or os.environ.get(
            "TIKTOK_TASK_QUEUE", ""
        )
        self._table = os.environ.get("MEDIA_JOBS_TABLE") or os.environ.get("TIKTOK_JOBS_TABLE", "")
        self._bucket = (
            os.environ.get("MEDIA_JOB_BUCKET")
            or os.environ.get("TIKTOK_S3_BUCKET")
            or _DEFAULT_BUCKET
        )
        self._kms_key_id = os.environ.get("MEDIA_JOB_KMS_KEY_ID", "")

    def _session(self) -> Any:
        import boto3

        return boto3.session.Session()

    def _client(self, session: Any) -> MediaJobClient:
        return MediaJobClient(
            session=session,
            queue_url=self._queue_url,
            table=self._table,
            bucket=self._bucket,
            kms_key_id=self._kms_key_id,
        )

    def submit(self, spec: dict[str, Any]) -> bool:
        """Validate and submit one bounded, content-hashed media request."""

        if not self._queue_url or not self._table:
            logger.warning(
                "tiktok_submit_misconfigured",
                has_queue=bool(self._queue_url),
                has_table=bool(self._table),
            )
            return False
        try:
            raw_client = spec.get("client") or {}
            if not isinstance(raw_client, dict):
                raise ValueError("client config must be an object")
            operation = TikTokAcquireOperation(
                kind="tiktok_acquire",
                keywords=tuple(spec["keywords"]),
                n_per_kw=spec["n_per_kw"],
                videos_per_kw=spec["videos_per_kw"],
                sort=spec["sort"],
                max_video_bytes=spec.get("max_video_bytes", 30 * 1024 * 1024),
                client=TikTokClientConfig(
                    client=raw_client.get("client"),
                    client_short=raw_client.get("client_short"),
                    competitors=tuple(raw_client.get("competitors") or ()),
                    industry=raw_client.get("industry"),
                ),
            )
            job_id = str(spec["job_id"])
            request_fingerprint = str(spec["request_fingerprint"])
            request = make_job_request(
                operation=operation,
                output_bucket=self._bucket,
                request_fingerprint=request_fingerprint,
                timeout_s=_DEADLINE_S,
                artifact_ttl_s=_ARTIFACT_TTL_S,
                job_id=job_id,
                output_prefix=f"tiktok-acquire/{job_id}/",
                audit_principal_hash=spec.get("audit_principal_hash"),
            )
            self._client(self._session()).submit(request)
            logger.info("tiktok_submitted", job_id=job_id, kw=len(operation.keywords))
            return True
        except (KeyError, TypeError, ValueError, MediaJobError) as exc:
            logger.warning(
                "tiktok_submit_failed",
                job_id=spec.get("job_id"),
                error=type(exc).__name__,
            )
            return False
        except Exception as exc:
            logger.warning(
                "tiktok_submit_failed",
                job_id=spec.get("job_id"),
                error=type(exc).__name__,
            )
            return False

    def get_status(self, job_id: str) -> dict[str, Any] | None:
        """Read generic status and map done artifacts to the legacy schema."""

        if not self._table:
            return None
        try:
            session = self._session()
            client = self._client(session)
            result = client.get_result(job_id)
            if result is None:
                return None
            output: dict[str, Any] = {
                "job_id": job_id,
                "status": result.status,
                "progress": result.metadata.get("progress"),
                "counts": result.metadata.get("counts"),
                "error_code": result.error_code,
                "stop_reason": result.metadata.get("stop_reason"),
                "warnings": result.metadata.get("warnings") or [],
            }
            if result.status == "done":
                output.update(self._presign_outputs(session, client, result))
            return output
        except Exception as exc:
            logger.warning("tiktok_status_failed", job_id=job_id, error=type(exc).__name__)
            return {
                "job_id": job_id,
                "status": "unknown",
                "error_code": "STATUS_READ_FAILED",
            }

    def _presign_outputs(
        self,
        session: Any,
        client: MediaJobClient,
        result: MediaJobResult,
    ) -> dict[str, Any]:
        """Presign verified, worker-produced artifacts for five minutes."""

        s3 = session.client("s3", region_name=self._region)
        artifacts = {artifact.name: artifact.object for artifact in result.artifacts}
        prefix = str(result.metadata.get("s3_prefix") or f"tiktok-acquire/{result.job_id}/")
        prefix = prefix if prefix.endswith("/") else f"{prefix}/"

        def presign(key: str) -> str | None:
            if not key.startswith(prefix):
                return None
            try:
                return str(
                    s3.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": self._bucket, "Key": key},
                        ExpiresIn=_PRESIGN_S,
                    )
                )
            except Exception:
                return None

        def artifact_url(name: str) -> str | None:
            ref = artifacts.get(name)
            return presign(ref.key) if ref is not None else None

        output: dict[str, Any] = {
            "s3_bucket": self._bucket,
            "s3_prefix": prefix,
            "posts_json_url": artifact_url("posts.json"),
            "config_json_url": artifact_url("config.json"),
            "manifest_url": artifact_url("manifest.json"),
            "videos": [],
        }
        manifest_ref = artifacts.get("manifest.json")
        if manifest_ref is None:
            return output
        try:
            manifest = json.loads(client.download(manifest_ref).decode("utf-8"))
            videos: list[dict[str, Any]] = []
            for item in manifest.get("items", []):
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("pid") or "")
                video_ref = artifacts.get(f"video-{pid}")
                thumb_ref = artifacts.get(f"thumb-{pid}")
                videos.append(
                    {
                        "pid": pid,
                        "kw": item.get("kw"),
                        "downloaded": video_ref is not None,
                        "s3_key": video_ref.key if video_ref is not None else None,
                        "url": presign(video_ref.key) if video_ref is not None else None,
                        "thumb_url": presign(thumb_ref.key) if thumb_ref is not None else None,
                        "tiktok_url": item.get("tiktok_url"),
                    }
                )
            output["videos"] = videos
        except Exception as exc:
            logger.info("tiktok_manifest_read_skipped", error=type(exc).__name__)
        return output


def new_job_id(request_fingerprint: str | None = None) -> str:
    if request_fingerprint:
        digest = hashlib.sha256(request_fingerprint.encode("utf-8")).hexdigest()
        return f"tk_{digest[:12]}"
    return f"tk_{uuid.uuid4().hex[:12]}"


__all__ = ["TikTokTaskStore", "new_job_id"]
