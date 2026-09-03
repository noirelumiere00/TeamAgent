"""MediaJobClient.find_staged / get_request（二段構えの冪等キーと当初要求の読み出し）。"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest

from teamagent.adapters.media_job import MediaJobClient, MediaJobError
from teamagent.media.contracts import TikTokAcquireOperation, make_job_request

_BUCKET = "teamagent-media-test"
_JOB = "tk_0123456789ab"
_AUDIT = "a" * 64
_BODY = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 40
_KEY = f"media-jobs/{_JOB}/input/apify-p01002.mp4"


class _MissingObjectError(Exception):
    def __init__(self) -> None:
        super().__init__("Not Found")
        self.response = {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}


class _BrokenS3Error(Exception):
    def __init__(self) -> None:
        super().__init__("AccessDenied")
        self.response = {
            "Error": {"Code": "AccessDenied"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        }


def _head(body: bytes, *, metadata: dict[str, str] | None = None) -> dict[str, Any]:
    digest = hashlib.sha256(body).hexdigest()
    return {
        "ContentLength": len(body),
        "ContentType": "video/mp4",
        "ChecksumSHA256": base64.b64encode(bytes.fromhex(digest)).decode("ascii"),
        "ServerSideEncryption": "AES256",
        "VersionId": "version-7",
        "Metadata": {"sha256": digest, "job-id": _JOB} if metadata is None else metadata,
    }


class _S3:
    def __init__(self, objects: dict[str, Any], *, raise_exc: Exception | None = None) -> None:
        self.objects = objects
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        if kwargs["Key"] not in self.objects:
            raise _MissingObjectError()
        return self.objects[kwargs["Key"]]


def _request() -> Any:
    return make_job_request(
        operation=TikTokAcquireOperation(
            kind="tiktok_acquire", keywords=("x", "y"), n_per_kw=1, videos_per_kw=2
        ),
        output_bucket=_BUCKET,
        request_fingerprint="fp",
        now_epoch_s=100,
        timeout_s=900,
        job_id=_JOB,
        output_prefix=f"media-jobs/{_JOB}/",
        audit_principal_hash=_AUDIT,
    )


class _Dynamo:
    def __init__(self, item: dict[str, Any] | None) -> None:
        self.item = item

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["Key"] == {"job_id": {"S": _JOB}}
        return {"Item": self.item} if self.item is not None else {}


class _Session:
    def __init__(self, *, s3: Any = None, ddb: Any = None) -> None:
        self._s3 = s3
        self._ddb = ddb

    def client(self, service: str, **_kwargs: Any) -> Any:
        return {"s3": self._s3, "dynamodb": self._ddb}[service]


def _client(*, s3: Any = None, ddb: Any = None) -> MediaJobClient:
    return MediaJobClient(
        session=_Session(s3=s3, ddb=ddb),
        queue_url="queue",
        table="jobs",
        bucket=_BUCKET,
        clock=lambda: 101.0,
    )


def test_find_staged_returns_none_when_object_is_missing() -> None:
    s3 = _S3({})
    assert (
        _client(s3=s3).find_staged(job_id=_JOB, name="apify-p01002.mp4", deadline_epoch_s=400)
        is None
    )
    assert s3.calls[0]["Key"] == _KEY
    assert s3.calls[0]["ChecksumMode"] == "ENABLED"


def test_find_staged_returns_verified_ref_for_core_written_object() -> None:
    s3 = _S3({_KEY: _head(_BODY)})
    ref = _client(s3=s3).find_staged(job_id=_JOB, name="apify-p01002.mp4", deadline_epoch_s=400)
    assert ref is not None
    assert ref.key == _KEY
    assert ref.version_id == "version-7"
    assert ref.sha256 == hashlib.sha256(_BODY).hexdigest()
    assert ref.size == len(_BODY)
    assert ref.content_type == "video/mp4"


def test_find_staged_ignores_object_without_core_sha_metadata() -> None:
    # 別の書き手（sha256 メタ無し）の object は「無い」扱い＝別物を掴んで再利用しない
    s3 = _S3({_KEY: _head(_BODY, metadata={"job-id": _JOB})})
    assert (
        _client(s3=s3).find_staged(job_id=_JOB, name="apify-p01002.mp4", deadline_epoch_s=400)
        is None
    )


def test_find_staged_rejects_bad_names_and_surfaces_non_404_failures() -> None:
    client = _client(s3=_S3({}))
    with pytest.raises(MediaJobError, match="MEDIA_INPUT_NAME_INVALID"):
        client.find_staged(job_id=_JOB, name="../x.mp4", deadline_epoch_s=400)
    with pytest.raises(MediaJobError, match="MEDIA_JOB_ID_INVALID"):
        client.find_staged(job_id="nope", name="apify-p.mp4", deadline_epoch_s=400)
    broken = _client(s3=_S3({}, raise_exc=_BrokenS3Error()))
    with pytest.raises(MediaJobError, match="MEDIA_ARTIFACT_HEAD_FAILED"):
        broken.find_staged(job_id=_JOB, name="apify-p.mp4", deadline_epoch_s=400)


def _item(request: Any, *, body: str | None = None, audit: str = _AUDIT) -> dict[str, Any]:
    return {
        "job_id": {"S": _JOB},
        "audit_principal_hash": {"S": audit},
        "request_json": {"S": request.to_json_bytes().decode() if body is None else body},
        "status": {"S": "done"},
    }


def test_get_request_reads_canonical_request_and_checks_owner() -> None:
    request = _request()
    client = _client(ddb=_Dynamo(_item(request)))
    loaded = client.get_request(_JOB, deadline_epoch_s=400, expected_audit_principal_hash=_AUDIT)
    assert loaded is not None
    assert loaded.job_id == _JOB
    assert loaded.operation.videos_per_kw == 2  # type: ignore[union-attr]
    assert loaded.operation.keywords == ("x", "y")  # type: ignore[union-attr]
    with pytest.raises(MediaJobError, match="MEDIA_JOB_AUDIT_PRINCIPAL_MISMATCH"):
        client.get_request(_JOB, deadline_epoch_s=400, expected_audit_principal_hash="b" * 64)
    assert _client(ddb=_Dynamo(None)).get_request(_JOB, deadline_epoch_s=400) is None


def test_get_request_rejects_tampered_request_json() -> None:
    request = _request()
    tampered = request.to_json_bytes().decode().replace('"videos_per_kw":2', '"videos_per_kw":9')
    client = _client(ddb=_Dynamo(_item(request, body=tampered)))
    with pytest.raises(MediaJobError, match="MEDIA_JOB_REQUEST_INVALID"):
        client.get_request(_JOB, deadline_epoch_s=400, expected_audit_principal_hash=_AUDIT)
