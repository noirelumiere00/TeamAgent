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
                "acl_groups": list(doc.acl_groups),
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
    # 既定 kinds = ['slack','gdrive','gsheets','shared_drives'] すべて key として作られる
    assert set(result.by_kind.keys()) == {"slack", "gdrive", "gsheets", "shared_drives"}


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

    from teamagent.adapters.slack_channel_ingest_client import SlackChannelMember

    fake_client = MagicMock()
    fake_client.list_channel_history.return_value = fake_history
    fake_client.list_thread_replies.return_value = fake_replies
    # 新規 ACL 解決経路: list_channel_members + get_user_emails
    fake_client.list_channel_members.return_value = (["U001", "U002"], None)
    fake_client.get_user_emails.return_value = [
        SlackChannelMember(user_id="U001", email="taro@x.jp", display_name="Taro"),
        SlackChannelMember(user_id="U002", email="jiro@x.jp", display_name="Jiro"),
    ]

    monkeypatch.setattr(
        "teamagent.adapters.slack_channel_ingest_client.SlackChannelIngestClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )

    # 共有 cache をテスト間で汚さないようにクリア
    from teamagent.ingest import pipeline as pipeline_mod

    pipeline_mod._USER_EMAIL_CACHE.clear()

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
    assert call["acl_groups"] == []  # §G env 未設定なら従来どおり空（後方互換）


def test_company_acl_groups_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """§G: TEAMAGENT_SHARED_COMPANY_DOMAINS 設定時のみ会社ドメインを acl_groups に付与。"""
    from teamagent.ingest.pipeline import _company_acl_groups

    monkeypatch.delenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", raising=False)
    assert _company_acl_groups() == []
    monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", "VectorInc.co.jp")
    assert _company_acl_groups() == ["vectorinc.co.jp"]


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
    fake_client.list_channel_members.return_value = ([], None)
    fake_client.get_user_emails.return_value = []
    monkeypatch.setattr(
        "teamagent.adapters.slack_channel_ingest_client.SlackChannelIngestClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )

    from teamagent.ingest import pipeline as pipeline_mod

    pipeline_mod._USER_EMAIL_CACHE.clear()

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
# ACL 解決ヘルパー（_collect_all_member_ids / _resolve_member_emails）
# -----------------------------------------------------------
def test_collect_all_member_ids_paginates() -> None:
    """list_channel_members を cursor 続く限り呼んで全 ID を集める。"""
    from teamagent.ingest.pipeline import _collect_all_member_ids

    fake = MagicMock()
    fake.list_channel_members.side_effect = [
        (["U001", "U002"], "PAGE2"),
        (["U003"], None),
    ]
    ids = _collect_all_member_ids(fake, "C0", "r")
    assert ids == ["U001", "U002", "U003"]
    assert fake.list_channel_members.call_count == 2


def test_ingest_slack_channel_skips_when_no_acl_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """email 解決 0 件 + extra_acl_emails 空 → channel ごと skip (fail-safe)。"""
    from teamagent.adapters.slack_channel_ingest_client import HistoryBatch, SlackMessage

    parent = SlackMessage(ts="1700.000001", user="U1", text="x")
    fake_client = MagicMock()
    fake_client.list_channel_history.return_value = HistoryBatch(
        messages=(parent,), next_cursor=None, has_more=False
    )
    fake_client.list_channel_members.return_value = (["U001", "U002"], None)
    # email 全部 None → 解決ゼロ
    from teamagent.adapters.slack_channel_ingest_client import SlackChannelMember

    fake_client.get_user_emails.return_value = [
        SlackChannelMember(user_id="U001", email=None, display_name="A"),
        SlackChannelMember(user_id="U002", email=None, display_name="B"),
    ]
    monkeypatch.setattr(
        "teamagent.adapters.slack_channel_ingest_client.SlackChannelIngestClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )
    from teamagent.ingest import pipeline as pipeline_mod

    pipeline_mod._USER_EMAIL_CACHE.clear()

    from teamagent.ingest.pipeline import _ingest_slack_channel

    spec = SlackChannelSpec(
        channel_id="C0NOACL",
        channel_name="#noacl",
        description="",
        extra_acl_emails=(),  # 空
    )
    repo = _FakeRepository()
    docs_n, chunks_n = _ingest_slack_channel(
        spec,
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="x@y.jp",
        dry_run=False,
        request_id="r",
    )
    # skip された → 0 件、repository も呼ばれない
    assert docs_n == 0
    assert chunks_n == 0
    assert len(repo.upsert_calls) == 0
    # list_channel_history が呼ばれる前に skip するので、呼ばれていてもいいし
    # 呼ばれていなくてもいい（実装は呼ばれない想定だが厳密にしすぎないこと）


def test_ingest_slack_channel_uses_extra_acl_when_email_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """extra_acl_emails があれば email 解決ゼロでも取り込み続行。"""
    from teamagent.adapters.slack_channel_ingest_client import HistoryBatch, SlackMessage

    parent = SlackMessage(ts="1700.000001", user="U1", text="hello world")
    fake_client = MagicMock()
    fake_client.list_channel_history.return_value = HistoryBatch(
        messages=(parent,), next_cursor=None, has_more=False
    )
    fake_client.list_channel_members.return_value = (["U001"], None)
    from teamagent.adapters.slack_channel_ingest_client import SlackChannelMember

    fake_client.get_user_emails.return_value = [
        SlackChannelMember(user_id="U001", email=None, display_name="A"),
    ]
    monkeypatch.setattr(
        "teamagent.adapters.slack_channel_ingest_client.SlackChannelIngestClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )
    from teamagent.ingest import pipeline as pipeline_mod

    pipeline_mod._USER_EMAIL_CACHE.clear()

    from teamagent.ingest.pipeline import _ingest_slack_channel

    spec = SlackChannelSpec(
        channel_id="C0EXTRA",
        channel_name="#extra",
        description="",
        extra_acl_emails=("alice@x.jp",),  # 解決失敗してもこれが ACL
    )
    repo = _FakeRepository()
    docs_n, _ = _ingest_slack_channel(
        spec,
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="x@y.jp",
        dry_run=False,
        request_id="r",
    )
    assert docs_n == 1
    assert len(repo.upsert_calls) == 1


def test_resolve_member_emails_caches_and_excludes_bots() -> None:
    """get_user_emails を呼んで cache、Bot/deleted は除外。"""
    from teamagent.adapters.slack_channel_ingest_client import SlackChannelMember
    from teamagent.ingest import pipeline as pipeline_mod
    from teamagent.ingest.pipeline import _resolve_member_emails

    pipeline_mod._USER_EMAIL_CACHE.clear()

    fake = MagicMock()
    fake.get_user_emails.return_value = [
        SlackChannelMember(user_id="U001", email="taro@x.jp", display_name="Taro"),
        SlackChannelMember(user_id="U002", email=None, display_name="NoMail"),
        SlackChannelMember(user_id="U003", email="bot@x.jp", display_name="Bot", is_bot=True),
        SlackChannelMember(user_id="U004", email="old@x.jp", display_name="Old", deleted=True),
    ]
    emails = _resolve_member_emails(fake, ["U001", "U002", "U003", "U004"], "r")
    # bot / deleted / email無し はすべて除外
    assert emails == ["taro@x.jp"]
    # 2 回目は cache hit で API 呼ばない
    fake.get_user_emails.reset_mock()
    emails2 = _resolve_member_emails(fake, ["U001"], "r")
    assert emails2 == ["taro@x.jp"]
    fake.get_user_emails.assert_not_called()


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
        classmethod(lambda cls, **kwargs: fake_client),
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


# -----------------------------------------------------------
# Drive folder ingest — PDF 本文抽出 + ACL 解決
# -----------------------------------------------------------
def _make_drive_file(
    id: str,
    name: str = "test.pdf",
    mime: str = "application/pdf",
    owners: tuple[str, ...] = (),
) -> Any:
    from teamagent.adapters.gdrive_client import DriveFile

    return DriveFile(
        id=id,
        name=name,
        mime_type=mime,
        modified_time="2026-05-26T16:00:00Z",
        size=1234,
        parents=(),
        web_view_link=f"https://drive.google.com/file/d/{id}/view",
        owners_email=owners,
    )


def _make_drive_perm(type: str, role: str, email: str | None, deleted: bool = False) -> Any:
    from teamagent.adapters.gdrive_client import DrivePermission

    return DrivePermission(
        id="p1", type=type, role=role, email_address=email, domain=None, deleted=deleted
    )


def test_ingest_gdrive_folder_pdf_extracts_chunks_and_resolves_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PDF を download → 抽出 → 複数 chunk + ACL を解決して upsert する。"""
    fake_client = MagicMock()
    fake_client.list_files.return_value = (
        [_make_drive_file(id="F1", name="提案書.pdf", owners=("alice@x.jp",))],
        None,
    )
    fake_client.list_permissions.return_value = [
        _make_drive_perm("user", "owner", "alice@x.jp"),
        _make_drive_perm("user", "writer", "bob@x.jp"),
        _make_drive_perm("group", "reader", "sales@x.jp"),
    ]
    fake_client.download_file_bytes.return_value = b"<fake-pdf-bytes>"

    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )

    # pypdf を monkeypatch して 3 ページの PDF を擬似する
    fake_reader = MagicMock()
    p1 = MagicMock()
    p1.extract_text.return_value = "ページ 1 提案書本文" * 50  # 長文 → 複数 chunk
    p2 = MagicMock()
    p2.extract_text.return_value = "ページ 2"
    fake_reader.pages = [p1, p2]
    monkeypatch.setattr("pypdf.PdfReader", lambda _s: fake_reader)

    from teamagent.ingest.pipeline import _ingest_gdrive_folder

    spec = GDriveFolderSpec(
        folder_id="FOLDER1",
        folder_name="営業提案書",
        description="",
        mime_type_filter="application/pdf",
    )
    repo = _FakeRepository()
    docs_n, chunks_n = _ingest_gdrive_folder(
        spec,
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@x.jp",
        dry_run=False,
        request_id="r-drive",
    )
    assert docs_n == 1
    assert chunks_n >= 2  # 複数 chunks（page 1 が長文なので分割される）
    call = repo.upsert_calls[0]
    assert call["external_id"] == "F1"
    assert call["source_type"] == "gdrive"


def test_ingest_gdrive_folder_non_pdf_uses_title_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 PDF（Google Doc 等）は title だけで 1 chunk 作る（雛形動作）。"""
    fake_client = MagicMock()
    fake_client.list_files.return_value = (
        [
            _make_drive_file(
                id="DOC1",
                name="議事録.gdoc",
                mime="application/vnd.google-apps.document",
                owners=("alice@x.jp",),
            )
        ],
        None,
    )
    fake_client.list_permissions.return_value = [_make_drive_perm("user", "owner", "alice@x.jp")]
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )

    from teamagent.ingest.pipeline import _ingest_gdrive_folder

    spec = GDriveFolderSpec(
        folder_id="FOLDER2",
        folder_name="議事録",
        description="",
        mime_type_filter=None,
    )
    repo = _FakeRepository()
    docs_n, chunks_n = _ingest_gdrive_folder(
        spec,
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@x.jp",
        dry_run=False,
        request_id="r",
    )
    assert docs_n == 1
    assert chunks_n == 1
    # download_file_bytes は呼ばれない（PDF 以外）
    fake_client.download_file_bytes.assert_not_called()


def test_ingest_gdrive_folder_skips_pdf_when_download_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """download_file_bytes が例外を投げると、その 1 件は skip され他はそのまま処理される。"""
    fake_client = MagicMock()
    fake_client.list_files.return_value = (
        [
            _make_drive_file(id="OK", name="ok.pdf", owners=("alice@x.jp",)),
            _make_drive_file(id="FAIL", name="fail.pdf", owners=("alice@x.jp",)),
        ],
        None,
    )
    fake_client.list_permissions.return_value = [_make_drive_perm("user", "owner", "alice@x.jp")]

    def _dl(file_id: str, request_id: str) -> bytes:
        if file_id == "FAIL":
            raise RuntimeError("simulated 403")
        return b"<fake>"

    fake_client.download_file_bytes.side_effect = _dl
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )
    fake_reader = MagicMock()
    p = MagicMock()
    p.extract_text.return_value = "ok content"
    fake_reader.pages = [p]
    monkeypatch.setattr("pypdf.PdfReader", lambda _s: fake_reader)

    from teamagent.ingest.pipeline import _ingest_gdrive_folder

    spec = GDriveFolderSpec(
        folder_id="F",
        folder_name="x",
        description="",
        mime_type_filter="application/pdf",
    )
    repo = _FakeRepository()
    docs_n, chunks_n = _ingest_gdrive_folder(
        spec,
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@x.jp",
        dry_run=False,
        request_id="r",
    )
    # OK 1 件のみ upsert される
    assert docs_n == 1
    assert chunks_n >= 1
    assert len(repo.upsert_calls) == 1
    assert repo.upsert_calls[0]["external_id"] == "OK"


def test_ingest_gdrive_folder_paginates_list_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_files の next_page_token を辿って全件取得する。"""
    fake_client = MagicMock()
    fake_client.list_files.side_effect = [
        ([_make_drive_file(id="F1", mime="application/vnd.google-apps.document")], "PAGE2"),
        ([_make_drive_file(id="F2", mime="application/vnd.google-apps.document")], None),
    ]
    fake_client.list_permissions.return_value = []
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )

    from teamagent.ingest.pipeline import _ingest_gdrive_folder

    spec = GDriveFolderSpec(folder_id="F", folder_name="x", description="", mime_type_filter=None)
    repo = _FakeRepository()
    docs_n, _chunks_n = _ingest_gdrive_folder(
        spec,
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@x.jp",
        dry_run=False,
        request_id="r",
    )
    assert docs_n == 2
    assert fake_client.list_files.call_count == 2


def test_resolve_drive_file_acl_extracts_owner_and_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_resolve_drive_file_acl が owner / user / group を正しく分解する。"""
    fake_client = MagicMock()
    fake_client.list_permissions.return_value = [
        _make_drive_perm("user", "owner", "owner@x.jp"),
        _make_drive_perm("user", "writer", "bob@x.jp"),
        _make_drive_perm("user", "reader", "carol@x.jp", deleted=True),  # 除外
        _make_drive_perm("group", "reader", "sales@x.jp"),
    ]

    from teamagent.ingest.pipeline import _resolve_drive_file_acl

    owner, emails, groups = _resolve_drive_file_acl(
        fake_client, "F1", "r", fallback_owner_email="fallback@x.jp"
    )
    assert owner == "owner@x.jp"
    # owner は ACL に含まれる（fail-safe）+ user role!='owner' な bob のみ
    assert set(emails) == {"owner@x.jp", "bob@x.jp"}
    assert groups == ["sales@x.jp"]


def test_resolve_drive_file_acl_returns_fallback_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """permissions.list が例外を投げたら fallback owner を返す。"""
    fake_client = MagicMock()
    fake_client.list_permissions.side_effect = RuntimeError("403")

    from teamagent.ingest.pipeline import _resolve_drive_file_acl

    owner, emails, groups = _resolve_drive_file_acl(
        fake_client, "F1", "r", fallback_owner_email="fallback@x.jp"
    )
    assert owner == "fallback@x.jp"
    assert emails == ["fallback@x.jp"]
    assert groups == []
