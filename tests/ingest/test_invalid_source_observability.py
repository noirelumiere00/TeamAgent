"""Drive Office invalid payload の全経路・再試行・warning状態回帰テスト。"""

from __future__ import annotations

import hashlib
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from teamagent.adapters.gdrive_client import DriveFile, DrivePermission, SharedDrive
from teamagent.ingest.loader import (
    GDriveFolderSpec,
    IngestSources,
    SharedDriveCrawlSpec,
)
from teamagent.ingest.office_extract import PPTX_MIME
from teamagent.ingest.ops_alert import IngestOpsAlerter
from teamagent.ingest.pipeline import (
    ChunkUpsert,
    DocumentUpsert,
    IngestRunner,
    _guarded_upsert,
    _ingest_gdrive_folder,
    _ingest_shared_drives_crawl,
    _IngestWarningCollector,
    _process_one_gdrive_file,
    _send_ops_warning_summary,
)


class _Embedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024

    def embed_passage(self, text: str) -> list[float]:
        return self.embed(text)


class _Repository:
    def __init__(
        self,
        known: set[tuple[str, str, int]] | None = None,
    ) -> None:
        self.known = known or set()
        self.lookup_calls: list[tuple[str, str, str | None, int | None]] = []
        self.invalid_records: list[dict[str, Any]] = []
        self.upserts: list[tuple[Any, list[Any]]] = []
        self.saved_states: list[dict[str, Any]] = []
        self.connector_runs: list[dict[str, Any]] = []

    def find_invalid_source_reason(
        self,
        source_type: str,
        external_id: str,
        md5_checksum: str | None,
        size_bytes: int | None,
    ) -> str | None:
        self.lookup_calls.append((source_type, external_id, md5_checksum, size_bytes))
        if md5_checksum is not None and size_bytes is not None:
            if (external_id, md5_checksum, size_bytes) in self.known:
                return "corrupt_zip"
        return None

    def record_invalid_source(
        self,
        source_type: str,
        external_id: str,
        **kwargs: Any,
    ) -> bool:
        self.invalid_records.append(
            {"source_type": source_type, "external_id": external_id, **kwargs}
        )
        return True

    def upsert_document_with_chunks(
        self,
        doc: Any,
        chunks: list[Any],
        request_id: str,
        *,
        replace_existing_chunks: bool = True,
    ) -> str:
        self.upserts.append((doc, list(chunks)))
        return "doc-id"

    def load_connector_state(self, source_kind: str, source_id: str) -> None:
        return None

    def save_connector_state(self, source_kind: str, source_id: str, **kwargs: Any) -> None:
        self.saved_states.append({"source_kind": source_kind, "source_id": source_id, **kwargs})

    def record_ingest_job(self, *args: Any, **kwargs: Any) -> None:
        return None

    def record_connector_run(self, **kwargs: Any) -> bool:
        self.connector_runs.append(kwargs)
        return True


def _pptx_bytes() -> bytes:
    from pptx import Presentation

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "safe content"
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _file(
    file_id: str,
    data: bytes,
    *,
    md5_checksum: str | None = None,
) -> DriveFile:
    return DriveFile(
        id=file_id,
        name="confidential.pptx",
        mime_type=PPTX_MIME,
        modified_time="2026-07-17T00:00:00Z",
        size=len(data),
        md5_checksum=md5_checksum or hashlib.md5(data, usedforsecurity=False).hexdigest(),
        web_view_link=f"https://drive.google.com/file/d/{file_id}/view",
        owners_email=("owner@example.jp",),
    )


def _owner_permission() -> DrivePermission:
    return DrivePermission(
        id="permission",
        type="user",
        role="owner",
        email_address="owner@example.jp",
    )


def _folder_spec() -> GDriveFolderSpec:
    return GDriveFolderSpec(
        folder_id="FOLDER",
        folder_name="folder",
        description="",
        mime_type_filter=None,
    )


def test_corrupt_payload_is_persisted_without_document_mutation_or_identifier_logs() -> None:
    broken = b"PK\x03\x04" + (b"x" * 64)
    drive_file = _file("SENSITIVE-DRIVE-ID", broken)
    client = MagicMock()
    client.list_permissions.return_value = [_owner_permission()]
    client.download_file_bytes.return_value = broken
    repo = _Repository()
    collector = _IngestWarningCollector()
    skipped: list[str] = []

    with capture_logs() as logs:
        result = _process_one_gdrive_file(
            drive_file,
            _folder_spec(),
            client=client,
            embedder=_Embedder(),
            repository=repo,  # type: ignore[arg-type]
            owner_email="bot@example.jp",
            dry_run=False,
            request_id="req",
            skipped=skipped,
            warning_collector=collector,
            warning_source_id="FOLDER",
        )

    assert result == (0, 0)
    assert repo.upserts == []
    assert len(repo.invalid_records) == 1
    assert repo.invalid_records[0]["reason"] == "corrupt_zip"
    assert repo.invalid_records[0]["md5_checksum"] == drive_file.md5_checksum
    warning = next(log for log in logs if log["event"] == "gdrive_office_payload_invalid")
    assert warning["existing_document_preserved"] is True
    assert "file_id" not in warning
    assert "file_name" not in warning
    assert drive_file.id not in str(warning)
    snapshot = collector.snapshot("gdrive", "FOLDER")
    assert snapshot.reasons == {"corrupt_zip": 1}


def test_known_invalid_suppresses_acl_and_body_download_and_touches_observation() -> None:
    broken = b"PK\x03\x04" + (b"x" * 64)
    drive_file = _file("KNOWN", broken)
    repo = _Repository(known={("KNOWN", str(drive_file.md5_checksum), len(broken))})
    client = MagicMock()
    collector = _IngestWarningCollector()

    result = _process_one_gdrive_file(
        drive_file,
        _folder_spec(),
        client=client,
        embedder=_Embedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@example.jp",
        dry_run=False,
        request_id="req",
        skipped=[],
        warning_collector=collector,
        warning_source_id="FOLDER",
    )

    assert result == (0, 0)
    client.list_permissions.assert_not_called()
    client.download_file_bytes.assert_not_called()
    assert len(repo.invalid_records) == 1
    snapshot = collector.snapshot("gdrive", "FOLDER")
    assert snapshot.reasons == {"corrupt_zip": 1}
    assert snapshot.suppressed == 1


@pytest.mark.parametrize("change", ["md5", "size"])
def test_fingerprint_change_forces_revalidation(change: str) -> None:
    data = _pptx_bytes()
    drive_file = _file("RECOVERED", data)
    current_md5 = str(drive_file.md5_checksum)
    known_md5 = "0" * 32 if change == "md5" else current_md5
    known_size = len(data) if change == "md5" else len(data) - 1
    repo = _Repository(known={("RECOVERED", known_md5, known_size)})
    client = MagicMock()
    client.list_permissions.return_value = [_owner_permission()]
    client.download_file_bytes.return_value = data

    result = _process_one_gdrive_file(
        drive_file,
        _folder_spec(),
        client=client,
        embedder=_Embedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@example.jp",
        dry_run=False,
        request_id="req",
        skipped=[],
    )

    assert result[0] == 1
    client.download_file_bytes.assert_called_once()
    assert len(repo.upserts) == 1
    assert repo.lookup_calls[-1][2:] == (current_md5, len(data))


def test_checksum_mismatch_is_warning_but_not_cached_as_known_invalid() -> None:
    data = _pptx_bytes()
    drive_file = _file("TRANSIENT", data, md5_checksum="0" * 32)
    repo = _Repository()
    client = MagicMock()
    client.list_permissions.return_value = [_owner_permission()]
    client.download_file_bytes.return_value = data

    result = _process_one_gdrive_file(
        drive_file,
        _folder_spec(),
        client=client,
        embedder=_Embedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@example.jp",
        dry_run=False,
        request_id="req",
        skipped=[],
    )
    assert result == (0, 0)
    assert repo.invalid_records == []
    assert repo.upserts == []


def test_office_download_failure_is_nonpersistent_connector_warning() -> None:
    data = _pptx_bytes()
    drive_file = _file("DOWNLOAD-FAIL", data)
    repo = _Repository()
    client = MagicMock()
    client.list_permissions.return_value = [_owner_permission()]
    client.download_file_bytes.side_effect = TimeoutError("temporary")
    collector = _IngestWarningCollector()

    result = _process_one_gdrive_file(
        drive_file,
        _folder_spec(),
        client=client,
        embedder=_Embedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@example.jp",
        dry_run=False,
        request_id="req",
        skipped=[],
        warning_collector=collector,
        warning_source_id="FOLDER",
    )
    assert result == (0, 0)
    assert repo.invalid_records == []
    assert collector.snapshot("gdrive", "FOLDER").reasons == {"office_download_failed": 1}


def test_shared_drive_known_invalid_is_observed_and_suppressed_before_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = b"PK\x03\x04" + (b"x" * 64)
    drive_file = _file("SHARED-KNOWN", broken)
    repo = _Repository(known={("SHARED-KNOWN", str(drive_file.md5_checksum), len(broken))})
    client = MagicMock()
    client.list_shared_drives.return_value = [SharedDrive(id="DRIVE", name="drive")]
    client.walk_files_recursive.return_value = [drive_file]
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: client),
    )
    observed: set[str] = set()

    result = _ingest_shared_drives_crawl(
        SharedDriveCrawlSpec(enabled=True, sales_relevance_filter=False),
        embedder=_Embedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@example.jp",
        dry_run=False,
        request_id="req",
        observed_gdrive_ids=observed,
        warning_collector=_IngestWarningCollector(),
    )

    assert result == (0, 0)
    assert observed == {"SHARED-KNOWN"}
    client.list_permissions.assert_not_called()
    client.download_file_bytes.assert_not_called()
    assert repo.upserts == []


def test_incremental_cursor_records_success_with_warnings_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "1")
    broken = b"PK\x03\x04" + (b"x" * 64)
    drive_file = _file("INCREMENTAL-KNOWN", broken)
    repo = _Repository(known={("INCREMENTAL-KNOWN", str(drive_file.md5_checksum), len(broken))})
    client = MagicMock()
    client.list_files.return_value = ([drive_file], None)
    client.get_start_page_token.return_value = "NEXT"
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: client),
    )

    result = _ingest_gdrive_folder(
        _folder_spec(),
        embedder=_Embedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@example.jp",
        dry_run=False,
        request_id="req",
        warning_collector=_IngestWarningCollector(),
    )

    assert result == (0, 0)
    saved = repo.saved_states[-1]
    assert saved["success"] is True
    assert saved["metadata"]["outcome"] == "success_with_warnings"
    assert saved["metadata"]["warning_reasons"] == {"corrupt_zip": 1}
    assert saved["metadata"]["known_invalid_suppressed"] == 1


def test_runner_records_connector_warning_outcome_and_notifies_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def warning_handler(spec: Any, **kwargs: Any) -> tuple[int, int]:
        collector = kwargs["warning_collector"]
        collector.add("gdrive", spec.folder_id, "corrupt_zip", suppressed=True)
        return 0, 0

    monkeypatch.setattr("teamagent.ingest.pipeline._ingest_gdrive_folder", warning_handler)
    monkeypatch.setattr(
        "teamagent.ingest.pipeline.IngestRunner._maybe_check_freshness",
        lambda self, *, request_id: None,
    )
    repo = _Repository()
    alerter = IngestOpsAlerter(webhook_url="https://hooks.slack.test/ingest")
    runner = IngestRunner(
        repository=repo,  # type: ignore[arg-type]
        embedder=_Embedder(),
        owner_email="bot@example.jp",
        dry_run=False,
        alerter=alerter,
    )
    sources = IngestSources(
        version=1,
        slack_channels=(),
        gdrive_folders=(_folder_spec(),),
        gsheets=(),
    )

    with patch("httpx.post") as mock_post:
        mock_post.return_value.status_code = 200
        result = runner.run(sources, kinds=["gdrive"])

    stats = result.by_kind["gdrive"]
    assert stats.outcome == "success_with_warnings"
    assert stats.warning_reasons == {"corrupt_zip": 1}
    assert result.outcome == "success_with_warnings"
    assert repo.connector_runs[0]["outcome"] == "success_with_warnings"
    assert repo.connector_runs[0]["warning_reasons"] == {"corrupt_zip": 1}
    mock_post.assert_called_once()
    rendered_notification = str(mock_post.call_args.kwargs["json"])
    assert "success_with_warnings" in rendered_notification
    assert "corrupt_zip" in rendered_notification
    assert "file_id" not in rendered_notification
    assert "file_name" not in rendered_notification


def test_warning_notification_failure_is_fail_open_and_dry_run_is_noop() -> None:
    alerter = IngestOpsAlerter(webhook_url="https://hooks.slack.test/ingest")
    with patch("httpx.post", side_effect=RuntimeError("network unavailable")):
        assert (
            _send_ops_warning_summary(
                alerter,
                kind="gdrive",
                warning_reasons={"corrupt_zip": 1},
                suppressed_retry_count=0,
                request_id="req",
                dry_run=False,
            )
            is False
        )
    with patch("httpx.post") as mock_post:
        assert (
            _send_ops_warning_summary(
                alerter,
                kind="gdrive",
                warning_reasons={"corrupt_zip": 1},
                suppressed_retry_count=0,
                request_id="req",
                dry_run=True,
            )
            is False
        )
    mock_post.assert_not_called()


def test_pipeline_title_only_guard_consults_database_when_process_registry_is_empty() -> None:
    class _GuardRepository:
        def __init__(self) -> None:
            self.title_guard_calls = 0

        def upsert_title_only_if_no_content(
            self,
            doc: Any,
            chunks: list[Any],
            request_id: str,
        ) -> None:
            self.title_guard_calls += 1
            return None

        def upsert_document_with_chunks(self, *args: Any, **kwargs: Any) -> str:
            raise AssertionError("unguarded replacement must not run")

    repo = _GuardRepository()
    doc = DocumentUpsert(
        source_type="gdrive",
        external_id="EXISTING",
        owner_email="bot@example.jp",
    )
    chunks = [
        ChunkUpsert(
            chunk_idx=0,
            content="title",
            embedding=[0.1] * 4,
            metadata={"title_only": True},
        )
    ]
    assert (
        _guarded_upsert(
            repo,  # type: ignore[arg-type]
            doc,
            chunks,
            request_id="req",
            content_registry=set(),
        )
        is False
    )
    assert repo.title_guard_calls == 1
