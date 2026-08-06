"""取り込み高速化のバッチ/fingerprintガードを実DBなしで検証する。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gdrive_client import DriveFile
from teamagent.ingest.loader import GDriveFolderSpec


class _SequentialEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [float(len(text)), float(sum(ord(char) for char in text))]

    def embed_passage(self, text: str) -> list[float]:
        self.calls.append(text)
        return self._vector(text)


class _BatchEmbedder(_SequentialEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.batch_calls: list[list[str]] = []

    def embed_passage_batch(self, texts: list[str]) -> list[list[float]]:
        self.batch_calls.append(list(texts))
        return [self._vector(text) for text in texts]


def test_embed_page_chunks_batches_with_same_result_and_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.ingest.pipeline import _embed_page_chunks

    monkeypatch.setenv("EMBED_BATCH_SIZE", "2")
    page_chunks = [(1, "alpha"), (1, "beta"), (2, "gamma"), (3, "delta"), (3, "omega")]
    batch_embedder = _BatchEmbedder()
    sequential_embedder = _SequentialEmbedder()
    heartbeats: list[bool] = []

    batched = _embed_page_chunks(
        page_chunks,
        embedder=batch_embedder,
        lease_heartbeat=heartbeats.append,
    )
    sequential = _embed_page_chunks(page_chunks, embedder=sequential_embedder)

    assert batched == sequential
    assert batch_embedder.batch_calls == [
        ["alpha", "beta"],
        ["gamma", "delta"],
        ["omega"],
    ]
    assert batch_embedder.calls == []
    # 各batchの直前・直後にlease更新機会を作る。
    assert heartbeats == [False, False] * 3


def test_embed_page_chunks_without_batch_api_keeps_sequential_path() -> None:
    from teamagent.ingest.pipeline import _embed_page_chunks

    embedder = _SequentialEmbedder()
    heartbeats: list[bool] = []
    chunks = _embed_page_chunks(
        [(1, "one"), (2, "two")],
        embedder=embedder,
        lease_heartbeat=heartbeats.append,
    )

    assert [chunk.embedding for chunk in chunks] == [
        _SequentialEmbedder._vector("one"),
        _SequentialEmbedder._vector("two"),
    ]
    assert embedder.calls == ["one", "two"]
    assert heartbeats == [False, False]


class _ChecksumRepository:
    def __init__(self, stored_checksum: str | None) -> None:
        self.stored_checksum = stored_checksum
        self.lookup_calls: list[tuple[str, str]] = []
        self.upsert_calls: list[Any] = []

    def get_document_checksum(self, source_type: str, external_id: str) -> str | None:
        self.lookup_calls.append((source_type, external_id))
        return self.stored_checksum

    def upsert_document_with_chunks(
        self,
        doc: Any,
        chunks: list[Any],
        request_id: str,
        *,
        replace_existing_chunks: bool = True,
    ) -> str:
        self.upsert_calls.append((doc, chunks, request_id))
        return "document-id"


def _drive_file(*, checksum: str | None, native: bool = False) -> DriveFile:
    return DriveFile(
        id="FILE-1",
        name="proposal",
        mime_type=("application/vnd.google-apps.presentation" if native else "application/pdf"),
        modified_time="2026-08-01T00:00:00Z",
        size=None if native else 123,
        web_view_link="https://drive.google.com/file/d/FILE-1/view",
        owners_email=("owner@example.com",),
        md5_checksum=checksum,
    )


def _process_file(
    monkeypatch: pytest.MonkeyPatch,
    *,
    repository: _ChecksumRepository,
    drive_file: DriveFile,
) -> tuple[tuple[int, int], MagicMock, _SequentialEmbedder, list[str]]:
    from teamagent.ingest.pipeline import _process_one_gdrive_file

    monkeypatch.delenv("INGEST_RICH_EXTRACT", raising=False)
    client = MagicMock()
    client.list_permissions.return_value = []
    client.download_file_bytes.return_value = b"pdf"
    embedder = _SequentialEmbedder()
    skipped: list[str] = []
    result = _process_one_gdrive_file(
        drive_file,
        GDriveFolderSpec(folder_id="FOLDER", folder_name="folder", description=""),
        client=client,
        embedder=embedder,
        repository=repository,  # type: ignore[arg-type]
        owner_email="owner@example.com",
        dry_run=False,
        request_id="request",
        skipped=skipped,
    )
    return result, client, embedder, skipped


def test_matching_md5_skips_before_download_extract_embed_and_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_UNCHANGED_SKIP", "true")
    checksum = "a" * 32
    repository = _ChecksumRepository(checksum)

    result, client, embedder, skipped = _process_file(
        monkeypatch,
        repository=repository,
        drive_file=_drive_file(checksum=checksum),
    )

    assert result == (0, 0)
    assert skipped == ["FILE-1"]
    assert repository.lookup_calls == [("gdrive", "FILE-1")]
    assert repository.upsert_calls == []
    client.list_permissions.assert_not_called()
    client.download_file_bytes.assert_not_called()
    assert embedder.calls == []


def test_changed_md5_is_processed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_UNCHANGED_SKIP", "true")
    repository = _ChecksumRepository("a" * 32)
    drive_file = _drive_file(checksum="b" * 32)
    # 抽出器だけstub化し、downloadからupsertまでの経路を通す。
    monkeypatch.setattr(
        "teamagent.ingest.pdf_extract.extract_pdf_pages",
        lambda data: [(1, "changed body")],
    )

    result, client, embedder, skipped = _process_file(
        monkeypatch,
        repository=repository,
        drive_file=drive_file,
    )

    assert result == (1, 1)
    assert skipped == []
    client.download_file_bytes.assert_called_once()
    assert embedder.calls == ["changed body"]
    assert len(repository.upsert_calls) == 1


def test_google_native_without_md5_is_always_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("USE_UNCHANGED_SKIP", "true")
    repository = _ChecksumRepository("a" * 32)

    result, _client, embedder, skipped = _process_file(
        monkeypatch,
        repository=repository,
        drive_file=_drive_file(checksum=None, native=True),
    )

    assert result == (1, 1)
    assert skipped == []
    assert repository.lookup_calls == []
    assert embedder.calls == [
        "proposal (application/vnd.google-apps.presentation)",
    ]
    assert len(repository.upsert_calls) == 1


def test_unchanged_skip_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_UNCHANGED_SKIP", "false")
    checksum = "a" * 32
    repository = _ChecksumRepository(checksum)
    monkeypatch.setattr(
        "teamagent.ingest.pdf_extract.extract_pdf_pages",
        lambda data: [(1, "body")],
    )

    result, _client, embedder, skipped = _process_file(
        monkeypatch,
        repository=repository,
        drive_file=_drive_file(checksum=checksum),
    )

    assert result == (1, 1)
    assert skipped == []
    assert repository.lookup_calls == []
    assert embedder.calls == ["body"]
    assert len(repository.upsert_calls) == 1
