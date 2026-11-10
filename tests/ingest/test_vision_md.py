"""Real PDF/PPTX extraction and Gemini SDK payloads; all external services are fake."""

from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pypdf import PdfReader, PdfWriter
from structlog.testing import capture_logs

from teamagent.adapters.gdrive_client import SharedDrive
from teamagent.adapters.gemini_client import GeminiClient
from teamagent.ingest.loader import SharedDriveCrawlSpec
from teamagent.ingest.office_extract import PPTX_MIME
from teamagent.ingest.pipeline import (
    _ingest_shared_drives_crawl,
    _process_one_gdrive_file,
)
from teamagent.ingest.repository import ConnectorState
from teamagent.ingest.vision_md import EXTRACTOR, VisionMDError, VisionMDRun
from tests.ingest.test_invalid_source_observability import (
    _Embedder,
    _file,
    _folder_spec,
    _owner_permission,
    _Repository,
)

MARKDOWN = "# 秘密の売上\n\n| 年 | 金額 |\n| --- | --- |\n| 2026 | 123万円 |\n\n（判読不能）"


def image_pdf(pages: int = 3) -> bytes:
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    writer = PdfWriter()
    for index in range(pages):
        page = writer.add_blank_page(width=600 + index, height=800)
        image = DecodedStreamObject()
        image.set_data(b"\xff\x00\x00")
        for key, value in {
            "/Type": NameObject("/XObject"),
            "/Subtype": NameObject("/Image"),
            "/Width": NumberObject(1),
            "/Height": NumberObject(1),
            "/ColorSpace": NameObject("/DeviceRGB"),
            "/BitsPerComponent": NumberObject(8),
        }.items():
            image[NameObject(key)] = value
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/XObject"): DictionaryObject(
                    {NameObject("/Im0"): writer._add_object(image)}
                )
            }
        )
        stream = DecodedStreamObject()
        stream.set_data(b"q 600 0 0 800 0 0 cm /Im0 Do Q")
        page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def image_pptx() -> tuple[bytes, bytes]:
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Inches

    image = BytesIO()
    Image.new("RGB", (30, 20), "red").save(image, format="PNG")
    picture = image.getvalue()
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])  # Empty slide must not be sent.
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    for index in range(2):
        slide.shapes.add_picture(BytesIO(picture), Inches(index), 0)
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    group.shapes.add_picture(BytesIO(picture), 0, 0)
    buffer = BytesIO()
    deck.save(buffer)
    return buffer.getvalue(), picture


@pytest.fixture
def gemini(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setenv("VISION_MD_ENABLED", "1")
    monkeypatch.setenv("VISION_MD_MAX_PAGES", "30")
    monkeypatch.setenv("VISION_MD_MAX_PER_RUN", "50")
    sdk = MagicMock()
    sdk.models.generate_content.return_value = SimpleNamespace(
        text=MARKDOWN,
        candidates=[SimpleNamespace(finish_reason="STOP")],
        usage_metadata=SimpleNamespace(prompt_token_count=700, candidates_token_count=100),
    )
    adapter = GeminiClient(api_key="fake-key", client=sdk, model_id="gemini-3.5-flash")
    monkeypatch.setattr(GeminiClient, "from_env", lambda: adapter)
    monkeypatch.setattr(
        "teamagent.adapters.gemini_client.call_with_retry", lambda fn, **kwargs: fn()
    )
    return sdk


class VisionRepository(_Repository):
    def __init__(self) -> None:
        super().__init__()
        self.states: dict[tuple[str, str], ConnectorState] = {}

    def load_connector_state(self, kind: str, source_id: str) -> ConnectorState | None:
        return self.states.get((kind, source_id))

    def save_connector_state(self, kind: str, source_id: str, **kwargs: Any) -> None:
        self.states[kind, source_id] = ConnectorState(kind, source_id, metadata=kwargs["metadata"])


def drive_run(repo: VisionRepository, client: MagicMock, f: Any, route: str) -> tuple[int, int]:
    if route == "folder":
        return _process_one_gdrive_file(
            f,
            _folder_spec(),
            client=client,
            embedder=_Embedder(),
            repository=repo,
            owner_email="bot@example.jp",
            dry_run=False,
            request_id="req",
            skipped=[],
        )
    return _ingest_shared_drives_crawl(
        SharedDriveCrawlSpec(enabled=True, sales_relevance_filter=False),
        embedder=_Embedder(),
        repository=repo,
        owner_email="bot@example.jp",
        dry_run=False,
        request_id="req",
    )


@pytest.fixture
def drive_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "0")
    monkeypatch.setenv("USE_UNCHANGED_SKIP", "0")
    monkeypatch.setattr("teamagent.ingest.classify.build_classifier_from_env", lambda: None)
    monkeypatch.setattr(
        "teamagent.ingest.contextualize.build_contextualizer_from_env", lambda: None
    )
    client = MagicMock()
    client.list_permissions.return_value = [_owner_permission()]
    client.list_shared_drives.return_value = [SharedDrive(id="DRIVE", name="drive")]
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env", lambda **k: client
    )
    return client


@pytest.mark.parametrize("route", ["folder", "crawl"])
@pytest.mark.parametrize("mime", ["application/pdf", PPTX_MIME])
def test_vision_empty_drive_is_saved_with_markdown_and_marker(
    gemini: MagicMock,
    drive_client: MagicMock,
    route: str,
    mime: str,
) -> None:
    data = image_pdf() if mime == "application/pdf" else image_pptx()[0]
    f = replace(_file("DOC", data), mime_type=mime)
    drive_client.download_file_bytes.return_value = data
    drive_client.walk_files_recursive.return_value = [f]
    repo = VisionRepository()
    with capture_logs() as logs:
        assert drive_run(repo, drive_client, f, route)[0] == 1
    doc, chunks = repo.upserts[0]
    assert doc.metadata["extractor"] == EXTRACTOR
    assert [chunk.content for chunk in chunks] == (
        [MARKDOWN] if mime == "application/pdf" else [MARKDOWN + "\n\n" + MARKDOWN, MARKDOWN]
    )
    assert gemini.models.generate_content.call_count == (1 if mime == "application/pdf" else 3)
    assert MARKDOWN not in str(logs) and f.name not in str(logs)
    assert "owner@example.jp" not in str(logs)
    assert any(log["event"] == "vision_md_done" and log["files"] == 1 for log in logs)


def test_vision_pdf_boundary_receives_only_first_pages(
    gemini: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISION_MD_MAX_PAGES", "2")
    with capture_logs() as logs:
        assert VisionMDRun().extract(image_pdf(4), "application/pdf", request_id="req") == [
            (1, MARKDOWN)
        ]
    kwargs = gemini.models.generate_content.call_args.kwargs
    assert kwargs["model"] == "gemini-3.5-flash"
    part = kwargs["contents"][0].parts[0].inline_data
    assert part.mime_type == "application/pdf"
    sent = PdfReader(BytesIO(part.data))
    assert len(sent.pages) == 2
    assert [int(p.mediabox.width) for p in sent.pages] == [600, 601]
    assert all(p.images and not p.extract_text() for p in sent.pages)
    assert kwargs["config"].response_mime_type is None
    done = next(log for log in logs if log["event"] == "vision_md_done")
    assert (done["pages"], done["input_tokens"], done["output_tokens"]) == (2, 700, 100)
    prompt = kwargs["contents"][0].parts[1].text
    assert all(
        text in prompt for text in ["そのまま", "Markdown の表", "（判読不能）", "要約しない"]
    )


def test_vision_pptx_boundary_has_order_slide_numbers_and_page_cap(
    gemini: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISION_MD_MAX_PAGES", "2")
    data, image = image_pptx()
    assert VisionMDRun().extract(data, PPTX_MIME, request_id="req") == [
        (2, MARKDOWN + "\n\n" + MARKDOWN)
    ]
    calls = gemini.models.generate_content.call_args_list
    assert len(calls) == 2
    for index, call in enumerate(calls, start=1):
        parts = call.kwargs["contents"][0].parts
        assert parts[0].inline_data.mime_type == "image/png"
        assert parts[0].inline_data.data == image
        assert f"スライド番号: 2、画像番号: {index}" in parts[1].text


@pytest.mark.parametrize("route", ["folder", "crawl"])
@pytest.mark.parametrize("mime", ["application/pdf", PPTX_MIME])
@pytest.mark.parametrize("failure", ["429", "empty", "truncated"])
def test_vision_drive_failure_is_versioned_and_logs_are_safe(
    gemini: MagicMock,
    drive_client: MagicMock,
    route: str,
    failure: str,
    mime: str,
) -> None:
    data = image_pdf() if mime == "application/pdf" else image_pptx()[0]
    f = replace(_file("DOC", data), mime_type=mime)
    drive_client.download_file_bytes.return_value = data
    drive_client.walk_files_recursive.return_value = [f]
    if failure == "429":
        error = RuntimeError(f"{MARKDOWN} confidential.pptx owner@example.jp")
        error.code = 429  # type: ignore[attr-defined]
        gemini.models.generate_content.side_effect = error
    elif failure == "empty":
        gemini.models.generate_content.return_value.text = "  "
    else:
        gemini.models.generate_content.return_value.candidates[0].finish_reason = "MAX_TOKENS"
    repo = VisionRepository()
    with capture_logs() as logs:
        assert drive_run(repo, drive_client, f, route) == (0, 0)
        # A fresh repository instance must retain versioned suppression.
        reloaded = VisionRepository()
        reloaded.states = dict(repo.states)
        assert drive_run(reloaded, drive_client, f, route) == (0, 0)
    assert not repo.upserts
    assert all("exc_info" not in log for log in logs if log["event"] == "gemini_generate_failed")
    assert gemini.models.generate_content.call_count == 1
    assert drive_client.download_file_bytes.call_count == 1
    expected = {
        "429": "rate_limited",
        "empty": "empty_response",
        "truncated": "incomplete_response",
    }[failure]
    assert any(log["event"] == "vision_md_failed" and log["reason"] == expected for log in logs)
    assert (
        MARKDOWN not in str(logs)
        and f.name not in str(logs)
        and "owner@example.jp" not in str(logs)
    )
    newer = replace(f, modified_time="2026-11-10T00:00:00Z")
    drive_client.walk_files_recursive.return_value = [newer]
    assert drive_run(reloaded, drive_client, newer, route) == (0, 0)
    assert gemini.models.generate_content.call_count == 2


def test_vision_run_limit_counts_failures_and_resets(
    gemini: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISION_MD_MAX_PER_RUN", "1")
    budget = VisionMDRun()
    gemini.models.generate_content.return_value.text = ""
    with pytest.raises(VisionMDError, match="empty_response"):
        budget.extract(image_pdf(), "application/pdf", request_id="req")
    with pytest.raises(VisionMDError, match="run_limit"):
        budget.extract(image_pdf(), "application/pdf", request_id="req")
    gemini.models.generate_content.return_value.text = MARKDOWN
    assert VisionMDRun().extract(image_pdf(), "application/pdf", request_id="req")
    assert gemini.models.generate_content.call_count == 2


@pytest.mark.parametrize("enabled", [None, "0"])
def test_vision_off_keeps_empty_drive_behavior(
    gemini: MagicMock,
    drive_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    enabled: str | None,
) -> None:
    if enabled is None:
        monkeypatch.delenv("VISION_MD_ENABLED")
    else:
        monkeypatch.setenv("VISION_MD_ENABLED", enabled)
    data = image_pdf()
    f = replace(_file("DOC", data), mime_type="application/pdf")
    drive_client.download_file_bytes.return_value = data
    repo = VisionRepository()
    with capture_logs() as logs:
        assert drive_run(repo, drive_client, f, "folder") == (0, 0)
    gemini.models.generate_content.assert_not_called()
    assert not repo.upserts and not repo.states
    assert not any(log["event"].startswith("vision_md_") for log in logs)


def test_vision_lease_failure_is_not_cached_as_document_failure(gemini: MagicMock) -> None:
    from teamagent.ingest.pipeline import _RetryLeaseLostError

    def heartbeat(force: bool) -> None:
        raise _RetryLeaseLostError("lease expired")

    with pytest.raises(_RetryLeaseLostError):
        VisionMDRun().extract(image_pdf(), "application/pdf", request_id="req", heartbeat=heartbeat)
    gemini.models.generate_content.assert_not_called()


def test_vision_pptx_skips_unreadable_images_without_failing_file(
    gemini: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # EMF のロゴ 1 枚のせいで資料ごと失敗にしない（PNG だけ読む）。
    monkeypatch.setenv("VISION_MD_MAX_PAGES", "1")
    data, image = image_pptx()
    monkeypatch.setattr(
        "teamagent.ingest.vision_md._pictures",
        lambda shapes: iter(
            [
                SimpleNamespace(content_type="image/x-emf", blob=b"emf"),
                SimpleNamespace(content_type="image/png", blob=image),
            ]
        ),
    )
    assert VisionMDRun().extract(data, PPTX_MIME, request_id="req") == [(1, MARKDOWN)]
    calls = gemini.models.generate_content.call_args_list
    assert [c.kwargs["contents"][0].parts[0].inline_data.mime_type for c in calls] == ["image/png"]
