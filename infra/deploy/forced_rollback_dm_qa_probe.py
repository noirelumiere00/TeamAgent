from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

(
    aws_bin,
    snapshot_path,
    apply_attempt_id,
    timeout_seconds_raw,
    evidence_bucket,
    evidence_prefix,
    encryption_kms_key_arn,
    signing_kms_key_arn,
) = sys.argv[1:]

ACCOUNT = "718959508629"
REGION = "ap-northeast-1"
CLUSTER = "teamagent-dev"
SERVICE = "teamagent-dev-openclaw"
CONTAINER = "openclaw"
MCP_SERVICE = "teamagent-dev-mcp"
MCP_CONTAINER = "teamagent-mcp"
LOG_GROUP = "/teamagent/dev/openclaw"
CANARY_SECRET = "teamagent/dev/openclaw/rollout-canary"
EVIDENCE_BUCKET = evidence_bucket
EVIDENCE_PREFIX = evidence_prefix.rstrip("/")
SIGNING_ALGORITHM = "RSASSA_PSS_SHA_256"
TRUSTED_AUTOMATION_ARN = (
    "arn:aws:sts::718959508629:assumed-role/"
    "teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker"
)
TASK_DEFINITION_RE = re.compile(
    r"^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/"
    r"teamagent-dev-openclaw:[1-9][0-9]*$"
)
MCP_TASK_DEFINITION_RE = re.compile(
    r"^arn:aws:ecs:ap-northeast-1:718959508629:task-definition/"
    r"teamagent-dev-mcp:[1-9][0-9]*$"
)
TASK_ARN_RE = re.compile(
    r"^arn:aws:ecs:ap-northeast-1:718959508629:task/"
    r"teamagent-dev/[0-9a-f]{32}$"
)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
KMS_ARN_RE = re.compile(r"^arn:aws:kms:ap-northeast-1:718959508629:key/[0-9a-f-]{36}$")
VERSION_ID_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,1024}$")
started_monotonic = time.monotonic()
deadline_monotonic = started_monotonic + int(timeout_seconds_raw)


class DmQaTimeoutError(RuntimeError):
    pass


def remaining() -> float:
    value = deadline_monotonic - time.monotonic()
    if value <= 0:
        raise DmQaTimeoutError("forced rollback DM QA exceeded its absolute timeout")
    return value


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def endpoint(service: str) -> str:
    if service == "s3api":
        return f"https://s3.{REGION}.amazonaws.com"
    return f"https://{service}.{REGION}.amazonaws.com"


def aws_json(
    service: str,
    operation: str,
    arguments: list[str],
) -> dict[str, object]:
    command = [
        aws_bin,
        service,
        operation,
        "--region",
        REGION,
        "--endpoint-url",
        endpoint(service),
        *arguments,
        "--no-cli-pager",
        "--no-paginate",
        "--output",
        "json",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=remaining(),
        )
    except subprocess.TimeoutExpired as exc:
        raise DmQaTimeoutError(
            f"forced rollback DM QA timed out during AWS {service} {operation}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[:1000]
        raise RuntimeError(f"AWS {service} {operation} failed: {detail}")
    try:
        value = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AWS {service} {operation} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"AWS {service} {operation} returned a non-object")
    return value


def cloudwatch_events(arguments: list[str]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    next_token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        page_arguments = list(arguments)
        if next_token is not None:
            page_arguments.extend(["--next-token", next_token])
        response = aws_json(
            "logs",
            "filter-log-events",
            page_arguments,
        )
        page = response.get("events")
        if not isinstance(page, list) or not all(isinstance(event, dict) for event in page):
            raise RuntimeError("CloudWatch DM QA correlation is malformed")
        events.extend(page)
        returned_token = response.get("nextToken")
        if returned_token is None:
            return events
        if not isinstance(returned_token, str) or not returned_token:
            raise RuntimeError("CloudWatch DM QA pagination token is malformed")
        if returned_token in seen_tokens:
            return events
        seen_tokens.add(returned_token)
        next_token = returned_token


def slack_api(method: str, token: str, body: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=min(15.0, remaining()),
        ) as response:
            raw = response.read()
    except TimeoutError as exc:
        raise DmQaTimeoutError(f"Slack {method} timed out") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise DmQaTimeoutError(f"Slack {method} timed out") from exc
        raise RuntimeError(f"Slack {method} failed") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Slack {method} returned invalid JSON") from exc
    if not isinstance(value, dict) or value.get("ok") is not True:
        reason = value.get("error") if isinstance(value, dict) else "invalid-response"
        raise RuntimeError(f"Slack {method} failed: {reason}")
    return value


def service_inventory(
    expected_task_definition: str,
    *,
    service_name: str = SERVICE,
    container_name: str = CONTAINER,
    log_stream_prefix: str = "openclaw/openclaw",
) -> tuple[dict[str, object], list[dict[str, str]]]:
    service_document = aws_json(
        "ecs",
        "describe-services",
        ["--cluster", CLUSTER, "--services", service_name],
    )
    services = service_document.get("services")
    failures = service_document.get("failures")
    if not isinstance(services, list) or len(services) != 1 or failures not in (None, []):
        raise RuntimeError("OpenClaw service description is incomplete")
    service = services[0]
    if not isinstance(service, dict):
        raise RuntimeError("OpenClaw service description is malformed")
    deployments = service.get("deployments")
    circuit_breaker = (
        service.get("deploymentConfiguration", {}).get(
            "deploymentCircuitBreaker",
            {},
        )
        if isinstance(service.get("deploymentConfiguration"), dict)
        else {}
    )
    desired_count = service.get("desiredCount")
    if (
        service.get("serviceName") != service_name
        or service.get("clusterArn") != f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/{CLUSTER}"
        or service.get("taskDefinition") != expected_task_definition
        or not isinstance(desired_count, int)
        or desired_count < 1
        or service.get("runningCount") != desired_count
        or service.get("pendingCount") != 0
        or circuit_breaker != {"enable": True, "rollback": True}
        or not isinstance(deployments, list)
        or len(deployments) != 1
        or deployments[0].get("status") != "PRIMARY"
        or deployments[0].get("rolloutState") != "COMPLETED"
        or deployments[0].get("taskDefinition") != expected_task_definition
    ):
        raise RuntimeError("OpenClaw service is not one exact stable deployment")

    listed = aws_json(
        "ecs",
        "list-tasks",
        [
            "--cluster",
            CLUSTER,
            "--service-name",
            service_name,
            "--desired-status",
            "RUNNING",
        ],
    )
    task_arns = listed.get("taskArns")
    if (
        not isinstance(task_arns, list)
        or len(task_arns) != desired_count
        or len(set(task_arns)) != len(task_arns)
        or not all(isinstance(item, str) and TASK_ARN_RE.fullmatch(item) for item in task_arns)
    ):
        raise RuntimeError("OpenClaw running task inventory is incomplete")
    described = aws_json(
        "ecs",
        "describe-tasks",
        ["--cluster", CLUSTER, "--tasks", *sorted(task_arns)],
    )
    tasks = described.get("tasks")
    task_failures = described.get("failures")
    if (
        not isinstance(tasks, list)
        or len(tasks) != desired_count
        or task_failures not in (None, [])
    ):
        raise RuntimeError("OpenClaw running task details are incomplete")
    inventory: list[dict[str, str]] = []
    for task in tasks:
        if not isinstance(task, dict):
            raise RuntimeError("OpenClaw running task detail is malformed")
        task_arn = task.get("taskArn")
        containers = task.get("containers")
        matching = (
            [
                container
                for container in containers
                if isinstance(container, dict) and container.get("name") == container_name
            ]
            if isinstance(containers, list)
            else []
        )
        if (
            not isinstance(task_arn, str)
            or task_arn not in task_arns
            or task.get("taskDefinitionArn") != expected_task_definition
            or task.get("lastStatus") != "RUNNING"
            or task.get("desiredStatus") != "RUNNING"
            or task.get("group") != f"service:{service_name}"
            or len(matching) != 1
            or matching[0].get("lastStatus") != "RUNNING"
        ):
            raise RuntimeError("OpenClaw running task is not bound to the active revision")
        task_id = task_arn.rsplit("/", 1)[-1]
        inventory.append(
            {
                "task_arn": task_arn,
                "task_definition_arn": expected_task_definition,
                "log_stream_name": f"{log_stream_prefix}/{task_id}",
            }
        )
    return service, sorted(inventory, key=lambda item: item["task_arn"])


def normalize_timestamp(value: str) -> str:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(dt.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def exact_version_download(
    *,
    key: str,
    version_id: str,
    expected_bytes: bytes,
    encryption_key_arn: str,
    destination: Path,
) -> tuple[dict[str, object], str]:
    metadata = aws_json(
        "s3api",
        "get-object",
        [
            "--bucket",
            EVIDENCE_BUCKET,
            "--key",
            key,
            "--version-id",
            version_id,
            "--expected-bucket-owner",
            ACCOUNT,
            str(destination),
        ],
    )
    downloaded = destination.read_bytes()
    downloaded_sha256 = sha256_bytes(downloaded)
    returned_version = metadata.get("VersionId")
    if (
        downloaded != expected_bytes
        or downloaded_sha256 != sha256_bytes(expected_bytes)
        or returned_version != version_id
        or metadata.get("ContentLength") != len(expected_bytes)
        or metadata.get("ServerSideEncryption") != "aws:kms"
        or metadata.get("SSEKMSKeyId") != encryption_key_arn
        or metadata.get("ObjectLockMode") != "COMPLIANCE"
        or not isinstance(metadata.get("ObjectLockRetainUntilDate"), str)
    ):
        raise RuntimeError("immutable DM QA exact-version download did not match")
    return metadata, downloaded_sha256


def persist_evidence(
    payload: dict[str, object],
) -> dict[str, object]:
    evidence_bytes = canonical_bytes(payload)
    evidence_sha256 = sha256_bytes(evidence_bytes)
    retain_until = (dt.datetime.now(dt.UTC) + dt.timedelta(days=3651)).replace(microsecond=0)
    retain_until_text = retain_until.strftime("%Y-%m-%dT%H:%M:%SZ")
    payload_key = f"{EVIDENCE_PREFIX}/{apply_attempt_id}/dm-qa/result.json"
    signature_key = f"{payload_key}.sig"

    with tempfile.TemporaryDirectory(prefix="teamagent-drill-dm-qa.") as raw_dir:
        directory = Path(raw_dir)
        evidence_path = directory / "result.json"
        digest_path = directory / "result.sha256"
        signature_path = directory / "result.sig"
        envelope_path = directory / "result.sig.json"
        downloaded_evidence = directory / "downloaded-result.json"
        downloaded_signature = directory / "downloaded-result.sig.json"
        evidence_path.write_bytes(evidence_bytes)
        digest_path.write_bytes(bytes.fromhex(evidence_sha256))
        signed = aws_json(
            "kms",
            "sign",
            [
                "--key-id",
                signing_kms_key_arn,
                "--message",
                f"fileb://{digest_path}",
                "--message-type",
                "DIGEST",
                "--signing-algorithm",
                SIGNING_ALGORITHM,
            ],
        )
        signature_base64 = signed.get("Signature")
        if (
            signed.get("KeyId") != signing_kms_key_arn
            or signed.get("SigningAlgorithm") != SIGNING_ALGORITHM
            or not isinstance(signature_base64, str)
        ):
            raise RuntimeError("KMS returned an invalid DM QA signature")
        try:
            signature_bytes = base64.b64decode(
                signature_base64,
                validate=True,
            )
        except (ValueError, TypeError) as exc:
            raise RuntimeError("KMS returned malformed DM QA signature bytes") from exc
        if len(signature_bytes) < 256:
            raise RuntimeError("KMS returned a short DM QA signature")
        signature_path.write_bytes(signature_bytes)
        envelope = {
            "schema_version": 1,
            "apply_attempt_id": apply_attempt_id,
            "payload_key": payload_key,
            "payload_sha256": evidence_sha256,
            "signing_kms_key_arn": signing_kms_key_arn,
            "signing_algorithm": SIGNING_ALGORITHM,
            "signature_base64": signature_base64,
        }
        envelope_bytes = canonical_bytes(envelope)
        envelope_path.write_bytes(envelope_bytes)

        def put_object(
            *,
            key: str,
            path: Path,
            content_type: str,
        ) -> str:
            response = aws_json(
                "s3api",
                "put-object",
                [
                    "--bucket",
                    EVIDENCE_BUCKET,
                    "--key",
                    key,
                    "--body",
                    str(path),
                    "--content-type",
                    content_type,
                    "--server-side-encryption",
                    "aws:kms",
                    "--ssekms-key-id",
                    encryption_kms_key_arn,
                    "--object-lock-mode",
                    "COMPLIANCE",
                    "--object-lock-retain-until-date",
                    retain_until_text,
                    "--expected-bucket-owner",
                    ACCOUNT,
                    "--if-none-match",
                    "*",
                ],
            )
            version_id = response.get("VersionId")
            if (
                not isinstance(version_id, str)
                or not VERSION_ID_RE.fullmatch(version_id)
                or version_id in {"null", "None"}
            ):
                raise RuntimeError("S3 did not return an exact DM QA VersionId")
            return version_id

        payload_version = put_object(
            key=payload_key,
            path=evidence_path,
            content_type="application/json",
        )
        signature_version = put_object(
            key=signature_key,
            path=envelope_path,
            content_type="application/json",
        )
        payload_metadata, downloaded_sha256 = exact_version_download(
            key=payload_key,
            version_id=payload_version,
            expected_bytes=evidence_bytes,
            encryption_key_arn=encryption_kms_key_arn,
            destination=downloaded_evidence,
        )
        _, downloaded_signature_sha256 = exact_version_download(
            key=signature_key,
            version_id=signature_version,
            expected_bytes=envelope_bytes,
            encryption_key_arn=encryption_kms_key_arn,
            destination=downloaded_signature,
        )
        verified = aws_json(
            "kms",
            "verify",
            [
                "--key-id",
                signing_kms_key_arn,
                "--message",
                f"fileb://{digest_path}",
                "--message-type",
                "DIGEST",
                "--signature",
                f"fileb://{signature_path}",
                "--signing-algorithm",
                SIGNING_ALGORITHM,
            ],
        )
        if (
            verified.get("KeyId") != signing_kms_key_arn
            or verified.get("SigningAlgorithm") != SIGNING_ALGORITHM
            or verified.get("SignatureValid") is not True
        ):
            raise RuntimeError("KMS DM QA signature verification failed")
        returned_retain_until = normalize_timestamp(
            str(payload_metadata["ObjectLockRetainUntilDate"])
        )
        return {
            "bucket": EVIDENCE_BUCKET,
            "key": payload_key,
            "version_id": payload_version,
            "sha256": evidence_sha256,
            "size": len(evidence_bytes),
            "content_type": "application/json",
            "object_lock_mode": "COMPLIANCE",
            "retain_until": returned_retain_until,
            "encryption_kms_key_arn": encryption_kms_key_arn,
            "signature": {
                "key": signature_key,
                "version_id": signature_version,
                "sha256": downloaded_signature_sha256,
                "verified": True,
            },
            "signer": {
                "kms_key_arn": signing_kms_key_arn,
                "algorithm": SIGNING_ALGORITHM,
            },
            "exact_version_redownload": {
                "requested_version_id": payload_version,
                "returned_version_id": payload_version,
                "sha256": downloaded_sha256,
                "size": len(evidence_bytes),
                "bytes_match": True,
            },
        }


def write_output(value: dict[str, object]) -> None:
    sys.stdout.buffer.write(canonical_bytes(value))
    sys.stdout.buffer.flush()


try:
    timeout_seconds = int(timeout_seconds_raw)
    if (
        timeout_seconds < 1
        or timeout_seconds > 300
        or evidence_bucket != "teamagent-dev-openclaw-rollout-evidence"
        or evidence_prefix != "forced-rollback-drills/"
        or not UUID_RE.fullmatch(apply_attempt_id)
        or not KMS_ARN_RE.fullmatch(encryption_kms_key_arn)
        or not KMS_ARN_RE.fullmatch(signing_kms_key_arn)
        or encryption_kms_key_arn == signing_kms_key_arn
    ):
        raise RuntimeError("forced rollback DM QA inputs are invalid")
    with open(snapshot_path, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    openclaw_task_definition = snapshot["taskdefs"]["openclaw"]["arn"]
    mcp_task_definition = snapshot["taskdefs"]["mcp"]["arn"]
    if not TASK_DEFINITION_RE.fullmatch(
        openclaw_task_definition
    ) or not MCP_TASK_DEFINITION_RE.fullmatch(mcp_task_definition):
        raise RuntimeError("forced rollback DM QA task definitions are invalid")

    caller = aws_json("sts", "get-caller-identity", [])
    if caller.get("Account") != ACCOUNT or caller.get("Arn") != TRUSTED_AUTOMATION_ARN:
        raise RuntimeError("forced rollback DM QA requires the trusted automation role")

    _, running_before = service_inventory(openclaw_task_definition)
    _, mcp_running_before = service_inventory(
        mcp_task_definition,
        service_name=MCP_SERVICE,
        container_name=MCP_CONTAINER,
        log_stream_prefix="mcp/teamagent-mcp",
    )
    secret_response = aws_json(
        "secretsmanager",
        "get-secret-value",
        [
            "--secret-id",
            CANARY_SECRET,
            "--version-stage",
            "AWSCURRENT",
        ],
    )
    secret_string = secret_response.get("SecretString")
    if not isinstance(secret_string, str):
        raise RuntimeError("forced rollback DM QA secret is not a JSON string")
    secret = json.loads(secret_string)
    if (
        not isinstance(secret, dict)
        or set(secret) != {"userToken", "channelId", "botUserId"}
        or not re.fullmatch(r"xoxp-[A-Za-z0-9-]{20,}", secret.get("userToken", ""))
        or not re.fullmatch(r"[CG][A-Z0-9]{8,}", secret.get("channelId", ""))
        or not re.fullmatch(r"U[A-Z0-9]{8,}", secret.get("botUserId", ""))
    ):
        raise RuntimeError("forced rollback DM QA secret has an invalid shape")

    nonce = os.urandom(12).hex()
    fragment_a = f"OPENCLAW_DRILL_DM_QA_{nonce[:12]}"
    fragment_b = nonce[12:]
    response_token = f"{fragment_a}{fragment_b}"
    prompt = (
        f"<@{secret['botUserId']}> forced rollback drill DM QA. "
        "Reply with only the concatenation of fragment A and fragment B, "
        f"with no separator or other text. Fragment A: {fragment_a}; "
        f"fragment B: {fragment_b}"
    )
    if response_token in prompt:
        raise RuntimeError("DM QA response token must not appear in the prompt")
    posted = slack_api(
        "chat.postMessage",
        secret["userToken"],
        {"channel": secret["channelId"], "text": prompt},
    )
    posted_ts = posted.get("ts")
    if not isinstance(posted_ts, str):
        raise RuntimeError("Slack did not return a DM QA message timestamp")
    matching_message: dict[str, object] | None = None
    try:
        reply_deadline = min(deadline_monotonic, time.monotonic() + 120)
        while time.monotonic() < reply_deadline:
            thread = slack_api(
                "conversations.replies",
                secret["userToken"],
                {
                    "channel": secret["channelId"],
                    "ts": posted_ts,
                    "limit": 100,
                    "inclusive": True,
                },
            )
            messages = thread.get("messages")
            if not isinstance(messages, list):
                raise RuntimeError("Slack DM QA thread is malformed")
            candidates = [
                message
                for message in messages
                if isinstance(message, dict)
                and message.get("ts") != posted_ts
                and message.get("user") == secret["botUserId"]
                and str(message.get("text", "")).strip() == response_token
            ]
            if len(candidates) == 1:
                matching_message = candidates[0]
                break
            if len(candidates) > 1:
                raise RuntimeError("Slack DM QA received duplicate exact replies")
            time.sleep(min(3.0, remaining()))
        if matching_message is None:
            raise DmQaTimeoutError("Slack DM QA exact reply timed out")
    finally:
        try:
            slack_api(
                "chat.delete",
                secret["userToken"],
                {"channel": secret["channelId"], "ts": posted_ts},
            )
        except Exception:
            pass

    reply_ts = matching_message.get("ts")
    if not isinstance(reply_ts, str):
        raise RuntimeError("Slack DM QA reply timestamp is invalid")
    try:
        posted_time_ms = int(float(posted_ts) * 1000)
        reply_time_ms = int(float(reply_ts) * 1000)
    except (ValueError, OverflowError) as exc:
        raise RuntimeError("Slack DM QA timestamps are malformed") from exc
    if reply_time_ms < posted_time_ms:
        raise RuntimeError("Slack DM QA reply predates the mention")
    start_time_ms = reply_time_ms - 5000
    streams = [item["log_stream_name"] for item in running_before]
    stream_to_task = {item["log_stream_name"]: item["task_arn"] for item in running_before}
    correlation: dict[str, object] | None = None
    correlation_deadline = min(deadline_monotonic, time.monotonic() + 60)
    while time.monotonic() < correlation_deadline:
        events = cloudwatch_events(
            [
                "--log-group-name",
                LOG_GROUP,
                "--log-stream-names",
                *streams,
                "--start-time",
                str(start_time_ms),
                "--end-time",
                str(int(time.time() * 1000) + 5000),
                "--filter-pattern",
                f'"{response_token}"',
            ]
        )
        matching_events = [
            event
            for event in events
            if isinstance(event, dict)
            and event.get("logStreamName") in stream_to_task
            and response_token in str(event.get("message", ""))
        ]
        matched_streams = sorted({str(event["logStreamName"]) for event in matching_events})
        if len(matched_streams) == 1:
            selected = sorted(
                (
                    event
                    for event in matching_events
                    if event.get("logStreamName") == matched_streams[0]
                ),
                key=lambda event: int(event.get("timestamp", 0)),
            )[0]
            selected_event_id = selected.get("eventId")
            selected_timestamp = selected.get("timestamp")
            if (
                not isinstance(selected_event_id, str)
                or not selected_event_id
                or type(selected_timestamp) is not int
                or selected_timestamp < reply_time_ms - 5000
                or selected_timestamp > reply_time_ms + 60_000
            ):
                raise RuntimeError("DM QA task-log correlation has invalid event binding")
            correlation = {
                "matched": True,
                "task_arn": stream_to_task[matched_streams[0]],
                "log_stream_name": matched_streams[0],
                "event_id": selected_event_id,
                "event_timestamp": selected_timestamp,
                "token_sha256": sha256_bytes(response_token.encode()),
            }
            break
        if len(matched_streams) > 1:
            raise RuntimeError("DM QA token appeared in multiple service task logs")
        time.sleep(min(2.0, remaining()))
    if correlation is None:
        raise DmQaTimeoutError("DM QA reply-to-task-log correlation timed out")

    _, running_after = service_inventory(openclaw_task_definition)
    _, mcp_running_after = service_inventory(
        mcp_task_definition,
        service_name=MCP_SERVICE,
        container_name=MCP_CONTAINER,
        log_stream_prefix="mcp/teamagent-mcp",
    )
    if running_before != running_after:
        raise RuntimeError("OpenClaw running task inventory changed during DM QA")
    if mcp_running_before != mcp_running_after:
        raise RuntimeError("MCP running task inventory changed during DM QA")
    verified_at_utc = dt.datetime.now(dt.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    evidence = {
        "kind": "teamagent-forced-rollback-dm-qa-evidence",
        "schema_version": 1,
        "result": "PASSED",
        "verified_at_utc": verified_at_utc,
        "apply_attempt_id": apply_attempt_id,
        "openclaw_task_definition_arn": openclaw_task_definition,
        "mcp_task_definition_arn": mcp_task_definition,
        "running_tasks_before": running_before,
        "running_tasks_after": running_after,
        "mcp_running_tasks_before": mcp_running_before,
        "mcp_running_tasks_after": mcp_running_after,
        "slack": {
            "connected": True,
            "exact_reply": True,
            "response_token_absent_from_prompt": True,
            "posted_ts": posted_ts,
            "reply_ts": reply_ts,
            "token_sha256": sha256_bytes(response_token.encode()),
            "correlation": correlation,
        },
    }
    locator = persist_evidence(evidence)
    write_output(
        {
            "kind": "teamagent-forced-rollback-dm-qa-result",
            "schema_version": 1,
            "result": "PASSED",
            "verified_at_utc": verified_at_utc,
            "applyAttemptId": apply_attempt_id,
            "openclawTaskDefinitionArn": openclaw_task_definition,
            "mcpTaskDefinitionArn": mcp_task_definition,
            "locator": locator,
        }
    )
except DmQaTimeoutError:
    print("FATAL: forced rollback DM QA probe timed out", file=sys.stderr)
    raise SystemExit(124) from None
except Exception:
    print("FATAL: forced rollback DM QA probe failed", file=sys.stderr)
    raise SystemExit(24) from None
