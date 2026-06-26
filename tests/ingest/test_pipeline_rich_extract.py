"""INGEST_RICH_EXTRACT 配線のテスト（pipeline.py の Google ネイティブ本文化 + dedup ガード）。

実 adapter / 実 Google API / 実 DB は呼ばず、fake / monkeypatch で注入する。

検証観点:
- OFF（既定）で挙動が変わらない（gdoc/gslide/gsheet/plain-text の本文化が起きない）。
- ON で gdoc/gslide/gsheet/plain-text が title_only でなく本文 chunk になる。
- ON の fail-open（adapter 例外 → WARN + title_only フォールバック）。
- §2 dedup ガード（本文 chunk を title_only が上書きしない）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.ingest.loader import GDriveFolderSpec, SharedDriveCrawlSpec
from teamagent.ingest.pipeline import (
    ChunkUpsert,
    DocumentUpsert,
    _guarded_upsert,
    _ingest_gdrive_folder,
    _ingest_shared_drives_crawl,
)

# Google ネイティブ mime（pipeline 内の定数と一致）
GDOC_MIME = "application/vnd.google-apps.document"
GSLIDE_MIME = "application/vnd.google-apps.presentation"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"


class _FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1] * 1024

    def embed_passage(self, text: str) -> list[float]:
        return self.embed(text)


class _FakeRepository:
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
                "metadata": dict(doc.metadata),
                "chunks": list(chunks),
            }
        )
        return "fake-doc-id"


def _make_drive_file(
    id: str,
    name: str,
    mime: str,
    *,
    size: int | None = 1234,
    owners: tuple[str, ...] = ("alice@x.jp",),
) -> Any:
    from teamagent.adapters.gdrive_client import DriveFile

    return DriveFile(
        id=id,
        name=name,
        mime_type=mime,
        modified_time="2026-06-20T16:00:00Z",
        size=size,
        parents=(),
        web_view_link=f"https://drive.google.com/file/d/{id}/view",
        owners_email=owners,
    )


def _make_owner_perm(email: str = "alice@x.jp") -> Any:
    from teamagent.adapters.gdrive_client import DrivePermission

    return DrivePermission(
        id="p1", type="user", role="owner", email_address=email, domain=None, deleted=False
    )


def _fake_gdrive(monkeypatch: pytest.MonkeyPatch, files: list[Any]) -> MagicMock:
    """list_files / list_permissions を返す fake GDriveClient（folder 経路用）。"""
    fake_client = MagicMock()
    fake_client.list_files.return_value = (files, None)
    fake_client.list_permissions.return_value = [_make_owner_perm()]
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )
    return fake_client


def _fake_gdrive_crawl(monkeypatch: pytest.MonkeyPatch, files: list[Any]) -> MagicMock:
    """list_shared_drives / walk_files_recursive を返す fake GDriveClient（crawl 経路用）。"""
    from teamagent.adapters.gdrive_client import SharedDrive

    fake_client = MagicMock()
    fake_client.list_shared_drives.return_value = [SharedDrive(id="D1", name="営業ナレッジ")]
    fake_client.walk_files_recursive.return_value = files
    fake_client.list_permissions.return_value = [_make_owner_perm()]
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client),
    )
    return fake_client


def _crawl_spec() -> SharedDriveCrawlSpec:
    return SharedDriveCrawlSpec(
        enabled=True,
        name_filter=("営業",),
        sales_relevance_filter=False,  # fake file を確実に通す
    )


def _folder_spec() -> GDriveFolderSpec:
    return GDriveFolderSpec(folder_id="F", folder_name="x", description="", mime_type_filter=None)


def _run_crawl(
    repo: _FakeRepository, *, content_registry: set[tuple[str, str]] | None = None
) -> tuple[int, int]:
    return _ingest_shared_drives_crawl(
        _crawl_spec(),
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@x.jp",
        dry_run=False,
        request_id="r",
        content_registry=content_registry,
    )


def _run_folder(
    repo: _FakeRepository, *, content_registry: set[tuple[str, str]] | None = None
) -> tuple[int, int]:
    return _ingest_gdrive_folder(
        _folder_spec(),
        embedder=_FakeEmbedder(),
        repository=repo,  # type: ignore[arg-type]
        owner_email="bot@x.jp",
        dry_run=False,
        request_id="r",
        content_registry=content_registry,
    )


# -----------------------------------------------------------
# OFF（既定）: 挙動不変 — Google ネイティブ gslide/gsheet/plain-text は title_only のまま
# -----------------------------------------------------------
def test_crawl_gslide_off_is_title_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF: gslide は本文化されず title_only（後方互換）。GSlidesClient は呼ばれない。"""
    monkeypatch.delenv("INGEST_RICH_EXTRACT", raising=False)
    _fake_gdrive_crawl(
        monkeypatch, [_make_drive_file("G1", "提案.gslides", GSLIDE_MIME, size=None)]
    )

    def _boom(cls: type) -> None:
        raise AssertionError("OFF で GSlidesClient を呼んではいけない")

    monkeypatch.setattr(
        "teamagent.adapters.gslides_client.GSlidesClient.from_env", classmethod(_boom)
    )

    repo = _FakeRepository()
    docs_n, chunks_n = _run_crawl(repo)
    assert docs_n == 1
    assert chunks_n == 1
    chunks = repo.upsert_calls[0]["chunks"]
    assert len(chunks) == 1
    assert chunks[0].metadata.get("title_only") is True


def test_crawl_plaintext_off_is_title_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """OFF: text/plain は本文化されず title_only。download_file_bytes は呼ばれない。"""
    monkeypatch.delenv("INGEST_RICH_EXTRACT", raising=False)
    fake_client = _fake_gdrive_crawl(
        monkeypatch, [_make_drive_file("T1", "memo.txt", "text/plain")]
    )

    repo = _FakeRepository()
    docs_n, _ = _run_crawl(repo)
    assert docs_n == 1
    chunks = repo.upsert_calls[0]["chunks"]
    assert chunks[0].metadata.get("title_only") is True
    fake_client.download_file_bytes.assert_not_called()


# -----------------------------------------------------------
# ON: gdoc / gslide / gsheet / plain-text が本文 chunk になる
# -----------------------------------------------------------
def test_crawl_gdoc_on_extracts_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """ON: gdoc は GDocsClient.get_document_text で本文 chunk になる。"""
    monkeypatch.setenv("INGEST_RICH_EXTRACT", "1")
    _fake_gdrive_crawl(monkeypatch, [_make_drive_file("GDOC1", "議事録", GDOC_MIME, size=None)])

    from teamagent.adapters.gdocs_client import DocContent

    fake_gdocs = MagicMock()
    fake_gdocs.get_document_text.return_value = DocContent(
        document_id="GDOC1", title="議事録", text="議事録の本文サンプル。" * 30
    )
    monkeypatch.setattr(
        "teamagent.adapters.gdocs_client.GDocsClient.from_env",
        classmethod(lambda cls, **kwargs: fake_gdocs),
    )

    repo = _FakeRepository()
    docs_n, chunks_n = _run_crawl(repo)
    assert docs_n == 1
    assert chunks_n >= 1
    fake_gdocs.get_document_text.assert_called_once()
    chunks = repo.upsert_calls[0]["chunks"]
    assert all("page_num" in c.metadata for c in chunks)
    assert all(not c.metadata.get("title_only") for c in chunks)


def test_crawl_gslide_on_extracts_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """ON: gslide は GSlidesClient.get_presentation_text(2引数) で本文 chunk になる。"""
    monkeypatch.setenv("INGEST_RICH_EXTRACT", "1")
    _fake_gdrive_crawl(monkeypatch, [_make_drive_file("GS1", "deck", GSLIDE_MIME, size=None)])

    from teamagent.adapters.gslides_client import SlidesContent

    fake_gslides = MagicMock()
    fake_gslides.get_presentation_text.return_value = SlidesContent(
        presentation_id="GS1", title="deck", text="スライド本文 + ノート。" * 20, slide_count=3
    )
    monkeypatch.setattr(
        "teamagent.adapters.gslides_client.GSlidesClient.from_env",
        classmethod(lambda cls, **kwargs: fake_gslides),
    )

    repo = _FakeRepository()
    docs_n, chunks_n = _run_crawl(repo)
    assert docs_n == 1
    assert chunks_n >= 1
    # 2 引数（presentation_id, request_id）で呼ばれる
    assert fake_gslides.get_presentation_text.call_count == 1
    args, kwargs = fake_gslides.get_presentation_text.call_args
    assert (args[0] if args else kwargs["presentation_id"]) == "GS1"
    chunks = repo.upsert_calls[0]["chunks"]
    assert all(not c.metadata.get("title_only") for c in chunks)


def test_crawl_gsheet_on_extracts_per_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    """ON: gsheet は get_tab_rows→format_row_as_document でタブ単位の本文 chunk になる。"""
    monkeypatch.setenv("INGEST_RICH_EXTRACT", "1")
    _fake_gdrive_crawl(monkeypatch, [_make_drive_file("SH1", "売上", GSHEET_MIME, size=None)])

    from teamagent.adapters.gsheets_client import SheetMetadata, SheetTab, TabRows

    fake_gsheets = MagicMock()
    fake_gsheets.get_sheet_metadata.return_value = SheetMetadata(
        sheet_id="SH1",
        title="売上",
        tabs=(
            SheetTab(sheet_id="SH1", gid=0, title="Sheet1", row_count=2, col_count=2),
            SheetTab(sheet_id="SH1", gid=1, title="Sheet2", row_count=2, col_count=2),
        ),
    )

    def _tab_rows(*, sheet_id: str, tab_name: str, request_id: str) -> TabRows:
        return TabRows(
            sheet_id=sheet_id,
            tab_name=tab_name,
            headers=("業界", "金額"),
            rows=(("飲食", "100"), ("コスメ", "200")),
            row_count=2,
        )

    fake_gsheets.get_tab_rows.side_effect = _tab_rows
    monkeypatch.setattr(
        "teamagent.adapters.gsheets_client.GSheetsClient.from_env",
        classmethod(lambda cls, **kwargs: fake_gsheets),
    )

    repo = _FakeRepository()
    docs_n, chunks_n = _run_crawl(repo)
    assert docs_n == 1
    # 2 タブ → タブ単位 page → 2 page。各 page は小さいので 1 chunk → 2 chunk
    assert chunks_n == 2
    chunks = repo.upsert_calls[0]["chunks"]
    assert all(not c.metadata.get("title_only") for c in chunks)
    # format_row_as_document の "header: value" 形式が本文に入っている
    body = "\n".join(c.content for c in chunks)
    assert "業界: 飲食" in body


def test_crawl_plaintext_on_extracts_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """ON: text/plain は download→utf-8 decode→本文 chunk になる。"""
    monkeypatch.setenv("INGEST_RICH_EXTRACT", "1")
    fake_client = _fake_gdrive_crawl(
        monkeypatch, [_make_drive_file("T1", "memo.md", "text/markdown")]
    )
    fake_client.download_file_bytes.return_value = "# 見出し\n本文サンプル。".encode()

    repo = _FakeRepository()
    docs_n, chunks_n = _run_crawl(repo)
    assert docs_n == 1
    assert chunks_n >= 1
    chunks = repo.upsert_calls[0]["chunks"]
    assert all(not c.metadata.get("title_only") for c in chunks)
    assert "本文サンプル" in "\n".join(c.content for c in chunks)


def test_folder_gslide_on_extracts_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """ON: folder 経路でも gslide が本文化される（folder には gdoc は既存・gslide を追加）。"""
    monkeypatch.setenv("INGEST_RICH_EXTRACT", "1")
    _fake_gdrive(monkeypatch, [_make_drive_file("GS1", "deck", GSLIDE_MIME, size=None)])

    from teamagent.adapters.gslides_client import SlidesContent

    fake_gslides = MagicMock()
    fake_gslides.get_presentation_text.return_value = SlidesContent(
        presentation_id="GS1", title="deck", text="本文。" * 30, slide_count=2
    )
    monkeypatch.setattr(
        "teamagent.adapters.gslides_client.GSlidesClient.from_env",
        classmethod(lambda cls, **kwargs: fake_gslides),
    )

    repo = _FakeRepository()
    docs_n, chunks_n = _run_folder(repo)
    assert docs_n == 1
    assert chunks_n >= 1
    fake_gslides.get_presentation_text.assert_called_once()
    chunks = repo.upsert_calls[0]["chunks"]
    assert all(not c.metadata.get("title_only") for c in chunks)


# -----------------------------------------------------------
# fail-open: adapter 例外 → WARN + title_only フォールバック（crawl は止まらない）
# -----------------------------------------------------------
def test_crawl_gslide_failopen_falls_back_to_title_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ON: GSlidesClient が例外 → fail-open で title_only にフォールバックし doc は作られる。"""
    monkeypatch.setenv("INGEST_RICH_EXTRACT", "1")
    _fake_gdrive_crawl(monkeypatch, [_make_drive_file("GS1", "deck", GSLIDE_MIME, size=None)])

    fake_gslides = MagicMock()
    fake_gslides.get_presentation_text.side_effect = RuntimeError("403 from Slides API")
    monkeypatch.setattr(
        "teamagent.adapters.gslides_client.GSlidesClient.from_env",
        classmethod(lambda cls, **kwargs: fake_gslides),
    )

    repo = _FakeRepository()
    docs_n, chunks_n = _run_crawl(repo)
    # fail-open: 1 件 skip ではなく title_only で doc 化される
    assert docs_n == 1
    assert chunks_n == 1
    chunks = repo.upsert_calls[0]["chunks"]
    assert chunks[0].metadata.get("title_only") is True


def test_crawl_failopen_one_file_does_not_kill_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """ON: 1 ファイルの gslide 抽出失敗が source 全体を落とさない（他ファイルは処理継続）。"""
    monkeypatch.setenv("INGEST_RICH_EXTRACT", "1")
    fake_client = _fake_gdrive_crawl(
        monkeypatch,
        [
            _make_drive_file("BAD", "fail", GSLIDE_MIME, size=None),
            _make_drive_file("OKTXT", "ok.txt", "text/plain"),
        ],
    )
    fake_client.download_file_bytes.return_value = "正常な本文".encode()

    fake_gslides = MagicMock()
    fake_gslides.get_presentation_text.side_effect = RuntimeError("boom")
    monkeypatch.setattr(
        "teamagent.adapters.gslides_client.GSlidesClient.from_env",
        classmethod(lambda cls, **kwargs: fake_gslides),
    )

    repo = _FakeRepository()
    docs_n, _ = _run_crawl(repo)
    # BAD は title_only、OKTXT は本文 → どちらも doc 化（2 件）
    assert docs_n == 2
    ext_ids = {c["external_id"] for c in repo.upsert_calls}
    assert ext_ids == {"BAD", "OKTXT"}


# -----------------------------------------------------------
# §2 dedup ガード: 本文 chunk を title_only chunk が上書きしない
# -----------------------------------------------------------
def test_guarded_upsert_blocks_title_only_over_content() -> None:
    """本文版を書いた (source_type, external_id) を title_only 版で上書きしようとすると skip。"""
    registry: set[tuple[str, str]] = set()
    repo = _FakeRepository()

    content_doc = DocumentUpsert(
        source_type="gdrive",
        external_id="F1",
        source_uri="gdrive://F1",
        title="提案",
        owner_email="bot@x.jp",
        acl_emails=["bot@x.jp"],
        acl_groups=[],
        metadata={},
        modified_at=None,
    )
    content_chunks = [
        ChunkUpsert(chunk_idx=0, content="本文", embedding=[0.1] * 4, metadata={"page_num": 1})
    ]
    title_only_doc = DocumentUpsert(
        source_type="gdrive",
        external_id="F1",
        source_uri="gdrive://F1",
        title="提案",
        owner_email="bot@x.jp",
        acl_emails=["bot@x.jp"],
        acl_groups=[],
        metadata={},
        modified_at=None,
    )
    title_only_chunks = [
        ChunkUpsert(
            chunk_idx=0,
            content="提案 (mime)",
            embedding=[0.1] * 4,
            metadata={"title_only": True},
        )
    ]

    # 1. 本文版を書く → upsert される
    assert (
        _guarded_upsert(
            repo, content_doc, content_chunks, request_id="r", content_registry=registry
        )
        is True
    )
    # 2. 同 key を title_only で上書きしようとする → skip（False）
    assert (
        _guarded_upsert(
            repo, title_only_doc, title_only_chunks, request_id="r", content_registry=registry
        )
        is False
    )
    # repository には本文版の 1 回しか到達していない
    assert len(repo.upsert_calls) == 1
    assert repo.upsert_calls[0]["chunks"][0].metadata.get("title_only") is None


def test_guarded_upsert_allows_content_over_title_only() -> None:
    """逆順（title_only → 本文）は許可。本文版は title_only を上書きしてよい。"""
    registry: set[tuple[str, str]] = set()
    repo = _FakeRepository()

    def _doc() -> DocumentUpsert:
        return DocumentUpsert(
            source_type="gdrive",
            external_id="F2",
            source_uri="gdrive://F2",
            title="t",
            owner_email="bot@x.jp",
            acl_emails=["bot@x.jp"],
            acl_groups=[],
            metadata={},
            modified_at=None,
        )

    title_only_chunks = [
        ChunkUpsert(
            chunk_idx=0, content="t (mime)", embedding=[0.1] * 4, metadata={"title_only": True}
        )
    ]
    content_chunks = [
        ChunkUpsert(chunk_idx=0, content="本文", embedding=[0.1] * 4, metadata={"page_num": 1})
    ]
    assert (
        _guarded_upsert(repo, _doc(), title_only_chunks, request_id="r", content_registry=registry)
        is True
    )
    assert (
        _guarded_upsert(repo, _doc(), content_chunks, request_id="r", content_registry=registry)
        is True
    )
    assert len(repo.upsert_calls) == 2


def test_folder_then_crawl_content_not_clobbered_by_title_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """folder→crawl の実行順で同一 gslide file が本文版→（OFF相当の）title_only版に
    上書きされない。ここでは folder で本文を書いた後、crawl 側を OFF にして同 file が
    title_only になるケースをガードが弾くことを統合的に確認する。

    run() が folder/crawl で 1 個の content_registry を共有することを模して、
    両経路に同じ set を明示注入する（直接 handler 呼び出しでも run と同条件を再現）。"""
    from teamagent.adapters.gdrive_client import DriveFile, SharedDrive
    from teamagent.adapters.gslides_client import SlidesContent

    registry: set[tuple[str, str]] = set()

    file = DriveFile(
        id="GSDUP",
        name="deck",
        mime_type=GSLIDE_MIME,
        modified_time="2026-06-20T16:00:00Z",
        size=None,
        parents=(),
        web_view_link="https://drive.google.com/file/d/GSDUP/view",
        owners_email=("alice@x.jp",),
    )

    repo = _FakeRepository()

    # --- (1) folder 経路を ON で実行 → 本文版が書かれる ---
    monkeypatch.setenv("INGEST_RICH_EXTRACT", "1")
    fake_client_folder = MagicMock()
    fake_client_folder.list_files.return_value = ([file], None)
    fake_client_folder.list_permissions.return_value = [_make_owner_perm()]
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client_folder),
    )
    fake_gslides = MagicMock()
    fake_gslides.get_presentation_text.return_value = SlidesContent(
        presentation_id="GSDUP", title="deck", text="本文。" * 30, slide_count=2
    )
    monkeypatch.setattr(
        "teamagent.adapters.gslides_client.GSlidesClient.from_env",
        classmethod(lambda cls, **kwargs: fake_gslides),
    )
    docs_f, _ = _run_folder(repo, content_registry=registry)
    assert docs_f == 1
    assert repo.upsert_calls[-1]["chunks"][0].metadata.get("title_only") is None

    # --- (2) crawl 経路を OFF で実行 → 同 file は title_only になるが、ガードで skip ---
    monkeypatch.delenv("INGEST_RICH_EXTRACT", raising=False)
    fake_client_crawl = MagicMock()
    fake_client_crawl.list_shared_drives.return_value = [SharedDrive(id="D1", name="営業ナレッジ")]
    fake_client_crawl.walk_files_recursive.return_value = [file]
    fake_client_crawl.list_permissions.return_value = [_make_owner_perm()]
    monkeypatch.setattr(
        "teamagent.adapters.gdrive_client.GDriveClient.from_env",
        classmethod(lambda cls, **kwargs: fake_client_crawl),
    )
    docs_c, _ = _run_crawl(repo, content_registry=registry)
    # ガードで title_only 上書きが skip された → docs=0
    assert docs_c == 0
    # repository には本文版の 1 回のみ到達
    assert len(repo.upsert_calls) == 1
    assert repo.upsert_calls[0]["chunks"][0].metadata.get("title_only") is None
