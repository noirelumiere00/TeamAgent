"""ingest/pipeline.py のテスト。

実 adapter / 実 DB は呼ばず、IngestRunner の orchestration logic を検証。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.ingest.loader import (
    GDriveFolderSpec,
    GSheetSpec,
    GSheetsTabSpec,
    IngestSources,
    SlackChannelSpec,
)
from teamagent.ingest.pipeline import IngestResult, IngestRunner, IngestStats


class _FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024


class _FakeRepository:
    """実 DB なしで upsert を記録する fake。"""

    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, Any]] = []

    def upsert_document_with_chunks(
        self,
        doc: Any,
        chunks: list[Any],
        request_id: str,
        *,
        replace_existing_chunks: bool = True,
    ) -> str:
        self.upsert_calls.append(
            {
                "external_id": doc.external_id,
                "source_type": doc.source_type,
                "chunk_count": len(chunks),
                "request_id": request_id,
            }
        )
        return "fake-doc-id"


# -----------------------------------------------------------
# IngestRunner.run() — dry-run / 集計
# -----------------------------------------------------------
def test_runner_with_no_sources_returns_empty_stats() -> None:
    """空 IngestSources を渡しても落ちず、kind ごとに stats=0 を返す。"""
    runner = IngestRunner(
        repository=_FakeRepository(),  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),
        owner_email="x@y.jp",
        dry_run=True,
    )
    empty = IngestSources(version=1, slack_channels=(), gdrive_folders=(), gsheets=())
    result = runner.run(empty)
    assert isinstance(result, IngestResult)
    assert result.total_documents() == 0
    assert result.total_errors() == 0
    # 既定 kinds = ['slack','gdrive','gsheets'] すべて key としては作られる
    assert set(result.by_kind.keys()) == {"slack", "gdrive", "gsheets"}


def test_runner_filters_by_kinds() -> None:
    """kinds=['slack'] を渡すと gdrive/gsheets は処理されない。"""
    runner = IngestRunner(
        repository=_FakeRepository(),  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),
        owner_email="x@y.jp",
        dry_run=True,
    )
    empty = IngestSources(version=1, slack_channels=(), gdrive_folders=(), gsheets=())
    result = runner.run(empty, kinds=["slack"])
    assert "slack" in result.by_kind
    assert "gdrive" not in result.by_kind
    assert "gsheets" not in result.by_kind


def test_runner_catches_handler_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """1 source の取り込みが例外を投げても、他 source は処理される（partial failure 許容）。"""
    repo = _FakeRepository()
    runner = IngestRunner(
        repository=repo,  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),
        owner_email="x@y.jp",
        dry_run=True,
    )

    sources = IngestSources(
        version=1,
        slack_channels=(),
        gdrive_folders=(
            GDriveFolderSpec(folder_id="OK1", folder_name="ok", description=""),
            GDriveFolderSpec(folder_id="FAIL", folder_name="fail", description=""),
            GDriveFolderSpec(folder_id="OK2", folder_name="ok", description=""),
        ),
        gsheets=(),
    )

    # gdrive handler を fake で差し替え（FAIL spec のみ例外）
    def _fake_handler(spec: GDriveFolderSpec, **kwargs: Any) -> tuple[int, int]:
        if spec.folder_id == "FAIL":
            raise RuntimeError("simulated failure")
        return (2, 3)

    monkeypatch.setattr("teamagent.ingest.pipeline._ingest_gdrive_folder", _fake_handler)

    result = runner.run(sources, kinds=["gdrive"])
    stats = result.by_kind["gdrive"]
    assert stats.sources_processed == 2
    assert stats.sources_skipped == 1
    assert len(stats.errors) == 1
    assert stats.documents_upserted == 4  # 2 OK source × 2 docs
    assert stats.chunks_inserted == 6


# -----------------------------------------------------------
# IngestStats / IngestResult helpers
# -----------------------------------------------------------
def test_ingest_result_aggregates_totals() -> None:
    r = IngestResult()
    r.by_kind["slack"] = IngestStats(source_kind="slack", documents_upserted=10, chunks_inserted=10)
    r.by_kind["gdrive"] = IngestStats(
        source_kind="gdrive", documents_upserted=5, chunks_inserted=5, errors=["e1", "e2"]
    )
    assert r.total_documents() == 15
    assert r.total_errors() == 2


# -----------------------------------------------------------
# Slack ingest handler を単独で確認（adapter を MagicMock 差し替え）
# -----------------------------------------------------------
def test_ingest_slack_channel_handler_calls_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_ingest_slack_channel が SlackChannelIngestClient → repository.upsert を呼ぶ。"""
    from teamagent.adapters.slack_channel_ingest_client import HistoryBatch, SlackMessage

    parent = SlackMessage(
        ts="1700000001.000001",
        user="U001",
        text="親メッセージ",
        thread_ts="1700000001.000001",
        reply_count=2,
    )
    fake_history = HistoryBatch(messages=(parent,), next_cursor=None, has_more=False)
    fake_replies = HistoryBatch(messages=(parent,), next_cursor=None, has_more=False)

    fake_client = MagicMock()
    fake_client.list_channel_history.return_value = fake_history
    fake_client.list_thread_replies.return_value = fake_replies

    monkeypatch.setattr(
        "teamagent.adapters.slack_channel_ingest_client.SlackChannelIngestClient.from_env",
        classmethod(lambda cls: fake_client),
    )

    from teamagent.ingest.pipeline import _ingest_slack_channel

    spec = SlackChannelSpec(
        channel_id="C0XYZ",
        channel_name="#test",
        description="test",
        extra_acl_emails=("alice@x.jp",),
    )
    repo = _FakeRepository()
    docs_n, chunks_n = _ingest_slack_channel(
        spec,
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bob@x.jp",
        dry_run=False,
        request_id="req-1",
    )
    assert docs_n == 1
    assert chunks_n == 1
    assert len(repo.upsert_calls) == 1
    call = repo.upsert_calls[0]
    assert call["external_id"] == "C0XYZ:1700000001.000001"
    assert call["source_type"] == "slack"


def test_ingest_slack_channel_dry_run_skips_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dry_run=True なら repository.upsert は呼ばれない。"""
    from teamagent.adapters.slack_channel_ingest_client import HistoryBatch, SlackMessage

    parent = SlackMessage(ts="1700.000001", user="U1", text="x")
    fake_client = MagicMock()
    fake_client.list_channel_history.return_value = HistoryBatch(
        messages=(parent,), next_cursor=None, has_more=False
    )
    monkeypatch.setattr(
        "teamagent.adapters.slack_channel_ingest_client.SlackChannelIngestClient.from_env",
        classmethod(lambda cls: fake_client),
    )

    from teamagent.ingest.pipeline import _ingest_slack_channel

    spec = SlackChannelSpec(channel_id="C0", channel_name="#t", description="")
    repo = _FakeRepository()
    _ingest_slack_channel(
        spec,
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="x@y.jp",
        dry_run=True,
        request_id="r",
    )
    assert len(repo.upsert_calls) == 0


# -----------------------------------------------------------
# Sheet handler の最小確認（row → document 化）
# -----------------------------------------------------------
def test_ingest_gsheet_handler_row_per_document(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters.gsheets_client import TabRows

    fake_client = MagicMock()
    fake_client.get_tab_rows.return_value = TabRows(
        sheet_id="1V",
        tab_name="フォーム回答 1",
        headers=("業界", "温度感"),
        rows=(("飲食", "高"), ("コスメ", "中")),
        row_count=2,
    )
    monkeypatch.setattr(
        "teamagent.adapters.gsheets_client.GSheetsClient.from_env",
        classmethod(lambda cls: fake_client),
    )

    from teamagent.ingest.pipeline import _ingest_gsheet

    spec = GSheetSpec(
        sheet_id="1V",
        sheet_name="FB",
        description="",
        tabs=(GSheetsTabSpec(gid=537831563, tab_name="フォーム回答 1"),),
    )
    repo = _FakeRepository()
    docs_n, chunks_n = _ingest_gsheet(
        spec,
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="x@y.jp",
        dry_run=False,
        request_id="r",
    )
    assert docs_n == 2  # 2 行 = 2 document
    assert chunks_n == 2
    # external_id は <sheet_id>:<gid>:<row_idx> 形式（row_idx=2 から）
    assert repo.upsert_calls[0]["external_id"] == "1V:537831563:2"
    assert repo.upsert_calls[1]["external_id"] == "1V:537831563:3"
