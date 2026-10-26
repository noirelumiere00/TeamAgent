"""Fake-only job guards, durable checkpoints and stored Slack user credentials."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest
from botocore.exceptions import ClientError

from teamagent.adapters import oauth_token_store, pgvector_client
from teamagent.slack2drive import job
from teamagent.slack2drive.transfer import Transfer, TransferError

HASH = hashlib.sha256(b"fake document").hexdigest()
LEDGER_URI = "s3://test-bucket/slack2drive/progress/batch.json"
LOCK_NAME = "slack2drive/progress/transfer.lock"


def row(file_id: str = "FTEST1", **changes: Any) -> dict[str, Any]:
    return {
        "slack_file_id": file_id,
        "drive_filename": f"{file_id}_report.pdf",
        "approval_status": "approved",
        **changes,
    }


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.writes: list[dict[str, Any]] = []
        self.deletes: list[str] = []
        self.fail_save = False

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        names = sorted(name for name in self.objects if name.startswith(kwargs["Prefix"]))
        return {"Contents": [{"Key": name} for name in names[: kwargs["MaxKeys"]]]}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}

    def put_object(self, **kwargs: Any) -> None:
        self.writes.append(kwargs)
        if kwargs.get("IfNoneMatch") == "*" and kwargs["Key"] in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        if self.fail_save and kwargs["Key"] != LOCK_NAME:
            raise RuntimeError("simulated storage failure")
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def delete_object(self, **kwargs: Any) -> None:
        self.deletes.append(kwargs["Key"])
        self.objects.pop(kwargs["Key"], None)


class JobTransfer(Transfer):
    """Exercise real Transfer.one; replace only external I/O and body hashing."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.reads: list[str] = []
        self.uploads: list[str] = []
        self.reservations = 0
        self.on_upload: Callable[[], None] = lambda: None

    def destination(self, folder_id: str) -> None:
        assert folder_id == "folder-id"

    def inventory(self) -> list[dict[str, Any]]:
        return list(self.files.values())

    def info(self, file_id: str) -> dict[str, Any]:
        self.reads.append(file_id)
        return {"id": file_id, "size": 13, "mimetype": "application/pdf"}

    def digest(self, file: dict[str, Any]) -> str:
        return hashlib.sha256(file["id"].encode()).hexdigest()

    def metadata(self, file_id: str) -> dict[str, Any] | None:
        return self.files.get(file_id)

    def drive(self, method: str, url: str = "", **kwargs: Any) -> httpx.Response:
        assert method == "GET" and url.endswith("/generateIds")
        self.reservations += 1
        return httpx.Response(200, json={"ids": [f"reserved-{self.reservations}"]})

    def upload(
        self, file: dict[str, Any], entry: dict[str, Any], state: dict[str, Any], folder_id: str
    ) -> dict[str, Any]:
        self.uploads.append(file["id"])
        result = {
            "id": state["drive_id"],
            "size": file["size"],
            "sha256Checksum": state["sha256"],
            "name": entry["drive_filename"],
        }
        self.files[result["id"]] = result
        self.on_upload()
        return result


@pytest.fixture(autouse=True)
def isolated_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_DOCS_TO_DRIVE_ENABLED", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    monkeypatch.setattr(logging, "disable", lambda *args: None)
    monkeypatch.setattr(
        boto3, "client", lambda *args, **kwargs: pytest.fail("unexpected AWS client")
    )
    monkeypatch.setattr(
        job, "credentials", lambda *args: pytest.fail("unexpected credential access")
    )


@pytest.mark.parametrize("wrapped", [False, True])
def test_load_json(wrapped: bool) -> None:
    rows = [row()]
    assert job.load_rows(json.dumps({"records": rows} if wrapped else rows).encode()) == rows


def test_load_csv_with_bom() -> None:
    rows = job.load_rows(
        "\ufeffslack_file_id,drive_filename,approval_status\nFTEST1,report.pdf,approved\n".encode(),
        csv_input=True,
    )
    assert rows == [row(drive_filename="report.pdf")]


@pytest.mark.parametrize(
    ("rows", "error"),
    [
        ([], "invalid_manifest"),
        ([None], "invalid_manifest_row"),
        ([row("invalid")], "invalid_slack_id"),
        ([row(), row()], "duplicate_slack_id"),
        ([row(drive_filename="../escape.pdf")], "invalid_drive_filename"),
        ([row(drive_filename="a\n.pdf")], "invalid_drive_filename"),
        ([row(destination="other")], "invalid_destination"),
        ([row(sha256="invalid")], "invalid_sha256"),
    ],
)
def test_manifest_rejects_invalid_rows(rows: list[Any], error: str) -> None:
    with pytest.raises(TransferError, match=error):
        job.load_rows(json.dumps(rows).encode())


def test_manifest_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job, "MAX_MANIFEST", 4)
    with pytest.raises(TransferError, match="manifest_too_large"):
        job.load_rows(b"12345")


def test_default_dry_run_does_not_write_or_reserve() -> None:
    transfer = JobTransfer()
    checkpoint: dict[str, Any] = {}
    assert job.run(transfer, [row()], checkpoint, lambda: pytest.fail("save"), "folder-id") == 0
    assert checkpoint["files"]["FTEST1"]["status"] == "would_upload"
    assert transfer.reservations == 0 and not transfer.uploads


def test_commit_is_off_by_default() -> None:
    transfer = JobTransfer()
    with pytest.raises(TransferError, match="feature_disabled"):
        job.run(transfer, [row()], {}, lambda: None, "folder-id", commit=True)
    assert not transfer.reads


def test_commit_requires_all_rows_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_DOCS_TO_DRIVE_ENABLED", "1")
    with pytest.raises(TransferError, match="unapproved_manifest"):
        job.run(
            JobTransfer(),
            [row(approval_status="pending")],
            {},
            lambda: None,
            "folder-id",
            commit=True,
        )


def test_s3_roundtrip_and_exclusive_lock() -> None:
    s3 = FakeS3()
    ledger = job.S3Ledger(s3, LEDGER_URI)
    assert ledger.read(optional=True) is None
    with ledger.lock():
        ledger.save({"files": {"FTEST1": {"status": "saved"}}})
        assert json.loads(ledger.read() or b"")["files"]["FTEST1"]["status"] == "saved"
        with pytest.raises(TransferError, match="ledger_locked_or_unwritable"):
            with job.S3Ledger(s3, LEDGER_URI).lock():
                pytest.fail("second task acquired the lock")
        assert LOCK_NAME in s3.objects and not s3.deletes
    assert s3.deletes == [LOCK_NAME]
    assert s3.writes[0]["IfNoneMatch"] == "*"
    assert all(write["ServerSideEncryption"] == "AES256" for write in s3.writes)


def test_lock_released_on_job_failure() -> None:
    s3 = FakeS3()
    with pytest.raises(RuntimeError, match="job failure"):
        with job.S3Ledger(s3, LEDGER_URI).lock():
            raise RuntimeError("job failure")
    assert LOCK_NAME not in s3.objects


@pytest.mark.parametrize(
    ("object_name", "error"),
    [("other/batch.json", "invalid_checkpoint_prefix"), (LOCK_NAME, "checkpoint_is_lock")],
)
def test_lock_rejects_unsafe_checkpoint_location(object_name: str, error: str) -> None:
    s3 = FakeS3()
    with pytest.raises(TransferError, match=error):
        with job.S3Ledger(s3, f"s3://test-bucket/{object_name}").lock():
            pytest.fail("invalid lock was acquired")
    assert not s3.writes


def test_resume_and_limit_keep_completed_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_DOCS_TO_DRIVE_ENABLED", "1")
    s3 = FakeS3()
    ledger = job.S3Ledger(s3, LEDGER_URI)
    transfer = JobTransfer()
    rows = [row("FTEST1"), row("FTEST2")]
    checkpoint: dict[str, Any] = {}
    assert (
        job.run(
            transfer,
            rows,
            checkpoint,
            lambda: ledger.save(checkpoint),
            "folder-id",
            commit=True,
            limit=1,
        )
        == 0
    )
    resumed = json.loads(ledger.read() or b"")
    assert (
        job.run(
            transfer,
            rows,
            resumed,
            lambda: ledger.save(resumed),
            "folder-id",
            commit=True,
            limit=1,
        )
        == 0
    )
    assert transfer.uploads == ["FTEST1", "FTEST2"]
    assert all(state["verified"] for state in resumed["files"].values())
    assert all(state["attempts"] == 1 for state in resumed["files"].values())


def test_changed_completed_file_blocks_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_DOCS_TO_DRIVE_ENABLED", "1")
    checkpoint = {
        "files": {"FTEST1": {"verified": True, "drive_id": "missing", "size": 13, "sha256": HASH}}
    }
    with pytest.raises(TransferError, match="completed_drive_file_changed"):
        job.run(JobTransfer(), [row()], checkpoint, lambda: None, "folder-id", commit=True)


def test_stop_after_file_checkpoints_then_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_DOCS_TO_DRIVE_ENABLED", "1")
    transfer = JobTransfer()
    s3 = FakeS3()
    ledger = job.S3Ledger(s3, LEDGER_URI)
    checkpoint: dict[str, Any] = {}
    assert (
        job.run(
            transfer,
            [row("FTEST1"), row("FTEST2")],
            checkpoint,
            lambda: ledger.save(checkpoint),
            "folder-id",
            commit=True,
            stopped=lambda: bool(transfer.uploads),
        )
        == 2
    )
    assert transfer.uploads == ["FTEST1"]
    assert json.loads(ledger.read() or b"")["files"]["FTEST1"]["verified"] is True


def test_failed_checkpoint_prevents_drive_create(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_DOCS_TO_DRIVE_ENABLED", "1")
    s3 = FakeS3()
    s3.fail_save = True
    ledger = job.S3Ledger(s3, LEDGER_URI)
    transfer = JobTransfer()
    checkpoint: dict[str, Any] = {}
    with pytest.raises(TransferError, match="checkpoint_write_failed"):
        job.run(
            transfer,
            [row()],
            checkpoint,
            lambda: ledger.save(checkpoint),
            "folder-id",
            commit=True,
        )
    assert transfer.reservations == 1
    assert not transfer.uploads


def manifest_file(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps([row()]))
    return path


def test_validate_only_never_reads_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = manifest_file(tmp_path)
    assert (
        job.main(["--manifest", str(manifest), "--folder-id", "folder-id", "--validate-only"]) == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "records": 1,
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize("remote", ["manifest", "ledger"])
def test_validate_only_rejects_remote_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote: str
) -> None:
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: pytest.fail("AWS client created"))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([row()]))
    args = ["--manifest", str(manifest), "--folder-id", "folder-id", "--validate-only"]
    if remote == "manifest":
        args[1] = "s3://test-bucket/manifest.json"
    else:
        args.extend(["--ledger-s3", LEDGER_URI])
    assert job.main(args) == 1


def test_cli_default_dry_run_has_no_remote_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s3 = FakeS3()
    transfer = JobTransfer()
    modes: list[str] = []

    def fake_credentials(owner: str, mode: str) -> tuple[str, Callable[[], dict[str, str]]]:
        modes.append(mode)
        return "test-credential", lambda: {}

    monkeypatch.setattr(job, "credentials", fake_credentials)
    monkeypatch.setattr(boto3, "client", lambda service: s3)
    monkeypatch.setattr(job, "Transfer", lambda *args, **kwargs: transfer)
    assert (
        job.main(
            [
                "--manifest",
                str(manifest_file(tmp_path)),
                "--folder-id",
                "folder-id",
                "--ledger-s3",
                LEDGER_URI,
            ]
        )
        == 0
    )
    assert modes == ["user"]
    assert not s3.writes and not s3.deletes and not transfer.uploads


def test_cli_approval_hash_must_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SLACK_DOCS_TO_DRIVE_ENABLED", "1")
    monkeypatch.setattr(boto3, "client", lambda service: FakeS3())
    assert (
        job.main(
            [
                "--manifest",
                str(manifest_file(tmp_path)),
                "--folder-id",
                "folder-id",
                "--commit",
                "--ledger-s3",
                LEDGER_URI,
                "--approved-manifest-sha256",
                "0" * 64,
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().err) == {"error": "manifest_approval_mismatch"}


def test_slack_defaults_to_stored_user_token(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    class Store:
        def __init__(self, db: Any, cipher: Any) -> None:
            pass

        def scopes(self, owner: str) -> tuple[str, ...]:
            calls.append(("scopes", owner))
            return ("files:read",)

        def get(self, owner: str) -> oauth_token_store.SlackOAuthToken:
            calls.append(("get", owner))
            return oauth_token_store.SlackOAuthToken("test-user-credential", ("files:read",))

    monkeypatch.setattr(pgvector_client.PgVectorClient, "from_env", lambda: object())
    monkeypatch.setattr(oauth_token_store, "SlackTokenStore", Store)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-bot-credential")
    assert job.slack_credential("owner@example.test") == "test-user-credential"
    assert calls == [("scopes", "owner@example.test"), ("get", "owner@example.test")]


@pytest.mark.parametrize("scopes", [None, (), ("search:read",)])
def test_missing_files_scope_refuses_before_decrypt(
    scopes: tuple[str, ...] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Store:
        def __init__(self, db: Any, cipher: Any) -> None:
            pass

        def scopes(self, owner: str) -> tuple[str, ...] | None:
            return scopes

        def get(self, owner: str) -> None:
            pytest.fail("must not decrypt an unconsented token")

    monkeypatch.setattr(pgvector_client.PgVectorClient, "from_env", lambda: object())
    monkeypatch.setattr(oauth_token_store, "SlackTokenStore", Store)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-bot-credential")
    with pytest.raises(TransferError, match="slack_reconnect_required"):
        job.slack_credential("owner@example.test")


def test_explicit_bot_does_not_open_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pgvector_client.PgVectorClient,
        "from_env",
        lambda: pytest.fail("unexpected database access"),
    )
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-bot-credential")
    assert job.slack_credential("owner@example.test", "bot") == "test-bot-credential"


def test_bot_without_injected_credential_refuses() -> None:
    with pytest.raises(TransferError, match="slack_bot_unconfigured"):
        job.slack_credential("owner@example.test", "bot")
