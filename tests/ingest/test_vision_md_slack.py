"""Vision fallback uses the existing Slack file persistence and version skips."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from teamagent.ingest.loader import IngestSources
from teamagent.ingest.pipeline import IngestRunner
from tests.ingest.test_slack_public_files import SPEC, Repository, file, run
from tests.ingest.test_slack_public_files import setup as setup
from tests.ingest.test_vision_md import MARKDOWN, _file, _folder_spec, image_pdf, image_pptx
from tests.ingest.test_vision_md import drive_client as drive_client
from tests.ingest.test_vision_md import gemini as gemini


@pytest.mark.parametrize("filetype", ["pdf", "pptx", "ppt"])
def test_vision_slack_empty_file_is_saved(
    setup: Any,
    gemini: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    filetype: str,
) -> None:
    api, _, _ = setup
    entry = file("EMPTY-DOC", filetype, office_pdf="https://files.slack.com/EMPTY-CONVERTED")
    api.pages = [{"files": [entry]}]
    if filetype == "pptx":
        data = image_pptx()[0]

        async def download(self: Any, url: str, **kwargs: Any) -> bytes:
            return data

        monkeypatch.setattr(
            "teamagent.adapters.slack_client.SlackClient.download_file_guarded", download
        )
    repo = Repository()
    with capture_logs() as logs:
        assert run(repo).total_documents() == 1
        assert run(repo).total_documents_unchanged() == 1
    assert repo.documents["EMPTY-DOC"].metadata["extractor"] == "vision-md-v1"
    assert [c.content for c in repo.chunks["EMPTY-DOC"]] == (
        [MARKDOWN + "\n\n" + MARKDOWN, MARKDOWN] if filetype == "pptx" else [MARKDOWN]
    )
    assert gemini.models.generate_content.call_count == (3 if filetype == "pptx" else 1)
    assert MARKDOWN not in str(logs) and entry["name"] not in str(logs)


@pytest.mark.parametrize("failure", ["429", "empty", "truncated"])
def test_vision_slack_failure_uses_existing_version_skip(
    setup: Any,
    gemini: MagicMock,
    failure: str,
) -> None:
    api, _, downloads = setup
    api.pages = [{"files": [file("EMPTY-DOC")]}]
    if failure == "429":
        error = RuntimeError("private-file.pdf owner@vectorinc.co.jp")
        error.code = 429  # type: ignore[attr-defined]
        gemini.models.generate_content.side_effect = error
    elif failure == "empty":
        gemini.models.generate_content.return_value.text = ""
    else:
        gemini.models.generate_content.return_value.candidates[0].finish_reason = "MAX_TOKENS"
    repo = Repository()
    with capture_logs() as logs:
        assert run(repo).total_documents() == 0
        reloaded = Repository()
        reloaded.states = dict(repo.states)
        assert run(reloaded).total_documents() == 0
    assert repo.states["slack", "files:file:EMPTY-DOC"].metadata["failure_reason"] == "empty_text"
    assert downloads == ["EMPTY-DOC"]
    assert gemini.models.generate_content.call_count == 1
    assert MARKDOWN not in str(logs) and "private-file.pdf" not in str(logs)
    assert "owner@vectorinc.co.jp" not in str(logs)
    api.pages[0]["files"][0]["updated"] += 1
    run(reloaded)
    assert gemini.models.generate_content.call_count == 2


def test_vision_slack_retries_previously_empty_file_on_enable(
    setup: Any,
    gemini: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _, downloads = setup
    api.pages = [{"files": [file("EMPTY-DOC")]}]
    repo = Repository()
    monkeypatch.setenv("VISION_MD_ENABLED", "0")
    assert run(repo).total_documents() == 0
    assert run(repo).total_documents() == 0
    gemini.models.generate_content.assert_not_called()
    monkeypatch.setenv("VISION_MD_ENABLED", "1")
    assert run(repo).total_documents() == 1
    assert downloads == ["EMPTY-DOC", "EMPTY-DOC"]


def test_vision_run_budget_is_shared_between_slack_and_drive_and_resumes(
    setup: Any,
    gemini: MagicMock,
    drive_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.ingest.test_invalid_source_observability import _Embedder

    monkeypatch.setenv("VISION_MD_MAX_PER_RUN", "1")
    api, _, _ = setup
    api.pages = [{"files": [file("EMPTY-A"), file("EMPTY-B")]}]
    data = image_pdf()
    f = replace(_file("DRIVE-DOC", data), mime_type="application/pdf")
    drive_client.list_files.return_value = ([f], None)
    drive_client.download_file_bytes.return_value = data
    repo = Repository()
    sources = IngestSources(
        version=1,
        slack_channels=(),
        gsheets=(),
        gdrive_folders=(_folder_spec(),),
        slack_public_files=SPEC,
    )
    runner = IngestRunner(repo, _Embedder(), owner_email="bot@example.jp", dry_run=False)
    with capture_logs() as logs:
        assert runner.run(sources, kinds=["slack_public_files", "gdrive"]).total_documents() == 1
    assert gemini.models.generate_content.call_count == 1
    assert not any(key[1] == "files:file:EMPTY-B" for key in repo.states)
    assert not any(key[1].startswith("vision-md:file:") for key in repo.states)
    assert drive_client.download_file_bytes.call_count == 1
    assert (
        sum(log["event"] == "vision_md_failed" and log["reason"] == "run_limit" for log in logs)
        == 2
    )
    # Next run has a fresh budget and can process the deferred Slack file.
    assert runner.run(sources, kinds=["slack_public_files"]).total_documents() == 1
    assert gemini.models.generate_content.call_count == 2
