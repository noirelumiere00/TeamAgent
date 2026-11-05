"""CLI と本番経路で metadata・位置・本文の根拠 hash が一致する。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gdrive_client import (
    GDriveClient,
    SharedDrive,
    drive_file_from_metadata,
)
from teamagent.ingest import case_state_evidence as evidence
from teamagent.ingest import case_state_source_ref as cli
from teamagent.ingest import pipeline
from teamagent.ingest.loader import GDriveFolderSpec, IngestSources, SharedDriveCrawlSpec
from tests.ingest.test_case_state_evidence import official_review


@pytest.fixture
def drive_input(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, IngestSources, dict[str, Any]]:
    monkeypatch.setenv("INGEST_OWNER_EMAIL", "ingest@example.test")
    monkeypatch.setenv("USE_CONTEXTUAL_INGEST", "0")
    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "0")
    monkeypatch.setenv("USE_UNCHANGED_SKIP", "0")
    monkeypatch.delenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", raising=False)
    raw = {
        "id": "FILE",
        "name": "提案書.pdf",
        "mimeType": "application/pdf",
        "modifiedTime": "2026-09-11T00:00:00Z",
        "size": "4096",
        "md5Checksum": "ABCDEF",
        "parents": ["CHILD"],
        "driveId": "DRIVE",
        "webViewLink": "https://drive.google.com/file/d/FILE/view",
    }
    rows = {
        "FILE": raw,
        "CHILD": {"id": "CHILD", "name": "案件", "parents": ["FOLDER"]},
        "FOLDER": {"id": "FOLDER", "name": "資料", "parents": ["DRIVE"]},
        "DRIVE": {"id": "DRIVE", "name": "営業"},
    }
    client = MagicMock(spec=GDriveClient)
    client.get_file_metadata.side_effect = lambda file_id, *a, **kw: rows[file_id]
    client.list_permissions.return_value = []
    client.download_file_bytes.return_value = b"content"
    client.list_shared_drives.return_value = [SharedDrive(id="DRIVE", name="営業")]
    client.walk_files_recursive.side_effect = lambda *a, **kw: [drive_file_from_metadata(raw)]
    sources = IngestSources(
        version=1,
        slack_channels=(),
        gsheets=(),
        gdrive_folders=(
            GDriveFolderSpec(
                folder_id="FOLDER",
                folder_name="資料",
                description="",
                include_subfolders=True,
                extra_metadata={"topic": "提案", "priority": "high", "custom": 123},
            ),
        ),
    )
    return client, sources, raw


@pytest.mark.parametrize("route", ["folder", "crawl"])
@pytest.mark.parametrize(
    "mime",
    [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ],
)
@pytest.mark.parametrize("rich", ["0", "1"])
def test_case_state_cli_matches_pipeline_attach_source_ref(
    drive_input: tuple[MagicMock, IngestSources, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    route: str,
    mime: str,
    rich: str,
) -> None:
    from dataclasses import replace

    from structlog.testing import capture_logs

    client, sources, raw = drive_input
    raw["mimeType"] = mime
    monkeypatch.setenv("INGEST_RICH_EXTRACT", rich)
    pages = [(1, "提案の本文" * 100), (2, "見送りの確認用の引用" * 100)]
    monkeypatch.setattr("teamagent.ingest.pdf_extract.extract_pdf_pages", lambda *a, **kw: pages)
    monkeypatch.setattr(
        "teamagent.ingest.office_extract.extract_office_pages", lambda *a, **kw: pages
    )
    monkeypatch.setattr(GDriveClient, "from_env", lambda **kw: client)
    classifier = MagicMock()
    classifier.classify.return_value.as_metadata.return_value = {
        "cls_doc_type": "提案書",
        "cls_industry": "旅行",
        "industry": "旅行",
        "cls_phase": "提案",
    }
    monkeypatch.setattr("teamagent.ingest.classify.build_classifier_from_env", lambda: classifier)
    monkeypatch.setattr(
        "teamagent.ingest.contextualize.build_contextualizer_from_env", lambda: None
    )
    if route == "crawl":
        sources = replace(
            sources,
            shared_drives_crawl=SharedDriveCrawlSpec(
                enabled=True,
                modified_within_days=None,
                extra_metadata={"topic": "共有資料", "custom": "other"},
            ),
        )
    doc, body = cli.prepare_source("FILE", client=client, sources=sources, request_id="cli")
    assert all(chunk.embedding == [] for chunk in body)
    ref = evidence.source_ref(doc, body)
    assert all(not key.startswith("cls_") for key in doc.metadata)
    client.get_file_metadata.assert_any_call("FILE", "cli", for_ingest=True)
    # 通常経路では分類・embedding を実行し、古い確認表でも upsert まで進む。
    review = official_review(doc, body)
    review["record"]["source_ref"]["content_sha256"] = "0" * 64
    manifest = tmp_path / "reviews.json"
    manifest.write_text(json.dumps({"version": 1, "reviews": [review]}))
    monkeypatch.setenv("CASE_STATE_EVIDENCE_ALLOWED_EMAILS", "reader@example.test")
    monkeypatch.setenv("CASE_STATE_REVIEW_MANIFEST", str(manifest))
    embedder = MagicMock()
    embedder.embed_passage.return_value = [0.1]
    embedder.embed_passage_batch.side_effect = lambda texts: [[0.1] for _ in texts]
    repo = MagicMock()
    repo.find_invalid_source_reason.return_value = None
    with capture_logs() as logs:
        if route == "folder":
            counts = pipeline._process_one_gdrive_file(
                drive_file_from_metadata(raw),
                sources.gdrive_folders[0],
                client=client,
                repository=repo,
                embedder=embedder,
                owner_email="ingest@example.test",
                dry_run=False,
                request_id="r",
                skipped=[],
                classifier=classifier,
            )
        else:
            assert sources.shared_drives_crawl is not None
            counts = pipeline._ingest_shared_drives_crawl(
                sources.shared_drives_crawl,
                repository=repo,
                embedder=embedder,
                owner_email="ingest@example.test",
                dry_run=False,
                request_id="r",
            )
    assert counts == (1, len(body))
    actual_doc, actual_chunks = repo.upsert_document_with_chunks.call_args.args[:2]
    assert actual_doc.metadata[evidence.KEY]["source_ref"] == ref.model_dump(mode="json")
    assert evidence.source_ref(actual_doc, actual_chunks) == ref
    assert actual_doc.metadata[evidence.KEY]["records"] == []
    assert any(log["event"] == "case_state_review_mismatch" for log in logs)
    classifier.classify.assert_called_once()
    # CLI の stdout は JSON だけ。分類器・embedder・DB を構築しない。
    classifier.reset_mock()
    monkeypatch.setattr(cli, "load_ingest_sources", lambda path: sources)
    monkeypatch.setattr(sys, "argv", ["case_state_source_ref", "FILE"])
    capsys.readouterr()
    cli.main()
    output = json.loads(capsys.readouterr().out)
    assert {key: output[key] for key in ref.model_dump()} == ref.model_dump(mode="json")
    assert {p["locator"] for p in output["positions"]} >= {
        "document",
        "chunk:0",
        "page:1",
        "page:2",
    }
    assert all(len(p["excerpt"]) <= 120 for p in output["positions"])
    classifier.classify.assert_not_called()


def test_case_state_ingest_metadata_includes_hash_inputs() -> None:
    service = MagicMock()
    service.files.return_value.get.return_value.execute.return_value = {"id": "FILE"}
    GDriveClient(service=service).get_file_metadata("FILE", "r", for_ingest=True)
    fields = service.files.return_value.get.call_args.kwargs["fields"]
    for name in ("modifiedTime", "size", "md5Checksum", "owners(emailAddress)"):
        assert name in fields


@pytest.mark.parametrize("failure", ["excluded", "unconfigured", "extract", "cycle"])
def test_case_state_cli_rejects_incomplete_source(
    drive_input: tuple[MagicMock, IngestSources, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from dataclasses import replace

    client, sources, raw = drive_input
    if failure == "excluded":
        sources = replace(sources, gdrive_exclude_folder_name_re="案件")
    elif failure == "unconfigured":
        sources = replace(sources, gdrive_folders=())
    elif failure == "extract":
        monkeypatch.setattr("teamagent.ingest.pdf_extract.extract_pdf_pages", lambda *a, **kw: [])
    else:
        raw["parents"] = ["FILE"]
    with pytest.raises(ValueError):
        cli.prepare_source("FILE", client=client, sources=sources, request_id="r")
