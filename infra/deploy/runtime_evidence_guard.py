#!/usr/bin/env python3
"""Fail-closed AWS evidence primitives for the Terraform runtime guard.

This module intentionally uses the pinned AWS CLI executable instead of a
provider SDK.  Every call has an explicit regional AWS endpoint, explicit
pagination, a parsed AWS HTTP Date, and a request identifier.  The shell guard
is the only operator entrypoint; this file is a checked-in implementation
detail used by that entrypoint at plan and apply.
"""

from __future__ import annotations

import argparse
import ast
import base64
import datetime as dt
import email.utils
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ACCOUNT_ID = "718959508629"
REGION = "ap-northeast-1"
APPROVED_EMAIL = "s-komata@vectorinc.co.jp"
CANONICAL_TOPIC = (
    "arn:aws:sns:ap-northeast-1:718959508629:"
    "teamagent-dev-openclaw-alarms"
)
LEGACY_TOPIC = (
    "arn:aws:sns:ap-northeast-1:718959508629:teamagent-dev-alarms"
)
AUTOMATION_ARN = (
    "arn:aws:sts::718959508629:assumed-role/"
    "teamagent-dev-terraform-runtime-automation/teamagent-terraform-worker"
)
ACK_SIGNER_ARN_PREFIX = (
    "arn:aws:sts::718959508629:assumed-role/"
    "teamagent-dev-alarm-recipient-ack-signer/"
)
ACK_KEY_ALIAS = "alias/teamagent-dev-alarm-recipient-ack"
SHARED_LEDGER_TABLE = "teamagent-dev-image-deployment-intents"
ALARM_LEDGER_TABLE = SHARED_LEDGER_TABLE
MIGRATION_LEDGER_TABLE = SHARED_LEDGER_TABLE
SHARED_LOCK_RECORD_ID = "lock#teamagent/terraform.tfstate"
VERSIONING_LEDGER_RECORD_PREFIX = "versioning-cutover#"
RUNTIME_LOCK_LEASE_SECONDS = 7200
SETTLE_SECONDS = 900

ENDPOINTS = {
    "autoscaling": f"https://autoscaling.{REGION}.amazonaws.com",
    "bedrock": f"https://bedrock.{REGION}.amazonaws.com",
    "budgets": "https://budgets.amazonaws.com",
    "chatbot": f"https://chatbot.{REGION}.amazonaws.com",
    "cloudtrail": f"https://cloudtrail.{REGION}.amazonaws.com",
    "cloudwatch": f"https://monitoring.{REGION}.amazonaws.com",
    "codestar-notifications": (
        f"https://codestar-notifications.{REGION}.amazonaws.com"
    ),
    "ce": "https://ce.us-east-1.amazonaws.com",
    "dynamodb": f"https://dynamodb.{REGION}.amazonaws.com",
    "ecs": f"https://ecs.{REGION}.amazonaws.com",
    "events": f"https://events.{REGION}.amazonaws.com",
    "kms": f"https://kms.{REGION}.amazonaws.com",
    "lambda": f"https://lambda.{REGION}.amazonaws.com",
    "logs": f"https://logs.{REGION}.amazonaws.com",
    "rds": f"https://rds.{REGION}.amazonaws.com",
    "s3api": f"https://s3.{REGION}.amazonaws.com",
    "scheduler": f"https://scheduler.{REGION}.amazonaws.com",
    "sns": f"https://sns.{REGION}.amazonaws.com",
    "sqs": f"https://sqs.{REGION}.amazonaws.com",
    "sts": f"https://sts.{REGION}.amazonaws.com",
}

WRITER_FAMILIES = (
    "teamagent-dev-openclaw",
    "teamagent-dev-mcp",
    "teamagent-dev-connect-web",
    "teamagent-dev-ingest",
    "teamagent-dev-morning-digest",
    "teamagent-dev-canary",
    "teamagent-dev-tiktok-acquire",
    "teamagent-dev-x-buzz-worker",
)
WRITER_SERVICES = (
    "teamagent-dev-openclaw",
    "teamagent-dev-mcp",
    "teamagent-dev-connect-web",
)
QUEUE_NAME_PREFIX = "teamagent-dev-"
KNOWN_SNS_PUBLISHER_TYPES = (
    "cloudwatch.metric-alarm",
    "cloudwatch.composite-alarm",
    "cloudwatch.log-metric-filter",
    "budgets.subscriber",
    "cost-anomaly.subscriber",
    "eventbridge.target",
    "scheduler.target",
    "s3.topic-notification",
    "lambda.dead-letter",
    "lambda.on-success",
    "lambda.on-failure",
    "autoscaling.notification",
    "codestar-notifications.target",
    "rds.event-subscription",
)

HEX64 = re.compile(r"^[0-9a-f]{64}$")
UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
VERSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,1024}$")
ETAG = re.compile(r'^"?[0-9a-fA-F]{32}(?:-[0-9]+)?"?$')


class ContractError(RuntimeError):
    """The observed state cannot authorize the requested transition."""


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def require_keys(value: Mapping[str, Any], expected: Iterable[str], label: str) -> None:
    actual = set(value)
    required = set(expected)
    if actual != required:
        raise ContractError(
            f"{label} keys differ: missing={sorted(required - actual)} "
            f"extra={sorted(actual - required)}"
        )


def require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError(f"{label} must be an integer >= {minimum}")
    return value


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{label} must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ContractError(f"{label} contains a control byte")
    return value


def parse_aws_date(value: str) -> tuple[str, int]:
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise ContractError("AWS response Date is invalid") from exc
    if parsed.tzinfo is None:
        raise ContractError("AWS response Date has no timezone")
    utc = parsed.astimezone(dt.UTC).replace(microsecond=0)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ"), int(utc.timestamp())


def parse_iso_epoch(value: Any, label: str) -> int:
    text = require_string(value, label)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{label} is not RFC3339") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{label} has no timezone")
    return int(parsed.timestamp())


def assert_no_error_fields(value: Any) -> None:
    forbidden = {"errorcode", "errormessage", "addendum"}
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in forbidden:
                raise ContractError(f"AWS response contains forbidden {key}")
            assert_no_error_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            assert_no_error_fields(nested)


@dataclass(frozen=True)
class HttpEvidence:
    date: str
    date_epoch: int
    request_id: str


@dataclass(frozen=True)
class ExecutableEvidence:
    path: str
    device: int
    inode: int
    size: int
    sha256: str
    version: str


@dataclass(frozen=True)
class FileIdentity:
    path: str
    device: int
    inode: int
    nlink: int
    size: int
    mode: int
    uid: int
    mtime_ns: int
    ctime_ns: int
    birthtime_ns: int | None

    @classmethod
    def from_fd(cls, path: Path, fd: int) -> FileIdentity:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ContractError("export FD is not a regular file")
        return cls(
            path=str(path),
            device=info.st_dev,
            inode=info.st_ino,
            nlink=info.st_nlink,
            size=info.st_size,
            mode=stat.S_IMODE(info.st_mode),
            uid=info.st_uid,
            mtime_ns=info.st_mtime_ns,
            ctime_ns=info.st_ctime_ns,
            birthtime_ns=(
                int(info.st_birthtime * 1_000_000_000)
                if hasattr(info, "st_birthtime")
                else None
            ),
        )


def assert_path_matches_identity(path: Path, identity: FileIdentity) -> None:
    if path.is_symlink():
        raise ContractError("export path became a symlink")
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ContractError("export path was renamed or removed") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_dev != identity.device
        or info.st_ino != identity.inode
        or info.st_nlink != 1
        or info.st_size != identity.size
    ):
        raise ContractError("export path no longer names the exact fresh FD")


def _canonical_file(path_text: str) -> Path:
    path = Path(require_string(path_text, "export path"))
    if not path.is_absolute():
        raise ContractError("export path must be absolute")
    if path.is_symlink():
        raise ContractError("export path must not be a symlink")
    try:
        canonical = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ContractError("export path does not exist") from exc
    if canonical != path:
        raise ContractError("export path is not canonical")
    return path


def open_export_for_rehash(path_text: str) -> tuple[Path, int]:
    path = _canonical_file(path_text)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    identity = FileIdentity.from_fd(path, fd)
    if identity.nlink != 1:
        os.close(fd)
        raise ContractError("export file must have exactly one hard link")
    if identity.uid != os.getuid():
        os.close(fd)
        raise ContractError("export file is not owned by the guard user")
    if identity.mode != 0o600:
        os.close(fd)
        raise ContractError("export file mode must be 0600")
    return path, fd


def hash_open_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def verify_file_binding(binding: Mapping[str, Any]) -> dict[str, Any]:
    binding_keys = set(binding)
    allowed_keys = {"path", "identity", "content_sha256"}
    if binding_keys not in (
        allowed_keys,
        allowed_keys | {"acquisition_identity_before"},
    ):
        raise ContractError("export file binding keys differ")
    expected_identity = binding["identity"]
    if not isinstance(expected_identity, Mapping):
        raise ContractError("export identity must be an object")
    require_keys(
        expected_identity,
        (
            "path",
            "device",
            "inode",
            "nlink",
            "size",
            "mode",
            "uid",
            "mtime_ns",
            "ctime_ns",
            "birthtime_ns",
        ),
        "export identity",
    )
    acquisition_identity = binding.get("acquisition_identity_before")
    if acquisition_identity is not None:
        if not isinstance(acquisition_identity, Mapping):
            raise ContractError("fresh acquisition identity must be an object")
        require_keys(
            acquisition_identity,
            (
                "path",
                "device",
                "inode",
                "nlink",
                "size",
                "mode",
                "uid",
                "mtime_ns",
                "ctime_ns",
                "birthtime_ns",
            ),
            "fresh acquisition identity",
        )
        if (
            acquisition_identity.get("path") != expected_identity.get("path")
            or acquisition_identity.get("device") != expected_identity.get("device")
            or acquisition_identity.get("inode") != expected_identity.get("inode")
            or acquisition_identity.get("nlink") != 1
            or acquisition_identity.get("size") != 0
            or acquisition_identity.get("mode") != 0o600
            or acquisition_identity.get("uid") != os.getuid()
            or not isinstance(acquisition_identity.get("mtime_ns"), int)
            or not isinstance(acquisition_identity.get("ctime_ns"), int)
            or (
                acquisition_identity.get("birthtime_ns") is not None
                and not isinstance(acquisition_identity.get("birthtime_ns"), int)
            )
        ):
            raise ContractError("fresh acquisition before/after identity differs")
    path, fd = open_export_for_rehash(require_string(binding["path"], "export path"))
    try:
        before = FileIdentity.from_fd(path, fd)
        digest = hash_open_fd(fd)
        after = FileIdentity.from_fd(path, fd)
    finally:
        os.close(fd)
    if before != after:
        raise ContractError("export file mutated while it was rehashed")
    if asdict(before) != dict(expected_identity):
        raise ContractError("export file identity differs from evidence")
    expected_hash = binding["content_sha256"]
    if not isinstance(expected_hash, str) or not HEX64.fullmatch(expected_hash):
        raise ContractError("export content SHA-256 is invalid")
    if digest != expected_hash:
        raise ContractError("export content SHA-256 differs from evidence")
    return {"identity": asdict(before), "content_sha256": digest}


class AwsCli:
    """Pinned, explicit-endpoint AWS CLI invocation boundary."""

    def __init__(self, executable: Path):
        if not executable.is_absolute():
            raise ContractError("AWS executable path must be absolute")
        if executable.is_symlink() or executable.resolve(strict=True) != executable:
            raise ContractError("AWS executable path must be canonical and not a symlink")
        info = executable.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ContractError("AWS executable must be a single-link regular file")
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise ContractError("AWS executable must not be group/other writable")
        if info.st_uid not in {0, os.getuid()}:
            raise ContractError("AWS executable owner is not trusted")
        digest = sha256_bytes(executable.read_bytes())
        completed = subprocess.run(
            [str(executable), "--version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=self._environment(),
        )
        version = completed.stdout.strip()
        if completed.returncode or not version.startswith("aws-cli/2."):
            raise ContractError("only AWS CLI v2 is trusted")
        self.executable = executable
        self._initial_stat = (info.st_dev, info.st_ino, info.st_size)
        self.evidence = ExecutableEvidence(
            path=str(executable),
            device=info.st_dev,
            inode=info.st_ino,
            size=info.st_size,
            sha256=digest,
            version=version,
        )

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_SECURITY_TOKEN",
        }
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        environment.update(
            {
                "AWS_CONFIG_FILE": "/dev/null",
                "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
                "AWS_DEFAULT_REGION": REGION,
                "AWS_REGION": REGION,
                "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
                "AWS_PAGER": "",
                "LC_ALL": "C",
            }
        )
        return environment

    def assert_unchanged(self) -> None:
        info = self.executable.stat()
        if (info.st_dev, info.st_ino, info.st_size) != self._initial_stat:
            raise ContractError("AWS executable identity changed")
        if sha256_bytes(self.executable.read_bytes()) != self.evidence.sha256:
            raise ContractError("AWS executable bytes changed")

    @staticmethod
    def _http_evidence(stderr: str) -> HttpEvidence:
        marker = re.findall(r"TEAMAGENT_HTTP_METADATA:(\{[^\n]+\})", stderr)
        headers: Mapping[str, Any] | None = None
        if marker:
            parsed = json.loads(marker[-1])
            if not isinstance(parsed, Mapping):
                raise ContractError("fake AWS HTTP metadata is malformed")
            headers = parsed
        else:
            candidates = re.findall(
                r"(?:Response headers|headers):\s*(\{[^\n]+\})",
                stderr,
                flags=re.IGNORECASE,
            )
            for candidate in reversed(candidates):
                try:
                    parsed = ast.literal_eval(candidate)
                except (SyntaxError, ValueError):
                    continue
                if isinstance(parsed, Mapping):
                    headers = parsed
                    break
        if headers is None:
            raise ContractError("AWS response headers were not observed")
        lowered = {str(key).lower(): value for key, value in headers.items()}
        date_value = lowered.get("date")
        request_id = (
            lowered.get("x-amz-request-id")
            or lowered.get("x-amzn-requestid")
            or lowered.get("x-amzn-request-id")
        )
        if not isinstance(date_value, str) or not isinstance(request_id, str):
            raise ContractError("AWS Date/request-id response headers are missing")
        date, epoch = parse_aws_date(date_value)
        if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", request_id):
            raise ContractError("AWS request id has an invalid form")
        return HttpEvidence(date=date, date_epoch=epoch, request_id=request_id)

    def call(
        self,
        service: str,
        operation: str,
        arguments: Sequence[str] = (),
        *,
        output_fd: int | None = None,
    ) -> tuple[dict[str, Any], HttpEvidence]:
        if service not in ENDPOINTS:
            raise ContractError(f"AWS service endpoint is not pinned: {service}")
        self.assert_unchanged()
        region = "us-east-1" if service in {"budgets", "ce"} else REGION
        command = [
            str(self.executable),
            "--region",
            region,
            "--endpoint-url",
            ENDPOINTS[service],
            "--no-cli-pager",
            "--debug",
            service,
            operation,
            *arguments,
            "--output",
            "json",
        ]
        pass_fds: tuple[int, ...] = ()
        if output_fd is not None:
            command.append(f"/dev/fd/{output_fd}")
            pass_fds = (output_fd,)
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            env=self._environment(),
            pass_fds=pass_fds,
        )
        if completed.returncode:
            raise ContractError(
                f"AWS {service} {operation} failed with exit "
                f"{completed.returncode}"
            )
        try:
            response = (
                json.loads(completed.stdout)
                if completed.stdout.strip()
                else {}
            )
        except json.JSONDecodeError as exc:
            raise ContractError(f"AWS {service} {operation} returned non-JSON") from exc
        if not isinstance(response, dict):
            raise ContractError(f"AWS {service} {operation} response is not an object")
        assert_no_error_fields(response)
        http = self._http_evidence(completed.stderr.decode(errors="replace"))
        self.assert_unchanged()
        return response, http

    def pages(
        self,
        service: str,
        operation: str,
        arguments: Sequence[str],
        *,
        token_field: str = "NextToken",
        token_argument: str = "--next-token",
    ) -> list[tuple[dict[str, Any], HttpEvidence]]:
        pages: list[tuple[dict[str, Any], HttpEvidence]] = []
        token: str | None = None
        seen: set[str] = set()
        while True:
            call_arguments = [*arguments, "--no-paginate"]
            if token is not None:
                call_arguments.extend((token_argument, token))
            response, http = self.call(service, operation, call_arguments)
            pages.append((response, http))
            next_token = response.get(token_field)
            if next_token in (None, ""):
                return pages
            if not isinstance(next_token, str) or next_token in seen:
                raise ContractError(
                    f"AWS {service} {operation} pagination token is invalid"
                )
            seen.add(next_token)
            token = next_token


def _items(
    pages: Sequence[tuple[Mapping[str, Any], HttpEvidence]],
    field: str,
    label: str,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for page, _ in pages:
        value = page.get(field)
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise ContractError(f"{label} page has an invalid {field}")
        collected.extend(value)
    return collected


def _walk_sns_refs(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, Mapping):
        for key in sorted(value):
            nested_path = f"{path}/{key}"
            yield from _walk_sns_refs(value[key], nested_path)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _walk_sns_refs(nested, f"{path}/{index}")
    elif isinstance(value, str) and value.startswith("arn:aws:sns:"):
        yield path, value


def _record_pages(
    raw_sources: list[dict[str, Any]],
    source_type: str,
    source_id: str,
    pages: Sequence[tuple[dict[str, Any], HttpEvidence]],
) -> None:
    for index, (page, http) in enumerate(pages):
        raw_sources.append(
            {
                "source_type": source_type,
                "source_id": source_id,
                "page": index,
                "response_sha256": canonical_sha256(page),
                "aws_date_epoch": http.date_epoch,
                "request_id_sha256": sha256_bytes(http.request_id.encode()),
            }
        )


def collect_inventory(aws: AwsCli) -> dict[str, Any]:
    """Collect every known SNS publisher and every writer control, all pages."""

    raw_sources: list[dict[str, Any]] = []
    raw_documents: list[tuple[str, str, Any]] = []
    coverage: set[str] = set()

    def collect(
        source_type: str,
        source_id: str,
        pages: list[tuple[dict[str, Any], HttpEvidence]],
    ) -> list[tuple[dict[str, Any], HttpEvidence]]:
        _record_pages(raw_sources, source_type, source_id, pages)
        for page_index, (page, _) in enumerate(pages):
            raw_documents.append((source_type, f"{source_id}:page:{page_index}", page))
        if source_type in KNOWN_SNS_PUBLISHER_TYPES:
            coverage.add(source_type)
        return pages

    topic_pages = collect(
        "sns.topic",
        "account",
        aws.pages("sns", "list-topics", ()),
    )
    topics = _items(topic_pages, "Topics", "SNS topics")
    topic_arns = [topic.get("TopicArn") for topic in topics]
    if topic_arns.count(CANONICAL_TOPIC) != 1 or topic_arns.count(LEGACY_TOPIC) > 1:
        raise ContractError("canonical/legacy SNS topic inventory is not exact")

    subscriptions: dict[str, list[dict[str, Any]]] = {}
    for topic in (CANONICAL_TOPIC, LEGACY_TOPIC):
        if topic not in topic_arns:
            subscriptions[topic] = []
            continue
        pages = collect(
            "sns.subscription",
            topic,
            aws.pages(
                "sns",
                "list-subscriptions-by-topic",
                ("--topic-arn", topic),
            ),
        )
        subscriptions[topic] = _items(pages, "Subscriptions", "SNS subscriptions")

    canonical_subscriptions = subscriptions[CANONICAL_TOPIC]
    all_subscriptions = [
        (topic_arn, item)
        for topic_arn, items in subscriptions.items()
        for item in items
    ]
    if len(canonical_subscriptions) != 1 or len(all_subscriptions) != 1:
        raise ContractError(
            "alarm topics must have exactly one subscription in total"
        )
    if all_subscriptions[0][0] != CANONICAL_TOPIC:
        raise ContractError("the sole alarm subscription must use the canonical topic")
    subscription = canonical_subscriptions[0]
    if set(subscription) != {
        "Endpoint",
        "Owner",
        "Protocol",
        "SubscriptionArn",
        "TopicArn",
    }:
        raise ContractError("SNS subscription summary schema is not exact")
    raw_endpoint = subscription.get("Endpoint")
    protocol = subscription.get("Protocol")
    subscription_arn = subscription.get("SubscriptionArn")
    if protocol != "email":
        raise ContractError("canonical SNS protocol must be exactly email")
    if not isinstance(raw_endpoint, str) or raw_endpoint.encode() != APPROVED_EMAIL.encode():
        raise ContractError("SNS endpoint bytes do not equal the approved email")
    if subscription_arn in {"PendingConfirmation", "Deleted", None, ""}:
        raise ContractError("pending/deleted SNS subscription is forbidden")
    if (
        subscription.get("Owner") != ACCOUNT_ID
        or subscription.get("TopicArn") != CANONICAL_TOPIC
        or not isinstance(subscription_arn, str)
        or not re.fullmatch(
            re.escape(CANONICAL_TOPIC)
            + (
                r":[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
            ),
            subscription_arn,
        )
    ):
        raise ContractError("SNS subscription owner/topic/ARN is not exact")
    attributes, attributes_http = aws.call(
        "sns",
        "get-subscription-attributes",
        ("--subscription-arn", str(subscription_arn)),
    )
    collect(
        "sns.subscription-attributes",
        str(subscription_arn),
        [(attributes, attributes_http)],
    )
    observed_attributes = attributes.get("Attributes")
    if not isinstance(observed_attributes, dict):
        raise ContractError("SNS subscription attributes are missing")
    if (
        set(observed_attributes)
        != {
            "ConfirmationWasAuthenticated",
            "Endpoint",
            "Owner",
            "PendingConfirmation",
            "Protocol",
            "RawMessageDelivery",
            "SubscriptionArn",
            "TopicArn",
        }
        or observed_attributes.get("Endpoint") != APPROVED_EMAIL
        or observed_attributes.get("Protocol") != "email"
        or observed_attributes.get("TopicArn") != CANONICAL_TOPIC
        or observed_attributes.get("SubscriptionArn") != subscription_arn
        or observed_attributes.get("Owner") != ACCOUNT_ID
        or observed_attributes.get("PendingConfirmation") != "false"
        or observed_attributes.get("ConfirmationWasAuthenticated") != "true"
        or observed_attributes.get("RawMessageDelivery") != "false"
    ):
        raise ContractError("SNS subscription attributes are not exact/exclusive")
    subscription_metadata = {
        "endpoint": raw_endpoint,
        "owner": ACCOUNT_ID,
        "protocol": protocol,
        "subscription_arn": subscription_arn,
        "topic_arn": CANONICAL_TOPIC,
        "attributes": observed_attributes,
    }

    alarm_pages = collect(
        "cloudwatch.alarm",
        "account",
        aws.pages("cloudwatch", "describe-alarms", ()),
    )
    metric_alarms = _items(alarm_pages, "MetricAlarms", "CloudWatch alarms")
    composite_alarms = _items(
        alarm_pages, "CompositeAlarms", "CloudWatch composite alarms"
    )
    coverage.update({"cloudwatch.metric-alarm", "cloudwatch.composite-alarm"})
    for alarm in metric_alarms:
        raw_documents.append(
            (
                "cloudwatch.metric-alarm",
                require_string(alarm.get("AlarmName"), "metric alarm name"),
                alarm,
            )
        )
    for alarm in composite_alarms:
        raw_documents.append(
            (
                "cloudwatch.composite-alarm",
                require_string(alarm.get("AlarmName"), "composite alarm name"),
                alarm,
            )
        )

    log_filter_pages = collect(
        "cloudwatch.log-metric-filter",
        "account",
        aws.pages("logs", "describe-metric-filters", ()),
    )
    metric_filters = _items(
        log_filter_pages, "metricFilters", "CloudWatch Logs metric filters"
    )
    for metric_filter in metric_filters:
        raw_documents.append(
            (
                "cloudwatch.log-metric-filter",
                require_string(
                    metric_filter.get("filterName"), "log metric filter name"
                ),
                metric_filter,
            )
        )

    budget_pages = collect(
        "budgets.budget",
        ACCOUNT_ID,
        aws.pages(
            "budgets",
            "describe-budgets",
            ("--account-id", ACCOUNT_ID),
        ),
    )
    budgets = _items(budget_pages, "Budgets", "Budgets")
    for budget in budgets:
        budget_name = require_string(budget.get("BudgetName"), "budget name")
        notification_pages = collect(
            "budgets.notification",
            budget_name,
            aws.pages(
                "budgets",
                "describe-notifications-for-budget",
                ("--account-id", ACCOUNT_ID, "--budget-name", budget_name),
            ),
        )
        notifications = _items(
            notification_pages, "Notifications", "Budget notifications"
        )
        for notification_index, notification in enumerate(notifications):
            notification_input = {
                key: notification[key]
                for key in (
                    "NotificationType",
                    "ComparisonOperator",
                    "Threshold",
                    "ThresholdType",
                )
                if key in notification
            }
            subscriber_pages = collect(
                "budgets.subscriber",
                f"{budget_name}:{notification_index}",
                aws.pages(
                    "budgets",
                    "describe-subscribers-for-notification",
                    (
                        "--account-id",
                        ACCOUNT_ID,
                        "--budget-name",
                        budget_name,
                        "--notification",
                        json.dumps(notification_input, separators=(",", ":")),
                    ),
                ),
            )
            _items(subscriber_pages, "Subscribers", "Budget subscribers")

    anomaly_pages = collect(
        "cost-anomaly.subscriber",
        "account",
        aws.pages(
            "ce",
            "get-anomaly-subscriptions",
            (),
            token_field="NextPageToken",
            token_argument="--next-page-token",
        ),
    )
    _items(anomaly_pages, "AnomalySubscriptions", "Cost Anomaly subscriptions")

    event_bus_pages = collect(
        "eventbridge.bus",
        "account",
        aws.pages("events", "list-event-buses", ()),
    )
    event_buses = _items(
        event_bus_pages, "EventBuses", "EventBridge event buses"
    )
    event_bus_names = [
        require_string(bus.get("Name"), "EventBridge event bus name")
        for bus in event_buses
    ]
    if (
        "default" not in event_bus_names
        or len(event_bus_names) != len(set(event_bus_names))
    ):
        raise ContractError("EventBridge event-bus inventory is incomplete")
    event_rules: list[dict[str, Any]] = []
    event_targets: list[dict[str, Any]] = []
    for event_bus_name in sorted(event_bus_names):
        rule_pages = collect(
            "eventbridge.rule",
            event_bus_name,
            aws.pages(
                "events",
                "list-rules",
                ("--event-bus-name", event_bus_name),
            ),
        )
        bus_rules = _items(rule_pages, "Rules", "EventBridge rules")
        for rule in bus_rules:
            if rule.get("EventBusName") != event_bus_name:
                raise ContractError("EventBridge rule belongs to another bus")
            rule_name = require_string(rule.get("Name"), "EventBridge rule name")
            target_pages = collect(
                "eventbridge.target",
                f"{event_bus_name}/{rule_name}",
                aws.pages(
                    "events",
                    "list-targets-by-rule",
                    (
                        "--rule",
                        rule_name,
                        "--event-bus-name",
                        event_bus_name,
                    ),
                ),
            )
            event_targets.extend(
                _items(target_pages, "Targets", "EventBridge targets")
            )
        event_rules.extend(bus_rules)

    schedule_group_pages = collect(
        "scheduler.schedule-group",
        "account",
        aws.pages("scheduler", "list-schedule-groups", ()),
    )
    schedule_groups = _items(
        schedule_group_pages, "ScheduleGroups", "Scheduler schedule groups"
    )
    schedule_group_names = [
        require_string(group.get("Name"), "Scheduler schedule group name")
        for group in schedule_groups
    ]
    if (
        "default" not in schedule_group_names
        or len(schedule_group_names) != len(set(schedule_group_names))
    ):
        raise ContractError("Scheduler schedule-group inventory is incomplete")
    schedules: list[dict[str, Any]] = []
    schedule_details: list[dict[str, Any]] = []
    for schedule_group_name in sorted(schedule_group_names):
        schedule_pages = collect(
            "scheduler.schedule",
            schedule_group_name,
            aws.pages(
                "scheduler",
                "list-schedules",
                ("--group-name", schedule_group_name),
            ),
        )
        group_schedules = _items(
            schedule_pages, "Schedules", "Scheduler schedules"
        )
        for schedule in group_schedules:
            name = require_string(schedule.get("Name"), "schedule name")
            group = require_string(
                schedule.get("GroupName"), "schedule group"
            )
            if group != schedule_group_name:
                raise ContractError("Scheduler schedule belongs to another group")
            detail, http = aws.call(
                "scheduler",
                "get-schedule",
                ("--name", name, "--group-name", group),
            )
            if (
                detail.get("Name") != name
                or detail.get("GroupName") != group
            ):
                raise ContractError("Scheduler schedule detail identity differs")
            collect("scheduler.target", f"{group}/{name}", [(detail, http)])
            schedule_details.append(detail)
        schedules.extend(group_schedules)

    function_pages = collect(
        "lambda.function",
        "account",
        aws.pages(
            "lambda",
            "list-functions",
            (),
            token_field="NextMarker",
            token_argument="--marker",
        ),
    )
    functions = _items(function_pages, "Functions", "Lambda functions")
    function_configs: list[dict[str, Any]] = []
    invoke_configs: list[dict[str, Any]] = []
    for function in functions:
        name = require_string(function.get("FunctionName"), "Lambda function name")
        config, http = aws.call(
            "lambda",
            "get-function-configuration",
            ("--function-name", name),
        )
        collect("lambda.dead-letter", name, [(config, http)])
        function_configs.append(config)
        pages = collect(
            "lambda.event-invoke",
            name,
            aws.pages(
                "lambda",
                "list-function-event-invoke-configs",
                ("--function-name", name),
                token_field="NextMarker",
                token_argument="--marker",
            ),
        )
        invoke_configs.extend(
            _items(pages, "FunctionEventInvokeConfigs", "Lambda invoke configs")
        )
        for invoke_config in _items(
            pages, "FunctionEventInvokeConfigs", "Lambda invoke configs"
        ):
            destination_config = invoke_config.get("DestinationConfig", {})
            if not isinstance(destination_config, Mapping):
                raise ContractError("Lambda destination config is malformed")
            on_success = destination_config.get("OnSuccess", {})
            on_failure = destination_config.get("OnFailure", {})
            if not isinstance(on_success, Mapping) or not isinstance(
                on_failure, Mapping
            ):
                raise ContractError("Lambda invoke destination is malformed")
            raw_documents.append(
                ("lambda.on-success", name, dict(on_success))
            )
            raw_documents.append(
                ("lambda.on-failure", name, dict(on_failure))
            )
        coverage.update({"lambda.on-success", "lambda.on-failure"})

    mapping_pages = collect(
        "lambda.event-source-mapping",
        "account",
        aws.pages(
            "lambda",
            "list-event-source-mappings",
            (),
            token_field="NextMarker",
            token_argument="--marker",
        ),
    )
    mappings = _items(
        mapping_pages, "EventSourceMappings", "Lambda event source mappings"
    )

    bucket_pages = collect(
        "s3.bucket",
        "account",
        aws.pages(
            "s3api",
            "list-buckets",
            (),
            token_field="ContinuationToken",
            token_argument="--continuation-token",
        ),
    )
    buckets = _items(bucket_pages, "Buckets", "S3 buckets")
    notifications: list[dict[str, Any]] = []
    for bucket in buckets:
        name = require_string(bucket.get("Name"), "S3 bucket name")
        notification, http = aws.call(
            "s3api",
            "get-bucket-notification-configuration",
            ("--bucket", name, "--expected-bucket-owner", ACCOUNT_ID),
        )
        collect("s3.topic-notification", name, [(notification, http)])
        notifications.append(notification)

    autoscaling_pages = collect(
        "autoscaling.notification",
        "account",
        aws.pages("autoscaling", "describe-notification-configurations", ()),
    )
    _items(
        autoscaling_pages,
        "NotificationConfigurations",
        "Auto Scaling notifications",
    )

    notification_rule_pages = collect(
        "codestar-notifications.rule",
        "account",
        aws.pages("codestar-notifications", "list-notification-rules", ()),
    )
    notification_rules = _items(
        notification_rule_pages,
        "NotificationRules",
        "CodeStar notification rules",
    )
    for rule in notification_rules:
        rule_arn = require_string(rule.get("Arn"), "notification rule ARN")
        detail, http = aws.call(
            "codestar-notifications",
            "describe-notification-rule",
            ("--arn", rule_arn),
        )
        collect("codestar-notifications.target", rule_arn, [(detail, http)])

    rds_pages = collect(
        "rds.event-subscription",
        "account",
        aws.pages(
            "rds",
            "describe-event-subscriptions",
            (),
            token_field="Marker",
            token_argument="--marker",
        ),
    )
    _items(rds_pages, "EventSubscriptionsList", "RDS event subscriptions")

    chatbot_configurations: list[dict[str, Any]] = []
    for operation, field, source_type in (
        (
            "describe-slack-channel-configurations",
            "SlackChannelConfigurations",
            "chatbot.slack",
        ),
        (
            "list-microsoft-teams-channel-configurations",
            "TeamChannelConfigurations",
            "chatbot.teams",
        ),
        (
            "describe-chime-webhook-configurations",
            "WebhookConfigurations",
            "chatbot.chime",
        ),
    ):
        pages = collect(
            source_type,
            "account",
            aws.pages("chatbot", operation, ()),
        )
        chatbot_configurations.extend(_items(pages, field, source_type))

    references: list[dict[str, str]] = []
    for source_type, source_id, document in raw_documents:
        for pointer, topic_arn in _walk_sns_refs(document):
            references.append(
                {
                    "source_type": source_type,
                    "source_id": source_id,
                    "json_pointer": pointer,
                    "topic_arn": topic_arn,
                }
            )
    references.sort(
        key=lambda item: (
            item["source_type"],
            item["source_id"],
            item["json_pointer"],
            item["topic_arn"],
        )
    )
    publisher_references = [
        reference
        for reference in references
        if reference["source_type"] in KNOWN_SNS_PUBLISHER_TYPES
        and reference["topic_arn"] in {CANONICAL_TOPIC, LEGACY_TOPIC}
    ]
    publisher_groups: dict[tuple[str, str], set[str]] = {}
    for reference in publisher_references:
        key = (reference["source_type"], reference["source_id"])
        publisher_groups.setdefault(key, set()).add(reference["topic_arn"])
    publishers = [
        {
            "publisher_id": f"{source_type}:{source_id}",
            "source_type": source_type,
            "source_id": source_id,
            "topic_arns": sorted(topic_arns),
        }
        for (source_type, source_id), topic_arns in sorted(publisher_groups.items())
    ]
    chatbot_refs = [
        reference
        for reference in references
        if reference["source_type"].startswith("chatbot.")
        and reference["topic_arn"] in {CANONICAL_TOPIC, LEGACY_TOPIC}
    ]
    if chatbot_refs:
        raise ContractError("Chatbot delivery mode is forbidden for alarm topics")

    destination_state = {
        "chatbot_configuration_arns": [],
        "subscription": {
            "endpoint": APPROVED_EMAIL,
            "filter_policy_present": False,
            "protocol": "email",
            "raw_message_delivery": False,
            "state": "confirmed",
        },
        "topic_arn": CANONICAL_TOPIC,
    }
    raw_sources.sort(
        key=lambda item: (
            item["source_type"],
            item["source_id"],
            item["page"],
        )
    )
    missing_coverage = set(KNOWN_SNS_PUBLISHER_TYPES) - coverage
    if missing_coverage:
        raise ContractError(
            f"known SNS publisher coverage is incomplete: {sorted(missing_coverage)}"
        )
    inventory_contract = {
        "references": references,
        "publisher_references": publisher_references,
        "publishers": publishers,
        "destination": destination_state,
        "subscription_metadata": subscription_metadata,
        "topic_inventory": sorted(
            topic_arn
            for topic_arn in topic_arns
            if topic_arn in {CANONICAL_TOPIC, LEGACY_TOPIC}
        ),
        "alarm_subscription_count": len(all_subscriptions),
        "publisher_coverage": sorted(coverage),
        "source_pages": raw_sources,
    }
    validate_inventory_contract(inventory_contract)
    return {
        "kind": "teamagent-runtime-inventory",
        "schema_version": 1,
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "canonical_topic_arn": CANONICAL_TOPIC,
        "legacy_topic_arn": LEGACY_TOPIC,
        "raw_endpoint_utf8_sha256": sha256_bytes(raw_endpoint.encode()),
        "subscription_metadata": subscription_metadata,
        "subscription_metadata_sha256": canonical_sha256(
            subscription_metadata
        ),
        "destination_state": destination_state,
        "destination_state_sha256": canonical_sha256(destination_state),
        "raw_reference_set": references,
        "raw_reference_set_sha256": canonical_sha256(references),
        "publisher_reference_set": publisher_references,
        "publisher_reference_set_sha256": canonical_sha256(
            publisher_references
        ),
        "publishers": publishers,
        "publishers_sha256": canonical_sha256(publishers),
        "topic_inventory": sorted(
            topic_arn
            for topic_arn in topic_arns
            if topic_arn in {CANONICAL_TOPIC, LEGACY_TOPIC}
        ),
        "alarm_subscription_count": len(all_subscriptions),
        "source_pages": raw_sources,
        "source_pages_sha256": canonical_sha256(raw_sources),
        "publisher_coverage": sorted(coverage),
        "eventbridge_rules": event_rules,
        "scheduler_schedules": schedule_details,
        "lambda_event_source_mappings": mappings,
        "metric_alarm_count": len(metric_alarms),
        "composite_alarm_count": len(composite_alarms),
        "chatbot_configuration_count": len(chatbot_configurations),
        "inventory_contract": inventory_contract,
        "inventory_sha256": canonical_sha256(inventory_contract),
    }


def validate_inventory_contract(contract: Mapping[str, Any]) -> None:
    require_keys(
        contract,
        (
            "references",
            "publisher_references",
            "publishers",
            "destination",
            "subscription_metadata",
            "topic_inventory",
            "alarm_subscription_count",
            "publisher_coverage",
            "source_pages",
        ),
        "runtime inventory contract",
    )
    destination = contract.get("destination")
    expected_destination = {
        "chatbot_configuration_arns": [],
        "subscription": {
            "endpoint": APPROVED_EMAIL,
            "filter_policy_present": False,
            "protocol": "email",
            "raw_message_delivery": False,
            "state": "confirmed",
        },
        "topic_arn": CANONICAL_TOPIC,
    }
    if destination != expected_destination:
        raise ContractError("runtime inventory destination is not the exact raw email")

    metadata = contract.get("subscription_metadata")
    if not isinstance(metadata, Mapping):
        raise ContractError("runtime inventory subscription metadata is missing")
    require_keys(
        metadata,
        (
            "endpoint",
            "owner",
            "protocol",
            "subscription_arn",
            "topic_arn",
            "attributes",
        ),
        "runtime inventory subscription metadata",
    )
    attributes = metadata.get("attributes")
    subscription_arn = metadata.get("subscription_arn")
    if (
        metadata.get("endpoint") != APPROVED_EMAIL
        or not isinstance(metadata.get("endpoint"), str)
        or str(metadata["endpoint"]).encode() != APPROVED_EMAIL.encode()
        or metadata.get("owner") != ACCOUNT_ID
        or metadata.get("protocol") != "email"
        or metadata.get("topic_arn") != CANONICAL_TOPIC
        or not isinstance(subscription_arn, str)
        or not re.fullmatch(
            re.escape(CANONICAL_TOPIC)
            + (
                r":[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
            ),
            subscription_arn,
        )
        or not isinstance(attributes, Mapping)
        or set(attributes)
        != {
            "ConfirmationWasAuthenticated",
            "Endpoint",
            "Owner",
            "PendingConfirmation",
            "Protocol",
            "RawMessageDelivery",
            "SubscriptionArn",
            "TopicArn",
        }
        or attributes.get("Endpoint") != APPROVED_EMAIL
        or attributes.get("Owner") != ACCOUNT_ID
        or attributes.get("PendingConfirmation") != "false"
        or attributes.get("ConfirmationWasAuthenticated") != "true"
        or attributes.get("Protocol") != "email"
        or attributes.get("RawMessageDelivery") != "false"
        or attributes.get("SubscriptionArn") != subscription_arn
        or attributes.get("TopicArn") != CANONICAL_TOPIC
    ):
        raise ContractError("runtime inventory subscription is not exact/confirmed")

    topic_inventory = contract.get("topic_inventory")
    if (
        not isinstance(topic_inventory, list)
        or topic_inventory != sorted(topic_inventory)
        or len(topic_inventory) != len(set(topic_inventory))
        or CANONICAL_TOPIC not in topic_inventory
        or not set(topic_inventory).issubset({CANONICAL_TOPIC, LEGACY_TOPIC})
        or contract.get("alarm_subscription_count") != 1
        or contract.get("publisher_coverage") != sorted(KNOWN_SNS_PUBLISHER_TYPES)
    ):
        raise ContractError("runtime inventory topic/coverage set is incomplete")

    references = contract.get("references")
    publisher_references = contract.get("publisher_references")
    publishers = contract.get("publishers")
    if not all(
        isinstance(value, list)
        for value in (references, publisher_references, publishers)
    ):
        raise ContractError("runtime inventory reference sets are missing")
    reference_keys = ("source_type", "source_id", "json_pointer", "topic_arn")
    for reference in references:
        if (
            not isinstance(reference, Mapping)
            or set(reference) != set(reference_keys)
            or any(
                not isinstance(reference.get(key), str)
                for key in reference_keys
            )
            or not str(reference["topic_arn"]).startswith("arn:aws:sns:")
        ):
            raise ContractError("runtime inventory raw SNS reference is malformed")
    sorted_references = sorted(
        references,
        key=lambda item: (
            item["source_type"],
            item["source_id"],
            item["json_pointer"],
            item["topic_arn"],
        ),
    )
    if references != sorted_references:
        raise ContractError("runtime inventory raw SNS references are not canonical")
    expected_publisher_references = [
        reference
        for reference in references
        if reference["source_type"] in KNOWN_SNS_PUBLISHER_TYPES
        and reference["topic_arn"] in {CANONICAL_TOPIC, LEGACY_TOPIC}
    ]
    if publisher_references != expected_publisher_references:
        raise ContractError("runtime inventory publisher reference set differs")
    if any(
        reference["source_type"].startswith("chatbot.")
        and reference["topic_arn"] in {CANONICAL_TOPIC, LEGACY_TOPIC}
        for reference in references
    ):
        raise ContractError("runtime inventory contains forbidden Chatbot delivery")

    grouped: dict[tuple[str, str], set[str]] = {}
    for reference in publisher_references:
        key = (reference["source_type"], reference["source_id"])
        grouped.setdefault(key, set()).add(reference["topic_arn"])
    expected_publishers = [
        {
            "publisher_id": f"{source_type}:{source_id}",
            "source_type": source_type,
            "source_id": source_id,
            "topic_arns": sorted(topic_arns),
        }
        for (source_type, source_id), topic_arns in sorted(grouped.items())
    ]
    if publishers != expected_publishers:
        raise ContractError("runtime inventory publisher grouping differs")

    source_pages = contract.get("source_pages")
    if not isinstance(source_pages, list) or not source_pages:
        raise ContractError("runtime inventory all-page evidence is missing")
    page_groups: dict[tuple[str, str], list[int]] = {}
    previous_sort_key: tuple[str, str, int] | None = None
    for source in source_pages:
        if not isinstance(source, Mapping):
            raise ContractError("runtime inventory page evidence is malformed")
        require_keys(
            source,
            (
                "source_type",
                "source_id",
                "page",
                "response_sha256",
                "aws_date_epoch",
                "request_id_sha256",
            ),
            "runtime inventory page evidence",
        )
        source_type = require_string(
            source.get("source_type"), "inventory page source type"
        )
        source_id = require_string(
            source.get("source_id"), "inventory page source id"
        )
        page = require_int(source.get("page"), "inventory page number")
        if (
            not HEX64.fullmatch(str(source.get("response_sha256", "")))
            or not HEX64.fullmatch(str(source.get("request_id_sha256", "")))
            or require_int(
                source.get("aws_date_epoch"), "inventory page AWS Date", minimum=1
            )
            < 1
        ):
            raise ContractError("runtime inventory page evidence hash/time is invalid")
        sort_key = (source_type, source_id, page)
        if previous_sort_key is not None and sort_key <= previous_sort_key:
            raise ContractError("runtime inventory page evidence is not canonical")
        previous_sort_key = sort_key
        page_groups.setdefault((source_type, source_id), []).append(page)
    if any(pages != list(range(len(pages))) for pages in page_groups.values()):
        raise ContractError("runtime inventory has a pagination gap")


def assert_writer_inventory_disabled(inventory: Mapping[str, Any]) -> None:
    rules = inventory.get("eventbridge_rules")
    schedules = inventory.get("scheduler_schedules")
    mappings = inventory.get("lambda_event_source_mappings")
    if not isinstance(rules, list) or not all(
        isinstance(rule, Mapping) and rule.get("State") == "DISABLED"
        for rule in rules
    ):
        raise ContractError("every EventBridge rule must be DISABLED")
    if not isinstance(schedules, list) or not all(
        isinstance(schedule, Mapping) and schedule.get("State") == "DISABLED"
        for schedule in schedules
    ):
        raise ContractError("every Scheduler schedule must be DISABLED")
    if not isinstance(mappings, list) or not all(
        isinstance(mapping, Mapping) and mapping.get("State") == "Disabled"
        for mapping in mappings
    ):
        raise ContractError("every Lambda event source mapping must be Disabled")


def _caller_identity(aws: AwsCli) -> tuple[dict[str, Any], HttpEvidence]:
    identity, http = aws.call("sts", "get-caller-identity")
    require_keys(identity, ("UserId", "Account", "Arn"), "caller identity")
    if identity["Account"] != ACCOUNT_ID or identity["Arn"] != AUTOMATION_ARN:
        raise ContractError("caller is not the exact Terraform automation session")
    return identity, http


def _bucket_identity(
    aws: AwsCli, bucket_name: str
) -> tuple[dict[str, Any], HttpEvidence]:
    pages = aws.pages(
        "s3api",
        "list-buckets",
        (),
        token_field="ContinuationToken",
        token_argument="--continuation-token",
    )
    owner_ids: set[str] = set()
    buckets: list[dict[str, Any]] = []
    for response, _ in pages:
        owner = response.get("Owner")
        values = response.get("Buckets")
        if not isinstance(owner, Mapping) or not isinstance(values, list):
            raise ContractError("S3 owner/bucket identity response is incomplete")
        owner_ids.add(require_string(owner.get("ID"), "bucket canonical owner ID"))
        if not all(isinstance(value, dict) for value in values):
            raise ContractError("S3 bucket identity page is malformed")
        buckets.extend(values)
    if len(owner_ids) != 1:
        raise ContractError("S3 canonical owner changed across pages")
    owner_id = next(iter(owner_ids))
    matches = [bucket for bucket in buckets if bucket.get("Name") == bucket_name]
    if len(matches) != 1:
        raise ContractError(f"bucket identity is not unique: {bucket_name}")
    creation_date = require_string(
        matches[0].get("CreationDate"), "bucket CreationDate"
    )
    parse_iso_epoch(creation_date, "bucket CreationDate")
    return (
        {
            "name": bucket_name,
            "arn": f"arn:aws:s3:::{bucket_name}",
            "owner_canonical_id": owner_id,
            "creation_date": creation_date,
        },
        pages[-1][1],
    )


def _object_versions_hash(aws: AwsCli, bucket: str) -> tuple[str, int]:
    key_marker: str | None = None
    version_marker: str | None = None
    entries: list[dict[str, Any]] = []
    observed_at = 0
    seen: set[tuple[str, str]] = set()
    while True:
        arguments = [
            "--bucket",
            bucket,
            "--expected-bucket-owner",
            ACCOUNT_ID,
            "--no-paginate",
        ]
        if key_marker is not None:
            arguments.extend(("--key-marker", key_marker))
        if version_marker is not None:
            arguments.extend(("--version-id-marker", version_marker))
        response, http = aws.call("s3api", "list-object-versions", arguments)
        observed_at = max(observed_at, http.date_epoch)
        for field, kind in (("Versions", "version"), ("DeleteMarkers", "delete-marker")):
            values = response.get(field, [])
            if not isinstance(values, list):
                raise ContractError("S3 object-version page is malformed")
            for value in values:
                if not isinstance(value, Mapping):
                    raise ContractError("S3 object-version entry is malformed")
                entries.append(
                    {
                        "kind": kind,
                        "key": value.get("Key"),
                        "version_id": value.get("VersionId"),
                        "is_latest": value.get("IsLatest"),
                        "last_modified": value.get("LastModified"),
                        "size": value.get("Size", 0),
                        "etag": value.get("ETag", ""),
                    }
                )
        if not response.get("IsTruncated", False):
            break
        next_key = response.get("NextKeyMarker")
        next_version = response.get("NextVersionIdMarker")
        if not isinstance(next_key, str) or not isinstance(next_version, str):
            raise ContractError("S3 object-version pagination token is missing")
        marker = (next_key, next_version)
        if marker in seen:
            raise ContractError("S3 object-version pagination token repeated")
        seen.add(marker)
        key_marker, version_marker = marker
    entries.sort(
        key=lambda item: (
            str(item["key"]),
            str(item["version_id"]),
            str(item["kind"]),
        )
    )
    return canonical_sha256(entries), observed_at


def _list_family_tasks(
    aws: AwsCli, family: str, desired_status: str
) -> tuple[list[str], int]:
    pages = aws.pages(
        "ecs",
        "list-tasks",
        (
            "--cluster",
            "teamagent-dev",
            "--family",
            family,
            "--desired-status",
            desired_status,
        ),
        token_field="nextToken",
        token_argument="--next-token",
    )
    tasks: list[str] = []
    for page, _ in pages:
        values = page.get("taskArns")
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise ContractError("ECS task page is malformed")
        tasks.extend(values)
    if len(tasks) != len(set(tasks)):
        raise ContractError("ECS task inventory contains duplicates")
    observed_at = max((http.date_epoch for _, http in pages), default=0)
    if observed_at <= 0:
        raise ContractError("ECS task inventory has no AWS observation time")
    return tasks, observed_at


def _describe_writer_services(
    aws: AwsCli,
) -> tuple[dict[str, dict[str, int | str]], int]:
    response, http = aws.call(
        "ecs",
        "describe-services",
        (
            "--cluster",
            "teamagent-dev",
            "--services",
            *WRITER_SERVICES,
        ),
    )
    services = response.get("services")
    failures = response.get("failures", [])
    if not isinstance(services, list) or not isinstance(failures, list):
        raise ContractError("ECS service inventory is malformed")
    states: dict[str, dict[str, int | str]] = {}
    accounted: set[str] = set()
    for service in services:
        if not isinstance(service, Mapping):
            raise ContractError("ECS service entry is malformed")
        name = require_string(service.get("serviceName"), "ECS service name")
        if name not in WRITER_SERVICES or name in accounted:
            raise ContractError("ECS service inventory is not exact/unique")
        desired = require_int(service.get("desiredCount"), "ECS desired count")
        running = require_int(service.get("runningCount"), "ECS running count")
        pending = require_int(service.get("pendingCount"), "ECS pending count")
        status = require_string(service.get("status"), "ECS service status")
        states[name] = {
            "status": status,
            "desired": desired,
            "running": running,
            "pending": pending,
        }
        accounted.add(name)
    for failure in failures:
        if not isinstance(failure, Mapping):
            raise ContractError("ECS service failure entry is malformed")
        service_arn = require_string(
            failure.get("arn"), "missing ECS service ARN"
        )
        name = service_arn.rsplit("/", 1)[-1]
        if (
            name not in WRITER_SERVICES
            or name in accounted
            or failure.get("reason") != "MISSING"
        ):
            raise ContractError("ECS service inventory has an unknown failure")
        states[name] = {
            "status": "MISSING",
            "desired": 0,
            "running": 0,
            "pending": 0,
        }
        accounted.add(name)
    if accounted != set(WRITER_SERVICES):
        raise ContractError("ECS service inventory coverage is incomplete")
    return states, http.date_epoch


def _queue_depths(
    aws: AwsCli,
) -> tuple[dict[str, dict[str, int]], int]:
    depths: dict[str, dict[str, int]] = {}
    list_pages = aws.pages(
        "sqs",
        "list-queues",
        (
            "--queue-name-prefix",
            QUEUE_NAME_PREFIX,
            "--max-results",
            "1000",
        ),
    )
    queue_urls: list[str] = []
    for page, _ in list_pages:
        values = page.get("QueueUrls", [])
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise ContractError("SQS queue URL inventory is malformed")
        queue_urls.extend(values)
    if len(queue_urls) != len(set(queue_urls)):
        raise ContractError("SQS queue inventory contains duplicates")
    observed_times = [http.date_epoch for _, http in list_pages]
    for queue_url in sorted(queue_urls):
        expected_prefix = (
            f"https://sqs.{REGION}.amazonaws.com/"
            f"{ACCOUNT_ID}/{QUEUE_NAME_PREFIX}"
        )
        if not queue_url.startswith(expected_prefix):
            raise ContractError("SQS queue URL differs from fixed account/region")
        queue_name = queue_url.rsplit("/", 1)[-1]
        attributes, attributes_http = aws.call(
            "sqs",
            "get-queue-attributes",
            (
                "--queue-url",
                queue_url,
                "--attribute-names",
                "QueueArn",
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
            ),
        )
        observed_times.append(attributes_http.date_epoch)
        raw = attributes.get("Attributes")
        if not isinstance(raw, Mapping):
            raise ContractError("SQS queue attributes are missing")
        expected_arn = f"arn:aws:sqs:{REGION}:{ACCOUNT_ID}:{queue_name}"
        if raw.get("QueueArn") != expected_arn:
            raise ContractError("SQS queue ARN differs from the fixed queue")
        counts: dict[str, int] = {}
        for name in (
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
        ):
            value = raw.get(name)
            if not isinstance(value, str) or not value.isdecimal():
                raise ContractError(f"SQS {name} is invalid")
            counts[name] = int(value)
        if any(counts.values()):
            raise ContractError("SQS queued/in-flight/delayed messages must all be zero")
        depths[expected_arn] = counts
    observed_at = max(observed_times, default=0)
    if observed_at <= 0:
        raise ContractError("SQS inventory has no AWS observation time")
    return depths, observed_at


def _cloudtrail_identity_contract(
    response: Mapping[str, Any],
) -> dict[str, Any]:
    trail = response.get("Trail")
    expected_name = "teamagent-dev-trail"
    expected_bucket = f"teamagent-dev-cloudtrail-{ACCOUNT_ID}"
    expected_arn = (
        f"arn:aws:cloudtrail:{REGION}:{ACCOUNT_ID}:trail/{expected_name}"
    )
    if (
        not isinstance(trail, Mapping)
        or trail.get("Name") != expected_name
        or trail.get("S3BucketName") != expected_bucket
        or trail.get("TrailARN") != expected_arn
        or trail.get("HomeRegion") != REGION
        or trail.get("IsMultiRegionTrail") is not True
        or trail.get("IncludeGlobalServiceEvents") is not True
        or trail.get("LogFileValidationEnabled") is not True
        or trail.get("IsOrganizationTrail", False) is not False
        or trail.get("S3KeyPrefix") not in (None, "")
        or trail.get("SnsTopicName") not in (None, "")
        or trail.get("SnsTopicARN") not in (None, "")
        or trail.get("CloudWatchLogsLogGroupArn") not in (None, "")
        or trail.get("CloudWatchLogsRoleArn") not in (None, "")
        or not re.fullmatch(
            (
                rf"arn:aws:kms:{re.escape(REGION)}:{ACCOUNT_ID}:key/"
                r"[0-9a-fA-F-]{36}"
            ),
            str(trail.get("KmsKeyId", "")),
        )
    ):
        raise ContractError("CloudTrail producer identity/configuration is not exact")
    return {
        "trail_name": expected_name,
        "trail_arn": expected_arn,
        "home_region": REGION,
        "bucket": expected_bucket,
        "is_multi_region": True,
        "include_global_service_events": True,
        "log_file_validation_enabled": True,
        "kms_key_arn": trail["KmsKeyId"],
        "is_organization_trail": False,
    }


def _log_producer_state(
    aws: AwsCli,
) -> tuple[dict[str, Any], int]:
    trail, trail_http = aws.call(
        "cloudtrail", "get-trail", ("--name", "teamagent-dev-trail")
    )
    status, status_http = aws.call(
        "cloudtrail", "get-trail-status", ("--name", "teamagent-dev-trail")
    )
    bedrock, bedrock_http = aws.call(
        "bedrock", "get-model-invocation-logging-configuration"
    )
    trail_identity = _cloudtrail_identity_contract(trail)
    if not isinstance(status.get("IsLogging"), bool):
        raise ContractError("CloudTrail producer identity/state is not exact")
    bedrock_config = bedrock.get("loggingConfig")
    if bedrock_config is not None and not isinstance(bedrock_config, Mapping):
        raise ContractError("Bedrock producer configuration is malformed")
    state = {
        "cloudtrail": {
            "trail_name": "teamagent-dev-trail",
            "bucket": f"teamagent-dev-cloudtrail-{ACCOUNT_ID}",
            "identity": trail_identity,
            "is_logging": status["IsLogging"],
            "trail_response_sha256": canonical_sha256(trail),
            "status_response_sha256": canonical_sha256(status),
        },
        "bedrock": {
            "configured": bedrock_config is not None,
            "logging_config_sha256": canonical_sha256(bedrock_config),
        },
    }
    return (
        state,
        max(
            trail_http.date_epoch,
            status_http.date_epoch,
            bedrock_http.date_epoch,
        ),
    )


def capture_quiescence(
    aws: AwsCli,
    *,
    require_log_producers_off: bool = True,
) -> dict[str, Any]:
    inventory = collect_inventory(aws)
    assert_writer_inventory_disabled(inventory)
    family_counts: dict[str, dict[str, int]] = {}
    source_times = [
        source["aws_date_epoch"] for source in inventory["source_pages"]
    ]
    for family in WRITER_FAMILIES:
        running, running_at = _list_family_tasks(aws, family, "RUNNING")
        pending, pending_at = _list_family_tasks(aws, family, "PENDING")
        source_times.extend((running_at, pending_at))
        if running or pending:
            raise ContractError(
                f"ECS family {family} still has RUNNING/PENDING tasks"
            )
        family_counts[family] = {"running": 0, "pending": 0}
    service_states, services_at = _describe_writer_services(aws)
    if any(
        state["desired"] != 0
        or state["running"] != 0
        or state["pending"] != 0
        for state in service_states.values()
    ):
        raise ContractError("every ECS writer service must be fully scaled to zero")
    queues, queues_at = _queue_depths(aws)
    log_producers, log_producers_at = _log_producer_state(aws)
    if require_log_producers_off and (
        log_producers["cloudtrail"]["is_logging"] is not False
        or log_producers["bedrock"]["configured"] is not False
    ):
        raise ContractError("CloudTrail/Bedrock producers must be disconnected")
    _, final_http = _caller_identity(aws)
    source_times.extend((services_at, queues_at, log_producers_at))
    if max(source_times, default=0) > final_http.date_epoch:
        raise ContractError("quiescence evidence exceeds its final observation")
    observed_at = final_http.date_epoch
    if observed_at <= 0:
        raise ContractError("quiescence has no independently sourced AWS Date")
    contract = {
        "inventory_sha256": inventory["inventory_sha256"],
        "raw_reference_set_sha256": inventory["raw_reference_set_sha256"],
        "eventbridge_all_disabled": True,
        "scheduler_all_disabled": True,
        "lambda_mappings_all_disabled": True,
        "writer_controls": {
            "eventbridge": sorted(
                f"{require_string(rule.get('EventBusName'), 'event bus')}/"
                f"{require_string(rule.get('Name'), 'event rule')}"
                for rule in inventory["eventbridge_rules"]
            ),
            "scheduler": sorted(
                f"{require_string(schedule.get('GroupName'), 'schedule group')}/"
                f"{require_string(schedule.get('Name'), 'schedule')}"
                for schedule in inventory["scheduler_schedules"]
            ),
            "lambda_mappings": sorted(
                require_string(mapping.get("UUID"), "event source mapping UUID")
                for mapping in inventory["lambda_event_source_mappings"]
            ),
        },
        "ecs_families": family_counts,
        "ecs_services": service_states,
        "queues": queues,
        "log_producers": log_producers,
        "observed_at_epoch": observed_at,
    }
    return {
        "contract": contract,
        "contract_sha256": canonical_sha256(contract),
        "inventory": inventory,
    }


def disconnect_all_writers(
    aws: AwsCli,
    *,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Create a fresh disconnect event for every enumerated writer control."""

    inventory = collect_inventory(aws)
    actions: list[dict[str, Any]] = []

    def record(
        kind: str,
        resource_id: str,
        response: Mapping[str, Any],
        http: HttpEvidence,
    ) -> None:
        actions.append(
            {
                "kind": kind,
                "resource_id": resource_id,
                "response_sha256": canonical_sha256(response),
                "request_id_sha256": sha256_bytes(http.request_id.encode()),
                "aws_date_epoch": http.date_epoch,
            }
        )

    response, http = aws.call(
        "cloudtrail",
        "stop-logging",
        ("--name", "teamagent-dev-trail"),
    )
    record(
        "cloudtrail.StopLogging",
        "teamagent-dev-trail",
        response,
        http,
    )
    response, http = aws.call(
        "bedrock",
        "delete-model-invocation-logging-configuration",
    )
    record(
        "bedrock.DeleteModelInvocationLoggingConfiguration",
        "account",
        response,
        http,
    )

    rules = inventory["eventbridge_rules"]
    for rule in rules:
        name = require_string(rule.get("Name"), "EventBridge rule name")
        event_bus = require_string(
            rule.get("EventBusName"), "EventBridge event bus name"
        )
        arguments = [
            "--name",
            name,
            "--event-bus-name",
            event_bus,
        ]
        response, http = aws.call("events", "disable-rule", arguments)
        record(
            "eventbridge.DisableRule",
            f"{event_bus}/{name}",
            response,
            http,
        )

    for schedule in inventory["scheduler_schedules"]:
        allowed = {
            "Name",
            "GroupName",
            "ScheduleExpression",
            "FlexibleTimeWindow",
            "Target",
            "Description",
            "StartDate",
            "EndDate",
            "ScheduleExpressionTimezone",
            "KmsKeyArn",
            "ActionAfterCompletion",
        }
        response_only = {"Arn", "CreationDate", "LastModificationDate", "State"}
        required = {
            "Name",
            "GroupName",
            "ScheduleExpression",
            "FlexibleTimeWindow",
            "Target",
            "State",
        }
        if not required.issubset(schedule):
            raise ContractError("Scheduler schedule omits a required update field")
        if set(schedule) - allowed - response_only:
            raise ContractError("Scheduler schedule contains an unknown field")
        update = {
            key: value
            for key, value in schedule.items()
            if key in allowed and value is not None
        }
        update["State"] = "DISABLED"
        name = require_string(update.get("Name"), "Scheduler schedule name")
        group = require_string(
            update.get("GroupName", "default"), "Scheduler schedule group"
        )
        update["GroupName"] = group
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix="teamagent-schedule.", delete=False
        ) as stream:
            schedule_input = Path(stream.name)
            stream.write(canonical_bytes(update))
        os.chmod(schedule_input, 0o600)
        try:
            response, http = aws.call(
                "scheduler",
                "update-schedule",
                ("--cli-input-json", f"file://{schedule_input}"),
            )
        finally:
            schedule_input.unlink(missing_ok=True)
        record("scheduler.UpdateSchedule", f"{group}/{name}", response, http)

    for mapping in inventory["lambda_event_source_mappings"]:
        uuid = require_string(mapping.get("UUID"), "event source mapping UUID")
        response, http = aws.call(
            "lambda",
            "update-event-source-mapping",
            ("--uuid", uuid, "--no-enabled"),
        )
        record("lambda.UpdateEventSourceMapping", uuid, response, http)

    service_states, _ = _describe_writer_services(aws)
    for service_name, service_state in sorted(service_states.items()):
        if service_state["status"] == "MISSING":
            continue
        response, http = aws.call(
            "ecs",
            "update-service",
            (
                "--cluster",
                "teamagent-dev",
                "--service",
                service_name,
                "--desired-count",
                "0",
            ),
        )
        record("ecs.UpdateService", service_name, response, http)

    for family in WRITER_FAMILIES:
        task_arns = sorted(
            set(
                _list_family_tasks(aws, family, "RUNNING")[0]
                + _list_family_tasks(aws, family, "PENDING")[0]
            )
        )
        for task_arn in task_arns:
            response, http = aws.call(
                "ecs",
                "stop-task",
                (
                    "--cluster",
                    "teamagent-dev",
                    "--task",
                    task_arn,
                    "--reason",
                    "teamagent first-time versioning quiescence",
                ),
            )
            record("ecs.StopTask", task_arn, response, http)

    if not actions:
        raise ContractError(
            "later observation alone is forbidden; no fresh writer disconnect "
            "action was produced"
        )
    actions.sort(key=lambda action: (action["kind"], action["resource_id"]))
    action_requirements = [
        {
            "kind": action["kind"],
            "resource_id": action["resource_id"],
        }
        for action in actions
    ]
    if len(action_requirements) != len(
        {
            (requirement["kind"], requirement["resource_id"])
            for requirement in action_requirements
        }
    ):
        raise ContractError("writer disconnect action set is not exact/unique")
    disconnect_event_epoch = max(action["aws_date_epoch"] for action in actions)
    deadline = int(clock()) + 300
    while True:
        try:
            quiescence = capture_quiescence(aws)
            break
        except ContractError:
            if int(clock()) >= deadline:
                raise
            sleeper(5)
    if quiescence["contract"]["observed_at_epoch"] < disconnect_event_epoch:
        raise ContractError("quiescence observation predates the disconnect event")
    return {
        "actions": actions,
        "action_set_sha256": canonical_sha256(actions),
        "action_requirements": action_requirements,
        "action_requirements_sha256": canonical_sha256(action_requirements),
        "event_time_epoch": disconnect_event_epoch,
        "quiescence": quiescence,
    }


def _versioning_status(
    aws: AwsCli, bucket: str
) -> tuple[dict[str, Any], HttpEvidence]:
    response, http = aws.call(
        "s3api",
        "get-bucket-versioning",
        ("--bucket", bucket, "--expected-bucket-owner", ACCOUNT_ID),
    )
    status = response.get("Status", "Unversioned")
    mfa_delete = response.get("MFADelete", "Disabled")
    if status not in {"Unversioned", "Enabled", "Suspended"}:
        raise ContractError("S3 versioning status is unknown")
    if mfa_delete == "Enabled":
        raise ContractError("MFA Delete is outside this migration")
    return {"status": status, "mfa_delete": mfa_delete}, http


def _versioning_ledger_item(
    workflow_claims: Mapping[str, Any],
    *,
    recorded_at_epoch: int,
) -> dict[str, dict[str, str]]:
    shared_lock = workflow_claims.get("shared_lock")
    disconnect = workflow_claims.get("producer_disconnect")
    buckets = workflow_claims.get("buckets_before")
    cutover = workflow_claims.get("cutover")
    if not all(
        isinstance(value, Mapping)
        for value in (shared_lock, disconnect, buckets, cutover)
    ):
        raise ContractError("versioning ledger claims are incomplete")
    workflow_id = require_string(
        shared_lock.get("workflow_id"), "versioning workflow ID"
    )
    record_id = f"{VERSIONING_LEDGER_RECORD_PREFIX}{workflow_id}"
    bucket_identities = {
        label: value.get("identity")
        for label, value in sorted(buckets.items())
        if isinstance(value, Mapping)
    }
    if set(bucket_identities) != {"bedrock", "cloudtrail"}:
        raise ContractError("versioning ledger bucket identities are incomplete")
    return {
        "record_id": _dynamodb_value(record_id),
        "record_type": _dynamodb_value(
            "teamagent.first-time-versioning-cutover"
        ),
        "schema_version": _dynamodb_value(1),
        "status": _dynamodb_value("COMPLETED"),
        "workflow_id": _dynamodb_value(workflow_id),
        "workflow_claims_sha256": _dynamodb_value(
            canonical_sha256(workflow_claims)
        ),
        "action_set_sha256": _dynamodb_value(
            require_string(
                disconnect.get("action_set_sha256"),
                "versioning disconnect action-set hash",
            )
        ),
        "bucket_identity_sha256": _dynamodb_value(
            canonical_sha256(bucket_identities)
        ),
        "cutover_sha256": _dynamodb_value(canonical_sha256(cutover)),
        "recorded_at_epoch": _dynamodb_value(recorded_at_epoch),
        "audit_expires_at": _dynamodb_value(recorded_at_epoch + 31536000),
    }


def first_time_versioning_cutover(
    aws: AwsCli,
    *,
    lock_id: str,
    lock_receipt: Mapping[str, Any],
    bedrock_config_path: Path,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Enable both buckets once, prove 900 seconds with no writes, then cut over."""

    if lock_id != SHARED_LOCK_RECORD_ID:
        raise ContractError("versioning workflow does not use the shared plan/apply lock")
    initial_lock = verify_runtime_workflow_lock(aws, lock_receipt)
    identity, identity_http = _caller_identity(aws)
    buckets = {
        "cloudtrail": f"teamagent-dev-cloudtrail-{ACCOUNT_ID}",
        "bedrock": f"teamagent-dev-bedrock-logs-{ACCOUNT_ID}",
    }
    before: dict[str, Any] = {}
    for label, bucket in buckets.items():
        bucket_identity, bucket_http = _bucket_identity(aws, bucket)
        versioning, versioning_http = _versioning_status(aws, bucket)
        if versioning["status"] != "Unversioned":
            raise ContractError(
                "first-time workflow rejects later Enabled/Suspended observation"
            )
        before[label] = {
            "identity": bucket_identity,
            "identity_observed_at_epoch": bucket_http.date_epoch,
            "versioning": versioning,
            "versioning_observed_at_epoch": versioning_http.date_epoch,
        }

    disconnect = disconnect_all_writers(aws, clock=clock, sleeper=sleeper)
    quiescence_before = disconnect["quiescence"]
    disconnect_event_epoch = disconnect["event_time_epoch"]
    baseline_hashes: dict[str, str] = {}
    for label, bucket in buckets.items():
        identity_after, identity_after_http = _bucket_identity(aws, bucket)
        if identity_after != before[label]["identity"]:
            raise ContractError("bucket identity changed during producer disconnection")
        versioning_after, versioning_after_http = _versioning_status(aws, bucket)
        if versioning_after != {"status": "Unversioned", "mfa_delete": "Disabled"}:
            raise ContractError("bucket was versioned before the guard-owned enablement")
        object_hash, object_observed = _object_versions_hash(aws, bucket)
        quiescence_observed = quiescence_before["contract"]["observed_at_epoch"]
        if min(
            identity_after_http.date_epoch,
            versioning_after_http.date_epoch,
            object_observed,
        ) < quiescence_observed:
            raise ContractError("no-write baseline predates producer quiescence")
        baseline_hashes[label] = object_hash
        before[label]["post_quiescence_identity_observed_at_epoch"] = (
            identity_after_http.date_epoch
        )
        before[label]["post_quiescence_versioning_observed_at_epoch"] = (
            versioning_after_http.date_epoch
        )
        before[label]["object_versions_observed_at_epoch"] = object_observed

    enablements: dict[str, Any] = {}
    for label, bucket in buckets.items():
        response, put_http = aws.call(
            "s3api",
            "put-bucket-versioning",
            (
                "--bucket",
                bucket,
                "--expected-bucket-owner",
                ACCOUNT_ID,
                "--versioning-configuration",
                '{"Status":"Enabled"}',
            ),
        )
        assert_no_error_fields(response)
        current, first_seen_http = _versioning_status(aws, bucket)
        if current != {"status": "Enabled", "mfa_delete": "Disabled"}:
            raise ContractError("versioning did not become Enabled")
        enablements[label] = {
            "bucket": bucket,
            "action": "PutBucketVersioning",
            "requested_status": "Enabled",
            "response_sha256": canonical_sha256(response),
            "request_id_sha256": sha256_bytes(put_http.request_id.encode()),
            "response_date": put_http.date,
            "event_time_epoch": put_http.date_epoch,
            "first_seen_enabled_epoch": first_seen_http.date_epoch,
            "timestamp_source": "aws-http-response-date",
            "error_code_present": False,
            "error_message_present": False,
            "addendum_present": False,
        }

    not_before = (
        max(
            max(
                enablement["event_time_epoch"],
                enablement["first_seen_enabled_epoch"],
            )
            for enablement in enablements.values()
        )
        + SETTLE_SECONDS
    )
    while int(clock()) < not_before:
        sleeper(min(30, not_before - int(clock())))

    observations: list[dict[str, Any]] = []
    for observation_index in range(2):
        if observation_index:
            sleeper(1)
        quiescence = capture_quiescence(aws)
        object_hashes: dict[str, str] = {}
        versioning_state: dict[str, Any] = {}
        observed_at = quiescence["contract"]["observed_at_epoch"]
        for label, bucket in buckets.items():
            status, status_http = _versioning_status(aws, bucket)
            if status != {"status": "Enabled", "mfa_delete": "Disabled"}:
                raise ContractError("versioning changed during settle observations")
            object_hash, object_observed = _object_versions_hash(aws, bucket)
            if object_hash != baseline_hashes[label]:
                raise ContractError("bucket write occurred during the settle window")
            object_hashes[label] = object_hash
            versioning_state[label] = status
            observed_at = max(
                observed_at, status_http.date_epoch, object_observed
            )
        if observed_at < not_before:
            raise ContractError("post-settle observation occurred too early")
        observations.append(
            {
                "sequence": observation_index + 1,
                "observed_at_epoch": observed_at,
                "quiescence": quiescence["contract"],
                "quiescence_sha256": quiescence["contract_sha256"],
                "object_versions_sha256": object_hashes,
                "versioning": versioning_state,
            }
        )
    if observations[1]["observed_at_epoch"] <= observations[0]["observed_at_epoch"]:
        raise ContractError("quiescence observations are time-inverted")

    final_quiescence = capture_quiescence(aws)
    final_object_hashes: dict[str, str] = {}
    final_versioning_state: dict[str, Any] = {}
    final_epoch = final_quiescence["contract"]["observed_at_epoch"]
    for label, bucket in buckets.items():
        status, status_http = _versioning_status(aws, bucket)
        if status != {"status": "Enabled", "mfa_delete": "Disabled"}:
            raise ContractError("versioning changed before the final cutover recheck")
        object_hash, object_observed = _object_versions_hash(aws, bucket)
        if object_hash != baseline_hashes[label]:
            raise ContractError("bucket write occurred before the final cutover recheck")
        final_object_hashes[label] = object_hash
        final_versioning_state[label] = status
        final_epoch = max(final_epoch, status_http.date_epoch, object_observed)
    if final_epoch < observations[1]["observed_at_epoch"]:
        raise ContractError("final cutover recheck is time-inverted")
    pre_cutover_lock = verify_runtime_workflow_lock(aws, lock_receipt)
    if pre_cutover_lock["workflow_id"] != initial_lock["workflow_id"]:
        raise ContractError("shared workflow lock changed before cutover")
    try:
        bedrock_configuration = json.loads(bedrock_config_path.read_text())
    except json.JSONDecodeError as exc:
        raise ContractError("Bedrock cutover configuration is not JSON") from exc
    expected_bedrock_configuration = {
        "textDataDeliveryEnabled": True,
        "embeddingDataDeliveryEnabled": True,
        "imageDataDeliveryEnabled": False,
        "videoDataDeliveryEnabled": False,
        "s3Config": {
            "bucketName": f"teamagent-dev-bedrock-logs-{ACCOUNT_ID}",
            "keyPrefix": "bedrock/",
        },
    }
    if bedrock_configuration != expected_bedrock_configuration:
        raise ContractError("Bedrock cutover configuration is not exact")
    cloudtrail_started = False
    bedrock_started = False

    def rollback_cutover_producers() -> None:
        rollback_errors: list[str] = []
        if cloudtrail_started:
            try:
                aws.call(
                    "cloudtrail",
                    "stop-logging",
                    ("--name", "teamagent-dev-trail"),
                )
            except ContractError as exc:
                rollback_errors.append(str(exc))
        if bedrock_started:
            try:
                aws.call(
                    "bedrock",
                    "delete-model-invocation-logging-configuration",
                )
            except ContractError as exc:
                rollback_errors.append(str(exc))
        if rollback_errors:
            raise ContractError(
                "producer rollback could not be confirmed: "
                + "; ".join(rollback_errors)
            )

    try:
        cloudtrail_response, cloudtrail_http = aws.call(
            "cloudtrail",
            "start-logging",
            ("--name", "teamagent-dev-trail"),
        )
        cloudtrail_started = True
        bedrock_response, bedrock_http = aws.call(
            "bedrock",
            "put-model-invocation-logging-configuration",
            ("--logging-config", f"file://{bedrock_config_path}"),
        )
        bedrock_started = True
    except ContractError as cutover_error:
        try:
            rollback_cutover_producers()
        except ContractError as rollback_error:
            raise rollback_error from cutover_error
        raise
    assert_no_error_fields(cloudtrail_response)
    assert_no_error_fields(bedrock_response)
    try:
        post_cutover_lock = verify_runtime_workflow_lock(aws, lock_receipt)
    except ContractError as cutover_error:
        try:
            rollback_cutover_producers()
        except ContractError as rollback_error:
            raise rollback_error from cutover_error
        raise
    if post_cutover_lock["workflow_id"] != initial_lock["workflow_id"]:
        rollback_cutover_producers()
        raise ContractError("shared workflow lock changed during cutover")
    if max(
        cloudtrail_http.date_epoch,
        bedrock_http.date_epoch,
    ) > post_cutover_lock["verified_at_epoch"]:
        rollback_cutover_producers()
        raise ContractError("producer cutover exceeds its shared-lock observation")

    cutover = {
        "cloudtrail": {
            "action": "StartLogging",
            "response_sha256": canonical_sha256(cloudtrail_response),
            "request_id_sha256": sha256_bytes(
                cloudtrail_http.request_id.encode()
            ),
            "response_date_epoch": cloudtrail_http.date_epoch,
        },
        "bedrock": {
            "action": "PutModelInvocationLoggingConfiguration",
            "response_sha256": canonical_sha256(bedrock_response),
            "request_id_sha256": sha256_bytes(
                bedrock_http.request_id.encode()
            ),
            "response_date_epoch": bedrock_http.date_epoch,
            "configuration": bedrock_configuration,
            "configuration_sha256": canonical_sha256(
                bedrock_configuration
            ),
        },
    }
    workflow_claims = {
        "kind": "teamagent-first-time-versioning-cutover",
        "schema_version": 1,
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "shared_lock_id": lock_id,
        "shared_lock": {
            "record_id": SHARED_LOCK_RECORD_ID,
            "workflow_id": initial_lock["workflow_id"],
            "acquired_at_epoch": initial_lock["acquired_at_epoch"],
            "lease_expires_at": initial_lock["lease_expires_at"],
            "lock_receipt_sha256": canonical_sha256(lock_receipt),
            "initial_verification_epoch": initial_lock["verified_at_epoch"],
            "pre_cutover_verification_epoch": pre_cutover_lock[
                "verified_at_epoch"
            ],
            "post_cutover_verification_epoch": post_cutover_lock[
                "verified_at_epoch"
            ],
        },
        "aws_executable": asdict(aws.evidence),
        "endpoints": ENDPOINTS,
        "caller_identity": identity,
        "caller_identity_sha256": canonical_sha256(identity),
        "caller_identity_observed_at_epoch": identity_http.date_epoch,
        "producer_disconnect": {
            "event_time_epoch": disconnect_event_epoch,
            "actions": disconnect["actions"],
            "action_set_sha256": disconnect["action_set_sha256"],
            "action_requirements": disconnect["action_requirements"],
            "action_requirements_sha256": disconnect[
                "action_requirements_sha256"
            ],
            "quiescence": quiescence_before["contract"],
            "quiescence_sha256": quiescence_before["contract_sha256"],
        },
        "buckets_before": before,
        "versioning_enablements": enablements,
        "settle_seconds": SETTLE_SECONDS,
        "not_before_epoch": not_before,
        "no_write_baseline_sha256": baseline_hashes,
        "post_settle_observations": observations,
        "final_recheck": {
            "observed_at_epoch": final_epoch,
            "quiescence": final_quiescence["contract"],
            "quiescence_sha256": final_quiescence["contract_sha256"],
            "object_versions_sha256": final_object_hashes,
            "versioning": final_versioning_state,
        },
        "cutover": cutover,
    }
    try:
        ledger_identity, ledger_identity_http = _caller_identity(aws)
    except ContractError as ledger_error:
        try:
            rollback_cutover_producers()
        except ContractError as rollback_error:
            raise rollback_error from ledger_error
        raise
    if (
        ledger_identity != identity
        or max(
            post_cutover_lock["verified_at_epoch"],
            cloudtrail_http.date_epoch,
            bedrock_http.date_epoch,
        )
        > ledger_identity_http.date_epoch
    ):
        rollback_cutover_producers()
        raise ContractError("versioning ledger observation is time-inverted")
    ledger_item = _versioning_ledger_item(
        workflow_claims,
        recorded_at_epoch=ledger_identity_http.date_epoch,
    )
    ledger_record_id = _ddb_scalar(ledger_item, "record_id")
    try:
        ledger_response, ledger_http = aws.call(
            "dynamodb",
            "put-item",
            (
                "--table-name",
                SHARED_LEDGER_TABLE,
                "--item",
                json.dumps(ledger_item, separators=(",", ":")),
                "--condition-expression",
                "attribute_not_exists(record_id)",
            ),
        )
        ledger_confirmation, confirmation_http = aws.call(
            "dynamodb",
            "get-item",
            (
                "--table-name",
                SHARED_LEDGER_TABLE,
                "--key",
                json.dumps(
                    {"record_id": _dynamodb_value(ledger_record_id)},
                    separators=(",", ":"),
                ),
                "--consistent-read",
            ),
        )
    except ContractError as ledger_error:
        try:
            rollback_cutover_producers()
        except ContractError as rollback_error:
            raise rollback_error from ledger_error
        raise
    if ledger_confirmation.get("Item") != ledger_item:
        rollback_cutover_producers()
        raise ContractError("versioning workflow ledger was not durably confirmed")
    try:
        final_identity, final_identity_http = _caller_identity(aws)
        final_lock = verify_runtime_workflow_lock(aws, lock_receipt)
    except ContractError as final_error:
        try:
            rollback_cutover_producers()
        except ContractError as rollback_error:
            raise rollback_error from final_error
        raise
    if (
        final_identity != identity
        or final_lock["workflow_id"] != initial_lock["workflow_id"]
        or max(
            ledger_identity_http.date_epoch,
            ledger_http.date_epoch,
            confirmation_http.date_epoch,
            final_identity_http.date_epoch,
        )
        > final_lock["verified_at_epoch"]
    ):
        rollback_cutover_producers()
        raise ContractError("versioning durable evidence exceeds the shared lock")
    durable_ledger = {
        "record_id": ledger_record_id,
        "record_type": "teamagent.first-time-versioning-cutover",
        "workflow_claims_sha256": canonical_sha256(workflow_claims),
        "item_sha256": canonical_sha256(ledger_item),
        "recorded_at_epoch": ledger_identity_http.date_epoch,
        "audit_expires_at": ledger_identity_http.date_epoch + 31536000,
        "put_response_sha256": canonical_sha256(ledger_response),
        "put_request_id_sha256": sha256_bytes(ledger_http.request_id.encode()),
        "put_aws_date_epoch": ledger_http.date_epoch,
        "confirmation_response_sha256": canonical_sha256(ledger_confirmation),
        "confirmation_request_id_sha256": sha256_bytes(
            confirmation_http.request_id.encode()
        ),
        "confirmed_at_epoch": confirmation_http.date_epoch,
        "final_observed_at_epoch": final_identity_http.date_epoch,
        "final_observation_request_id_sha256": sha256_bytes(
            final_identity_http.request_id.encode()
        ),
        "shared_lock_verified_at_epoch": final_lock["verified_at_epoch"],
    }
    workflow = {
        **workflow_claims,
        "durable_ledger": durable_ledger,
    }
    workflow["workflow_sha256"] = canonical_sha256(workflow)
    return workflow


def validate_versioning_workflow(workflow: Mapping[str, Any]) -> None:
    require_keys(
        workflow,
        (
            "kind",
            "schema_version",
            "account_id",
            "region",
            "shared_lock_id",
            "shared_lock",
            "aws_executable",
            "endpoints",
            "caller_identity",
            "caller_identity_sha256",
            "caller_identity_observed_at_epoch",
            "producer_disconnect",
            "buckets_before",
            "versioning_enablements",
            "settle_seconds",
            "not_before_epoch",
            "no_write_baseline_sha256",
            "post_settle_observations",
            "final_recheck",
            "cutover",
            "durable_ledger",
            "workflow_sha256",
        ),
        "versioning workflow",
    )
    expected_hash = workflow.get("workflow_sha256")
    if not isinstance(expected_hash, str) or not HEX64.fullmatch(expected_hash):
        raise ContractError("versioning workflow hash is invalid")
    without_hash = dict(workflow)
    del without_hash["workflow_sha256"]
    if canonical_sha256(without_hash) != expected_hash:
        raise ContractError("versioning workflow hash does not bind its content")
    if (
        workflow.get("kind") != "teamagent-first-time-versioning-cutover"
        or workflow.get("schema_version") != 1
        or workflow.get("account_id") != ACCOUNT_ID
        or workflow.get("region") != REGION
        or workflow.get("settle_seconds") != SETTLE_SECONDS
        or workflow.get("endpoints") != ENDPOINTS
        or workflow.get("caller_identity", {}).get("Arn") != AUTOMATION_ARN
        or workflow.get("shared_lock_id") != SHARED_LOCK_RECORD_ID
    ):
        raise ContractError("versioning workflow identity is invalid")
    shared_lock = workflow.get("shared_lock")
    if (
        not isinstance(shared_lock, Mapping)
        or set(shared_lock)
        != {
            "record_id",
            "workflow_id",
            "acquired_at_epoch",
            "lease_expires_at",
            "lock_receipt_sha256",
            "initial_verification_epoch",
            "pre_cutover_verification_epoch",
            "post_cutover_verification_epoch",
        }
        or shared_lock.get("record_id") != SHARED_LOCK_RECORD_ID
        or not isinstance(shared_lock.get("workflow_id"), str)
        or not UUID4.fullmatch(str(shared_lock.get("workflow_id")))
        or not HEX64.fullmatch(str(shared_lock.get("lock_receipt_sha256", "")))
    ):
        raise ContractError("versioning shared-lock binding is invalid")
    lock_acquired = require_int(
        shared_lock.get("acquired_at_epoch"), "shared lock acquisition"
    )
    lock_expires = require_int(
        shared_lock.get("lease_expires_at"), "shared lock expiry"
    )
    lock_initial = require_int(
        shared_lock.get("initial_verification_epoch"),
        "initial shared lock verification",
    )
    lock_pre_cutover = require_int(
        shared_lock.get("pre_cutover_verification_epoch"),
        "pre-cutover shared lock verification",
    )
    lock_post_cutover = require_int(
        shared_lock.get("post_cutover_verification_epoch"),
        "post-cutover shared lock verification",
    )
    if not (
        lock_acquired
        <= lock_initial
        <= lock_pre_cutover
        <= lock_post_cutover
        < lock_expires
    ):
        raise ContractError("versioning shared-lock timing is invalid")
    durable_ledger = workflow.get("durable_ledger")
    if not isinstance(durable_ledger, Mapping):
        raise ContractError("versioning durable ledger evidence is missing")
    require_keys(
        durable_ledger,
        (
            "record_id",
            "record_type",
            "workflow_claims_sha256",
            "item_sha256",
            "recorded_at_epoch",
            "audit_expires_at",
            "put_response_sha256",
            "put_request_id_sha256",
            "put_aws_date_epoch",
            "confirmation_response_sha256",
            "confirmation_request_id_sha256",
            "confirmed_at_epoch",
            "final_observed_at_epoch",
            "final_observation_request_id_sha256",
            "shared_lock_verified_at_epoch",
        ),
        "versioning durable ledger",
    )
    workflow_claims = dict(workflow)
    del workflow_claims["durable_ledger"]
    del workflow_claims["workflow_sha256"]
    workflow_id = require_string(
        shared_lock.get("workflow_id"), "versioning workflow ID"
    )
    recorded_at = require_int(
        durable_ledger.get("recorded_at_epoch"),
        "versioning ledger record time",
    )
    audit_expires_at = require_int(
        durable_ledger.get("audit_expires_at"),
        "versioning ledger audit expiry",
    )
    put_at = require_int(
        durable_ledger.get("put_aws_date_epoch"),
        "versioning ledger put time",
    )
    confirmed_at = require_int(
        durable_ledger.get("confirmed_at_epoch"),
        "versioning ledger confirmation time",
    )
    final_observed_at = require_int(
        durable_ledger.get("final_observed_at_epoch"),
        "versioning ledger final observation",
    )
    lock_verified_at = require_int(
        durable_ledger.get("shared_lock_verified_at_epoch"),
        "versioning ledger shared-lock observation",
    )
    expected_ledger_item = _versioning_ledger_item(
        workflow_claims,
        recorded_at_epoch=recorded_at,
    )
    if (
        durable_ledger.get("record_id")
        != f"{VERSIONING_LEDGER_RECORD_PREFIX}{workflow_id}"
        or durable_ledger.get("record_type")
        != "teamagent.first-time-versioning-cutover"
        or durable_ledger.get("workflow_claims_sha256")
        != canonical_sha256(workflow_claims)
        or durable_ledger.get("item_sha256")
        != canonical_sha256(expected_ledger_item)
        or audit_expires_at != recorded_at + 31536000
        or not (
            lock_post_cutover
            <= recorded_at
            <= put_at
            <= confirmed_at
            <= final_observed_at
            <= lock_verified_at
            < lock_expires
        )
    ):
        raise ContractError("versioning durable ledger binding/timing is invalid")
    for field in (
        "workflow_claims_sha256",
        "item_sha256",
        "put_response_sha256",
        "put_request_id_sha256",
        "confirmation_response_sha256",
        "confirmation_request_id_sha256",
        "final_observation_request_id_sha256",
    ):
        if not HEX64.fullmatch(str(durable_ledger.get(field, ""))):
            raise ContractError(f"versioning durable ledger {field} is invalid")
    if workflow.get("caller_identity_sha256") != canonical_sha256(
        workflow.get("caller_identity")
    ):
        raise ContractError("caller identity hash is invalid")
    caller_identity = workflow.get("caller_identity")
    executable = workflow.get("aws_executable")
    if (
        not isinstance(caller_identity, Mapping)
        or set(caller_identity) != {"UserId", "Account", "Arn"}
        or caller_identity.get("Account") != ACCOUNT_ID
        or caller_identity.get("Arn") != AUTOMATION_ARN
        or not isinstance(executable, Mapping)
        or set(executable)
        != {"path", "device", "inode", "size", "sha256", "version"}
        or not str(executable.get("path", "")).startswith("/")
        or require_int(executable.get("device"), "SNS AWS executable device") < 0
        or require_int(executable.get("inode"), "SNS AWS executable inode", minimum=1)
        < 1
        or require_int(executable.get("size"), "SNS AWS executable size", minimum=1)
        < 1
        or not HEX64.fullmatch(str(executable.get("sha256", "")))
        or not str(executable.get("version", "")).startswith("aws-cli/2.")
    ):
        raise ContractError("caller/AWS executable evidence is invalid")
    before = workflow.get("buckets_before")
    enablements = workflow.get("versioning_enablements")
    observations = workflow.get("post_settle_observations")
    baseline = workflow.get("no_write_baseline_sha256")
    if not all(isinstance(value, Mapping) for value in (before, enablements, baseline)):
        raise ContractError("versioning bucket contracts are missing")
    if set(before) != {"cloudtrail", "bedrock"} or set(enablements) != {
        "cloudtrail",
        "bedrock",
    }:
        raise ContractError("versioning workflow bucket set is not exact")
    if (
        set(baseline) != {"cloudtrail", "bedrock"}
        or not all(HEX64.fullmatch(str(value)) for value in baseline.values())
    ):
        raise ContractError("versioning no-write baseline is invalid")
    if not isinstance(observations, list) or len(observations) != 2:
        raise ContractError("versioning workflow needs exactly two observations")
    not_before = require_int(workflow.get("not_before_epoch"), "not_before_epoch")
    disconnect = workflow.get("producer_disconnect")
    if not isinstance(disconnect, Mapping):
        raise ContractError("producer disconnect contract is missing")
    require_keys(
        disconnect,
        (
            "event_time_epoch",
            "actions",
            "action_set_sha256",
            "action_requirements",
            "action_requirements_sha256",
            "quiescence",
            "quiescence_sha256",
        ),
        "producer disconnect",
    )
    disconnect_epoch = require_int(
        disconnect.get("event_time_epoch"), "disconnect event time"
    )
    if require_int(
        workflow.get("caller_identity_observed_at_epoch"),
        "caller identity observation",
    ) > disconnect_epoch:
        raise ContractError("caller observation exceeds producer disconnect")
    actions = disconnect.get("actions")
    action_requirements = disconnect.get("action_requirements")
    quiescence = disconnect.get("quiescence")
    if (
        not isinstance(actions, list)
        or not actions
        or not all(isinstance(action, Mapping) for action in actions)
        or disconnect.get("action_set_sha256") != canonical_sha256(actions)
        or not isinstance(action_requirements, list)
        or not all(
            isinstance(requirement, Mapping)
            and set(requirement) == {"kind", "resource_id"}
            for requirement in action_requirements
        )
        or disconnect.get("action_requirements_sha256")
        != canonical_sha256(action_requirements)
        or action_requirements
        != [
            {
                "kind": action.get("kind"),
                "resource_id": action.get("resource_id"),
            }
            for action in actions
        ]
        or len(action_requirements)
        != len(
            {
                (requirement["kind"], requirement["resource_id"])
                for requirement in action_requirements
            }
        )
        or not isinstance(quiescence, Mapping)
        or disconnect.get("quiescence_sha256") != canonical_sha256(quiescence)
    ):
        raise ContractError("producer disconnect action/quiescence binding is invalid")
    allowed_action_kinds = {
        "eventbridge.DisableRule",
        "scheduler.UpdateSchedule",
        "lambda.UpdateEventSourceMapping",
        "ecs.UpdateService",
        "ecs.StopTask",
        "cloudtrail.StopLogging",
        "bedrock.DeleteModelInvocationLoggingConfiguration",
    }
    for action in actions:
        require_keys(
            action,
            (
                "kind",
                "resource_id",
                "response_sha256",
                "request_id_sha256",
                "aws_date_epoch",
            ),
            "producer disconnect action",
        )
        if (
            action["kind"] not in allowed_action_kinds
            or not require_string(
                action["resource_id"], "producer disconnect resource"
            )
            or not HEX64.fullmatch(str(action["response_sha256"]))
            or not HEX64.fullmatch(str(action["request_id_sha256"]))
            or require_int(
                action["aws_date_epoch"], "producer disconnect AWS Date"
            )
            > disconnect_epoch
        ):
            raise ContractError("producer disconnect action is invalid")
    if disconnect_epoch != max(
        require_int(action["aws_date_epoch"], "producer disconnect AWS Date")
        for action in actions
    ):
        raise ContractError("producer disconnect event time is not authoritative")
    action_kinds = [requirement["kind"] for requirement in action_requirements]
    if (
        action_kinds.count("cloudtrail.StopLogging") != 1
        or action_kinds.count(
            "bedrock.DeleteModelInvocationLoggingConfiguration"
        )
        != 1
    ):
        raise ContractError("producer disconnect action coverage is incomplete")
    require_keys(
        quiescence,
        (
            "inventory_sha256",
            "raw_reference_set_sha256",
            "eventbridge_all_disabled",
            "scheduler_all_disabled",
            "lambda_mappings_all_disabled",
            "writer_controls",
            "ecs_families",
            "ecs_services",
            "queues",
            "log_producers",
            "observed_at_epoch",
        ),
        "producer quiescence",
    )
    if (
        quiescence["eventbridge_all_disabled"] is not True
        or quiescence["scheduler_all_disabled"] is not True
        or quiescence["lambda_mappings_all_disabled"] is not True
        or not HEX64.fullmatch(str(quiescence["inventory_sha256"]))
        or not HEX64.fullmatch(str(quiescence["raw_reference_set_sha256"]))
        or require_int(
            quiescence["observed_at_epoch"], "quiescence observation"
        )
        < disconnect_epoch
    ):
        raise ContractError("producer quiescence state/timing is invalid")
    writer_controls = quiescence.get("writer_controls")
    if (
        not isinstance(writer_controls, Mapping)
        or set(writer_controls)
        != {"eventbridge", "scheduler", "lambda_mappings"}
        or any(
            not isinstance(values, list)
            or not all(isinstance(value, str) and value for value in values)
            or values != sorted(values)
            or len(values) != len(set(values))
            for values in writer_controls.values()
        )
    ):
        raise ContractError("producer writer-control inventory is incomplete")
    family_states = quiescence.get("ecs_families")
    if (
        not isinstance(family_states, Mapping)
        or set(family_states) != set(WRITER_FAMILIES)
        or any(
            state != {"running": 0, "pending": 0}
            for state in family_states.values()
        )
    ):
        raise ContractError("producer ECS family quiescence is incomplete")
    service_states = quiescence.get("ecs_services")
    if (
        not isinstance(service_states, Mapping)
        or set(service_states) != set(WRITER_SERVICES)
        or any(
            not isinstance(state, Mapping)
            or state.get("desired") != 0
            or state.get("running") != 0
            or state.get("pending") != 0
            or state.get("status") not in {"ACTIVE", "DRAINING", "INACTIVE", "MISSING"}
            for state in service_states.values()
        )
    ):
        raise ContractError("producer ECS service quiescence is incomplete")
    required_action_pairs = {
        *{
            ("eventbridge.DisableRule", resource_id)
            for resource_id in writer_controls["eventbridge"]
        },
        *{
            ("scheduler.UpdateSchedule", resource_id)
            for resource_id in writer_controls["scheduler"]
        },
        *{
            ("lambda.UpdateEventSourceMapping", resource_id)
            for resource_id in writer_controls["lambda_mappings"]
        },
        *{
            ("ecs.UpdateService", service_name)
            for service_name, state in service_states.items()
            if state.get("status") != "MISSING"
        },
        ("cloudtrail.StopLogging", "teamagent-dev-trail"),
        (
            "bedrock.DeleteModelInvocationLoggingConfiguration",
            "account",
        ),
    }
    actual_action_pairs = {
        (requirement["kind"], requirement["resource_id"])
        for requirement in action_requirements
    }
    if (
        not required_action_pairs.issubset(actual_action_pairs)
        or any(
            pair not in required_action_pairs and pair[0] != "ecs.StopTask"
            for pair in actual_action_pairs
        )
    ):
        raise ContractError("writer disconnect actions do not cover every control")
    queues = quiescence.get("queues")
    if (
        not isinstance(queues, Mapping)
        or any(
            not isinstance(state, Mapping)
            or set(state)
            != {
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
                "ApproximateNumberOfMessagesDelayed",
            }
            or any(value != 0 for value in state.values())
            for state in queues.values()
        )
    ):
        raise ContractError("producer queue/in-flight quiescence is incomplete")
    log_producers = quiescence.get("log_producers")
    cloudtrail_producer = (
        log_producers.get("cloudtrail")
        if isinstance(log_producers, Mapping)
        else None
    )
    bedrock_producer = (
        log_producers.get("bedrock")
        if isinstance(log_producers, Mapping)
        else None
    )
    cloudtrail_identity = (
        cloudtrail_producer.get("identity")
        if isinstance(cloudtrail_producer, Mapping)
        else None
    )
    if (
        not isinstance(log_producers, Mapping)
        or set(log_producers) != {"cloudtrail", "bedrock"}
        or not isinstance(cloudtrail_producer, Mapping)
        or not isinstance(bedrock_producer, Mapping)
        or set(cloudtrail_producer)
        != {
            "trail_name",
            "bucket",
            "identity",
            "is_logging",
            "trail_response_sha256",
            "status_response_sha256",
        }
        or cloudtrail_producer.get("trail_name")
        != "teamagent-dev-trail"
        or cloudtrail_producer.get("bucket")
        != f"teamagent-dev-cloudtrail-{ACCOUNT_ID}"
        or not isinstance(cloudtrail_identity, Mapping)
        or set(cloudtrail_identity)
        != {
            "trail_name",
            "trail_arn",
            "home_region",
            "bucket",
            "is_multi_region",
            "include_global_service_events",
            "log_file_validation_enabled",
            "kms_key_arn",
            "is_organization_trail",
        }
        or cloudtrail_identity.get("trail_name") != "teamagent-dev-trail"
        or cloudtrail_identity.get("trail_arn")
        != (
            f"arn:aws:cloudtrail:{REGION}:{ACCOUNT_ID}:trail/"
            "teamagent-dev-trail"
        )
        or cloudtrail_identity.get("home_region") != REGION
        or cloudtrail_identity.get("bucket")
        != f"teamagent-dev-cloudtrail-{ACCOUNT_ID}"
        or cloudtrail_identity.get("is_multi_region") is not True
        or cloudtrail_identity.get("include_global_service_events") is not True
        or cloudtrail_identity.get("log_file_validation_enabled") is not True
        or cloudtrail_identity.get("is_organization_trail") is not False
        or not re.fullmatch(
            (
                rf"arn:aws:kms:{re.escape(REGION)}:{ACCOUNT_ID}:key/"
                r"[0-9a-fA-F-]{36}"
            ),
            str(cloudtrail_identity.get("kms_key_arn", "")),
        )
        or cloudtrail_producer.get("is_logging") is not False
        or not HEX64.fullmatch(
            str(
                cloudtrail_producer.get("trail_response_sha256", "")
            )
        )
        or not HEX64.fullmatch(
            str(
                cloudtrail_producer.get("status_response_sha256", "")
            )
        )
        or bedrock_producer
        != {
            "configured": False,
            "logging_config_sha256": canonical_sha256(None),
        }
    ):
        raise ContractError("CloudTrail/Bedrock producer-off state is incomplete")
    for label in ("cloudtrail", "bedrock"):
        bucket_before = before[label]
        expected_bucket = (
            f"teamagent-dev-cloudtrail-{ACCOUNT_ID}"
            if label == "cloudtrail"
            else f"teamagent-dev-bedrock-logs-{ACCOUNT_ID}"
        )
        expected_identity = bucket_before.get("identity")
        if (
            set(bucket_before)
            != {
                "identity",
                "identity_observed_at_epoch",
                "versioning",
                "versioning_observed_at_epoch",
                "post_quiescence_identity_observed_at_epoch",
                "post_quiescence_versioning_observed_at_epoch",
                "object_versions_observed_at_epoch",
            }
            or
            bucket_before.get("versioning")
            != {"status": "Unversioned", "mfa_delete": "Disabled"}
            or not isinstance(expected_identity, Mapping)
            or expected_identity.get("name") != expected_bucket
            or expected_identity.get("arn")
            != f"arn:aws:s3:::{expected_bucket}"
            or not expected_identity.get("owner_canonical_id")
            or not expected_identity.get("creation_date")
        ):
            raise ContractError("first-time bucket identity/status is invalid")
        parse_iso_epoch(
            expected_identity["creation_date"],
            f"{label} bucket CreationDate",
        )
        for observation_field in (
            "identity_observed_at_epoch",
            "versioning_observed_at_epoch",
        ):
            if require_int(
                bucket_before[observation_field],
                f"{label} {observation_field}",
            ) > disconnect_epoch:
                raise ContractError(
                    "bucket pre-versioning observation exceeds disconnect event"
                )
        post_identity_observed_at = require_int(
            bucket_before["post_quiescence_identity_observed_at_epoch"],
            f"{label} post-quiescence identity observation",
        )
        post_versioning_observed_at = require_int(
            bucket_before["post_quiescence_versioning_observed_at_epoch"],
            f"{label} post-quiescence versioning observation",
        )
        baseline_observed_at = require_int(
            bucket_before["object_versions_observed_at_epoch"],
            f"{label} no-write baseline observation",
        )
        quiescence_observed_at = require_int(
            quiescence["observed_at_epoch"], "quiescence observation"
        )
        if not (
            quiescence_observed_at
            <= post_identity_observed_at
            <= baseline_observed_at
            and quiescence_observed_at
            <= post_versioning_observed_at
            <= baseline_observed_at
        ):
            raise ContractError("bucket no-write baseline predates quiescence")
        enablement = enablements[label]
        required_enablement = {
            "bucket",
            "action",
            "requested_status",
            "response_sha256",
            "request_id_sha256",
            "response_date",
            "event_time_epoch",
            "first_seen_enabled_epoch",
            "timestamp_source",
            "error_code_present",
            "error_message_present",
            "addendum_present",
        }
        if set(enablement) != required_enablement:
            raise ContractError("versioning enablement response schema is not exact")
        if (
            enablement["action"] != "PutBucketVersioning"
            or enablement["bucket"] != expected_bucket
            or enablement["requested_status"] != "Enabled"
            or enablement["timestamp_source"] != "aws-http-response-date"
            or enablement["error_code_present"] is not False
            or enablement["error_message_present"] is not False
            or enablement["addendum_present"] is not False
            or not HEX64.fullmatch(str(enablement["request_id_sha256"]))
            or not HEX64.fullmatch(str(enablement["response_sha256"]))
        ):
            raise ContractError("versioning enablement response is not authoritative")
        _, response_date_epoch = parse_aws_date(enablement["response_date"])
        event_epoch = require_int(
            enablement["event_time_epoch"], "versioning event time"
        )
        first_seen_epoch = require_int(
            enablement["first_seen_enabled_epoch"],
            "versioning first-seen time",
        )
        if (
            response_date_epoch != event_epoch
            or baseline_observed_at > event_epoch
            or first_seen_epoch < event_epoch
        ):
            raise ContractError("versioning predates producer disconnection")
    expected_not_before = (
        max(
            max(
                require_int(value["event_time_epoch"], "event_time_epoch"),
                require_int(
                    value["first_seen_enabled_epoch"],
                    "first_seen_enabled_epoch",
                ),
            )
            for value in enablements.values()
        )
        + SETTLE_SECONDS
    )
    if not_before != expected_not_before:
        raise ContractError("versioning settle boundary is not exact")

    def stable_quiescence_state(value: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(value)
        normalized.pop("observed_at_epoch", None)
        # Every all-page inventory call has fresh AWS Date/request-id evidence,
        # so its evidence hash must change between independent observations.
        # Compare the fully enumerated semantic writer state and raw reference
        # set while validating each fresh inventory hash independently.
        normalized.pop("inventory_sha256", None)
        return normalized

    initial_stable_quiescence = stable_quiescence_state(quiescence)
    previous_observation = not_before
    for sequence, observation in enumerate(observations, 1):
        if not isinstance(observation, Mapping):
            raise ContractError("versioning observation is malformed")
        require_keys(
            observation,
            (
                "sequence",
                "observed_at_epoch",
                "quiescence",
                "quiescence_sha256",
                "object_versions_sha256",
                "versioning",
            ),
            "versioning observation",
        )
        observed_at = require_int(
            observation.get("observed_at_epoch"), "observation time"
        )
        observation_quiescence = observation.get("quiescence")
        if (
            observation.get("sequence") != sequence
            or (
                observed_at < previous_observation
                if sequence == 1
                else observed_at <= previous_observation
            )
            or not HEX64.fullmatch(
                str(observation.get("quiescence_sha256", ""))
            )
            or not isinstance(observation_quiescence, Mapping)
            or observation.get("quiescence_sha256")
            != canonical_sha256(observation_quiescence)
            or not HEX64.fullmatch(
                str(observation_quiescence.get("inventory_sha256", ""))
            )
            or stable_quiescence_state(observation_quiescence)
            != initial_stable_quiescence
            or require_int(
                observation_quiescence.get("observed_at_epoch"),
                "post-settle quiescence observation",
            )
            > observed_at
            or require_int(
                observation_quiescence.get("observed_at_epoch"),
                "post-settle quiescence observation",
            )
            < previous_observation
            or observation.get("object_versions_sha256") != baseline
            or observation.get("versioning")
            != {
                "cloudtrail": {"status": "Enabled", "mfa_delete": "Disabled"},
                "bedrock": {"status": "Enabled", "mfa_delete": "Disabled"},
            }
        ):
            raise ContractError("post-settle observation is invalid or time-inverted")
        previous_observation = observed_at
    final = workflow.get("final_recheck")
    cutover = workflow.get("cutover")
    if not isinstance(final, Mapping) or not isinstance(cutover, Mapping):
        raise ContractError("final recheck/cutover is missing")
    require_keys(
        final,
        (
            "observed_at_epoch",
            "quiescence",
            "quiescence_sha256",
            "object_versions_sha256",
            "versioning",
        ),
        "final versioning recheck",
    )
    final_epoch = require_int(final.get("observed_at_epoch"), "final recheck time")
    final_quiescence = final.get("quiescence")
    if (
        final_epoch < previous_observation
        or not HEX64.fullmatch(str(final.get("quiescence_sha256", "")))
        or not isinstance(final_quiescence, Mapping)
        or final.get("quiescence_sha256") != canonical_sha256(final_quiescence)
        or not HEX64.fullmatch(
            str(final_quiescence.get("inventory_sha256", ""))
        )
        or stable_quiescence_state(final_quiescence)
        != initial_stable_quiescence
        or require_int(
            final_quiescence.get("observed_at_epoch"),
            "final quiescence observation",
        )
        < previous_observation
        or require_int(
            final_quiescence.get("observed_at_epoch"),
            "final quiescence observation",
        )
        > final_epoch
        or final.get("object_versions_sha256") != baseline
        or final.get("versioning")
        != {
            "cloudtrail": {"status": "Enabled", "mfa_delete": "Disabled"},
            "bedrock": {"status": "Enabled", "mfa_delete": "Disabled"},
        }
    ):
        raise ContractError("final recheck is time-inverted")
    if set(cutover) != {"cloudtrail", "bedrock"}:
        raise ContractError("cutover producer set is not exact")
    if (
        set(cutover["cloudtrail"])
        != {
            "action",
            "response_sha256",
            "request_id_sha256",
            "response_date_epoch",
        }
        or set(cutover["bedrock"])
        != {
            "action",
            "response_sha256",
            "request_id_sha256",
            "response_date_epoch",
            "configuration",
            "configuration_sha256",
        }
        or
        cutover["cloudtrail"].get("action") != "StartLogging"
        or cutover["bedrock"].get("action")
        != "PutModelInvocationLoggingConfiguration"
    ):
        raise ContractError("cutover actions are not exact")
    for producer in cutover.values():
        response_epoch = require_int(
            producer.get("response_date_epoch"), "cutover response date"
        )
        if not (
            max(final_epoch, lock_pre_cutover)
            <= response_epoch
            <= lock_post_cutover
        ):
            raise ContractError(
                "cutover response is outside final shared-lock observations"
            )
        for hash_field in ("request_id_sha256", "response_sha256"):
            if not HEX64.fullmatch(str(producer.get(hash_field, ""))):
                raise ContractError(f"cutover {hash_field} is invalid")
    if not HEX64.fullmatch(
        str(cutover["bedrock"].get("configuration_sha256", ""))
    ):
        raise ContractError("Bedrock cutover configuration hash is invalid")
    expected_bedrock_configuration = {
        "textDataDeliveryEnabled": True,
        "embeddingDataDeliveryEnabled": True,
        "imageDataDeliveryEnabled": False,
        "videoDataDeliveryEnabled": False,
        "s3Config": {
            "bucketName": f"teamagent-dev-bedrock-logs-{ACCOUNT_ID}",
            "keyPrefix": "bedrock/",
        },
    }
    if (
        cutover["bedrock"].get("configuration")
        != expected_bedrock_configuration
        or cutover["bedrock"].get("configuration_sha256")
        != canonical_sha256(expected_bedrock_configuration)
    ):
        raise ContractError("Bedrock cutover configuration binding is invalid")


def verify_versioning_cutover_live(
    aws: AwsCli, workflow: Mapping[str, Any]
) -> dict[str, Any]:
    validate_versioning_workflow(workflow)
    if workflow.get("aws_executable") != asdict(aws.evidence):
        raise ContractError("AWS executable differs from the cutover receipt")
    durable_ledger = workflow["durable_ledger"]
    workflow_claims = dict(workflow)
    del workflow_claims["durable_ledger"]
    del workflow_claims["workflow_sha256"]
    expected_ledger_item = _versioning_ledger_item(
        workflow_claims,
        recorded_at_epoch=require_int(
            durable_ledger["recorded_at_epoch"],
            "versioning ledger record time",
        ),
    )
    ledger, ledger_http = aws.call(
        "dynamodb",
        "get-item",
        (
            "--table-name",
            SHARED_LEDGER_TABLE,
            "--key",
            json.dumps(
                {
                    "record_id": _dynamodb_value(
                        str(durable_ledger["record_id"])
                    )
                },
                separators=(",", ":"),
            ),
            "--consistent-read",
        ),
    )
    if ledger.get("Item") != expected_ledger_item:
        raise ContractError("versioning durable ledger differs from the receipt")
    identity, identity_http = _caller_identity(aws)
    if canonical_sha256(identity) != workflow["caller_identity_sha256"]:
        raise ContractError("caller identity differs from the cutover receipt")
    if ledger_http.date_epoch > identity_http.date_epoch:
        raise ContractError("versioning ledger observation is time-inverted")
    for label, bucket in (
        ("cloudtrail", f"teamagent-dev-cloudtrail-{ACCOUNT_ID}"),
        ("bedrock", f"teamagent-dev-bedrock-logs-{ACCOUNT_ID}"),
    ):
        identity_contract, _ = _bucket_identity(aws, bucket)
        if identity_contract != workflow["buckets_before"][label]["identity"]:
            raise ContractError("bucket owner/CreationDate identity changed")
        status, _ = _versioning_status(aws, bucket)
        if status != {"status": "Enabled", "mfa_delete": "Disabled"}:
            raise ContractError("versioning is no longer Enabled")
    quiescence = capture_quiescence(
        aws, require_log_producers_off=False
    )
    trail, trail_http = aws.call(
        "cloudtrail", "get-trail", ("--name", "teamagent-dev-trail")
    )
    trail_status, status_http = aws.call(
        "cloudtrail", "get-trail-status", ("--name", "teamagent-dev-trail")
    )
    bedrock, bedrock_http = aws.call(
        "bedrock", "get-model-invocation-logging-configuration"
    )
    expected_cloudtrail_bucket = f"teamagent-dev-cloudtrail-{ACCOUNT_ID}"
    expected_bedrock_bucket = f"teamagent-dev-bedrock-logs-{ACCOUNT_ID}"
    trail_identity = _cloudtrail_identity_contract(trail)
    bedrock_contract = bedrock.get("loggingConfig")
    if (
        trail_identity
        != workflow["producer_disconnect"]["quiescence"]["log_producers"][
            "cloudtrail"
        ]["identity"]
        or trail_identity.get("bucket") != expected_cloudtrail_bucket
        or trail_status.get("IsLogging") is not True
        or not isinstance(bedrock_contract, Mapping)
        or bedrock_contract.get("s3Config")
        != {"bucketName": expected_bedrock_bucket, "keyPrefix": "bedrock/"}
        or bedrock_contract.get("textDataDeliveryEnabled") is not True
        or bedrock_contract.get("embeddingDataDeliveryEnabled") is not True
        or bedrock_contract.get("imageDataDeliveryEnabled") is not False
        or bedrock_contract.get("videoDataDeliveryEnabled") is not False
    ):
        raise ContractError("CloudTrail/Bedrock cutover state differs from receipt")
    observed_at = max(
        ledger_http.date_epoch,
        identity_http.date_epoch,
        trail_http.date_epoch,
        status_http.date_epoch,
        bedrock_http.date_epoch,
        quiescence["contract"]["observed_at_epoch"],
    )
    return {
        "verified": True,
        "observed_at_epoch": observed_at,
        "quiescence_sha256": quiescence["contract_sha256"],
        "caller_identity_sha256": canonical_sha256(identity),
        "ledger_request_id_sha256": sha256_bytes(
            ledger_http.request_id.encode()
        ),
    }


def verify_bedrock_retention_live(aws: AwsCli) -> dict[str, Any]:
    """Prove the live minimum-60-day, overwrite-safe AI I/O contract."""

    bucket = f"teamagent-dev-bedrock-logs-{ACCOUNT_ID}"
    lifecycle, lifecycle_http = aws.call(
        "s3api",
        "get-bucket-lifecycle-configuration",
        ("--bucket", bucket, "--expected-bucket-owner", ACCOUNT_ID),
    )
    rules = lifecycle.get("Rules")
    if not isinstance(rules, list):
        raise ContractError("Bedrock lifecycle Rules are missing")
    normalized_rules = sorted(
        rules,
        key=lambda rule: str(rule.get("ID")) if isinstance(rule, Mapping) else "",
    )
    expected_rules = sorted(
        [
            {
                "Expiration": {"Days": 60},
                "Filter": {"Prefix": "bedrock/"},
                "ID": "bedrock-current-and-noncurrent-minimum-60-days",
                "NoncurrentVersionExpiration": {"NoncurrentDays": 60},
                "Status": "Enabled",
            },
            {
                "Expiration": {"ExpiredObjectDeleteMarker": True},
                "Filter": {"Prefix": "bedrock/"},
                "ID": "bedrock-expired-delete-markers",
                "Status": "Enabled",
            },
        ],
        key=lambda rule: rule["ID"],
    )
    if normalized_rules != expected_rules:
        raise ContractError(
            "Bedrock lifecycle is not exact current=60/noncurrent=60"
        )

    policy_response, policy_http = aws.call(
        "s3api",
        "get-bucket-policy",
        ("--bucket", bucket, "--expected-bucket-owner", ACCOUNT_ID),
    )
    policy_text = require_string(policy_response.get("Policy"), "Bedrock policy")
    try:
        policy = json.loads(policy_text)
    except json.JSONDecodeError as exc:
        raise ContractError("Bedrock bucket policy is not JSON") from exc
    statements = policy.get("Statement") if isinstance(policy, Mapping) else None
    if not isinstance(statements, list) or not all(
        isinstance(statement, Mapping) for statement in statements
    ):
        raise ContractError("Bedrock bucket policy statements are malformed")
    by_sid = {
        require_string(statement.get("Sid"), "Bedrock policy Sid"): statement
        for statement in statements
    }
    if len(by_sid) != len(statements):
        raise ContractError("Bedrock bucket policy has duplicate Sid values")
    delete_deny = by_sid.get("DenyManualBedrockPayloadDeletion")
    writer_deny = by_sid.get("DenyNonBedrockPayloadWriters")
    if (
        delete_deny is None
        or delete_deny.get("Effect") != "Deny"
        or delete_deny.get("Principal") != "*"
        or set(
            delete_deny.get("Action")
            if isinstance(delete_deny.get("Action"), list)
            else []
        )
        != {"s3:DeleteObject", "s3:DeleteObjectVersion"}
        or delete_deny.get("Resource") != f"arn:aws:s3:::{bucket}/bedrock/*"
    ):
        raise ContractError("manual AI I/O deletion is not denied")
    if (
        writer_deny is None
        or writer_deny.get("Effect") != "Deny"
        or writer_deny.get("Principal") != "*"
        or writer_deny.get("Action") != "s3:PutObject"
        or writer_deny.get("Resource") != f"arn:aws:s3:::{bucket}/bedrock/*"
        or writer_deny.get("Condition")
        != {
            "StringNotEquals": {
                "aws:PrincipalServiceName": "bedrock.amazonaws.com"
            }
        }
    ):
        raise ContractError("AI I/O writer identity is not restricted to Bedrock")
    observed_at = max(lifecycle_http.date_epoch, policy_http.date_epoch)
    contract = {
        "bucket": bucket,
        "current_expiration_days": 60,
        "noncurrent_expiration_days": 60,
        "manual_delete_denied": True,
        "writer_service": "bedrock.amazonaws.com",
        "lifecycle_sha256": canonical_sha256(lifecycle),
        "policy_sha256": canonical_sha256(policy),
        "observed_at_epoch": observed_at,
    }
    return {
        "kind": "teamagent-bedrock-retention-live-evidence",
        "schema_version": 1,
        "contract": contract,
        "contract_sha256": canonical_sha256(contract),
    }


def _fresh_output(path: Path) -> int:
    if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
        raise ContractError("fresh export path must have a canonical parent")
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path, flags, 0o600)


def fetch_exact_s3_export(
    aws: AwsCli,
    *,
    bucket: str,
    key: str,
    version_id: str,
    output_path: Path,
    observation_epoch: int,
) -> dict[str, Any]:
    if not VERSION_ID.fullmatch(version_id):
        raise ContractError("S3 VersionId is invalid")
    head, head_http = aws.call(
        "s3api",
        "head-object",
        (
            "--bucket",
            bucket,
            "--key",
            key,
            "--version-id",
            version_id,
            "--expected-bucket-owner",
            ACCOUNT_ID,
            "--checksum-mode",
            "ENABLED",
        ),
    )
    if head.get("VersionId") != version_id:
        raise ContractError("head-object returned a different S3 version")
    content_length = require_int(head.get("ContentLength"), "ContentLength", minimum=1)
    last_modified_epoch = parse_iso_epoch(head.get("LastModified"), "LastModified")
    etag = require_string(head.get("ETag"), "ETag")
    if not ETAG.fullmatch(etag):
        raise ContractError("S3 ETag is invalid")
    checksum_names = (
        "ChecksumCRC32",
        "ChecksumCRC32C",
        "ChecksumCRC64NVME",
        "ChecksumSHA1",
        "ChecksumSHA256",
    )
    head_checksums = {
        name: value
        for name in checksum_names
        if (value := head.get(name)) is not None
    }
    if not head_checksums or any(
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value)
        for value in head_checksums.values()
    ):
        raise ContractError("S3 object has no valid AWS checksum metadata")
    fd = _fresh_output(output_path)
    try:
        before = FileIdentity.from_fd(output_path, fd)
        if before.size != 0 or before.nlink != 1:
            raise ContractError("fresh export FD is not empty/single-link")
        response, get_http = aws.call(
            "s3api",
            "get-object",
            (
                "--bucket",
                bucket,
                "--key",
                key,
                "--version-id",
                version_id,
                "--expected-bucket-owner",
                ACCOUNT_ID,
                "--checksum-mode",
                "ENABLED",
            ),
            output_fd=fd,
        )
        os.fsync(fd)
        after = FileIdentity.from_fd(output_path, fd)
        content_sha = hash_open_fd(fd)
        final = FileIdentity.from_fd(output_path, fd)
        assert_path_matches_identity(output_path, final)
    finally:
        os.close(fd)
    if after != final:
        raise ContractError("fresh export mutated while hashing")
    if after.device != before.device or after.inode != before.inode:
        raise ContractError("fresh export FD identity changed")
    if after.nlink != 1 or after.size != content_length:
        raise ContractError("fresh export size/link count differs from S3")
    if response.get("VersionId") != version_id:
        raise ContractError("get-object returned a different S3 version")
    if response.get("ContentLength") != content_length:
        raise ContractError("get-object ContentLength differs from head-object")
    if response.get("ETag") != etag:
        raise ContractError("get-object ETag differs from head-object")
    if response.get("LastModified") != head.get("LastModified"):
        raise ContractError("get-object LastModified differs from head-object")
    for checksum_name in checksum_names:
        if head.get(checksum_name) != response.get(checksum_name):
            raise ContractError(f"S3 {checksum_name} differs between head/get")
    _, observation_http = _caller_identity(aws)
    effective_observation_epoch = observation_http.date_epoch
    if effective_observation_epoch < observation_epoch:
        raise ContractError("fresh S3 observation predates its required lower bound")
    if max(
        last_modified_epoch,
        head_http.date_epoch,
        get_http.date_epoch,
    ) > effective_observation_epoch:
        raise ContractError("S3 delivery evidence timestamp exceeds observation")
    nonce = secrets.token_hex(32)
    return {
        "kind": "teamagent-exact-s3-export",
        "schema_version": 1,
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "s3": {
            "bucket": bucket,
            "key": key,
            "version_id": version_id,
            "last_modified_epoch": last_modified_epoch,
            "content_length": content_length,
            "etag": etag,
            "checksums": head_checksums,
            "head_request_id_sha256": sha256_bytes(head_http.request_id.encode()),
            "get_request_id_sha256": sha256_bytes(get_http.request_id.encode()),
            "head_aws_date_epoch": head_http.date_epoch,
            "get_aws_date_epoch": get_http.date_epoch,
        },
        "fresh_nonce": nonce,
        "fresh_nonce_sha256": sha256_bytes(nonce.encode()),
        "observed_at_epoch": effective_observation_epoch,
        "file": {
            "path": str(output_path),
            "acquisition_identity_before": asdict(before),
            "identity": asdict(after),
            "content_sha256": content_sha,
        },
    }


def verify_exact_s3_export(
    aws: AwsCli,
    *,
    binding: Mapping[str, Any],
    fresh_directory: Path,
) -> dict[str, Any]:
    require_keys(
        binding,
        (
            "kind",
            "schema_version",
            "account_id",
            "region",
            "s3",
            "fresh_nonce",
            "fresh_nonce_sha256",
            "observed_at_epoch",
            "file",
        ),
        "exact S3 export",
    )
    if (
        binding["kind"] != "teamagent-exact-s3-export"
        or binding["schema_version"] != 1
        or binding["account_id"] != ACCOUNT_ID
        or binding["region"] != REGION
    ):
        raise ContractError("exact S3 export identity is invalid")
    nonce = require_string(binding["fresh_nonce"], "fresh export nonce")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", nonce)
        or binding["fresh_nonce_sha256"] != sha256_bytes(nonce.encode())
    ):
        raise ContractError("fresh export nonce binding is invalid")
    observed_at = require_int(binding["observed_at_epoch"], "export observation")
    s3 = binding["s3"]
    file_binding = binding["file"]
    if not isinstance(s3, Mapping) or not isinstance(file_binding, Mapping):
        raise ContractError("exact S3/file binding is missing")
    require_keys(
        s3,
        (
            "bucket",
            "key",
            "version_id",
            "last_modified_epoch",
            "content_length",
            "etag",
            "checksums",
            "head_request_id_sha256",
            "get_request_id_sha256",
            "head_aws_date_epoch",
            "get_aws_date_epoch",
        ),
        "exact S3 metadata",
    )
    for timestamp_name in (
        "last_modified_epoch",
        "head_aws_date_epoch",
        "get_aws_date_epoch",
    ):
        if require_int(s3[timestamp_name], timestamp_name) > observed_at:
            raise ContractError("delivery timestamp exceeds observation timestamp")
    checksums = s3.get("checksums")
    if (
        not isinstance(checksums, Mapping)
        or not checksums
        or not set(checksums).issubset(
            {
                "ChecksumCRC32",
                "ChecksumCRC32C",
                "ChecksumCRC64NVME",
                "ChecksumSHA1",
                "ChecksumSHA256",
            }
        )
        or any(
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value)
            for value in checksums.values()
        )
        or not VERSION_ID.fullmatch(
            require_string(s3.get("version_id"), "S3 VersionId")
        )
        or not ETAG.fullmatch(require_string(s3.get("etag"), "S3 ETag"))
        or not HEX64.fullmatch(
            str(s3.get("head_request_id_sha256", ""))
        )
        or not HEX64.fullmatch(
            str(s3.get("get_request_id_sha256", ""))
        )
        or require_int(
            s3.get("content_length"), "S3 ContentLength", minimum=1
        )
        < 1
    ):
        raise ContractError("exact S3 object metadata is invalid")
    local_verification = verify_file_binding(file_binding)
    if "acquisition_identity_before" not in file_binding:
        raise ContractError("exact S3 export omits pre-download fstat identity")
    if local_verification["identity"]["size"] != s3["content_length"]:
        raise ContractError("local export size differs from S3 ContentLength")
    if not fresh_directory.is_absolute():
        raise ContractError("fresh verification directory must be absolute")
    canonical_directory = fresh_directory.resolve(strict=True)
    if canonical_directory != fresh_directory or fresh_directory.is_symlink():
        raise ContractError("fresh verification directory is not canonical")
    mode = stat.S_IMODE(fresh_directory.stat().st_mode)
    if mode != 0o700 or fresh_directory.stat().st_uid != os.getuid():
        raise ContractError("fresh verification directory must be owned mode 0700")
    fresh_path = fresh_directory / f"rehydrate-{secrets.token_hex(16)}.bin"
    fresh = fetch_exact_s3_export(
        aws,
        bucket=require_string(s3["bucket"], "S3 bucket"),
        key=require_string(s3["key"], "S3 key"),
        version_id=require_string(s3["version_id"], "S3 VersionId"),
        output_path=fresh_path,
        observation_epoch=observed_at,
    )
    try:
        if (
            fresh["file"]["content_sha256"] != file_binding["content_sha256"]
            or fresh["s3"]["content_length"] != s3["content_length"]
            or fresh["s3"]["etag"] != s3["etag"]
            or fresh["s3"]["checksums"] != s3["checksums"]
            or fresh["s3"]["last_modified_epoch"] != s3["last_modified_epoch"]
        ):
            raise ContractError("fresh exact-version download differs from evidence")
    finally:
        fresh_path.unlink(missing_ok=True)
    return {
        "verified": True,
        "bucket": s3["bucket"],
        "key": s3["key"],
        "version_id": s3["version_id"],
        "content_sha256": file_binding["content_sha256"],
        "local_identity": local_verification["identity"],
        "fresh_download_sha256": fresh["file"]["content_sha256"],
    }


def _local_file_binding(path: Path) -> dict[str, Any]:
    canonical = path.resolve(strict=True)
    if canonical != path or path.is_symlink():
        raise ContractError("local evidence path is not canonical")
    opened_path, fd = open_export_for_rehash(str(path))
    try:
        identity = FileIdentity.from_fd(opened_path, fd)
        digest = hash_open_fd(fd)
        after = FileIdentity.from_fd(opened_path, fd)
    finally:
        os.close(fd)
    if identity != after:
        raise ContractError("local evidence file changed while hashing")
    return {
        "path": str(path),
        "identity": asdict(identity),
        "content_sha256": digest,
    }


def _s3_request(value: Any, label: str) -> tuple[str, str, str]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label} S3 request is missing")
    require_keys(value, ("bucket", "key", "version_id"), f"{label} S3 request")
    return (
        require_string(value["bucket"], f"{label} bucket"),
        require_string(value["key"], f"{label} key"),
        require_string(value["version_id"], f"{label} version"),
    )


def build_log_readiness(
    aws: AwsCli,
    *,
    spec: Mapping[str, Any],
    versioning_receipt: Mapping[str, Any],
    versioning_receipt_sha256: str,
    export_directory: Path,
    retention_path: Path,
    evidence_path: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    """Fetch all delivery/retention evidence from exact immutable S3 versions."""

    require_keys(spec, ("cloudtrail", "bedrock", "retention"), "readiness spec")
    validate_versioning_workflow(versioning_receipt["workflow"])
    if (
        export_directory.resolve(strict=True) != export_directory
        or export_directory.is_symlink()
        or stat.S_IMODE(export_directory.stat().st_mode) != 0o700
        or export_directory.stat().st_uid != os.getuid()
    ):
        raise ContractError("readiness export directory must be canonical owned 0700")
    identity, observation_http = _caller_identity(aws)
    del identity
    initial_observed_at = observation_http.date_epoch
    cutover_epoch = max(
        versioning_receipt["workflow"]["cutover"]["cloudtrail"][
            "response_date_epoch"
        ],
        versioning_receipt["workflow"]["cutover"]["bedrock"][
            "response_date_epoch"
        ],
    )
    if initial_observed_at < cutover_epoch:
        raise ContractError("readiness observation predates producer cutover")

    cloudtrail_spec = spec["cloudtrail"]
    bedrock_spec = spec["bedrock"]
    retention_spec = spec["retention"]
    if not isinstance(cloudtrail_spec, Mapping) or not isinstance(
        bedrock_spec, Mapping
    ):
        raise ContractError("delivery spec is malformed")
    require_keys(
        cloudtrail_spec, ("latest_log", "latest_digest"), "CloudTrail delivery spec"
    )
    require_keys(bedrock_spec, ("latest_delivery",), "Bedrock delivery spec")
    if not isinstance(retention_spec, list):
        raise ContractError("retention export spec must be an array")

    expected_cloudtrail_bucket = f"teamagent-dev-cloudtrail-{ACCOUNT_ID}"
    expected_bedrock_bucket = f"teamagent-dev-bedrock-logs-{ACCOUNT_ID}"

    def fetch(
        label: str,
        request: Any,
        *,
        expected_bucket: str,
        prefix: str,
    ) -> dict[str, Any]:
        bucket, key, version = _s3_request(request, label)
        if bucket != expected_bucket or not key.startswith(prefix):
            raise ContractError(f"{label} bucket/key is outside the exact prefix")
        export_path = export_directory / f"{label}-{secrets.token_hex(16)}.export"
        return fetch_exact_s3_export(
            aws,
            bucket=bucket,
            key=key,
            version_id=version,
            output_path=export_path,
            observation_epoch=initial_observed_at,
        )

    latest_log = fetch(
        "cloudtrail-log",
        cloudtrail_spec["latest_log"],
        expected_bucket=expected_cloudtrail_bucket,
        prefix=f"AWSLogs/{ACCOUNT_ID}/CloudTrail/",
    )
    latest_digest = fetch(
        "cloudtrail-digest",
        cloudtrail_spec["latest_digest"],
        expected_bucket=expected_cloudtrail_bucket,
        prefix=f"AWSLogs/{ACCOUNT_ID}/CloudTrail-Digest/",
    )
    latest_bedrock = fetch(
        "bedrock-delivery",
        bedrock_spec["latest_delivery"],
        expected_bucket=expected_bedrock_bucket,
        prefix=f"bedrock/AWSLogs/{ACCOUNT_ID}/BedrockModelInvocationLogs/",
    )

    expected_groups = {
        "/aws/codebuild/teamagent-dev-aiia-image-builder",
        "/aws/codebuild/teamagent-dev-image-builder",
        "/aws/ecs/containerinsights/teamagent-dev/performance",
        "/aws/ecs/containerinsights/teamagent-dev-tiktok/performance",
        "/aws/lambda/teamagent-dev-reminders-notify",
        "/aws/lambda/teamagent-dev-tiktok-acquire-dispatch",
        "/aws/lambda/teamagent-dev-x-buzz-dispatch",
    }
    retention_rows: list[dict[str, Any]] = []
    observed_groups: set[str] = set()
    for index, row in enumerate(retention_spec):
        if not isinstance(row, Mapping):
            raise ContractError("retention export row is malformed")
        require_keys(
            row,
            ("log_group", "event_count", "exported_through_epoch", "s3"),
            "retention export row",
        )
        group = require_string(row["log_group"], "retention log group")
        if group not in expected_groups or group in observed_groups:
            raise ContractError("retention log group set is not exact/unique")
        observed_groups.add(group)
        event_count = require_int(row["event_count"], "retention event count", minimum=1)
        exported_through = require_int(
            row["exported_through_epoch"], "retention exported-through"
        )
        if exported_through > initial_observed_at:
            raise ContractError("retention delivery timestamp exceeds observation")
        export = fetch(
            f"retention-{index}",
            row["s3"],
            expected_bucket="teamagent-dev-raw-files",
            prefix="cloudwatch-logs-export/",
        )
        retention_rows.append(
            {
                "log_group": group,
                "event_count": event_count,
                "exported_through_epoch": exported_through,
                "export": export,
            }
        )
    if observed_groups != expected_groups:
        raise ContractError("retention export does not cover the exact log-group set")
    retention_rows.sort(key=lambda row: row["log_group"])
    bedrock_retention = verify_bedrock_retention_live(aws)
    _, final_observation_http = _caller_identity(aws)
    observed_at = final_observation_http.date_epoch
    export_observations = [
        latest_log["observed_at_epoch"],
        latest_digest["observed_at_epoch"],
        latest_bedrock["observed_at_epoch"],
        *[
            row["export"]["observed_at_epoch"]
            for row in retention_rows
        ],
        bedrock_retention["contract"]["observed_at_epoch"],
    ]
    if any(timestamp > observed_at for timestamp in export_observations):
        raise ContractError("delivery/retention evidence exceeds final observation")
    retention_manifest = {
        "kind": "teamagent-log-retention-export-manifest",
        "schema_version": 2,
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "created_at_epoch": observed_at,
        "log_groups": retention_rows,
    }
    _write_new_json(retention_path, retention_manifest)
    retention_binding = _local_file_binding(retention_path)

    evidence = {
        "kind": "teamagent-log-readiness-evidence",
        "schema_version": 2,
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "pre_cutover_observed_at_epoch": cutover_epoch,
        "observed_at_epoch": observed_at,
        "retention_export_manifest_path": str(retention_path),
        "retention_export_manifest_inode": str(
            retention_binding["identity"]["inode"]
        ),
        "retention_export_manifest_size_bytes": retention_binding["identity"]["size"],
        "retention_export_manifest_sha256": retention_binding["content_sha256"],
        "cloudtrail": {
            "bucket": expected_cloudtrail_bucket,
            "latest_log": latest_log,
            "latest_digest": latest_digest,
        },
        "bedrock": {
            "bucket": expected_bedrock_bucket,
            "latest_delivery": latest_bedrock,
            "retention_live": bedrock_retention,
        },
    }
    _write_new_json(evidence_path, evidence)
    evidence_binding = _local_file_binding(evidence_path)
    receipt = {
        "kind": "teamagent-log-rollout-readiness-receipt",
        "schema_version": 3,
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "versioning_receipt_sha256": versioning_receipt_sha256,
        "created_at_epoch": observed_at,
        "expires_at_epoch": observed_at + 86400,
        "evidence_artifact_path": str(evidence_path),
        "evidence_artifact_inode": str(evidence_binding["identity"]["inode"]),
        "evidence_artifact_size_bytes": evidence_binding["identity"]["size"],
        "evidence_artifact_sha256": evidence_binding["content_sha256"],
    }
    _write_new_json(receipt_path, receipt)
    return receipt


def _ack_claims(ack: Mapping[str, Any]) -> dict[str, Any]:
    claims = ack.get("claims")
    if not isinstance(claims, dict):
        raise ContractError("recipient ack claims are missing")
    require_keys(
        claims,
        (
            "kind",
            "schema_version",
            "topic_arn",
            "message_id",
            "challenge_nonce",
            "challenge_sha256",
            "inventory_sha256",
            "recipient_email",
            "received_at_epoch",
            "expires_at_epoch",
            "signer_principal_arn",
        ),
        "recipient ack claims",
    )
    if (
        claims["kind"] != "teamagent-sns-recipient-ack"
        or claims["schema_version"] != 1
        or claims["topic_arn"] != CANONICAL_TOPIC
        or not isinstance(claims["recipient_email"], str)
        or claims["recipient_email"].encode() != APPROVED_EMAIL.encode()
        or not HEX64.fullmatch(str(claims["challenge_sha256"]))
        or not HEX64.fullmatch(str(claims["inventory_sha256"]))
        or not str(claims["signer_principal_arn"]).startswith(
            ACK_SIGNER_ARN_PREFIX
        )
    ):
        raise ContractError("recipient ack identity/destination is not exact")
    require_int(claims["received_at_epoch"], "ack received_at_epoch")
    require_int(claims["expires_at_epoch"], "ack expires_at_epoch")
    return claims


def validate_sns_challenge(challenge: Mapping[str, Any]) -> None:
    require_keys(
        challenge,
        (
            "kind",
            "schema_version",
            "challenge_id",
            "ledger_record_id",
            "topic_arn",
            "message_id",
            "message_id_sha256",
            "challenge_nonce",
            "challenge_nonce_sha256",
            "published_at_epoch",
            "expires_at_epoch",
            "publish_request_id_sha256",
            "ledger_request_id_sha256",
            "ledger_response_sha256",
            "ledger_aws_date_epoch",
            "observed_at_epoch",
            "observation_request_id_sha256",
            "aws_executable",
            "endpoints",
            "caller_identity",
            "caller_identity_sha256",
            "caller_identity_observed_at_epoch",
            "inventory_contract",
            "inventory_sha256",
            "destination_state_sha256",
            "subscription_metadata_sha256",
            "raw_reference_set_sha256",
            "ack_kms_key_arn",
            "ack_kms_key_metadata",
            "ack_kms_key_metadata_sha256",
            "ack_kms_key_request_id_sha256",
            "challenge_sha256",
        ),
        "SNS challenge",
    )
    challenge_id = require_string(challenge["challenge_id"], "challenge ID")
    message_id = require_string(challenge["message_id"], "SNS MessageId")
    nonce = require_string(challenge["challenge_nonce"], "challenge nonce")
    expected_hash = require_string(
        challenge["challenge_sha256"], "challenge SHA-256"
    )
    unhashed = dict(challenge)
    del unhashed["challenge_sha256"]
    if (
        challenge["kind"] != "teamagent-sns-delivery-challenge"
        or challenge["schema_version"] != 1
        or challenge["topic_arn"] != CANONICAL_TOPIC
        or not UUID4.fullmatch(challenge_id)
        or challenge["ledger_record_id"] != f"sns-challenge#{challenge_id}"
        or not re.fullmatch(r"[0-9a-fA-F-]{36}", message_id)
        or challenge["message_id_sha256"]
        != sha256_bytes(message_id.encode())
        or not HEX64.fullmatch(nonce)
        or challenge["challenge_nonce_sha256"]
        != sha256_bytes(nonce.encode())
        or not HEX64.fullmatch(expected_hash)
        or canonical_sha256(unhashed) != expected_hash
    ):
        raise ContractError("SNS challenge identity/hash binding is invalid")
    executable = challenge.get("aws_executable")
    caller_identity = challenge.get("caller_identity")
    if (
        not isinstance(executable, Mapping)
        or set(executable)
        != {"path", "device", "inode", "size", "sha256", "version"}
        or not str(executable.get("path", "")).startswith("/")
        or not HEX64.fullmatch(str(executable.get("sha256", "")))
        or not str(executable.get("version", "")).startswith("aws-cli/2.")
        or challenge.get("endpoints") != ENDPOINTS
        or not isinstance(caller_identity, Mapping)
        or set(caller_identity) != {"UserId", "Account", "Arn"}
        or caller_identity.get("Account") != ACCOUNT_ID
        or caller_identity.get("Arn") != AUTOMATION_ARN
        or challenge.get("caller_identity_sha256")
        != canonical_sha256(caller_identity)
    ):
        raise ContractError("SNS challenge executable/caller trust is invalid")
    inventory_contract = challenge.get("inventory_contract")
    if not isinstance(inventory_contract, Mapping):
        raise ContractError("SNS challenge inventory contract is missing")
    validate_inventory_contract(inventory_contract)
    if (
        challenge.get("inventory_sha256")
        != canonical_sha256(inventory_contract)
        or challenge.get("destination_state_sha256")
        != canonical_sha256(inventory_contract["destination"])
        or challenge.get("subscription_metadata_sha256")
        != canonical_sha256(inventory_contract["subscription_metadata"])
        or challenge.get("raw_reference_set_sha256")
        != canonical_sha256(inventory_contract["references"])
    ):
        raise ContractError("SNS challenge uses an arbitrary inventory hash")
    for field in (
        "publish_request_id_sha256",
        "ledger_request_id_sha256",
        "ledger_response_sha256",
        "observation_request_id_sha256",
        "inventory_sha256",
        "destination_state_sha256",
        "subscription_metadata_sha256",
        "raw_reference_set_sha256",
        "ack_kms_key_metadata_sha256",
        "ack_kms_key_request_id_sha256",
    ):
        if not HEX64.fullmatch(str(challenge[field])):
            raise ContractError(f"SNS challenge {field} is invalid")
    if not re.fullmatch(
        rf"arn:aws:kms:{re.escape(REGION)}:{ACCOUNT_ID}:key/"
        r"[0-9a-fA-F-]{36}",
        str(challenge["ack_kms_key_arn"]),
    ):
        raise ContractError("SNS challenge KMS key ARN is invalid")
    key_metadata = challenge.get("ack_kms_key_metadata")
    if not isinstance(key_metadata, Mapping):
        raise ContractError("SNS challenge KMS key metadata is missing")
    if (
        validate_ack_key_metadata(key_metadata)
        != challenge.get("ack_kms_key_arn")
        or canonical_sha256(key_metadata)
        != challenge.get("ack_kms_key_metadata_sha256")
    ):
        raise ContractError("SNS challenge KMS key metadata binding is invalid")
    published_at = require_int(
        challenge["published_at_epoch"], "challenge publication time"
    )
    ledger_at = require_int(
        challenge["ledger_aws_date_epoch"], "challenge ledger time"
    )
    observed_at = require_int(
        challenge["observed_at_epoch"], "challenge observation time"
    )
    caller_observed_at = require_int(
        challenge["caller_identity_observed_at_epoch"],
        "challenge caller observation time",
    )
    expires_at = require_int(
        challenge["expires_at_epoch"], "challenge expiry"
    )
    if not (
        caller_observed_at <= published_at <= ledger_at <= observed_at
        and published_at < expires_at <= published_at + 3600
    ):
        raise ContractError("SNS challenge lifetime is invalid")
    source_pages = inventory_contract["source_pages"]
    if any(
        require_int(source["aws_date_epoch"], "inventory page AWS Date")
        > published_at
        for source in source_pages
    ):
        raise ContractError("SNS destination inventory was observed after publication")


def verify_recipient_ack(
    aws: AwsCli,
    *,
    challenge: Mapping[str, Any],
    ack: Mapping[str, Any],
    now_epoch: int,
) -> dict[str, Any]:
    validate_sns_challenge(challenge)
    require_keys(
        ack,
        (
            "kind",
            "schema_version",
            "claims",
            "claims_sha256",
            "kms_key_arn",
            "signature_base64",
            "sign_request_id_sha256",
            "signed_at_epoch",
        ),
        "recipient signed acknowledgement",
    )
    claims = _ack_claims(ack)
    signer_suffix = str(claims["signer_principal_arn"])[
        len(ACK_SIGNER_ARN_PREFIX) :
    ]
    if (
        ack["kind"] != "teamagent-sns-recipient-signed-ack"
        or ack["schema_version"] != 1
        or ack["claims_sha256"] != canonical_sha256(claims)
        or not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{2,64}", signer_suffix)
        or not HEX64.fullmatch(str(ack["sign_request_id_sha256"]))
    ):
        raise ContractError("recipient signed acknowledgement binding is invalid")
    if (
        claims["message_id"] != challenge["message_id"]
        or claims["challenge_nonce"] != challenge["challenge_nonce"]
        or claims["challenge_sha256"] != challenge["challenge_sha256"]
        or claims["inventory_sha256"] != challenge["inventory_sha256"]
    ):
        raise ContractError("recipient ack belongs to another SNS challenge")
    published_at = require_int(
        challenge["published_at_epoch"], "challenge published_at_epoch"
    )
    challenge_expires = require_int(
        challenge["expires_at_epoch"], "challenge expires_at_epoch"
    )
    received_at = require_int(claims["received_at_epoch"], "ack received_at_epoch")
    ack_expires = require_int(claims["expires_at_epoch"], "ack expires_at_epoch")
    signed_at = require_int(ack["signed_at_epoch"], "ack signed_at_epoch")
    if not (
        published_at <= received_at <= now_epoch
        and received_at <= signed_at <= now_epoch
        and now_epoch < min(challenge_expires, ack_expires)
        and challenge_expires - published_at <= 3600
        and ack_expires - received_at <= 3600
    ):
        raise ContractError("recipient ack is absent, stale, future, or expired")
    if challenge.get("aws_executable") != asdict(aws.evidence):
        raise ContractError("AWS executable differs from the signed SNS challenge")
    key_arn = require_string(ack.get("kms_key_arn"), "ack KMS key ARN")
    if not re.fullmatch(
        rf"arn:aws:kms:{re.escape(REGION)}:{ACCOUNT_ID}:key/"
        r"[0-9a-fA-F-]{36}",
        key_arn,
    ):
        raise ContractError("ack KMS key ARN is invalid")
    (
        live_key_arn,
        live_key_metadata,
        live_key_metadata_sha256,
        key_http,
    ) = _validated_ack_key(aws)
    if (
        key_arn != challenge.get("ack_kms_key_arn")
        or live_key_arn != key_arn
        or live_key_metadata != challenge.get("ack_kms_key_metadata")
        or live_key_metadata_sha256
        != challenge.get("ack_kms_key_metadata_sha256")
        or key_http.date_epoch > now_epoch
    ):
        raise ContractError("ack KMS key differs from the published challenge")
    signature = require_string(ack.get("signature_base64"), "ack signature")
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
    except ValueError as exc:
        raise ContractError("ack signature is not canonical base64") from exc
    with tempfile.TemporaryDirectory(prefix="teamagent-sns-ack.") as directory:
        root = Path(directory)
        message_path = root / "claims.json"
        signature_path = root / "signature.bin"
        message_path.write_bytes(canonical_bytes(claims))
        signature_path.write_bytes(signature_bytes)
        os.chmod(message_path, 0o600)
        os.chmod(signature_path, 0o600)
        response, http = aws.call(
            "kms",
            "verify",
            (
                "--key-id",
                ACK_KEY_ALIAS,
                "--message",
                f"fileb://{message_path}",
                "--message-type",
                "RAW",
                "--signature",
                f"fileb://{signature_path}",
                "--signing-algorithm",
                "ECDSA_SHA_256",
            ),
        )
    if response.get("SignatureValid") is not True or response.get("KeyId") != key_arn:
        raise ContractError("managed KMS recipient signature is invalid")
    return {
        "claims_sha256": canonical_sha256(claims),
        "signature_sha256": sha256_bytes(signature_bytes),
        "kms_key_arn": key_arn,
        "kms_verify_request_id_sha256": sha256_bytes(http.request_id.encode()),
        "verified_at_epoch": http.date_epoch,
    }


def _dynamodb_value(value: str | int) -> dict[str, str]:
    if isinstance(value, int):
        return {"N": str(value)}
    return {"S": value}


def validate_ack_key_metadata(metadata: Mapping[str, Any]) -> str:
    key_arn = require_string(metadata.get("Arn"), "recipient KMS key ARN")
    if (
        metadata.get("AWSAccountId") != ACCOUNT_ID
        or not re.fullmatch(
            rf"arn:aws:kms:{re.escape(REGION)}:{ACCOUNT_ID}:key/"
            r"[0-9a-fA-F-]{36}",
            key_arn,
        )
        or metadata.get("KeyUsage") != "SIGN_VERIFY"
        or metadata.get("KeySpec") != "ECC_NIST_P256"
        or metadata.get("KeyState") != "Enabled"
        or metadata.get("Enabled") is not True
        or metadata.get("KeyManager") != "CUSTOMER"
        or metadata.get("Origin") != "AWS_KMS"
        or metadata.get("MultiRegion", False) is not False
        or metadata.get("SigningAlgorithms") != ["ECDSA_SHA_256"]
    ):
        raise ContractError("recipient KMS key is not the exact signing contract")
    return key_arn


def _validated_ack_key(
    aws: AwsCli,
) -> tuple[str, dict[str, Any], str, HttpEvidence]:
    response, http = aws.call(
        "kms", "describe-key", ("--key-id", ACK_KEY_ALIAS)
    )
    metadata = response.get("KeyMetadata")
    if not isinstance(metadata, dict):
        raise ContractError("recipient KMS key metadata is missing")
    key_arn = validate_ack_key_metadata(metadata)
    return key_arn, metadata, canonical_sha256(metadata), http


def issue_sns_challenge(aws: AwsCli) -> dict[str, Any]:
    """Publish one unpredictable challenge and reserve its one-use ledger row."""

    initial_identity, initial_identity_http = _caller_identity(aws)
    inventory = collect_inventory(aws)
    (
        ack_key_arn,
        ack_key_metadata,
        ack_key_metadata_sha256,
        ack_key_http,
    ) = _validated_ack_key(aws)
    nonce = secrets.token_hex(32)
    challenge_id = str(uuid.uuid4())
    message = {
        "kind": "teamagent-sns-delivery-challenge",
        "schema_version": 1,
        "challenge_id": challenge_id,
        "nonce": nonce,
        "recipient": APPROVED_EMAIL,
    }
    response, publish_http = aws.call(
        "sns",
        "publish",
        (
            "--topic-arn",
            CANONICAL_TOPIC,
            "--subject",
            "TeamAgent delivery verification",
            "--message",
            canonical_bytes(message).decode().rstrip("\n"),
        ),
    )
    message_id = require_string(response.get("MessageId"), "SNS MessageId")
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", message_id):
        raise ContractError("SNS MessageId has an invalid form")
    expires_at = publish_http.date_epoch + 3600
    record_id = f"sns-challenge#{challenge_id}"
    ledger_item = {
        "record_id": _dynamodb_value(record_id),
        "status": _dynamodb_value("PUBLISHED"),
        "topic_arn": _dynamodb_value(CANONICAL_TOPIC),
        "message_id": _dynamodb_value(message_id),
        "nonce_sha256": _dynamodb_value(sha256_bytes(nonce.encode())),
        "inventory_sha256": _dynamodb_value(inventory["inventory_sha256"]),
        "published_at_epoch": _dynamodb_value(publish_http.date_epoch),
        "expires_at_epoch": _dynamodb_value(expires_at),
        "audit_expires_at": _dynamodb_value(expires_at + 31536000),
    }
    ledger_response, ledger_http = aws.call(
        "dynamodb",
        "put-item",
        (
            "--table-name",
            ALARM_LEDGER_TABLE,
            "--item",
            json.dumps(ledger_item, separators=(",", ":")),
            "--condition-expression",
            "attribute_not_exists(record_id)",
        ),
    )
    final_identity, final_http = _caller_identity(aws)
    del final_identity
    inventory_times = [
        source["aws_date_epoch"] for source in inventory["source_pages"]
    ]
    if (
        max(
            [
                initial_identity_http.date_epoch,
                ack_key_http.date_epoch,
                *inventory_times,
            ]
        )
        > publish_http.date_epoch
        or publish_http.date_epoch > ledger_http.date_epoch
        or ledger_http.date_epoch > final_http.date_epoch
    ):
        raise ContractError("SNS challenge evidence exceeds final observation")
    challenge = {
        "kind": "teamagent-sns-delivery-challenge",
        "schema_version": 1,
        "challenge_id": challenge_id,
        "ledger_record_id": record_id,
        "topic_arn": CANONICAL_TOPIC,
        "message_id": message_id,
        "message_id_sha256": sha256_bytes(message_id.encode()),
        "challenge_nonce": nonce,
        "challenge_nonce_sha256": sha256_bytes(nonce.encode()),
        "published_at_epoch": publish_http.date_epoch,
        "expires_at_epoch": expires_at,
        "publish_request_id_sha256": sha256_bytes(
            publish_http.request_id.encode()
        ),
        "ledger_request_id_sha256": sha256_bytes(ledger_http.request_id.encode()),
        "ledger_response_sha256": canonical_sha256(ledger_response),
        "ledger_aws_date_epoch": ledger_http.date_epoch,
        "observed_at_epoch": final_http.date_epoch,
        "observation_request_id_sha256": sha256_bytes(
            final_http.request_id.encode()
        ),
        "aws_executable": asdict(aws.evidence),
        "endpoints": ENDPOINTS,
        "caller_identity": initial_identity,
        "caller_identity_sha256": canonical_sha256(initial_identity),
        "caller_identity_observed_at_epoch": initial_identity_http.date_epoch,
        "inventory_contract": inventory["inventory_contract"],
        "inventory_sha256": inventory["inventory_sha256"],
        "destination_state_sha256": inventory["destination_state_sha256"],
        "subscription_metadata_sha256": inventory[
            "subscription_metadata_sha256"
        ],
        "raw_reference_set_sha256": inventory["raw_reference_set_sha256"],
        "ack_kms_key_arn": ack_key_arn,
        "ack_kms_key_metadata": ack_key_metadata,
        "ack_kms_key_metadata_sha256": ack_key_metadata_sha256,
        "ack_kms_key_request_id_sha256": sha256_bytes(
            ack_key_http.request_id.encode()
        ),
    }
    challenge["challenge_sha256"] = canonical_sha256(challenge)
    return challenge


def sign_recipient_ack(
    aws: AwsCli,
    *,
    challenge: Mapping[str, Any],
) -> dict[str, Any]:
    """Record the recipient's explicit acknowledgement using managed KMS."""

    validate_sns_challenge(challenge)
    identity, identity_http = aws.call("sts", "get-caller-identity")
    signer_arn = require_string(identity.get("Arn"), "ack signer ARN")
    if identity.get("Account") != ACCOUNT_ID or not signer_arn.startswith(
        ACK_SIGNER_ARN_PREFIX
    ):
        raise ContractError("ack signing requires the exact managed recipient role")
    now = identity_http.date_epoch
    if now >= require_int(challenge.get("expires_at_epoch"), "challenge expiry"):
        raise ContractError("SNS challenge expired before recipient acknowledgement")
    claims = {
        "kind": "teamagent-sns-recipient-ack",
        "schema_version": 1,
        "topic_arn": CANONICAL_TOPIC,
        "message_id": require_string(challenge.get("message_id"), "SNS MessageId"),
        "challenge_nonce": require_string(
            challenge.get("challenge_nonce"), "challenge nonce"
        ),
        "challenge_sha256": require_string(
            challenge.get("challenge_sha256"), "challenge SHA-256"
        ),
        "inventory_sha256": require_string(
            challenge.get("inventory_sha256"), "challenge inventory SHA-256"
        ),
        "recipient_email": APPROVED_EMAIL,
        "received_at_epoch": now,
        "expires_at_epoch": min(
            now + 3600,
            require_int(challenge.get("expires_at_epoch"), "challenge expiry"),
        ),
        "signer_principal_arn": signer_arn,
    }
    key_arn, key_metadata, key_metadata_sha256, key_http = _validated_ack_key(
        aws
    )
    if (
        key_arn != challenge.get("ack_kms_key_arn")
        or key_metadata != challenge.get("ack_kms_key_metadata")
        or key_metadata_sha256
        != challenge.get("ack_kms_key_metadata_sha256")
        or key_http.date_epoch < identity_http.date_epoch
    ):
        raise ContractError("recipient KMS key changed after challenge publication")
    with tempfile.TemporaryDirectory(prefix="teamagent-sns-sign.") as directory:
        claims_path = Path(directory) / "claims.json"
        claims_path.write_bytes(canonical_bytes(claims))
        os.chmod(claims_path, 0o600)
        signed, sign_http = aws.call(
            "kms",
            "sign",
            (
                "--key-id",
                ACK_KEY_ALIAS,
                "--message",
                f"fileb://{claims_path}",
                "--message-type",
                "RAW",
                "--signing-algorithm",
                "ECDSA_SHA_256",
            ),
        )
    if signed.get("KeyId") != key_arn:
        raise ContractError("KMS signed with an unexpected key")
    if sign_http.date_epoch < now:
        raise ContractError("KMS signing response predates recipient observation")
    signature = require_string(signed.get("Signature"), "KMS signature")
    try:
        base64.b64decode(signature, validate=True)
    except ValueError as exc:
        raise ContractError("KMS signature is not canonical base64") from exc
    return {
        "kind": "teamagent-sns-recipient-signed-ack",
        "schema_version": 1,
        "claims": claims,
        "claims_sha256": canonical_sha256(claims),
        "kms_key_arn": key_arn,
        "signature_base64": signature,
        "sign_request_id_sha256": sha256_bytes(sign_http.request_id.encode()),
        "signed_at_epoch": sign_http.date_epoch,
    }


def attest_sns_delivery(
    aws: AwsCli,
    *,
    challenge: Mapping[str, Any],
    ack: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify the recipient's managed-KMS signature and burn the challenge."""

    validate_sns_challenge(challenge)
    now_identity, now_http = _caller_identity(aws)
    verification = verify_recipient_ack(
        aws,
        challenge=challenge,
        ack=ack,
        now_epoch=now_http.date_epoch,
    )
    inventory = collect_inventory(aws)
    if (
        inventory["inventory_sha256"] != challenge.get("inventory_sha256")
        or inventory["destination_state_sha256"]
        != challenge.get("destination_state_sha256")
        or inventory["subscription_metadata_sha256"]
        != challenge.get("subscription_metadata_sha256")
        or inventory["raw_reference_set_sha256"]
        != challenge.get("raw_reference_set_sha256")
    ):
        raise ContractError("SNS inventory changed after challenge publication")
    observation_response, observation_http = aws.call("sts", "get-caller-identity")
    if observation_response != now_identity:
        raise ContractError("caller identity changed during SNS attestation")
    claims = _ack_claims(ack)
    delivery_times = (
        require_int(challenge.get("published_at_epoch"), "published_at_epoch"),
        require_int(claims["received_at_epoch"], "received_at_epoch"),
        require_int(verification["verified_at_epoch"], "verified_at_epoch"),
    )
    if any(timestamp > observation_http.date_epoch for timestamp in delivery_times):
        raise ContractError("delivery evidence timestamp exceeds observation time")

    receipt_base = {
        "kind": "teamagent-alarm-delivery-test-receipt",
        "schema_version": 4,
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "topic_arn": CANONICAL_TOPIC,
        "raw_email": APPROVED_EMAIL,
        "raw_email_utf8_sha256": sha256_bytes(APPROVED_EMAIL.encode()),
        "challenge_id": challenge["challenge_id"],
        "challenge_sha256": challenge["challenge_sha256"],
        "message_id": challenge["message_id"],
        "message_id_sha256": challenge["message_id_sha256"],
        "challenge_nonce_sha256": challenge["challenge_nonce_sha256"],
        "published_at_epoch": challenge["published_at_epoch"],
        "received_at_epoch": claims["received_at_epoch"],
        "verified_at_epoch": verification["verified_at_epoch"],
        "observed_at_epoch": observation_http.date_epoch,
        "expires_at_epoch": min(
            require_int(challenge["expires_at_epoch"], "challenge expiry"),
            require_int(claims["expires_at_epoch"], "ack expiry"),
        ),
        "inventory_sha256": inventory["inventory_sha256"],
        "destination_state_sha256": inventory["destination_state_sha256"],
        "subscription_metadata_sha256": inventory[
            "subscription_metadata_sha256"
        ],
        "raw_reference_set_sha256": inventory["raw_reference_set_sha256"],
        "recipient_ack_claims_sha256": verification["claims_sha256"],
        "recipient_ack_signature_sha256": verification["signature_sha256"],
        "recipient_ack_kms_key_arn": verification["kms_key_arn"],
        "recipient_ack_signer_principal_arn": claims["signer_principal_arn"],
        "observation_request_id_sha256": sha256_bytes(
            observation_http.request_id.encode()
        ),
        "ledger_record_id": challenge["ledger_record_id"],
        "challenge": dict(challenge),
        "recipient_ack": dict(ack),
    }
    receipt_sha = canonical_sha256(receipt_base)
    key = {"record_id": _dynamodb_value(str(challenge["ledger_record_id"]))}
    values = {
        ":published": _dynamodb_value("PUBLISHED"),
        ":acknowledged": _dynamodb_value("ACKNOWLEDGED"),
        ":message": _dynamodb_value(str(challenge["message_id"])),
        ":nonce": _dynamodb_value(str(challenge["challenge_nonce_sha256"])),
        ":inventory": _dynamodb_value(str(challenge["inventory_sha256"])),
        ":receipt": _dynamodb_value(receipt_sha),
        ":now": _dynamodb_value(observation_http.date_epoch),
    }
    ledger_response, ledger_http = aws.call(
        "dynamodb",
        "update-item",
        (
            "--table-name",
            ALARM_LEDGER_TABLE,
            "--key",
            json.dumps(key, separators=(",", ":")),
            "--update-expression",
            "SET #status = :acknowledged, receipt_sha256 = :receipt",
            "--condition-expression",
            (
                "#status = :published AND message_id = :message AND "
                "nonce_sha256 = :nonce AND inventory_sha256 = :inventory AND "
                "expires_at_epoch > :now AND "
                "attribute_not_exists(receipt_sha256)"
            ),
            "--expression-attribute-names",
            '{"#status":"status"}',
            "--expression-attribute-values",
            json.dumps(values, separators=(",", ":")),
            "--return-values",
            "ALL_NEW",
        ),
    )
    receipt_base["ledger_ack_request_id_sha256"] = sha256_bytes(
        ledger_http.request_id.encode()
    )
    receipt_base["ledger_ack_response_sha256"] = canonical_sha256(ledger_response)
    final_identity, final_http = _caller_identity(aws)
    if final_identity != now_identity:
        raise ContractError("caller identity changed after SNS ledger acknowledgement")
    if max(
        observation_http.date_epoch,
        ledger_http.date_epoch,
    ) > final_http.date_epoch:
        raise ContractError("SNS acknowledgement evidence exceeds final observation")
    receipt_base["ledger_ack_aws_date_epoch"] = ledger_http.date_epoch
    receipt_base["final_observed_at_epoch"] = final_http.date_epoch
    receipt_base["final_observation_request_id_sha256"] = sha256_bytes(
        final_http.request_id.encode()
    )
    receipt_base["receipt_claims_sha256"] = receipt_sha
    return receipt_base


def verify_sns_delivery_receipt(
    aws: AwsCli,
    *,
    challenge: Mapping[str, Any],
    ack: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Reverify immutable ack bytes, exact destination, expiry, and ledger state."""

    require_keys(
        receipt,
        (
            "kind",
            "schema_version",
            "account_id",
            "region",
            "topic_arn",
            "raw_email",
            "raw_email_utf8_sha256",
            "challenge_id",
            "challenge_sha256",
            "message_id",
            "message_id_sha256",
            "challenge_nonce_sha256",
            "published_at_epoch",
            "received_at_epoch",
            "verified_at_epoch",
            "observed_at_epoch",
            "expires_at_epoch",
            "inventory_sha256",
            "destination_state_sha256",
            "subscription_metadata_sha256",
            "raw_reference_set_sha256",
            "recipient_ack_claims_sha256",
            "recipient_ack_signature_sha256",
            "recipient_ack_kms_key_arn",
            "recipient_ack_signer_principal_arn",
            "observation_request_id_sha256",
            "ledger_record_id",
            "challenge",
            "recipient_ack",
            "ledger_ack_request_id_sha256",
            "ledger_ack_response_sha256",
            "ledger_ack_aws_date_epoch",
            "final_observed_at_epoch",
            "final_observation_request_id_sha256",
            "receipt_claims_sha256",
        ),
        "SNS delivery receipt",
    )
    validate_sns_challenge(challenge)
    receipt_claims = dict(receipt)
    for field in (
        "ledger_ack_request_id_sha256",
        "ledger_ack_response_sha256",
        "ledger_ack_aws_date_epoch",
        "final_observed_at_epoch",
        "final_observation_request_id_sha256",
        "receipt_claims_sha256",
    ):
        del receipt_claims[field]
    if (
        receipt["receipt_claims_sha256"]
        != canonical_sha256(receipt_claims)
        or receipt["challenge"] != challenge
        or receipt["recipient_ack"] != ack
    ):
        raise ContractError("SNS delivery receipt immutable claims differ")
    for field in (
        "observation_request_id_sha256",
        "ledger_ack_request_id_sha256",
        "ledger_ack_response_sha256",
        "final_observation_request_id_sha256",
        "receipt_claims_sha256",
    ):
        if not HEX64.fullmatch(str(receipt[field])):
            raise ContractError(f"SNS delivery receipt {field} is invalid")
    now_identity, now_http = _caller_identity(aws)
    del now_identity
    verification = verify_recipient_ack(
        aws,
        challenge=challenge,
        ack=ack,
        now_epoch=now_http.date_epoch,
    )
    inventory = collect_inventory(aws)
    claims = _ack_claims(ack)
    if (
        receipt.get("kind") != "teamagent-alarm-delivery-test-receipt"
        or
        receipt.get("schema_version") != 4
        or receipt.get("account_id") != ACCOUNT_ID
        or receipt.get("region") != REGION
        or receipt.get("topic_arn") != CANONICAL_TOPIC
        or receipt.get("raw_email") != APPROVED_EMAIL
        or not isinstance(receipt.get("raw_email"), str)
        or str(receipt["raw_email"]).encode() != APPROVED_EMAIL.encode()
        or receipt.get("message_id") != challenge.get("message_id")
        or receipt.get("message_id_sha256")
        != challenge.get("message_id_sha256")
        or receipt.get("challenge_id") != challenge.get("challenge_id")
        or receipt.get("challenge_sha256") != challenge.get("challenge_sha256")
        or receipt.get("challenge_nonce_sha256")
        != challenge.get("challenge_nonce_sha256")
        or receipt.get("ledger_record_id")
        != challenge.get("ledger_record_id")
        or receipt.get("published_at_epoch")
        != challenge.get("published_at_epoch")
        or receipt.get("received_at_epoch")
        != claims.get("received_at_epoch")
        or receipt.get("inventory_sha256") != inventory["inventory_sha256"]
        or receipt.get("destination_state_sha256")
        != inventory["destination_state_sha256"]
        or receipt.get("subscription_metadata_sha256")
        != inventory["subscription_metadata_sha256"]
        or receipt.get("raw_reference_set_sha256")
        != inventory["raw_reference_set_sha256"]
        or receipt.get("recipient_ack_claims_sha256")
        != verification["claims_sha256"]
        or receipt.get("recipient_ack_signature_sha256")
        != verification["signature_sha256"]
        or receipt.get("recipient_ack_kms_key_arn")
        != verification["kms_key_arn"]
        or receipt.get("recipient_ack_signer_principal_arn")
        != claims.get("signer_principal_arn")
    ):
        raise ContractError("SNS delivery receipt binding is not exact")
    expires_at = require_int(receipt.get("expires_at_epoch"), "receipt expiry")
    observed_at = require_int(receipt.get("observed_at_epoch"), "receipt observation")
    ledger_ack_at = require_int(
        receipt.get("ledger_ack_aws_date_epoch"),
        "ledger acknowledgement time",
    )
    final_observed_at = require_int(
        receipt.get("final_observed_at_epoch"),
        "final receipt observation",
    )
    for field in (
        "published_at_epoch",
        "received_at_epoch",
        "verified_at_epoch",
    ):
        if require_int(receipt.get(field), field) > observed_at:
            raise ContractError("delivery evidence timestamp exceeds observation")
    if not (
        receipt["published_at_epoch"]
        <= receipt["received_at_epoch"]
        <= receipt["verified_at_epoch"]
        <= observed_at
        <= ledger_ack_at
        <= final_observed_at
        < expires_at
        and expires_at
        == min(challenge["expires_at_epoch"], claims["expires_at_epoch"])
        and now_http.date_epoch < expires_at
        and final_observed_at <= now_http.date_epoch
    ):
        raise ContractError("SNS delivery receipt is expired or future-dated")
    key = {"record_id": _dynamodb_value(str(receipt["ledger_record_id"]))}
    ledger, ledger_http = aws.call(
        "dynamodb",
        "get-item",
        (
            "--table-name",
            ALARM_LEDGER_TABLE,
            "--key",
            json.dumps(key, separators=(",", ":")),
            "--consistent-read",
        ),
    )
    item = ledger.get("Item")
    if not isinstance(item, Mapping):
        raise ContractError("SNS challenge ledger item is absent")
    if (
        item.get("status") != _dynamodb_value("ACKNOWLEDGED")
        or item.get("message_id")
        != _dynamodb_value(str(receipt["message_id"]))
        or item.get("topic_arn") != _dynamodb_value(CANONICAL_TOPIC)
        or item.get("nonce_sha256")
        != _dynamodb_value(str(receipt["challenge_nonce_sha256"]))
        or item.get("inventory_sha256")
        != _dynamodb_value(str(receipt["inventory_sha256"]))
        or item.get("expires_at_epoch")
        != _dynamodb_value(
            require_int(
                receipt["challenge"]["expires_at_epoch"],
                "challenge expiry",
            )
        )
        or item.get("receipt_sha256")
        != _dynamodb_value(str(receipt["receipt_claims_sha256"]))
    ):
        raise ContractError("SNS challenge was reused, replaced, or not acknowledged")
    return {
        "verified": True,
        "inventory_sha256": inventory["inventory_sha256"],
        "ledger_request_id_sha256": sha256_bytes(ledger_http.request_id.encode()),
        "verified_at_epoch": now_http.date_epoch,
    }


ALARM_PHASES = (
    "dual_publish",
    "publisher_checkpoint",
    "canonical_delivery_confirmed",
    "legacy_reference_zero",
    "legacy_retired",
)


def validate_alarm_migration_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None,
) -> None:
    require_keys(
        checkpoint,
        (
            "kind",
            "schema_version",
            "migration_id",
            "sequence",
            "phase",
            "publisher_id",
            "idempotency_key",
            "delivery_receipt_sha256",
            "inventory_sha256",
            "postcondition",
            "postcondition_receipt_sha256",
            "rollback_plan",
            "previous_checkpoint_sha256",
            "created_at_epoch",
        ),
        "alarm migration checkpoint",
    )
    if (
        checkpoint["kind"] != "teamagent-alarm-migration-checkpoint"
        or checkpoint["schema_version"] != 1
        or checkpoint["phase"] not in ALARM_PHASES
    ):
        raise ContractError("alarm migration checkpoint kind/phase is invalid")
    require_int(checkpoint["sequence"], "checkpoint sequence", minimum=1)
    require_int(
        checkpoint["created_at_epoch"],
        "checkpoint creation time",
        minimum=1,
    )
    if (
        not isinstance(checkpoint["migration_id"], str)
        or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[A-Za-z0-9_.-]{1,96}",
            checkpoint["migration_id"],
        )
        or not isinstance(checkpoint["publisher_id"], str)
    ):
        raise ContractError("alarm migration checkpoint identity is invalid")
    for field in ("idempotency_key", "inventory_sha256", "postcondition_receipt_sha256"):
        value = checkpoint[field]
        if not isinstance(value, str) or not HEX64.fullmatch(value):
            raise ContractError(f"checkpoint {field} is invalid")
    delivery_receipt_sha256 = checkpoint["delivery_receipt_sha256"]
    if (
        checkpoint["phase"] == "canonical_delivery_confirmed"
        and (
            not isinstance(delivery_receipt_sha256, str)
            or not HEX64.fullmatch(delivery_receipt_sha256)
        )
    ) or (
        checkpoint["phase"] != "canonical_delivery_confirmed"
        and delivery_receipt_sha256 != ""
    ):
        raise ContractError("checkpoint delivery receipt hash is in the wrong phase")
    if not isinstance(checkpoint["rollback_plan"], Mapping):
        raise ContractError("checkpoint rollback plan is missing")
    if (
        not isinstance(checkpoint["postcondition"], Mapping)
        or checkpoint["postcondition_receipt_sha256"]
        != canonical_sha256(checkpoint["postcondition"])
        or checkpoint["inventory_sha256"]
        != checkpoint["postcondition"].get("inventory_sha256")
    ):
        raise ContractError("checkpoint postcondition hash binding is invalid")
    postcondition = checkpoint["postcondition"]
    require_keys(
        postcondition,
        (
            "phase",
            "publisher_id",
            "inventory_sha256",
            "publisher_reference_set_sha256",
            "publishers_sha256",
            "publisher_ids",
            "publisher_topic_state",
            "legacy_publisher_ids",
            "canonical_publisher_ids",
            "legacy_publisher_count",
            "canonical_publisher_count",
            "delivery_verification",
        ),
        "alarm migration postcondition",
    )
    publisher_topic_state = postcondition.get("publisher_topic_state")
    publisher_ids = postcondition.get("publisher_ids")
    legacy_ids = postcondition.get("legacy_publisher_ids")
    canonical_ids = postcondition.get("canonical_publisher_ids")
    if (
        postcondition.get("phase") != checkpoint["phase"]
        or postcondition.get("publisher_id") != checkpoint["publisher_id"]
        or not isinstance(publisher_topic_state, Mapping)
        or not isinstance(publisher_ids, list)
        or not all(isinstance(value, str) and value for value in publisher_ids)
        or publisher_ids != sorted(publisher_ids)
        or len(publisher_ids) != len(set(publisher_ids))
        or set(publisher_topic_state) != set(publisher_ids)
        or not isinstance(legacy_ids, list)
        or not isinstance(canonical_ids, list)
        or legacy_ids != sorted(legacy_ids)
        or canonical_ids != sorted(canonical_ids)
    ):
        raise ContractError("alarm publisher postcondition identity is invalid")
    for publisher_id, state in publisher_topic_state.items():
        if (
            not isinstance(state, Mapping)
            or set(state) != {"source_type", "source_id", "topic_arns"}
            or not require_string(state.get("source_type"), "publisher source type")
            or not require_string(state.get("source_id"), "publisher source id")
            or not isinstance(state.get("topic_arns"), list)
            or state["topic_arns"] != sorted(state["topic_arns"])
            or len(state["topic_arns"]) != len(set(state["topic_arns"]))
            or not set(state["topic_arns"]).issubset(
                {CANONICAL_TOPIC, LEGACY_TOPIC}
            )
            or not state["topic_arns"]
            or publisher_id
            != f"{state['source_type']}:{state['source_id']}"
        ):
            raise ContractError("alarm publisher topic state is invalid")
    calculated_legacy = sorted(
        publisher_id
        for publisher_id, state in publisher_topic_state.items()
        if LEGACY_TOPIC in state["topic_arns"]
    )
    calculated_canonical = sorted(
        publisher_id
        for publisher_id, state in publisher_topic_state.items()
        if CANONICAL_TOPIC in state["topic_arns"]
    )
    if (
        legacy_ids != calculated_legacy
        or canonical_ids != calculated_canonical
        or postcondition.get("legacy_publisher_count") != len(calculated_legacy)
        or postcondition.get("canonical_publisher_count")
        != len(calculated_canonical)
        or not HEX64.fullmatch(
            str(postcondition.get("publisher_reference_set_sha256", ""))
        )
        or not HEX64.fullmatch(
            str(postcondition.get("publishers_sha256", ""))
        )
    ):
        raise ContractError("alarm publisher postcondition counts/hashes differ")
    delivery_verification = postcondition.get("delivery_verification")
    if (
        checkpoint["phase"] == "canonical_delivery_confirmed"
        and not isinstance(delivery_verification, Mapping)
    ) or (
        checkpoint["phase"] != "canonical_delivery_confirmed"
        and delivery_verification is not None
    ):
        raise ContractError("alarm delivery postcondition is in the wrong phase")
    if isinstance(delivery_verification, Mapping):
        require_keys(
            delivery_verification,
            (
                "verified",
                "inventory_sha256",
                "ledger_request_id_sha256",
                "verified_at_epoch",
            ),
            "alarm delivery verification",
        )
        if (
            delivery_verification.get("verified") is not True
            or delivery_verification.get("inventory_sha256")
            != checkpoint["inventory_sha256"]
            or not HEX64.fullmatch(
                str(delivery_verification.get("ledger_request_id_sha256", ""))
            )
            or require_int(
                delivery_verification.get("verified_at_epoch"),
                "alarm delivery verification time",
                minimum=1,
            )
            > checkpoint["created_at_epoch"]
        ):
            raise ContractError("alarm delivery verification is not exact/fresh")
    expected_idempotency_key = canonical_sha256(
        {
            "migration_id": checkpoint["migration_id"],
            "phase": checkpoint["phase"],
            "publisher_id": checkpoint["publisher_id"],
            "inventory_sha256": checkpoint["inventory_sha256"],
            "postcondition_sha256": checkpoint[
                "postcondition_receipt_sha256"
            ],
            "delivery_receipt_sha256": delivery_receipt_sha256,
        }
    )
    if checkpoint["idempotency_key"] != expected_idempotency_key:
        raise ContractError("alarm checkpoint idempotency binding is invalid")
    if (
        checkpoint["phase"] == "publisher_checkpoint"
    ) != bool(checkpoint["publisher_id"]):
        raise ContractError("alarm publisher checkpoint id is invalid")
    rollback_plan = checkpoint["rollback_plan"]
    require_keys(
        rollback_plan,
        ("mode", "automatic", "publisher_topic_state"),
        "alarm rollback plan",
    )
    rollback_modes = {
        "dual_publish": (
            "hold-dual-until-legacy-delivery-verified",
            False,
        ),
        "publisher_checkpoint": (
            "restore-exact-publisher-checkpoint",
            True,
        ),
        "canonical_delivery_confirmed": (
            "restore-all-durable-dual-publish",
            True,
        ),
        "legacy_reference_zero": (
            "restore-all-durable-dual-publish",
            True,
        ),
        "legacy_retired": (
            "new-reviewed-migration-required",
            False,
        ),
    }
    expected_mode, expected_automatic = rollback_modes[checkpoint["phase"]]
    if (
        rollback_plan.get("mode") != expected_mode
        or rollback_plan.get("automatic") is not expected_automatic
        or not isinstance(rollback_plan.get("publisher_topic_state"), Mapping)
    ):
        raise ContractError("alarm rollback plan mode is invalid")

    def dual_state(
        states: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        return {
            publisher_id: {
                "source_type": state["source_type"],
                "source_id": state["source_id"],
                "topic_arns": sorted([CANONICAL_TOPIC, LEGACY_TOPIC]),
            }
            for publisher_id, state in sorted(states.items())
        }

    if checkpoint["phase"] == "dual_publish":
        expected_rollback_state = dict(publisher_topic_state)
    elif checkpoint["phase"] == "publisher_checkpoint":
        if previous is None:
            raise ContractError("publisher rollback has no prior checkpoint")
        previous_postcondition = previous.get("postcondition")
        previous_state = (
            previous_postcondition.get("publisher_topic_state")
            if isinstance(previous_postcondition, Mapping)
            else None
        )
        if (
            not isinstance(previous_state, Mapping)
            or checkpoint["publisher_id"] not in previous_state
        ):
            raise ContractError("publisher rollback source checkpoint is missing")
        expected_rollback_state = {
            checkpoint["publisher_id"]: previous_state[
                checkpoint["publisher_id"]
            ]
        }
    elif checkpoint["phase"] in {
        "canonical_delivery_confirmed",
        "legacy_reference_zero",
    }:
        expected_rollback_state = dual_state(publisher_topic_state)
    else:
        expected_rollback_state = {}
    if rollback_plan["publisher_topic_state"] != expected_rollback_state:
        raise ContractError("alarm rollback plan does not bind exact publisher state")
    if previous is None:
        if checkpoint["sequence"] != 1:
            raise ContractError("first alarm checkpoint sequence must be one")
        if checkpoint["phase"] != "dual_publish":
            raise ContractError("alarm migration must begin with dual_publish")
        if checkpoint["previous_checkpoint_sha256"] != "":
            raise ContractError("first checkpoint must not name a predecessor")
        return
    previous_phase = previous.get("phase")
    current_phase = checkpoint["phase"]
    if (
        checkpoint["sequence"]
        != require_int(previous.get("sequence"), "previous checkpoint sequence")
        + 1
        or checkpoint["migration_id"] != previous.get("migration_id")
        or require_int(
            checkpoint["created_at_epoch"], "checkpoint creation time"
        )
        < require_int(
            previous.get("created_at_epoch"),
            "previous checkpoint creation time",
        )
    ):
        raise ContractError("alarm migration sequence/time/identity is invalid")
    allowed_transitions = {
        "dual_publish": {"publisher_checkpoint"},
        "publisher_checkpoint": {
            "publisher_checkpoint",
            "canonical_delivery_confirmed",
        },
        "canonical_delivery_confirmed": {"legacy_reference_zero"},
        "legacy_reference_zero": {"legacy_retired"},
        "legacy_retired": set(),
    }
    if current_phase not in allowed_transitions.get(str(previous_phase), set()):
        raise ContractError("alarm migration phase order is invalid")
    if checkpoint["previous_checkpoint_sha256"] != canonical_sha256(previous):
        raise ContractError("alarm migration checkpoint hash chain is broken")


def _ddb_scalar(item: Mapping[str, Any], name: str, kind: str = "S") -> str:
    value = item.get(name)
    if not isinstance(value, Mapping) or set(value) != {kind}:
        raise ContractError(f"DynamoDB checkpoint {name} is malformed")
    scalar = value[kind]
    if not isinstance(scalar, str):
        raise ContractError(f"DynamoDB checkpoint {name} is not a string")
    return scalar


def _runtime_lock_item(
    *,
    workflow_id: str,
    owner_token: str,
    acquired_at_epoch: int,
) -> dict[str, dict[str, str]]:
    return {
        "record_id": _dynamodb_value(SHARED_LOCK_RECORD_ID),
        "record_type": _dynamodb_value(
            "teamagent.runtime-evidence-workflow-lock"
        ),
        "schema_version": _dynamodb_value(1),
        "state": _dynamodb_value("LOCKED"),
        "workflow_id": _dynamodb_value(workflow_id),
        "owner_token": _dynamodb_value(owner_token),
        "acquired_at_epoch": _dynamodb_value(acquired_at_epoch),
        "lease_expires_at": _dynamodb_value(
            acquired_at_epoch + RUNTIME_LOCK_LEASE_SECONDS
        ),
        "audit_expires_at": _dynamodb_value(
            acquired_at_epoch + 31536000
        ),
    }


def _read_runtime_workflow_lock(
    aws: AwsCli,
) -> tuple[Mapping[str, Any] | None, HttpEvidence]:
    response, http = aws.call(
        "dynamodb",
        "get-item",
        (
            "--table-name",
            SHARED_LEDGER_TABLE,
            "--key",
            json.dumps(
                {"record_id": _dynamodb_value(SHARED_LOCK_RECORD_ID)},
                separators=(",", ":"),
            ),
            "--consistent-read",
        ),
    )
    item = response.get("Item")
    if item is not None and not isinstance(item, Mapping):
        raise ContractError("shared runtime workflow lock is malformed")
    return item, http


def _validate_runtime_workflow_lock_item(
    item: Mapping[str, Any],
    *,
    workflow_id: str,
    owner_token: str,
    acquired_at_epoch: int,
) -> tuple[int, int]:
    expected = _runtime_lock_item(
        workflow_id=workflow_id,
        owner_token=owner_token,
        acquired_at_epoch=acquired_at_epoch,
    )
    if dict(item) != expected:
        raise ContractError("shared runtime workflow lock ownership differs")
    lease = int(_ddb_scalar(item, "lease_expires_at", "N"))
    audit = int(_ddb_scalar(item, "audit_expires_at", "N"))
    if (
        lease != acquired_at_epoch + RUNTIME_LOCK_LEASE_SECONDS
        or audit <= lease
    ):
        raise ContractError("shared runtime workflow lock timing differs")
    return lease, audit


def acquire_runtime_workflow_lock(
    aws: AwsCli,
    *,
    workflow_id: str,
) -> dict[str, Any]:
    if not UUID4.fullmatch(workflow_id):
        raise ContractError("runtime workflow ID must be a lowercase UUIDv4")
    _, identity_http = _caller_identity(aws)
    acquired_at = identity_http.date_epoch
    owner_token = secrets.token_hex(32)
    item = _runtime_lock_item(
        workflow_id=workflow_id,
        owner_token=owner_token,
        acquired_at_epoch=acquired_at,
    )
    response, put_http = aws.call(
        "dynamodb",
        "put-item",
        (
            "--table-name",
            SHARED_LEDGER_TABLE,
            "--item",
            json.dumps(item, separators=(",", ":")),
            "--condition-expression",
            "attribute_not_exists(record_id) OR lease_expires_at < :now",
            "--expression-attribute-values",
            json.dumps(
                {":now": _dynamodb_value(acquired_at)},
                separators=(",", ":"),
            ),
        ),
    )
    confirmed, confirm_http = _read_runtime_workflow_lock(aws)
    if confirmed is None:
        raise ContractError("shared runtime workflow lock was not confirmed")
    lease, _ = _validate_runtime_workflow_lock_item(
        confirmed,
        workflow_id=workflow_id,
        owner_token=owner_token,
        acquired_at_epoch=acquired_at,
    )
    if max(identity_http.date_epoch, put_http.date_epoch) > confirm_http.date_epoch:
        raise ContractError("shared lock confirmation timestamp is time-inverted")
    return {
        "kind": "teamagent-runtime-workflow-lock-receipt",
        "schema_version": 1,
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "record_id": SHARED_LOCK_RECORD_ID,
        "record_type": "teamagent.runtime-evidence-workflow-lock",
        "workflow_id": workflow_id,
        "owner_token": owner_token,
        "acquired_at_epoch": acquired_at,
        "lease_expires_at": lease,
        "put_response_sha256": canonical_sha256(response),
        "put_request_id_sha256": sha256_bytes(put_http.request_id.encode()),
        "confirmed_at_epoch": confirm_http.date_epoch,
        "confirm_request_id_sha256": sha256_bytes(
            confirm_http.request_id.encode()
        ),
    }


def verify_runtime_workflow_lock(
    aws: AwsCli,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    require_keys(
        receipt,
        (
            "kind",
            "schema_version",
            "account_id",
            "region",
            "record_id",
            "record_type",
            "workflow_id",
            "owner_token",
            "acquired_at_epoch",
            "lease_expires_at",
            "put_response_sha256",
            "put_request_id_sha256",
            "confirmed_at_epoch",
            "confirm_request_id_sha256",
        ),
        "runtime shared-lock receipt",
    )
    workflow_id = require_string(receipt["workflow_id"], "runtime workflow ID")
    owner_token = require_string(receipt["owner_token"], "runtime lock owner")
    if (
        receipt["kind"] != "teamagent-runtime-workflow-lock-receipt"
        or receipt["schema_version"] != 1
        or receipt["account_id"] != ACCOUNT_ID
        or receipt["region"] != REGION
        or receipt["record_id"] != SHARED_LOCK_RECORD_ID
        or receipt["record_type"]
        != "teamagent.runtime-evidence-workflow-lock"
        or not UUID4.fullmatch(workflow_id)
        or not HEX64.fullmatch(owner_token)
    ):
        raise ContractError("runtime shared-lock receipt identity is invalid")
    for field in (
        "put_response_sha256",
        "put_request_id_sha256",
        "confirm_request_id_sha256",
    ):
        if not HEX64.fullmatch(str(receipt[field])):
            raise ContractError(f"runtime shared-lock {field} is invalid")
    acquired_at = require_int(
        receipt["acquired_at_epoch"], "runtime lock acquisition"
    )
    lease = require_int(receipt["lease_expires_at"], "runtime lock lease")
    confirmed_at = require_int(
        receipt["confirmed_at_epoch"], "runtime lock confirmation"
    )
    item, get_http = _read_runtime_workflow_lock(aws)
    if item is None:
        raise ContractError("shared runtime workflow lock is absent")
    live_lease, _ = _validate_runtime_workflow_lock_item(
        item,
        workflow_id=workflow_id,
        owner_token=owner_token,
        acquired_at_epoch=acquired_at,
    )
    _, observation_http = _caller_identity(aws)
    if (
        live_lease != lease
        or not (
            acquired_at
            <= confirmed_at
            <= get_http.date_epoch
            <= observation_http.date_epoch
            < lease
        )
    ):
        raise ContractError("shared runtime workflow lock is stale or time-inverted")
    return {
        "record_id": SHARED_LOCK_RECORD_ID,
        "workflow_id": workflow_id,
        "acquired_at_epoch": acquired_at,
        "lease_expires_at": lease,
        "verified_at_epoch": observation_http.date_epoch,
        "get_request_id_sha256": sha256_bytes(get_http.request_id.encode()),
    }


def release_runtime_workflow_lock(
    aws: AwsCli,
    *,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    verified = verify_runtime_workflow_lock(aws, receipt)
    response, delete_http = aws.call(
        "dynamodb",
        "delete-item",
        (
            "--table-name",
            SHARED_LEDGER_TABLE,
            "--key",
            json.dumps(
                {"record_id": _dynamodb_value(SHARED_LOCK_RECORD_ID)},
                separators=(",", ":"),
            ),
            "--condition-expression",
            (
                "record_type = :record_type AND workflow_id = :workflow "
                "AND owner_token = :owner AND #state = :locked"
            ),
            "--expression-attribute-names",
            '{"#state":"state"}',
            "--expression-attribute-values",
            json.dumps(
                {
                    ":record_type": _dynamodb_value(
                        "teamagent.runtime-evidence-workflow-lock"
                    ),
                    ":workflow": _dynamodb_value(
                        str(receipt["workflow_id"])
                    ),
                    ":owner": _dynamodb_value(str(receipt["owner_token"])),
                    ":locked": _dynamodb_value("LOCKED"),
                },
                separators=(",", ":"),
            ),
        ),
    )
    remaining, confirm_http = _read_runtime_workflow_lock(aws)
    if remaining is not None:
        raise ContractError("shared runtime workflow lock release was not confirmed")
    if max(
        verified["verified_at_epoch"], delete_http.date_epoch
    ) > confirm_http.date_epoch:
        raise ContractError("shared lock release timestamp is time-inverted")
    return {
        "kind": "teamagent-runtime-workflow-lock-release",
        "schema_version": 1,
        "record_id": SHARED_LOCK_RECORD_ID,
        "workflow_id": receipt["workflow_id"],
        "delete_response_sha256": canonical_sha256(response),
        "delete_request_id_sha256": sha256_bytes(
            delete_http.request_id.encode()
        ),
        "released_at_epoch": confirm_http.date_epoch,
    }


def _alarm_migration_history(
    aws: AwsCli, migration_id: str
) -> list[dict[str, Any]]:
    head_record_id = f"alarm-migration#{migration_id}#head"
    head_response, _ = aws.call(
        "dynamodb",
        "get-item",
        (
            "--table-name",
            MIGRATION_LEDGER_TABLE,
            "--key",
            json.dumps(
                {"record_id": _dynamodb_value(head_record_id)},
                separators=(",", ":"),
            ),
            "--consistent-read",
        ),
    )
    head = head_response.get("Item")
    if head is None:
        return []
    if not isinstance(head, Mapping):
        raise ContractError("alarm migration ledger head is malformed")
    if set(head) != {
        "record_id",
        "sequence",
        "phase",
        "checkpoint_sha256",
    }:
        raise ContractError("alarm migration ledger head schema is not exact")
    if _ddb_scalar(head, "record_id") != head_record_id:
        raise ContractError("alarm migration ledger head identity differs")
    sequence_text = _ddb_scalar(head, "sequence", "N")
    if not sequence_text.isdecimal():
        raise ContractError("alarm migration ledger sequence is invalid")
    final_sequence = int(sequence_text)
    if final_sequence <= 0 or final_sequence > 100000:
        raise ContractError("alarm migration ledger sequence is outside bounds")
    checkpoints: list[dict[str, Any]] = []
    for sequence in range(1, final_sequence + 1):
        record_id = f"alarm-migration#{migration_id}#{sequence:020d}"
        response, _ = aws.call(
            "dynamodb",
            "get-item",
            (
                "--table-name",
                MIGRATION_LEDGER_TABLE,
                "--key",
                json.dumps(
                    {"record_id": _dynamodb_value(record_id)},
                    separators=(",", ":"),
                ),
                "--consistent-read",
            ),
        )
        item = response.get("Item")
        if not isinstance(item, Mapping):
            raise ContractError("alarm migration ledger has a checkpoint gap")
        if set(item) != {
            "record_id",
            "migration_id",
            "sequence",
            "phase",
            "publisher_id",
            "idempotency_key",
            "checkpoint_sha256",
            "checkpoint_json",
            "created_at_epoch",
        }:
            raise ContractError("alarm migration ledger checkpoint schema is not exact")
        if (
            _ddb_scalar(item, "record_id") != record_id
            or _ddb_scalar(item, "migration_id") != migration_id
            or _ddb_scalar(item, "sequence", "N") != str(sequence)
        ):
            raise ContractError("alarm migration checkpoint identity differs")
        checkpoint_text = _ddb_scalar(item, "checkpoint_json")
        try:
            checkpoint = json.loads(checkpoint_text)
        except json.JSONDecodeError as exc:
            raise ContractError("alarm migration checkpoint JSON is invalid") from exc
        if not isinstance(checkpoint, dict):
            raise ContractError("alarm migration checkpoint is not an object")
        if canonical_sha256(checkpoint) != _ddb_scalar(
            item, "checkpoint_sha256"
        ):
            raise ContractError("alarm migration ledger checkpoint hash differs")
        checkpoints.append(checkpoint)
    previous: Mapping[str, Any] | None = None
    for expected_sequence, checkpoint in enumerate(checkpoints, 1):
        if checkpoint.get("sequence") != expected_sequence:
            raise ContractError("alarm migration checkpoint sequence has a gap")
        validate_alarm_migration_checkpoint(checkpoint, previous=previous)
        previous = checkpoint
    final = checkpoints[-1]
    if (
        _ddb_scalar(head, "phase") != final["phase"]
        or _ddb_scalar(head, "checkpoint_sha256")
        != canonical_sha256(final)
    ):
        raise ContractError("alarm migration ledger head does not bind the final checkpoint")
    return checkpoints


def _alarm_phase_postcondition(
    aws: AwsCli,
    *,
    phase: str,
    publisher_id: str,
    inventory: Mapping[str, Any],
    delivery_receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    publishers = inventory.get("publishers")
    if not isinstance(publishers, list) or not all(
        isinstance(publisher, Mapping) for publisher in publishers
    ):
        raise ContractError("alarm publisher inventory is malformed")
    publisher_by_id = {
        require_string(publisher.get("publisher_id"), "publisher id"): publisher
        for publisher in publishers
    }
    legacy_publishers = [
        publisher
        for publisher in publishers
        if LEGACY_TOPIC in publisher.get("topic_arns", [])
    ]
    canonical_publishers = [
        publisher
        for publisher in publishers
        if CANONICAL_TOPIC in publisher.get("topic_arns", [])
    ]

    delivery_verification: Mapping[str, Any] | None = None
    if phase == "dual_publish":
        if not publishers or any(
            set(publisher.get("topic_arns", []))
            != {CANONICAL_TOPIC, LEGACY_TOPIC}
            for publisher in publishers
        ):
            raise ContractError(
                "dual_publish requires every publisher to target exactly both topics"
            )
    elif phase == "publisher_checkpoint":
        publisher = publisher_by_id.get(publisher_id)
        if publisher is None or set(publisher.get("topic_arns", [])) != {
            CANONICAL_TOPIC
        }:
            raise ContractError(
                "publisher checkpoint requires the selected publisher's "
                "exact canonical-only post-state"
            )
    elif phase == "canonical_delivery_confirmed":
        if delivery_receipt is None:
            raise ContractError("canonical delivery phase requires a real SNS receipt")
        challenge = delivery_receipt.get("challenge")
        ack = delivery_receipt.get("recipient_ack")
        if not isinstance(challenge, Mapping) or not isinstance(ack, Mapping):
            raise ContractError("SNS delivery receipt omits challenge/ack")
        delivery_verification = verify_sns_delivery_receipt(
            aws,
            challenge=challenge,
            ack=ack,
            receipt=delivery_receipt,
        )
        if not publishers or any(
            set(publisher.get("topic_arns", [])) != {CANONICAL_TOPIC}
            for publisher in publishers
        ):
            raise ContractError(
                "canonical delivery requires every checkpointed publisher "
                "to be canonical-only"
            )
    elif phase == "legacy_reference_zero":
        if legacy_publishers:
            raise ContractError("legacy SNS publisher references are not zero")
        if not canonical_publishers or len(canonical_publishers) != len(publishers):
            raise ContractError("every alarm publisher must remain canonical")
    elif phase == "legacy_retired":
        if LEGACY_TOPIC in inventory.get("topic_inventory", []):
            raise ContractError("legacy SNS topic still exists")
        if legacy_publishers:
            raise ContractError("legacy SNS references remain after retirement")
        if not canonical_publishers or len(canonical_publishers) != len(publishers):
            raise ContractError("canonical publishers are incomplete after retirement")
    else:
        raise ContractError("unknown alarm migration phase")

    return {
        "phase": phase,
        "publisher_id": publisher_id,
        "inventory_sha256": inventory["inventory_sha256"],
        "publisher_reference_set_sha256": inventory[
            "publisher_reference_set_sha256"
        ],
        "publishers_sha256": inventory["publishers_sha256"],
        "publisher_ids": sorted(publisher_by_id),
        "publisher_topic_state": {
            current_id: {
                "source_type": require_string(
                    publisher.get("source_type"), "publisher source type"
                ),
                "source_id": require_string(
                    publisher.get("source_id"), "publisher source id"
                ),
                "topic_arns": sorted(
                    require_string(topic_arn, "publisher topic ARN")
                    for topic_arn in publisher.get("topic_arns", [])
                ),
            }
            for current_id, publisher in sorted(publisher_by_id.items())
        },
        "legacy_publisher_ids": sorted(
            require_string(publisher.get("publisher_id"), "publisher id")
            for publisher in legacy_publishers
        ),
        "canonical_publisher_ids": sorted(
            require_string(publisher.get("publisher_id"), "publisher id")
            for publisher in canonical_publishers
        ),
        "legacy_publisher_count": len(legacy_publishers),
        "canonical_publisher_count": len(canonical_publishers),
        "delivery_verification": delivery_verification,
    }


def advance_alarm_migration(
    aws: AwsCli,
    *,
    migration_id: str,
    phase: str,
    publisher_id: str,
    delivery_receipt: Mapping[str, Any] | None,
    lock_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Durably checkpoint one safe, resumable alarm migration phase."""

    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[A-Za-z0-9_.-]{1,96}", migration_id):
        raise ContractError("alarm migration id is invalid")
    if phase not in ALARM_PHASES:
        raise ContractError("alarm migration phase is invalid")
    if phase == "publisher_checkpoint":
        require_string(publisher_id, "publisher checkpoint id")
    elif publisher_id:
        raise ContractError("publisher id is only valid for publisher_checkpoint")

    initial_lock = verify_runtime_workflow_lock(aws, lock_receipt)
    _caller_identity(aws)
    history = _alarm_migration_history(aws, migration_id)
    previous = history[-1] if history else None
    first_postcondition = (
        history[0].get("postcondition")
        if history
        else None
    )
    if history and not isinstance(first_postcondition, Mapping):
        raise ContractError("initial dual-publish checkpoint is malformed")
    checkpointed_publishers = {
        str(checkpoint["publisher_id"])
        for checkpoint in history
        if checkpoint["phase"] == "publisher_checkpoint"
    }
    if phase == "publisher_checkpoint":
        expected_publishers = set(
            first_postcondition.get("publisher_ids", [])
            if first_postcondition is not None
            else []
        )
        if publisher_id not in expected_publishers:
            raise ContractError(
                "publisher checkpoint was not present in the initial dual-publish set"
            )
    if phase == "canonical_delivery_confirmed":
        expected_publishers = set(
            first_postcondition.get("publisher_ids", [])
            if first_postcondition is not None
            else []
        )
        if not expected_publishers or checkpointed_publishers != expected_publishers:
            raise ContractError(
                "canonical delivery requires one durable checkpoint per publisher"
            )
    inventory = collect_inventory(aws)
    current_publishers = {
        require_string(publisher.get("publisher_id"), "publisher id"): set(
            publisher.get("topic_arns", [])
        )
        for publisher in inventory.get("publishers", [])
        if isinstance(publisher, Mapping)
    }
    if len(current_publishers) != len(inventory.get("publishers", [])):
        raise ContractError("alarm publisher inventory is not exact/unique")
    if first_postcondition is not None:
        expected_publishers = set(first_postcondition.get("publisher_ids", []))
        if set(current_publishers) != expected_publishers:
            raise ContractError(
                "alarm publisher set changed after the initial dual-publish checkpoint"
            )
        if phase == "publisher_checkpoint":
            for current_id, topics in current_publishers.items():
                expected_topics = (
                    {CANONICAL_TOPIC}
                    if current_id in checkpointed_publishers | {publisher_id}
                    else {CANONICAL_TOPIC, LEGACY_TOPIC}
                )
                if topics != expected_topics:
                    raise ContractError(
                        "mixed alarm migration state does not match its durable "
                        "publisher checkpoints"
                    )
        elif phase in {
            "canonical_delivery_confirmed",
            "legacy_reference_zero",
            "legacy_retired",
        } and any(
            topics != {CANONICAL_TOPIC}
            for topics in current_publishers.values()
        ):
            raise ContractError(
                "post-checkpoint alarm publishers are not all canonical-only"
            )
    postcondition = _alarm_phase_postcondition(
        aws,
        phase=phase,
        publisher_id=publisher_id,
        inventory=inventory,
        delivery_receipt=delivery_receipt,
    )
    delivery_sha = (
        canonical_sha256(delivery_receipt)
        if delivery_receipt is not None
        else ""
    )
    idempotency_key = canonical_sha256(
        {
            "migration_id": migration_id,
            "phase": phase,
            "publisher_id": publisher_id,
            "inventory_sha256": inventory["inventory_sha256"],
            "postcondition_sha256": canonical_sha256(postcondition),
            "delivery_receipt_sha256": delivery_sha,
        }
    )
    for checkpoint in history:
        if checkpoint["idempotency_key"] == idempotency_key:
            if checkpoint != history[-1]:
                raise ContractError(
                    "cannot resume an alarm phase after a later checkpoint"
                )
            _, resumed_http = _caller_identity(aws)
            resumed_lock = verify_runtime_workflow_lock(aws, lock_receipt)
            if (
                resumed_lock["workflow_id"]
                != initial_lock["workflow_id"]
                or resumed_http.date_epoch
                > resumed_lock["verified_at_epoch"]
            ):
                raise ContractError("alarm resume shared-lock observation is invalid")
            if checkpoint["created_at_epoch"] > resumed_http.date_epoch:
                raise ContractError("resumed alarm checkpoint is future-dated")
            return {
                "kind": "teamagent-alarm-migration-phase-receipt",
                "schema_version": 1,
                "migration_id": migration_id,
                "phase": phase,
                "resumed": True,
                "checkpoint": checkpoint,
                "checkpoint_sha256": canonical_sha256(checkpoint),
                "postcondition": checkpoint["postcondition"],
                "history_sha256": canonical_sha256(history),
                "ledger_request_id_sha256": "",
                "ledger_response_sha256": "",
                "shared_lock_record_id": SHARED_LOCK_RECORD_ID,
                "shared_lock_workflow_id": initial_lock["workflow_id"],
                "shared_lock_receipt_sha256": canonical_sha256(lock_receipt),
                "observed_at_epoch": resumed_lock["verified_at_epoch"],
            }
    if (
        phase == "publisher_checkpoint"
        and publisher_id in checkpointed_publishers
    ):
        raise ContractError("publisher already has a different durable checkpoint")

    _, observed_http = _caller_identity(aws)
    source_times = [
        source["aws_date_epoch"] for source in inventory["source_pages"]
    ]
    if source_times and max(source_times) > observed_http.date_epoch:
        raise ContractError("alarm inventory timestamp exceeds observation")
    current_topic_state = postcondition["publisher_topic_state"]
    durable_dual_topic_state = {
        current_id: {
            "source_type": state["source_type"],
            "source_id": state["source_id"],
            "topic_arns": sorted([CANONICAL_TOPIC, LEGACY_TOPIC]),
        }
        for current_id, state in sorted(current_topic_state.items())
    }
    previous_topic_state = (
        previous["postcondition"]["publisher_topic_state"]
        if previous is not None
        else {}
    )
    if phase == "dual_publish":
        rollback_plan = {
            "mode": "hold-dual-until-legacy-delivery-verified",
            "automatic": False,
            "publisher_topic_state": current_topic_state,
        }
    elif phase == "publisher_checkpoint":
        rollback_plan = {
            "mode": "restore-exact-publisher-checkpoint",
            "automatic": True,
            "publisher_topic_state": {
                publisher_id: previous_topic_state[publisher_id]
            },
        }
    elif phase in {
        "canonical_delivery_confirmed",
        "legacy_reference_zero",
    }:
        rollback_plan = {
            "mode": "restore-all-durable-dual-publish",
            "automatic": True,
            "publisher_topic_state": durable_dual_topic_state,
        }
    else:
        rollback_plan = {
            "mode": "new-reviewed-migration-required",
            "automatic": False,
            "publisher_topic_state": {},
        }
    checkpoint = {
        "kind": "teamagent-alarm-migration-checkpoint",
        "schema_version": 1,
        "migration_id": migration_id,
        "sequence": len(history) + 1,
        "phase": phase,
        "publisher_id": publisher_id,
        "idempotency_key": idempotency_key,
        "delivery_receipt_sha256": delivery_sha,
        "inventory_sha256": inventory["inventory_sha256"],
        "postcondition": postcondition,
        "postcondition_receipt_sha256": canonical_sha256(postcondition),
        "rollback_plan": rollback_plan,
        "previous_checkpoint_sha256": (
            canonical_sha256(previous) if previous is not None else ""
        ),
        "created_at_epoch": observed_http.date_epoch,
    }
    validate_alarm_migration_checkpoint(checkpoint, previous=previous)
    checkpoint_sha = canonical_sha256(checkpoint)
    record_id = (
        f"alarm-migration#{migration_id}#{checkpoint['sequence']:020d}"
    )
    item = {
        "record_id": _dynamodb_value(record_id),
        "migration_id": _dynamodb_value(migration_id),
        "sequence": _dynamodb_value(checkpoint["sequence"]),
        "phase": _dynamodb_value(phase),
        "publisher_id": _dynamodb_value(publisher_id),
        "idempotency_key": _dynamodb_value(idempotency_key),
        "checkpoint_sha256": _dynamodb_value(checkpoint_sha),
        "checkpoint_json": _dynamodb_value(
            canonical_bytes(checkpoint).decode().rstrip("\n")
        ),
        "created_at_epoch": _dynamodb_value(observed_http.date_epoch),
    }
    head_record_id = f"alarm-migration#{migration_id}#head"
    head_names = {"#sequence": "sequence"}
    head_values = {
        ":next": _dynamodb_value(checkpoint["sequence"]),
        ":phase": _dynamodb_value(phase),
        ":checkpoint": _dynamodb_value(checkpoint_sha),
    }
    if previous is None:
        head_condition = "attribute_not_exists(record_id)"
    else:
        head_condition = "#sequence = :previous"
        head_values[":previous"] = _dynamodb_value(checkpoint["sequence"] - 1)
    transact_items = [
        {
            "Put": {
                "TableName": MIGRATION_LEDGER_TABLE,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(record_id)",
            }
        },
        {
            "Update": {
                "TableName": MIGRATION_LEDGER_TABLE,
                "Key": {"record_id": _dynamodb_value(head_record_id)},
                "UpdateExpression": (
                    "SET #sequence = :next, phase = :phase, "
                    "checkpoint_sha256 = :checkpoint"
                ),
                "ConditionExpression": head_condition,
                "ExpressionAttributeNames": head_names,
                "ExpressionAttributeValues": head_values,
            }
        },
    ]
    response, ledger_http = aws.call(
        "dynamodb",
        "transact-write-items",
        (
            "--transact-items",
            json.dumps(transact_items, separators=(",", ":")),
            "--client-request-token",
            str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key)),
        ),
    )
    persisted_history = _alarm_migration_history(aws, migration_id)
    if (
        len(persisted_history) != checkpoint["sequence"]
        or persisted_history[-1] != checkpoint
    ):
        raise ContractError("alarm migration checkpoint was not durably confirmed")
    _, final_http = _caller_identity(aws)
    final_lock = verify_runtime_workflow_lock(aws, lock_receipt)
    if final_lock["workflow_id"] != initial_lock["workflow_id"]:
        raise ContractError("alarm migration shared lock changed")
    source_times = [
        source["aws_date_epoch"] for source in inventory["source_pages"]
    ]
    if max(
        [
            observed_http.date_epoch,
            ledger_http.date_epoch,
            final_http.date_epoch,
            *source_times,
        ]
    ) > final_lock["verified_at_epoch"]:
        raise ContractError("alarm checkpoint evidence exceeds final observation")
    return {
        "kind": "teamagent-alarm-migration-phase-receipt",
        "schema_version": 1,
        "migration_id": migration_id,
        "phase": phase,
        "resumed": False,
        "checkpoint": checkpoint,
        "checkpoint_sha256": checkpoint_sha,
        "postcondition": postcondition,
        "history_sha256": canonical_sha256(persisted_history),
        "ledger_request_id_sha256": sha256_bytes(
            ledger_http.request_id.encode()
        ),
        "ledger_response_sha256": canonical_sha256(response),
        "shared_lock_record_id": SHARED_LOCK_RECORD_ID,
        "shared_lock_workflow_id": initial_lock["workflow_id"],
        "shared_lock_receipt_sha256": canonical_sha256(lock_receipt),
        "observed_at_epoch": final_lock["verified_at_epoch"],
    }


def verify_alarm_migration_final(
    aws: AwsCli,
    *,
    migration_id: str,
    phase_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-read the complete ledger and prove the safely retired final state."""

    require_keys(
        phase_receipt,
        (
            "kind",
            "schema_version",
            "migration_id",
            "phase",
            "resumed",
            "checkpoint",
            "checkpoint_sha256",
            "postcondition",
            "history_sha256",
            "ledger_request_id_sha256",
            "ledger_response_sha256",
            "shared_lock_record_id",
            "shared_lock_workflow_id",
            "shared_lock_receipt_sha256",
            "observed_at_epoch",
        ),
        "alarm migration phase receipt",
    )
    if (
        phase_receipt["kind"]
        != "teamagent-alarm-migration-phase-receipt"
        or phase_receipt["schema_version"] != 1
        or phase_receipt["migration_id"] != migration_id
        or phase_receipt["phase"] != "legacy_retired"
        or phase_receipt["shared_lock_record_id"] != SHARED_LOCK_RECORD_ID
        or not UUID4.fullmatch(
            str(phase_receipt["shared_lock_workflow_id"])
        )
        or not HEX64.fullmatch(
            str(phase_receipt["shared_lock_receipt_sha256"])
        )
        or not isinstance(phase_receipt["resumed"], bool)
        or not isinstance(phase_receipt["checkpoint"], Mapping)
        or not isinstance(phase_receipt["postcondition"], Mapping)
    ):
        raise ContractError("final alarm migration receipt identity is invalid")
    for field in ("checkpoint_sha256", "history_sha256"):
        if not HEX64.fullmatch(str(phase_receipt[field])):
            raise ContractError(f"final alarm migration {field} is invalid")
    for field in ("ledger_request_id_sha256", "ledger_response_sha256"):
        value = phase_receipt[field]
        if value != "" and not HEX64.fullmatch(str(value)):
            raise ContractError(f"final alarm migration {field} is invalid")

    history = _alarm_migration_history(aws, migration_id)
    if not history or history[-1]["phase"] != "legacy_retired":
        raise ContractError("alarm migration ledger is not at legacy_retired")
    if (
        phase_receipt["checkpoint"] != history[-1]
        or phase_receipt["checkpoint_sha256"]
        != canonical_sha256(history[-1])
        or phase_receipt["history_sha256"] != canonical_sha256(history)
        or phase_receipt["postcondition"] != history[-1]["postcondition"]
    ):
        raise ContractError("final alarm migration receipt differs from the ledger")

    expected_publishers = history[0]["postcondition"].get("publisher_ids")
    if (
        not isinstance(expected_publishers, list)
        or not expected_publishers
        or not all(isinstance(value, str) and value for value in expected_publishers)
        or len(expected_publishers) != len(set(expected_publishers))
    ):
        raise ContractError("initial alarm publisher set is invalid")
    publisher_checkpoints = [
        checkpoint
        for checkpoint in history
        if checkpoint["phase"] == "publisher_checkpoint"
    ]
    if sorted(
        checkpoint["publisher_id"] for checkpoint in publisher_checkpoints
    ) != sorted(expected_publishers):
        raise ContractError("alarm publisher checkpoint coverage is incomplete")
    canonical_checkpoints = [
        checkpoint
        for checkpoint in history
        if checkpoint["phase"] == "canonical_delivery_confirmed"
    ]
    if len(canonical_checkpoints) != 1:
        raise ContractError("canonical delivery checkpoint is not unique")
    delivery = canonical_checkpoints[0]["postcondition"].get(
        "delivery_verification"
    )
    if not isinstance(delivery, Mapping) or delivery.get("verified") is not True:
        raise ContractError("canonical delivery checkpoint lacks real SNS evidence")
    if [checkpoint["phase"] for checkpoint in history[-3:]] != [
        "canonical_delivery_confirmed",
        "legacy_reference_zero",
        "legacy_retired",
    ]:
        raise ContractError("alarm migration terminal phase order is invalid")

    inventory = collect_inventory(aws)
    current_postcondition = _alarm_phase_postcondition(
        aws,
        phase="legacy_retired",
        publisher_id="",
        inventory=inventory,
        delivery_receipt=None,
    )
    if current_postcondition != history[-1]["postcondition"]:
        raise ContractError("live alarm state changed after legacy retirement")
    _, observation_http = _caller_identity(aws)
    receipt_observed = require_int(
        phase_receipt["observed_at_epoch"],
        "alarm phase receipt observation",
    )
    source_times = [
        source["aws_date_epoch"] for source in inventory["source_pages"]
    ]
    if max(
        [
            receipt_observed,
            history[-1]["created_at_epoch"],
            *source_times,
        ]
    ) > observation_http.date_epoch:
        raise ContractError("final alarm migration evidence is time-inverted")
    return {
        "kind": "teamagent-alarm-migration-final-verification",
        "schema_version": 1,
        "migration_id": migration_id,
        "phase": "legacy_retired",
        "checkpoint_sha256": canonical_sha256(history[-1]),
        "history_sha256": canonical_sha256(history),
        "inventory_sha256": inventory["inventory_sha256"],
        "verified_at_epoch": observation_http.date_epoch,
    }


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"could not read JSON contract: {path}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"JSON contract is not an object: {path}")
    return value


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, canonical_bytes(value))
        os.fsync(fd)
    finally:
        os.close(fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aws-bin", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory")
    inventory.add_argument("--output", required=True, type=Path)

    quiescence = commands.add_parser("quiescence")
    quiescence.add_argument("--output", required=True, type=Path)

    acquire_lock = commands.add_parser("acquire-runtime-lock")
    acquire_lock.add_argument("--workflow-id", required=True)
    acquire_lock.add_argument("--output", required=True, type=Path)

    release_lock = commands.add_parser("release-runtime-lock")
    release_lock.add_argument("--lock", required=True, type=Path)
    release_lock.add_argument("--output", required=True, type=Path)

    versioning = commands.add_parser("first-time-versioning-cutover")
    versioning.add_argument("--lock-id", required=True)
    versioning.add_argument("--lock-receipt", required=True, type=Path)
    versioning.add_argument("--bedrock-config", required=True, type=Path)
    versioning.add_argument("--output", required=True, type=Path)

    fetch = commands.add_parser("fetch-s3-export")
    fetch.add_argument("--bucket", required=True)
    fetch.add_argument("--key", required=True)
    fetch.add_argument("--version-id", required=True)
    fetch.add_argument("--observation-epoch", required=True, type=int)
    fetch.add_argument("--export-path", required=True, type=Path)
    fetch.add_argument("--output", required=True, type=Path)

    rehash = commands.add_parser("rehash-export")
    rehash.add_argument("--binding", required=True, type=Path)
    rehash.add_argument("--output", required=True, type=Path)

    verify_export = commands.add_parser("verify-s3-export")
    verify_export.add_argument("--binding", required=True, type=Path)
    verify_export.add_argument("--fresh-dir", required=True, type=Path)
    verify_export.add_argument("--output", required=True, type=Path)

    readiness = commands.add_parser("build-log-readiness")
    readiness.add_argument("--spec", required=True, type=Path)
    readiness.add_argument("--versioning-receipt", required=True, type=Path)
    readiness.add_argument("--export-dir", required=True, type=Path)
    readiness.add_argument("--retention-output", required=True, type=Path)
    readiness.add_argument("--evidence-output", required=True, type=Path)
    readiness.add_argument("--receipt-output", required=True, type=Path)

    ack = commands.add_parser("verify-sns-ack")
    ack.add_argument("--challenge", required=True, type=Path)
    ack.add_argument("--ack", required=True, type=Path)
    ack.add_argument("--now-epoch", required=True, type=int)
    ack.add_argument("--output", required=True, type=Path)

    challenge = commands.add_parser("issue-sns-challenge")
    challenge.add_argument("--output", required=True, type=Path)

    sign_ack = commands.add_parser("sign-sns-ack")
    sign_ack.add_argument("--challenge", required=True, type=Path)
    sign_ack.add_argument("--output", required=True, type=Path)

    attest = commands.add_parser("attest-sns-delivery")
    attest.add_argument("--challenge", required=True, type=Path)
    attest.add_argument("--ack", required=True, type=Path)
    attest.add_argument("--output", required=True, type=Path)

    verify_delivery = commands.add_parser("verify-sns-delivery")
    verify_delivery.add_argument("--challenge", required=True, type=Path)
    verify_delivery.add_argument("--ack", required=True, type=Path)
    verify_delivery.add_argument("--receipt", required=True, type=Path)
    verify_delivery.add_argument("--output", required=True, type=Path)

    verify_versioning = commands.add_parser("verify-versioning-cutover")
    verify_versioning.add_argument("--workflow", required=True, type=Path)
    verify_versioning.add_argument("--output", required=True, type=Path)

    retention_live = commands.add_parser("verify-bedrock-retention")
    retention_live.add_argument("--output", required=True, type=Path)

    alarm_phase = commands.add_parser("advance-alarm-migration")
    alarm_phase.add_argument("--migration-id", required=True)
    alarm_phase.add_argument("--phase", required=True, choices=ALARM_PHASES)
    alarm_phase.add_argument("--publisher-id", default="")
    alarm_phase.add_argument("--delivery-receipt", type=Path)
    alarm_phase.add_argument("--lock-receipt", required=True, type=Path)
    alarm_phase.add_argument("--output", required=True, type=Path)

    alarm_final = commands.add_parser("verify-alarm-migration-final")
    alarm_final.add_argument("--migration-id", required=True)
    alarm_final.add_argument("--receipt", required=True, type=Path)
    alarm_final.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    aws = AwsCli(args.aws_bin.resolve(strict=True))
    if args.command == "inventory":
        result = collect_inventory(aws)
    elif args.command == "quiescence":
        result = capture_quiescence(aws)
    elif args.command == "acquire-runtime-lock":
        result = acquire_runtime_workflow_lock(
            aws,
            workflow_id=args.workflow_id,
        )
    elif args.command == "release-runtime-lock":
        result = release_runtime_workflow_lock(
            aws,
            receipt=_load_json(args.lock),
        )
    elif args.command == "first-time-versioning-cutover":
        result = first_time_versioning_cutover(
            aws,
            lock_id=args.lock_id,
            lock_receipt=_load_json(args.lock_receipt),
            bedrock_config_path=args.bedrock_config.resolve(strict=True),
        )
    elif args.command == "fetch-s3-export":
        result = fetch_exact_s3_export(
            aws,
            bucket=args.bucket,
            key=args.key,
            version_id=args.version_id,
            output_path=args.export_path,
            observation_epoch=args.observation_epoch,
        )
    elif args.command == "rehash-export":
        binding = _load_json(args.binding)
        file_binding = binding.get("file", binding)
        if not isinstance(file_binding, Mapping):
            raise ContractError("export binding has no file object")
        result = verify_file_binding(file_binding)
    elif args.command == "verify-s3-export":
        result = verify_exact_s3_export(
            aws,
            binding=_load_json(args.binding),
            fresh_directory=args.fresh_dir.resolve(strict=True),
        )
    elif args.command == "build-log-readiness":
        result = build_log_readiness(
            aws,
            spec=_load_json(args.spec),
            versioning_receipt=_load_json(args.versioning_receipt),
            versioning_receipt_sha256=sha256_bytes(
                args.versioning_receipt.read_bytes()
            ),
            export_directory=args.export_dir.resolve(strict=True),
            retention_path=args.retention_output,
            evidence_path=args.evidence_output,
            receipt_path=args.receipt_output,
        )
    elif args.command == "verify-sns-ack":
        result = verify_recipient_ack(
            aws,
            challenge=_load_json(args.challenge),
            ack=_load_json(args.ack),
            now_epoch=args.now_epoch,
        )
    elif args.command == "issue-sns-challenge":
        result = issue_sns_challenge(aws)
    elif args.command == "sign-sns-ack":
        result = sign_recipient_ack(
            aws,
            challenge=_load_json(args.challenge),
        )
    elif args.command == "attest-sns-delivery":
        result = attest_sns_delivery(
            aws,
            challenge=_load_json(args.challenge),
            ack=_load_json(args.ack),
        )
    elif args.command == "verify-sns-delivery":
        result = verify_sns_delivery_receipt(
            aws,
            challenge=_load_json(args.challenge),
            ack=_load_json(args.ack),
            receipt=_load_json(args.receipt),
        )
    elif args.command == "verify-versioning-cutover":
        result = verify_versioning_cutover_live(
            aws,
            _load_json(args.workflow),
        )
    elif args.command == "verify-bedrock-retention":
        result = verify_bedrock_retention_live(aws)
    elif args.command == "advance-alarm-migration":
        result = advance_alarm_migration(
            aws,
            migration_id=args.migration_id,
            phase=args.phase,
            publisher_id=args.publisher_id,
            delivery_receipt=(
                _load_json(args.delivery_receipt)
                if args.delivery_receipt is not None
                else None
            ),
            lock_receipt=_load_json(args.lock_receipt),
        )
    elif args.command == "verify-alarm-migration-final":
        result = verify_alarm_migration_final(
            aws,
            migration_id=args.migration_id,
            phase_receipt=_load_json(args.receipt),
        )
    else:  # pragma: no cover
        raise AssertionError(args.command)
    if args.command == "build-log-readiness":
        return 0
    _write_new_json(args.output, result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as exc:
        print(f"FATAL: {exc}", file=os.sys.stderr)
        raise SystemExit(1) from exc
