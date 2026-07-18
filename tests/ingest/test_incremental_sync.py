"""Batch C1: pipeline の増分同期（USE_INCREMENTAL_SYNC）配線の挙動を検証する。

実 Drive / 実 DB は使わず、GDriveClient.from_env を fake に、repository を connector_state/
ingest_jobs を記録する fake に差し替える。既定（フラグ OFF）が完全後方互換であることも固定する。
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.ingest.loader import GDriveFolderSpec, IngestSources
from teamagent.ingest.office_extract import OFFICE_VALIDATOR_SCHEMA_VERSION
from teamagent.ingest.repository import ConnectorState


class _FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024

    def embed_passage(self, text: str) -> list[float]:
        return self.embed(text)


class _FakeIncrementalRepo:
    """connector_state / ingest_jobs 呼び出しを記録する fake repository。"""

    def __init__(self, prior_state: ConnectorState | None = None) -> None:
        self.upsert_calls: list[str] = []
        self.saved_states: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.load_calls = 0
        self._prior_state = prior_state

    def upsert_document_with_chunks(
        self, doc: Any, chunks: list[Any], request_id: str, *, replace_existing_chunks: bool = True
    ) -> str:
        self.upsert_calls.append(doc.external_id)
        return "fake-doc-id"

    def load_connector_state(self, source_kind: str, source_id: str) -> ConnectorState | None:
        self.load_calls += 1
        return self._prior_state

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
                "error": error,
                "metadata": dict(metadata or {}),
            }
        )

    def claim_due_source_retries(self, **kwargs: Any) -> list[Any]:
        return []

    def record_ingest_job(
        self,
        source_type: str,
        external_id: str,
        *,
        state: str = "COMMITTED",
        batch_id: str | None = None,
        error: str | None = None,
        success: bool = True,
        max_attempts: int = 5,
    ) -> None:
        self.jobs.append({"external_id": external_id, "state": state, "success": success})


def _drive_file(file_id: str, name: str = "x.png", mime: str = "image/png") -> Any:
    from teamagent.adapters.gdrive_client import DriveFile

    return DriveFile(
        id=file_id,
        name=name,
        mime_type=mime,
        modified_time="2026-06-16T00:00:00Z",
        size=1234,
        parents=(),
        web_view_link=f"https://drive.google.com/file/d/{file_id}/view",
        owners_email=("a@x.jp",),
    )


def _owner_perm() -> Any:
    from teamagent.adapters.gdrive_client import DrivePermission

    return DrivePermission(
        id="p1", type="user", role="owner", email_address="a@x.jp", domain=None, deleted=False
    )


def _fake_gdrive_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from unittest.mock import MagicMock

    client = MagicMock()
    client.list_files.return_value = ([_drive_file("F1"), _drive_file("F2")], None)
    client.list_permissions.return_value = [_owner_perm()]
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: client),
    )
    return client


def _spec() -> GDriveFolderSpec:
    return GDriveFolderSpec(
        folder_id="FOLDER1", folder_name="x", description="", mime_type_filter=None
    )


# -----------------------------------------------------------
# 既定（フラグ OFF）= 完全後方互換
# -----------------------------------------------------------
def test_flag_off_does_not_touch_connector_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_INCREMENTAL_SYNC", raising=False)
    _fake_gdrive_client(monkeypatch)
    from teamagent.ingest.pipeline import _ingest_gdrive_folder

    repo = _FakeIncrementalRepo()
    docs_n, _ = _ingest_gdrive_folder(
        _spec(),
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@x.jp",
        dry_run=False,
        request_id="r",
    )
    assert docs_n == 2  # 全件フル走査
    assert repo.load_calls == 0
    assert repo.saved_states == []
    assert repo.jobs == []


# -----------------------------------------------------------
# フラグ ON・初回（cursor 無し）= フル走査 + start token を seed
# -----------------------------------------------------------
def test_flag_on_first_run_seeds_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "1")
    client = _fake_gdrive_client(monkeypatch)
    client.get_start_page_token.return_value = "TOKEN_SEED"
    from teamagent.ingest.pipeline import _ingest_gdrive_folder

    repo = _FakeIncrementalRepo(prior_state=None)
    docs_n, _ = _ingest_gdrive_folder(
        _spec(),
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@x.jp",
        dry_run=False,
        request_id="r",
    )
    assert docs_n == 2  # 初回はフル走査
    assert repo.load_calls == 1
    client.get_start_page_token.assert_called_once()
    client.get_changes.assert_not_called()
    # cursor を前進保存（success=True）
    assert len(repo.saved_states) == 1
    saved = repo.saved_states[0]
    assert saved == {
        "source_kind": "gdrive",
        "source_id": "FOLDER1",
        "cursor": "TOKEN_SEED",
        "success": True,
        "error": None,
        "metadata": {
            "outcome": "success",
            "warning_count": 0,
            "warning_reasons": {},
            "known_invalid_suppressed": 0,
            "office_validator_schema_version": OFFICE_VALIDATOR_SCHEMA_VERSION,
        },
    }
    # 各 file の COMMITTED を記録
    assert [j["external_id"] for j in repo.jobs] == ["F1", "F2"]
    assert all(j["state"] == "COMMITTED" for j in repo.jobs)


# -----------------------------------------------------------
# フラグ ON・2 回目（cursor あり）= 変更 file のみに絞る
# -----------------------------------------------------------
def test_flag_on_incremental_filters_to_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters.gdrive_client import ChangeBatch, DriveChange

    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "1")
    client = _fake_gdrive_client(monkeypatch)
    client.get_changes.return_value = ChangeBatch(
        changes=(DriveChange(change_type="file", file_id="F1", removed=False, time=None),),
        next_page_token=None,
        new_start_page_token="NEW_TOKEN",
    )
    from teamagent.ingest.pipeline import _ingest_gdrive_folder

    prior = ConnectorState(
        source_kind="gdrive",
        source_id="FOLDER1",
        cursor="PRIOR_TOKEN",
        metadata={"office_validator_schema_version": OFFICE_VALIDATOR_SCHEMA_VERSION},
    )
    repo = _FakeIncrementalRepo(prior_state=prior)
    docs_n, _ = _ingest_gdrive_folder(
        _spec(),
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@x.jp",
        dry_run=False,
        request_id="r",
    )
    # 変更 file は F1 のみ → F2 は処理しない
    assert docs_n == 1
    assert repo.upsert_calls == ["F1"]
    client.get_changes.assert_called_once()
    client.get_start_page_token.assert_not_called()
    # 新 cursor を保存
    assert repo.saved_states[0]["cursor"] == "NEW_TOKEN"
    assert repo.saved_states[0]["success"] is True
    assert [j["external_id"] for j in repo.jobs] == ["F1"]


def test_flag_on_dry_run_skips_state_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """dry_run=True なら state/job の書き込みはしない（差分の読み込みはしてよい）。"""
    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "1")
    client = _fake_gdrive_client(monkeypatch)
    client.get_start_page_token.return_value = "TOKEN_SEED"
    from teamagent.ingest.pipeline import _ingest_gdrive_folder

    repo = _FakeIncrementalRepo(prior_state=None)
    _ingest_gdrive_folder(
        _spec(),
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@x.jp",
        dry_run=True,
        request_id="r",
    )
    assert repo.saved_states == []
    assert repo.jobs == []


def test_drive_changes_pagination_loop_is_rejected() -> None:
    from unittest.mock import MagicMock

    from teamagent.adapters.gdrive_client import ChangeBatch
    from teamagent.ingest.pipeline import GDrivePaginationIncompleteError, _drain_changes

    client = MagicMock()
    client.get_changes.side_effect = [
        ChangeBatch(changes=(), next_page_token="LOOP", new_start_page_token=None),
        ChangeBatch(changes=(), next_page_token="LOOP", new_start_page_token=None),
    ]

    with pytest.raises(GDrivePaginationIncompleteError, match="token loop"):
        _drain_changes(client, "START", "request", max_pages=5)


def test_drive_file_pagination_saturation_is_rejected() -> None:
    from unittest.mock import MagicMock

    from teamagent.ingest.pipeline import (
        GDrivePaginationIncompleteError,
        _list_all_gdrive_files,
    )

    client = MagicMock()
    client.list_files.side_effect = [
        ([_drive_file("F1")], "NEXT-1"),
        ([_drive_file("F2")], "NEXT-2"),
    ]

    with pytest.raises(GDrivePaginationIncompleteError, match="exceeded 2 pages"):
        _list_all_gdrive_files(client, "FOLDER1", "request", None, max_pages=2)


def test_incomplete_file_listing_never_saves_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from teamagent.ingest.pipeline import (
        GDrivePaginationIncompleteError,
        _ingest_gdrive_folder,
    )

    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "1")
    client = MagicMock()
    client.get_start_page_token.return_value = "MUST-NOT-SAVE"
    client.list_files.return_value = ([], "REPEATED")
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: client),
    )
    repo = _FakeIncrementalRepo()

    with pytest.raises(GDrivePaginationIncompleteError, match="token loop"):
        _ingest_gdrive_folder(
            _spec(),
            embedder=_FakeEmbedder(),
            repository=repo,  # type: ignore[arg-type]
            owner_email="bot@x.jp",
            dry_run=False,
            request_id="request",
        )

    assert repo.saved_states == []


def test_recursive_walk_saturation_fails_before_cursor_or_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    import teamagent.adapters.gdrive_client as gdrive_client
    from teamagent.ingest.pipeline import (
        GDrivePaginationIncompleteError,
        _ingest_gdrive_folder,
    )

    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "1")
    monkeypatch.setattr(gdrive_client, "DEFAULT_WALK_MAX_FILES", 2)
    client = MagicMock()
    client.get_start_page_token.return_value = "MUST-NOT-SAVE"
    client.walk_files_recursive.return_value = [_drive_file("F1"), _drive_file("F2")]
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: client),
    )
    repo = _FakeIncrementalRepo()
    truncated: set[str] = set()
    recursive_spec = GDriveFolderSpec(
        folder_id="FOLDER1",
        folder_name="x",
        description="",
        include_subfolders=True,
        mime_type_filter=None,
    )

    with pytest.raises(GDrivePaginationIncompleteError, match="recursive listing reached 2"):
        _ingest_gdrive_folder(
            recursive_spec,
            embedder=_FakeEmbedder(),
            repository=repo,  # type: ignore[arg-type]
            owner_email="bot@x.jp",
            dry_run=False,
            request_id="request",
            truncated_walk_roots=truncated,
        )

    assert truncated == {"FOLDER1"}
    assert repo.saved_states == []
    assert repo.upsert_calls == []


# -----------------------------------------------------------
# Runner: source 失敗時の connector_state attempt_count++
# -----------------------------------------------------------
def test_runner_records_failure_in_connector_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from teamagent.ingest.ops_alert import IngestOpsAlerter
    from teamagent.ingest.pipeline import IngestRunner

    monkeypatch.setenv("USE_INCREMENTAL_SYNC", "1")
    repo = _FakeIncrementalRepo()
    runner = IngestRunner(
        repository=repo,  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),
        owner_email="bot@x.jp",
        dry_run=False,
        alerter=MagicMock(spec=IngestOpsAlerter),
    )

    def _boom(spec: Any, **kwargs: Any) -> tuple[int, int]:
        raise RuntimeError("boom")

    monkeypatch.setattr("teamagent.ingest.pipeline._ingest_gdrive_folder", _boom)

    sources = IngestSources(
        version=1,
        slack_channels=(),
        gdrive_folders=(GDriveFolderSpec(folder_id="FAIL1", folder_name="x", description=""),),
        gsheets=(),
    )
    runner.run(sources, kinds=["gdrive"])
    # 失敗 1 件 → connector_state に success=False を 1 件刻む
    failures = [s for s in repo.saved_states if s["success"] is False]
    assert len(failures) == 1
    assert failures[0]["source_kind"] == "gdrive"
    assert failures[0]["source_id"] == "FAIL1"
    assert "boom" in (failures[0]["error"] or "")


def test_runner_no_failure_record_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock

    from teamagent.ingest.ops_alert import IngestOpsAlerter
    from teamagent.ingest.pipeline import IngestRunner

    monkeypatch.delenv("USE_INCREMENTAL_SYNC", raising=False)
    repo = _FakeIncrementalRepo()
    runner = IngestRunner(
        repository=repo,  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),
        owner_email="bot@x.jp",
        dry_run=False,
        alerter=MagicMock(spec=IngestOpsAlerter),
    )

    def _boom(spec: Any, **kwargs: Any) -> tuple[int, int]:
        raise RuntimeError("boom")

    monkeypatch.setattr("teamagent.ingest.pipeline._ingest_gdrive_folder", _boom)
    sources = IngestSources(
        version=1,
        slack_channels=(),
        gdrive_folders=(GDriveFolderSpec(folder_id="FAIL1", folder_name="x", description=""),),
        gsheets=(),
    )
    runner.run(sources, kinds=["gdrive"])
    assert repo.saved_states == []  # フラグ OFF＝従来挙動
