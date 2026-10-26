"""One-shot job for the existing ECS ingest image; default OFF and read-only."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import signal
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from teamagent.slack2drive.transfer import Transfer, TransferError, require

MAX_MANIFEST = 16 * 1024 * 1024


def load_rows(body: bytes, csv_input: bool = False) -> list[dict[str, Any]]:
    require(len(body) <= MAX_MANIFEST, "manifest_too_large")
    rows: Any
    if csv_input:
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    else:
        payload = json.loads(body)
        rows = payload.get("records") if isinstance(payload, dict) else payload
    require(isinstance(rows, list) and bool(rows), "invalid_manifest")
    seen: set[str] = set()
    for row in rows:
        require(isinstance(row, dict), "invalid_manifest_row")
        file_id = row.get("slack_file_id", "")
        require(
            isinstance(file_id, str) and bool(re.fullmatch(r"F[A-Z0-9]+", file_id)),
            "invalid_slack_id",
        )
        require(file_id not in seen, "duplicate_slack_id")
        seen.add(file_id)
        name = row.get("drive_filename", "")
        require(
            isinstance(name, str)
            and 0 < len(name.encode("utf-8")) <= 255
            and not any(ord(c) < 32 or c in "/\\" for c in name),
            "invalid_drive_filename",
        )
        require(row.get("destination", "99_一次倉庫") == "99_一次倉庫", "invalid_destination")
        sha = row.get("sha256")
        require(
            sha in (None, "")
            or (isinstance(sha, str) and bool(re.fullmatch(r"[0-9a-f]{64}", sha))),
            "invalid_sha256",
        )
    return list(rows)


class S3Ledger:
    def __init__(self, client: Any, uri: str) -> None:
        require(uri.startswith("s3://"), "invalid_s3_uri")
        self.bucket, _, self.object_name = uri[5:].partition("/")
        require(bool(self.bucket and self.object_name), "invalid_s3_uri")
        self.client = client

    def read(self, *, optional: bool = False) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            if optional:
                found = self.client.list_objects_v2(
                    Bucket=self.bucket, Prefix=self.object_name, MaxKeys=1
                ).get("Contents", [])
                if not found or found[0]["Key"] != self.object_name:
                    return None
            response = self.client.get_object(Bucket=self.bucket, Key=self.object_name)
        except ClientError as exc:
            if optional and exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise TransferError("s3_read_failed") from None
        with response["Body"] as stream:
            body = stream.read(MAX_MANIFEST + 1)
        require(len(body) <= MAX_MANIFEST, "s3_object_too_large")
        return bytes(body)

    def save(self, state: dict[str, Any]) -> None:
        body = json.dumps(state, ensure_ascii=False, sort_keys=True).encode()
        require(len(body) <= MAX_MANIFEST, "checkpoint_too_large")
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self.object_name,
                Body=body,
                ContentType="application/json",
                ServerSideEncryption="AES256",
            )
        except Exception:
            # Fail the whole job: a failed checkpoint must never be treated as a file failure.
            raise TransferError("checkpoint_write_failed") from None

    @contextmanager
    def lock(self) -> Iterator[None]:
        require(self.object_name.startswith("slack2drive/progress/"), "invalid_checkpoint_prefix")
        lock_name = "slack2drive/progress/transfer.lock"
        require(self.object_name != lock_name, "checkpoint_is_lock")
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=lock_name,
                Body=uuid.uuid4().hex.encode(),
                IfNoneMatch="*",
                ServerSideEncryption="AES256",
            )
        except Exception:
            raise TransferError("ledger_locked_or_unwritable") from None
        try:
            yield
        finally:
            # No expiry: after SIGKILL an operator must confirm the task stopped before removal.
            self.client.delete_object(Bucket=self.bucket, Key=lock_name)


def run(
    transfer: Transfer,
    rows: list[dict[str, Any]],
    checkpoint: dict[str, Any],
    save: Callable[[], None],
    folder_id: str,
    *,
    commit: bool = False,
    limit: int = 10,
    stopped: Callable[[], bool] = lambda: False,
) -> int:
    require(1 <= limit <= 100, "invalid_limit")
    if commit:
        require(os.environ.get("SLACK_DOCS_TO_DRIVE_ENABLED") == "1", "feature_disabled")
        require(
            all(r.get("approval_status") in ("approved", "承認済み") for r in rows),
            "unapproved_manifest",
        )
    transfer.destination(folder_id)
    inventory = transfer.inventory()
    states = checkpoint.setdefault("files", {})
    processed = 0
    failures = 0
    for row in rows:
        if stopped():
            return 2
        state = states.setdefault(row["slack_file_id"], {})
        if state.get("verified"):
            existing = transfer.metadata(state["drive_id"])
            require(
                existing is not None and transfer.matches(existing, state["size"], state["sha256"]),
                "completed_drive_file_changed",
            )
            continue
        if processed >= limit:
            break
        processed += 1
        state["attempts"] = state.get("attempts", 0) + 1
        state["updated_at"] = datetime.now(UTC).isoformat()
        try:
            transfer.one(row, state, inventory, folder_id, save, commit=commit)
            if state["status"] in ("saved", "reused"):
                state["verified"] = True
        except TransferError as exc:
            if str(exc) == "checkpoint_write_failed":
                raise
            state.update(status="failed", error=str(exc))
            failures += 1
        except Exception:
            # SDK/HTTP exceptions may contain tokens, signed URLs or upstream bodies.
            state.update(status="failed", error="transfer_failed")
            failures += 1
        if commit:
            save()
        print(json.dumps({"slack_file_id": row["slack_file_id"], **state}), flush=True)
        if stopped():
            return 2
    return 1 if failures else 0


def slack_credential(owner_email: str, mode: str = "user") -> str:
    if mode == "bot":
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        require(bool(token), "slack_bot_unconfigured")
        return token
    require(mode == "user", "invalid_slack_auth")
    from teamagent.adapters.oauth_token_store import KmsCipher, SlackTokenStore
    from teamagent.adapters.pgvector_client import PgVectorClient

    store = SlackTokenStore(
        PgVectorClient.from_env(),
        KmsCipher(os.environ.get("OAUTH_KMS_KEY_ID", "alias/teamagent-oauth-tokens")),
    )
    scopes = store.scopes(owner_email)
    require(scopes is not None and "files:read" in scopes, "slack_reconnect_required")
    user_token = store.get(owner_email)
    require(user_token is not None, "slack_reconnect_required")
    assert user_token is not None
    require("files:read" in user_token.scopes, "slack_reconnect_required")
    return user_token.access_token


def credentials(owner_email: str, mode: str = "user") -> tuple[str, Callable[[], dict[str, str]]]:
    """Read secrets only inside the deployed task, never at import or validation time."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token = slack_credential(owner_email, mode)
    # Dedicated migration consent; never silently widen Aico's ordinary readonly OAuth.
    import boto3

    payload = json.loads(
        boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["SLACK2DRIVE_GOOGLE_OAUTH_SECRET_ARN"]
        )["SecretString"]
    )
    google = Credentials(  # type: ignore[no-untyped-call]
        token=None,
        refresh_token=payload["refresh_token"],
        client_id=payload["client_id"],
        client_secret=payload["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ],
    )

    def headers() -> dict[str, str]:
        result: dict[str, str] = {}
        google.before_request(Request(), "GET", "https://www.googleapis.com/", result)  # type: ignore[no-untyped-call]
        return result

    return token, headers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="JSON/CSV path or s3://bucket/object")
    parser.add_argument("--folder-id", required=True)
    parser.add_argument(
        "--ledger-s3", help="Dedicated immutable-batch checkpoint s3://bucket/object"
    )
    parser.add_argument("--owner-email", default="shogo@vectorinc.co.jp")
    parser.add_argument("--slack-auth", choices=("user", "bot"), default="user")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--approved-manifest-sha256")
    parser.add_argument("--commit", action="store_true", help="Otherwise dry-run: no remote writes")
    parser.add_argument(
        "--validate-only", action="store_true", help="Local manifest validation, no credentials/API"
    )
    args = parser.parse_args(argv)
    # Suppress third-party transport logs, including signed URLs and exception representations.
    logging.disable(logging.CRITICAL)
    stopped = False

    def stop(signum: int, frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        require(1 <= args.limit <= 100, "invalid_limit")
        if args.validate_only:
            require(
                not args.manifest.startswith("s3://") and not args.ledger_s3,
                "validate_only_requires_local_manifest",
            )
        if args.commit:
            require(os.environ.get("SLACK_DOCS_TO_DRIVE_ENABLED") == "1", "feature_disabled")
            require(bool(args.ledger_s3) and not args.validate_only, "commit_requires_s3_ledger")
        import boto3

        client = boto3.client("s3") if args.manifest.startswith("s3://") or args.ledger_s3 else None
        if args.manifest.startswith("s3://"):
            body = S3Ledger(client, args.manifest).read()
            assert body is not None
        else:
            with Path(args.manifest).open("rb") as stream:
                body = stream.read(MAX_MANIFEST + 1)
        rows = load_rows(body, args.manifest.lower().endswith(".csv"))
        manifest_sha = hashlib.sha256(body).hexdigest()
        if args.commit:
            require(args.approved_manifest_sha256 == manifest_sha, "manifest_approval_mismatch")
            require(
                all(r.get("approval_status") in ("approved", "承認済み") for r in rows),
                "unapproved_manifest",
            )
        if args.validate_only:
            print(json.dumps({"records": len(rows), "manifest_sha256": manifest_sha}))
            return 0
        token, drive_headers = credentials(args.owner_email, args.slack_auth)
        ledger = S3Ledger(client, args.ledger_s3) if args.ledger_s3 else None

        def execute() -> int:
            raw = ledger.read(optional=True) if ledger else None
            checkpoint = (
                json.loads(raw)
                if raw
                else {"manifest_sha256": manifest_sha, "folder_id": args.folder_id, "files": {}}
            )
            require(
                checkpoint.get("manifest_sha256") == manifest_sha
                and checkpoint.get("folder_id") == args.folder_id,
                "checkpoint_manifest_or_destination_changed",
            )
            checkpoint["run_id"] = uuid.uuid4().hex
            checkpoint["slack_auth"] = args.slack_auth

            def save() -> None:
                assert ledger is not None
                ledger.save(checkpoint)

            with httpx.Client(
                timeout=60, trust_env=False, headers={"Accept-Encoding": "identity"}
            ) as http:
                return run(
                    Transfer(http, token, drive_headers, stopped=lambda: stopped),
                    rows,
                    checkpoint,
                    save,
                    args.folder_id,
                    commit=args.commit,
                    limit=args.limit,
                    stopped=lambda: stopped,
                )

        if args.commit:
            assert ledger is not None
            with ledger.lock():
                return execute()
        return execute()
    except TransferError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    except Exception:
        print('{"error":"job_failed"}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
