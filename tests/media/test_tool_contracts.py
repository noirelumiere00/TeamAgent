from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import sys
import time
import urllib.parse
import zlib
from pathlib import Path
from typing import Any, cast

import boto3
import pytest
from botocore.config import Config

from teamagent.media.contracts import ProxyOperation, S3ObjectRef, make_job_request
from teamagent.media.tool_contracts import (
    MAX_COMPLETION_BYTES,
    ToolControlError,
    parse_control_from_env,
)

_ATTEMPT_ID = "01234567-89ab-4cde-8fab-0123456789ab"
_SECRET = "a" * 64
_DISPATCHER = (
    Path(__file__).parents[2] / "infra" / "terraform" / "lambda" / "tiktok_dispatch" / "handler.py"
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _request() -> Any:
    job_id = "mj_0123456789abcdef01234567"
    body = b"source"
    source = S3ObjectRef(
        bucket="teamagent-media-test",
        key=f"media-jobs/{job_id}/input/source.mp4",
        version_id="version-1",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        content_type="video/mp4",
    )
    return make_job_request(
        operation=ProxyOperation(kind="proxy", source=source),
        output_bucket=source.bucket,
        request_fingerprint="roleless-tool-contract",
        now_epoch_s=1_000,
        timeout_s=300,
        job_id=job_id,
    )


def _signing_time(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _get_url(ref: S3ObjectRef) -> str:
    query = urllib.parse.urlencode(
        {
            "versionId": ref.version_id,
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": ("AKIATEST/19700101/ap-northeast-1/s3/aws4_request"),
            "X-Amz-Date": _signing_time(1_050),
            "X-Amz-Expires": "200",
            "X-Amz-SignedHeaders": "host;x-amz-checksum-mode",
            "X-Amz-Security-Token": "session-token",
            "X-Amz-Signature": "b" * 64,
        }
    )
    path = urllib.parse.quote(ref.key, safe="/")
    return f"https://{ref.bucket}.s3.amazonaws.com/{path}?{query}"


def _post(
    *,
    request: Any,
    key: str,
    maximum: int,
    policy_mutation: Any | None = None,
) -> dict[str, Any]:
    capability_sha256 = hashlib.sha256(_SECRET.encode()).hexdigest()
    fixed = {
        "x-amz-server-side-encryption": "AES256",
        "x-amz-checksum-algorithm": "SHA256",
        "x-amz-meta-job-id": request.job_id,
        "x-amz-meta-attempt-id": _ATTEMPT_ID,
        "x-amz-meta-attempt-version": "1",
        "x-amz-meta-capability-sha256": capability_sha256,
    }
    signing = {
        "x-amz-algorithm": "AWS4-HMAC-SHA256",
        "x-amz-credential": ("AKIATEST/19700101/ap-northeast-1/s3/aws4_request"),
        "x-amz-date": _signing_time(1_050),
        "x-amz-security-token": "session-token",
    }
    policy = {
        "expiration": dt.datetime.fromtimestamp(1_250, tz=dt.UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "conditions": [
            *({name: value} for name, value in fixed.items()),
            ["starts-with", "$x-amz-checksum-sha256", ""],
            ["starts-with", "$Content-Type", ""],
            ["content-length-range", 1, maximum + 64 * 1024],
            {"bucket": request.output_bucket},
            {"key": key},
            *({name: value} for name, value in signing.items()),
        ],
    }
    if policy_mutation is not None:
        policy_mutation(policy)
    return {
        "url": f"https://{request.output_bucket}.s3.amazonaws.com/",
        "fields": {
            **fixed,
            "key": key,
            **signing,
            "policy": base64.b64encode(_canonical(policy)).decode(),
            "x-amz-signature": "c" * 64,
        },
    }


def _control() -> dict[str, Any]:
    request = _request()
    source = request.operation.source
    assert source is not None
    output_key = f"{request.output_prefix}attempts/1/{_ATTEMPT_ID}/output/proxy"
    completion_key = f"{request.output_prefix}attempts/1/{_ATTEMPT_ID}/_COMPLETION.json"
    return {
        "schema_version": "1",
        "request": request.model_dump(mode="json"),
        "attempt_id": _ATTEMPT_ID,
        "attempt_version": 1,
        "capability_secret": _SECRET,
        "inputs": [{"ref": source.model_dump(mode="json"), "get_url": _get_url(source)}],
        "outputs": [
            {
                "name": "proxy",
                "key": output_key,
                "max_bytes": request.operation.limit_bytes,
                "post": _post(
                    request=request,
                    key=output_key,
                    maximum=request.operation.limit_bytes,
                ),
            }
        ],
        "completion": {
            "key": completion_key,
            "max_bytes": MAX_COMPLETION_BYTES,
            "post": _post(
                request=request,
                key=completion_key,
                maximum=MAX_COMPLETION_BYTES,
            ),
        },
    }


def _environment(
    value: dict[str, Any] | None = None,
    *,
    encoded: bytes | None = None,
) -> dict[str, str]:
    body = encoded if encoded is not None else _canonical(value or _control())
    return {
        "MEDIA_CONTROL_ZLIB_B64": base64.b64encode(zlib.compress(body, 9)).decode(),
        "MEDIA_CONTROL_SHA256": hashlib.sha256(body).hexdigest(),
    }


def test_parse_control_accepts_only_the_exact_roleless_capability() -> None:
    parsed = parse_control_from_env(_environment())

    assert parsed.request == _request()
    assert parsed.attempt_id == _ATTEMPT_ID
    assert parsed.attempt_version == 1
    assert isinstance(parsed.request.operation, ProxyOperation)
    assert parsed.inputs[0].ref == parsed.request.operation.source
    assert parsed.outputs[0].name == "proxy"
    assert parsed.completion.max_bytes == MAX_COMPLETION_BYTES
    assert "boto" not in repr(parsed).lower()
    assert _SECRET not in repr(parsed)
    assert "X-Amz-Signature" not in repr(parsed)


def test_real_dispatcher_presigns_a_control_the_roleless_parser_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    job_id = "mj_0123456789abcdef01234567"
    source = S3ObjectRef(
        bucket="teamagent-media-test",
        key=f"media-jobs/{job_id}/input/source.mp4",
        version_id="version-1",
        sha256=hashlib.sha256(b"source").hexdigest(),
        size=6,
        content_type="video/mp4",
    )
    request = make_job_request(
        operation=ProxyOperation(kind="proxy", source=source),
        output_bucket=source.bucket,
        request_fingerprint="real-dispatcher-control",
        now_epoch_s=now,
        timeout_s=300,
        job_id=job_id,
    )
    signing_client = boto3.client(
        "s3",
        region_name="ap-northeast-1",
        aws_access_key_id="AKIATEST",
        aws_secret_access_key="secret",
        aws_session_token="session-token",
        config=Config(signature_version="s3v4"),
    )

    class SigningS3:
        def head_object(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "VersionId": source.version_id,
                "ServerSideEncryption": "AES256",
                "ContentLength": source.size,
                "ContentType": source.content_type,
                "ChecksumSHA256": base64.b64encode(bytes.fromhex(source.sha256)).decode(),
                "Metadata": {"sha256": source.sha256},
            }

        def generate_presigned_url(self, *args: Any, **kwargs: Any) -> str:
            return cast(str, signing_client.generate_presigned_url(*args, **kwargs))

        def generate_presigned_post(self, **kwargs: Any) -> dict[str, Any]:
            return cast(dict[str, Any], signing_client.generate_presigned_post(**kwargs))

    name = f"_roleless_dispatcher_contract_{now}"
    spec = importlib.util.spec_from_file_location(name, _DISPATCHER)
    assert spec is not None and spec.loader is not None
    dispatcher = importlib.util.module_from_spec(spec)
    sys.modules[name] = dispatcher
    spec.loader.exec_module(dispatcher)
    monkeypatch.setattr(dispatcher, "_client", lambda *_args, **_kwargs: SigningS3())
    capability_sha256 = hashlib.sha256(_SECRET.encode()).hexdigest()
    environment_file, _slots, _completion_key, control_sha256 = dispatcher._build_control(
        json.loads(request.to_json_bytes()),
        {
            "attempt_id": _ATTEMPT_ID,
            "attempt_version": 1,
            "capability_secret": _SECRET,
            "capability_sha256": capability_sha256,
        },
        deadline_epoch_s=request.deadline_epoch_s,
    )
    packed = environment_file.decode().strip().split("=", 1)[1]

    parsed = parse_control_from_env(
        {
            "MEDIA_CONTROL_ZLIB_B64": packed,
            "MEDIA_CONTROL_SHA256": control_sha256,
        }
    )

    assert parsed.request == request
    assert parsed.inputs[0].ref == source


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_control_key",
        "bad_request_hash",
        "bad_attempt",
        "extra_input",
        "wrong_get_host",
        "unsigned_checksum_mode",
        "get_outlives_job",
        "wrong_output_name",
        "wrong_output_key",
        "wrong_output_size",
        "extra_output",
        "wrong_completion_key",
        "missing_attempt_metadata",
        "dynamic_integrity_field",
        "post_outlives_job",
        "post_extra_condition",
        "post_wrong_length",
    ],
)
def test_parse_control_rejects_any_broadened_or_mutated_capability(
    mutation: str,
) -> None:
    control = _control()
    request = _request()
    if mutation == "extra_control_key":
        control["implicit_authority"] = True
    elif mutation == "bad_request_hash":
        control["request"]["payload_sha256"] = "f" * 64
    elif mutation == "bad_attempt":
        control["attempt_id"] = "not-an-attempt"
    elif mutation == "extra_input":
        control["inputs"].append(copy.deepcopy(control["inputs"][0]))
    elif mutation == "wrong_get_host":
        control["inputs"][0]["get_url"] = control["inputs"][0]["get_url"].replace(
            ".s3.amazonaws.com",
            ".evil.example",
        )
    elif mutation == "unsigned_checksum_mode":
        control["inputs"][0]["get_url"] = control["inputs"][0]["get_url"].replace(
            "host%3Bx-amz-checksum-mode",
            "host",
        )
    elif mutation == "get_outlives_job":
        control["inputs"][0]["get_url"] = control["inputs"][0]["get_url"].replace(
            "X-Amz-Expires=200",
            "X-Amz-Expires=900",
        )
    elif mutation == "wrong_output_name":
        control["outputs"][0]["name"] = "other"
    elif mutation == "wrong_output_key":
        control["outputs"][0]["key"] += "-other"
    elif mutation == "wrong_output_size":
        control["outputs"][0]["max_bytes"] += 1
    elif mutation == "extra_output":
        control["outputs"].append(copy.deepcopy(control["outputs"][0]))
    elif mutation == "wrong_completion_key":
        control["completion"]["key"] += "-other"
    elif mutation == "missing_attempt_metadata":
        control["outputs"][0]["post"]["fields"].pop("x-amz-meta-attempt-id")
    elif mutation == "dynamic_integrity_field":
        control["outputs"][0]["post"]["fields"]["Content-Type"] = "video/mp4"
    elif mutation.startswith("post_"):
        post = _post(
            request=request,
            key=control["outputs"][0]["key"],
            maximum=control["outputs"][0]["max_bytes"],
            policy_mutation={
                "post_outlives_job": lambda policy: policy.update(
                    {"expiration": "1970-01-01T01:00:00Z"}
                ),
                "post_extra_condition": lambda policy: policy["conditions"].append(
                    ["starts-with", "$key", ""]
                ),
                "post_wrong_length": lambda policy: policy["conditions"].__setitem__(
                    8,
                    ["content-length-range", 1, 999_999_999],
                ),
            }[mutation],
        )
        control["outputs"][0]["post"] = post

    with pytest.raises(ToolControlError):
        parse_control_from_env(_environment(control))


def test_parse_control_rejects_noncanonical_or_duplicate_json() -> None:
    control = _control()
    pretty = json.dumps(control, indent=2).encode()
    with pytest.raises(ToolControlError, match="not canonical"):
        parse_control_from_env(_environment(encoded=pretty))

    canonical = _canonical(control)
    duplicate = canonical.replace(
        b'{"attempt_id":',
        b'{"schema_version":"1","attempt_id":',
        1,
    )
    with pytest.raises(ToolControlError, match="duplicate JSON key"):
        parse_control_from_env(_environment(encoded=duplicate))


def test_parse_control_rejects_digest_trailing_stream_and_decompression_bomb() -> None:
    environment = _environment()
    environment["MEDIA_CONTROL_SHA256"] = "f" * 64
    with pytest.raises(ToolControlError, match="digest"):
        parse_control_from_env(environment)

    body = _canonical(_control())
    trailing = zlib.compress(body) + zlib.compress(b"second-stream")
    environment = {
        "MEDIA_CONTROL_ZLIB_B64": base64.b64encode(trailing).decode(),
        "MEDIA_CONTROL_SHA256": hashlib.sha256(body).hexdigest(),
    }
    with pytest.raises(ToolControlError, match="compression"):
        parse_control_from_env(environment)

    bomb = b"x" * (769 * 1024)
    environment = {
        "MEDIA_CONTROL_ZLIB_B64": base64.b64encode(zlib.compress(bomb, 9)).decode(),
        "MEDIA_CONTROL_SHA256": hashlib.sha256(bomb).hexdigest(),
    }
    with pytest.raises(ToolControlError, match="compression"):
        parse_control_from_env(environment)


def test_parse_control_rejects_legacy_aws_authority_instead_of_a_capability() -> None:
    with pytest.raises(ToolControlError):
        parse_control_from_env(
            {
                "MEDIA_JOB_BUCKET": "teamagent-media-test",
                "MEDIA_JOBS_TABLE": "jobs",
                "AWS_ACCESS_KEY_ID": "legacy",
            }
        )
