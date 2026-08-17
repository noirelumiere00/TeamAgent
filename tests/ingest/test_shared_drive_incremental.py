"""共有ドライブcrawlのper-drive cursor増分同期を検証する。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gdrive_client import (
    ChangeBatch,
    DriveChange,
    DriveFile,
    SharedDrive,
)
from teamagent.ingest.loader import SharedDriveCrawlSpec
from teamagent.ingest.office_extract import OFFICE_VALIDATOR_SCHEMA_VERSION
from teamagent.ingest.repository import ConnectorState


class _Embedder:
    def embed_passage(self, text: str) -> list[float]:
        return [float(len(text))]


class _SharedDriveRepository:
    def __init__(
        self,
        state: ConnectorState | None = None,
        *,
        load_error: Exception | None = None,
    ) -> None:
        self.state = state
        self.load_error = load_error
        self.load_calls: list[tuple[str, str]] = []
        self.saved_states: list[dict[str, Any]] = []
        self.upsert_calls: list[str] = []

    def get_document_classification_metadata(
        self,
        document_keys: list[tuple[str, str]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        return {}

    def load_connector_state(self, source_kind: str, source_id: str) -> ConnectorState | None:
        self.load_calls.append((source_kind, source_id))
        if self.load_error is not None:
            raise self.load_error
        return self.state

    def save_connector_state(
        self,
        source_kind: str,
        source_id: str,
        *,
        cursor: str | None = None,
        oldest: float | None = None,
        revision: int | None = None,
        success: bool = True,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.saved_states.append(
            {
                "source_kind": source_kind,
                "source_id": source_id,
                "cursor": cursor,
                "success": success,
                "metadata": dict(metadata or {}),
            }
        )

    def upsert_document_with_chunks(
        self,
        doc: Any,
        chunks: list[Any],
        request_id: str,
        *,
        replace_existing_chunks: bool = True,
    ) -> str:
        self.upsert_calls.append(doc.external_id)
        return "document-id"


def _file(file_id: str) -> DriveFile:
    return DriveFile(
        id=file_id,
        name=f"{file_id}.png",
        mime_type="image/png",
        modified_time="2026-08-01T00:00:00Z",
        size=100,
        web_view_link=f"https://drive.google.com/file/d/{file_id}/view",
        owners_email=("owner@example.com",),
        md5_checksum=(file_id.lower() * 32)[:32],
    )


def _client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock()
    client.list_shared_drives.return_value = [SharedDrive(id="DRIVE-1", name="Sales")]
    client.walk_files_recursive.return_value = [_file("F1"), _file("F2")]
    client.list_permissions.return_value = []
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: client),
    )
    monkeypatch.setattr(
        "teamagent.ingest.classify.build_classifier_from_env",
        lambda: None,
    )
    monkeypatch.setattr(
        "teamagent.ingest.contextualize.build_contextualizer_from_env",
        lambda: None,
    )
    monkeypatch.delenv("INGEST_RICH_EXTRACT", raising=False)
    return client


def _run(repository: _SharedDriveRepository) -> tuple[int, int]:
    from teamagent.ingest.pipeline import _ingest_shared_drives_crawl

    return _ingest_shared_drives_crawl(
        SharedDriveCrawlSpec(enabled=True, sales_relevance_filter=False),
        embedder=_Embedder(),
        repository=repository,  # type: ignore[arg-type]
        owner_email="owner@example.com",
        dry_run=False,
        request_id="request",
    )


def test_shared_drive_first_run_full_scans_then_saves_seed_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "true")
    client = _client(monkeypatch)
    client.get_start_page_token.return_value = "SEED"
    repository = _SharedDriveRepository()

    assert _run(repository) == (2, 2)

    assert repository.upsert_calls == ["F1", "F2"]
    assert repository.load_calls == [("shared_drives", "DRIVE-1")]
    assert repository.saved_states[0]["source_kind"] == "shared_drives"
    assert repository.saved_states[0]["source_id"] == "DRIVE-1"
    assert repository.saved_states[0]["cursor"] == "SEED"
    client.get_changes.assert_not_called()
    # seedはfull walkを始める前に取得する。
    call_names = [mock_call[0] for mock_call in client.mock_calls]
    assert call_names.index("get_start_page_token") < call_names.index("walk_files_recursive")


def test_shared_drive_second_run_processes_only_changed_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "true")
    client = _client(monkeypatch)
    client.get_changes.return_value = ChangeBatch(
        changes=(DriveChange("file", "F1", False, None, "DRIVE-1"),),
        new_start_page_token="NEXT",
    )
    repository = _SharedDriveRepository(
        ConnectorState(
            source_kind="shared_drives",
            source_id="DRIVE-1",
            cursor="PRIOR",
            metadata={
                "office_validator_schema_version": OFFICE_VALIDATOR_SCHEMA_VERSION,
            },
        )
    )

    assert _run(repository) == (1, 1)

    assert repository.upsert_calls == ["F1"]
    assert repository.saved_states[0]["cursor"] == "NEXT"
    client.get_changes.assert_called_once_with(page_token="PRIOR", request_id="request")
    client.get_start_page_token.assert_not_called()


def test_shared_drive_cursor_load_failure_fails_open_to_full_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "true")
    client = _client(monkeypatch)
    client.get_start_page_token.return_value = "RECOVERED-SEED"
    repository = _SharedDriveRepository(load_error=RuntimeError("state unavailable"))

    assert _run(repository) == (2, 2)

    assert repository.upsert_calls == ["F1", "F2"]
    client.get_changes.assert_not_called()
    client.get_start_page_token.assert_called_once_with("request")
    assert repository.saved_states[0]["cursor"] == "RECOVERED-SEED"


def test_shared_drive_changes_failure_fails_open_to_full_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "true")
    client = _client(monkeypatch)
    client.get_changes.side_effect = RuntimeError("changes unavailable")
    client.get_start_page_token.return_value = "FAIL-OPEN-SEED"
    repository = _SharedDriveRepository(
        ConnectorState(
            source_kind="shared_drives",
            source_id="DRIVE-1",
            cursor="PRIOR",
            metadata={
                "office_validator_schema_version": OFFICE_VALIDATOR_SCHEMA_VERSION,
            },
        )
    )

    assert _run(repository) == (2, 2)

    assert repository.upsert_calls == ["F1", "F2"]
    client.get_changes.assert_called_once_with(page_token="PRIOR", request_id="request")
    client.get_start_page_token.assert_called_once_with("request")
    assert repository.saved_states[0]["cursor"] == "FAIL-OPEN-SEED"


# -----------------------------------------------------------
# walk 上限到達 drive の skip（2026-08-17: 巨大 drive が crawl 全体を
# 道連れにして shared_drives が毎日 0 件になった本番障害の回帰）
# -----------------------------------------------------------
def test_shared_drive_walk_truncated_skips_only_that_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.ingest.pipeline import (
        _ingest_shared_drives_crawl,
        _IngestWarningCollector,
    )

    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "true")
    client = _client(monkeypatch)
    client.get_start_page_token.return_value = "SEED"
    client.list_shared_drives.return_value = [
        SharedDrive(id="GIANT", name="ドライブ"),
        SharedDrive(id="SMALL", name="Sales"),
    ]

    def _walk(*, root_id: str, **kwargs: Any) -> list[DriveFile]:
        if root_id == "GIANT":
            # max_files=2 ちょうどまで集まった＝完走と打ち切りを区別できない状態。
            return [_file("G1"), _file("G2")]
        return [_file("S1")]

    client.walk_files_recursive.side_effect = _walk
    repository = _SharedDriveRepository()
    truncated: set[str] = set()
    observed: set[str] = set()
    collector = _IngestWarningCollector()

    docs_n, chunks_n = _ingest_shared_drives_crawl(
        SharedDriveCrawlSpec(enabled=True, sales_relevance_filter=False, max_files_per_drive=2),
        embedder=_Embedder(),
        repository=repository,  # type: ignore[arg-type]
        owner_email="owner@example.com",
        dry_run=False,
        request_id="request",
        observed_gdrive_ids=observed,
        truncated_walk_roots=truncated,
        warning_collector=collector,
    )

    # 巨大 drive は 1 件も取り込まず、後続 drive は普通に取り込む。
    assert (docs_n, chunks_n) == (1, 1)
    assert repository.upsert_calls == ["S1"]
    # stale ガードへの伝搬: 打ち切り drive が登録され、部分列挙は観測集合に入らない。
    assert truncated == {"GIANT"}
    assert observed == {"S1"}
    # 打ち切り drive の cursor は保存されない（次回 run で必ず再 walk）。
    saved_ids = [s["source_id"] for s in repository.saved_states]
    assert "GIANT" not in saved_ids
    assert "SMALL" in saved_ids
    # source warning として記録され outcome が success_with_warnings に落ちる。
    snapshot = collector.snapshot("shared_drives", "shared_drives")
    assert snapshot.reasons.get("walk_truncated") == 1
