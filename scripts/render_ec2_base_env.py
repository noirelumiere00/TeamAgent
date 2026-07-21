#!/usr/bin/env python3
"""Render the EC2 base environment from an explicit non-secret allowlist."""

from __future__ import annotations

import argparse
import math
import os
import re
import shlex
import stat
from collections import Counter
from pathlib import Path


class BaseEnvironmentError(ValueError):
    """The proposed base environment is not safe to place on the worker."""


# Values for these names are non-secret configuration or identifiers. Runtime credentials are
# loaded by scripts/load_secrets.sh and are intentionally absent.
NONSECRET_KEYS = frozenset(
    {
        "AGENT_LOOP_MAX_ITERATIONS",
        "ANALYSIS_CACHE_BUCKET",
        "ANALYSIS_CACHE_ENABLED",
        "ANALYSIS_CACHE_PREFIX",
        "APP_ENV",
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "BEDROCK_HAIKU_MODEL_ID",
        "BEDROCK_MODEL_ID",
        "BEDROCK_RERANK_MODEL_ARN",
        "CHITCHAT_PROMPT_VERSION",
        "CHROMIUM_PATH",
        "CLOUDWATCH_LOG_GROUP",
        "COHERE_EMBED_MODEL_ID",
        "CONNECT_APP_HTML_S3_URI",
        "CONNECT_GOOGLE_CLIENT_ID",
        "CONNECT_SLACK_CLIENT_ID",
        "CONNECT_WEB_HOST",
        "CONNECT_WEB_PORT",
        "COST_GUARD_TABLE",
        "DRAFT_PROMPT_VERSION",
        "EMBEDDING_BACKEND",
        "EMBEDDING_BATCH_SIZE",
        "EMBEDDING_COLUMN",
        "EMBEDDING_MODEL",
        "FEATURE_DRIVE_INGEST",
        "FEATURE_GMAIL_INGEST",
        "FEATURE_PROPOSAL_GEN",
        "FEATURE_SLACK_INGEST",
        "FEATURE_VIDEO_ANALYSIS",
        "GEMINI_MODEL_ID",
        "GEMINI_RETRY_MAX_ATTEMPTS",
        "GEMINI_USE_VERTEX",
        "GEMINI_VERTEX_LOCATION",
        "GEMINI_VERTEX_PROJECT",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_FORCE_OAUTH",
        "GOOGLE_GMAIL_IMPERSONATE_USER",
        "GOOGLE_OAUTH_SCOPES",
        "GOOGLE_REDIRECT_URI",
        "GOOGLE_SERVICE_ACCOUNT_JSON_PATH",
        "GOOGLE_WORKSPACE_DOMAIN",
        "GRAPH_CONCEPT_EDGES",
        "HAIKU_INPUT_USD_PER_MTOK",
        "HAIKU_OUTPUT_USD_PER_MTOK",
        "HTTPS_PROXY",
        "IMPORTANT_SENDERS",
        "KARTE_PROMPT_VERSION",
        "LOCAL_EMBED_MODEL",
        "LOG_LEVEL",
        "MAIL_ACTION_TTL_S",
        "MKL_NUM_THREADS",
        "OAUTH_KMS_KEY_ID",
        "OAUTH_KMS_REGION",
        "OAUTH_REDIRECT_URI",
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "OPLOG_PROMPT_VERSION",
        "PGVECTOR_DIMENSIONS",
        "PG_CONNECT_TIMEOUT_S",
        "PG_IDLE_TX_TIMEOUT_MS",
        "PG_LOCK_TIMEOUT_MS",
        "PG_STATEMENT_TIMEOUT_MS",
        "PROMPT_VERSION",
        "RDS_DBNAME",
        "RDS_HOST",
        "RDS_PORT",
        "RDS_SSL_MODE",
        "RDS_USER",
        "REPORT_LINK_TTL_S",
        "REQUEST_GATE_ACQUIRE_TIMEOUT_S",
        "REQUEST_GATE_CONCURRENCY",
        "REQUEST_GATE_QUEUE_MAX",
        "RUNTIME_EXECUTOR_WORKERS",
        "S3_BUCKET_PROPOSALS",
        "S3_BUCKET_RAW_FILES",
        "S3_BUCKET_VIDEOS",
        "SCRAPE_ALLOWED_DOMAINS",
        "SEARCH_MAX_TOKENS",
        "SLACK_OAUTH_REDIRECT_URI",
        "SLACK_TEAM_ID",
        "SLACK_WORKSPACE",
        "SLACK_WORKSPACE_ID",
        "SONNET_INPUT_USD_PER_MTOK",
        "SONNET_OUTPUT_USD_PER_MTOK",
        "STRUCTLOG_FORMAT",
        "TEAMAGENT_FMT_TEMPLATE",
        "TIKTOK_JOBS_TABLE",
        "TIKTOK_NODE_BIN",
        "TIKTOK_S3_BUCKET",
        "TIKTOK_SESSIONS",
        "TIKTOK_TASK_QUEUE",
        "USE_CONTEXTUAL",
        "USE_LLM_ROUTER",
        "USE_MAIL_LINK_SUMMARY",
        "USE_OPERATION_LOG_TOOLS",
        "USE_PROPOSAL_DECK_PUBLISH",
        "USE_PROPOSAL_DECK_TOOLS",
        "USE_VIDEO_APPROVAL_POLLING",
        "VERTEX_SA_PATH",
        "VIDEO_ALGO_MAX_VIDEOS",
        "VIDEO_ALGO_PROMPT_VERSION",
        "VIDEO_APPROVAL_CLIENT_NAME",
        "VIDEO_APPROVAL_POLL_CHANNEL",
        "VIDEO_APPROVAL_POLL_INTERVAL_SEC",
        "VIDEO_APPROVAL_PROMPT_VERSION",
        "VIDEO_APPROVAL_SHEET_ID",
        "VIDEO_APPROVAL_STATE_PATH",
        "VIDEO_DL_ORDER",
        "VIDEO_MONTHLY_QUOTA",
        "VIDEO_PROMPT_VERSION",
        "VIDEO_QUOTA_ENABLED",
        "VIDEO_UPLOAD_CONCURRENCY",
        "VSEO_REPORT_BUCKET",
        "VSEO_REPORT_PREFIX",
        "WORKSPACE_DOMAIN",
        "X_JOBS_TABLE",
        "X_S3_BUCKET",
        "X_TASK_QUEUE",
    }
)

# These values are references only. Their payloads are fetched after startup into process memory or
# root-only materialization paths.
SECRET_REFERENCE_KEYS = frozenset(
    {
        "CONNECT_GOOGLE_CLIENT_SECRET_NAME",
        "CONNECT_SLACK_CLIENT_SECRET_NAME",
        "DB_PASSWORD_SECRET_NAME",
        "GOOGLE_OAUTH_SECRET_NAME",
        "OAUTH_STATE_SECRET_NAME",
        "OPS_SLACK_WEBHOOK_SECRET_NAME",
        "SENTRY_DSN_SECRET_NAME",
        "SLACK_APP_TOKEN_SECRET_NAME",
        "SLACK_BOT_TOKEN_SECRET_NAME",
        "VERTEX_SA_SECRET_NAME",
    }
)
FILE_REFERENCE_KEYS = frozenset(
    {
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_SERVICE_ACCOUNT_JSON_PATH",
        "VERTEX_SA_PATH",
    }
)
REFERENCE_KEYS = SECRET_REFERENCE_KEYS | FILE_REFERENCE_KEYS
OPAQUE_IDENTIFIER_KEYS = frozenset(
    {
        "BEDROCK_HAIKU_MODEL_ID",
        "BEDROCK_MODEL_ID",
        "BEDROCK_RERANK_MODEL_ARN",
        "CHITCHAT_PROMPT_VERSION",
        "COHERE_EMBED_MODEL_ID",
        "CONNECT_GOOGLE_CLIENT_ID",
        "CONNECT_SLACK_CLIENT_ID",
        "DRAFT_PROMPT_VERSION",
        "EMBEDDING_MODEL",
        "GEMINI_MODEL_ID",
        "KARTE_PROMPT_VERSION",
        "LOCAL_EMBED_MODEL",
        "OAUTH_KMS_KEY_ID",
        "OPLOG_PROMPT_VERSION",
        "PROMPT_VERSION",
        "SLACK_TEAM_ID",
        "SLACK_WORKSPACE_ID",
        "VIDEO_ALGO_PROMPT_VERSION",
        "VIDEO_APPROVAL_PROMPT_VERSION",
        "VIDEO_APPROVAL_SHEET_ID",
        "VIDEO_PROMPT_VERSION",
    }
)

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_REFERENCE_RE = re.compile(
    r"^(?:teamagent/(?:dev|prod)/[A-Za-z0-9/_+=.@-]{1,220}"
    r"|arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]{12}:"
    r"secret:teamagent/(?:dev|prod)/[A-Za-z0-9/_+=.@-]{1,220})$"
)
_FILE_REFERENCE_RE = re.compile(
    r"^/(?:opt/teamagent/(?:current|secrets)(?:/[A-Za-z0-9._-]+)+"
    r"|run/teamagent(?:/[A-Za-z0-9._-]+)+"
    r"|usr/(?:local/)?bin/[A-Za-z0-9._-]+)$"
)
_OPAQUE_VALUE_RE = re.compile(r"^[A-Za-z0-9_+=-]{32,4096}$")
_SECRET_KEY_FRAGMENT_RE = re.compile(
    r"(?:^|_)(?:ACCESS_KEY|API_KEY|CREDENTIALS?|DSN|PASSWORD|PRIVATE_KEY|SECRET|TOKEN|WEBHOOK)"
    r"(?:_|$)"
)
_SECRET_VALUE_RES = (
    re.compile(r"xox[baprs]-", re.IGNORECASE),
    re.compile(r"xapp-", re.IGNORECASE),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"sk-(?:ant-)?[0-9A-Za-z_-]{16,}", re.IGNORECASE),
    re.compile(r"https://hooks\.slack\.com/services/", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----"),
    re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/@:\s]+:[^/@\s]+@"),
)


def _looks_like_opaque_secret(value: str) -> bool:
    if _OPAQUE_VALUE_RE.fullmatch(value) is None or len(set(value)) < 10:
        return False
    counts = Counter(value)
    entropy = -sum(
        (count / len(value)) * math.log2(count / len(value)) for count in counts.values()
    )
    return entropy >= 3.5


def _assignment(line: str, *, source: Path, line_number: int) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    try:
        tokens = shlex.split(stripped, comments=True, posix=True)
    except ValueError as exc:
        raise BaseEnvironmentError(f"{source}:{line_number}: invalid quoting") from exc
    if len(tokens) != 1 or "=" not in tokens[0]:
        raise BaseEnvironmentError(f"{source}:{line_number}: one literal assignment is required")
    name, value = tokens[0].split("=", 1)
    if name == "export":
        raise BaseEnvironmentError(f"{source}:{line_number}: export syntax is forbidden")
    if _KEY_RE.fullmatch(name) is None:
        raise BaseEnvironmentError(f"{source}:{line_number}: invalid environment name")
    if any(char in value for char in ("\0", "\n", "\r", "`")) or "$(" in value or "${" in value:
        raise BaseEnvironmentError(f"{source}:{line_number}: shell expansion is forbidden")
    return name, value


def _validate(name: str, value: str) -> None:
    if name in SECRET_REFERENCE_KEYS:
        if _REFERENCE_RE.fullmatch(value) is None or value.startswith(("/", ".", "-")):
            raise BaseEnvironmentError(f"{name} is not a bounded secret reference")
    elif name in FILE_REFERENCE_KEYS:
        if _FILE_REFERENCE_RE.fullmatch(value) is None:
            raise BaseEnvironmentError(f"{name} is not a bounded private-file reference")
    elif name not in NONSECRET_KEYS:
        if _SECRET_KEY_FRAGMENT_RE.search(name):
            raise BaseEnvironmentError(f"{name} is a secret-like key")
        raise BaseEnvironmentError(f"{name} is not allowlisted")
    if _SECRET_KEY_FRAGMENT_RE.search(name) and name not in REFERENCE_KEYS:
        raise BaseEnvironmentError(f"{name} is a secret-like key")
    if any(pattern.search(value) for pattern in _SECRET_VALUE_RES):
        raise BaseEnvironmentError(f"{name} contains a secret-like value")
    if (
        name not in REFERENCE_KEYS
        and name not in OPAQUE_IDENTIFIER_KEYS
        and _looks_like_opaque_secret(value)
    ):
        raise BaseEnvironmentError(f"{name} contains an opaque secret-like value")
    if len(value.encode("utf-8")) > 4096:
        raise BaseEnvironmentError(f"{name} is too large")


def render(paths: tuple[Path, ...]) -> bytes:
    """Merge allowlisted assignments in source order and return shell-safe bytes."""

    values: dict[str, str] = {}
    order: list[str] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise BaseEnvironmentError(f"{path}: unreadable environment") from exc
        seen_in_source: set[str] = set()
        for line_number, line in enumerate(lines, start=1):
            parsed = _assignment(line, source=path, line_number=line_number)
            if parsed is None:
                continue
            name, value = parsed
            if name in seen_in_source:
                raise BaseEnvironmentError(f"{path}:{line_number}: duplicate {name}")
            seen_in_source.add(name)
            _validate(name, value)
            if name not in values:
                order.append(name)
            values[name] = value
    return "".join(f"{name}={shlex.quote(values[name])}\n" for name in order).encode("utf-8")


def write_private(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o777 != 0o600
            ):
                raise BaseEnvironmentError("private environment permissions changed")
    except (OSError, BaseEnvironmentError) as exc:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            path.unlink()
        except OSError:
            pass
        if isinstance(exc, BaseEnvironmentError):
            raise
        raise BaseEnvironmentError("private environment could not be created") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--override", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        write_private(args.output, render((args.base, *args.override)))
    except BaseEnvironmentError:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
