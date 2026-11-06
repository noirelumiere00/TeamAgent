"""チャンネル共有ファイル取込。API / token store / KMS / HTTP はフェイク、本文抽出は実物。"""

from __future__ import annotations

import dataclasses
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse
from structlog.testing import capture_logs

from teamagent.adapters.oauth_token_store import SlackOAuthToken
from teamagent.ingest.content_hash import INGEST_CONTENT_HASH_KEY
from teamagent.ingest.loader import (
    GDriveFolderSpec,
    IngestSources,
    SlackPublicFilesSpec,
    load_ingest_sources,
)
from teamagent.ingest.pipeline import IngestRunner

TOKEN = "xoxp-fake-for-test"
TOKEN2 = "xoxp-second-for-test"
BOT = "xoxb-fake-for-test"
OWNER = "owner@vectorinc.co.jp"
OWNER2 = "second@vectorinc.co.jp"
SPEC = SlackPublicFilesSpec(extra_metadata={"topic": "Slack 共有資料"})


class Repository:
    def __init__(self) -> None:
        self.documents: dict[str, Any] = {}
        self.chunks: dict[str, list[Any]] = {}

    def get_document_content_hashes(self, source_type: str, ids: list[str]) -> dict[str, str]:
        assert source_type == "other"
        return {
            fid: self.documents[fid].metadata[INGEST_CONTENT_HASH_KEY]
            for fid in ids
            if fid in self.documents
        }

    def upsert_document_with_chunks(self, doc: Any, chunks: list[Any], **kwargs: Any) -> str:
        self.documents[doc.external_id] = doc
        self.chunks[doc.external_id] = chunks
        return doc.external_id


class FakeApi:
    def __init__(self) -> None:
        self.pages: list[dict[str, Any] | Exception] = [{"files": []}]
        self.calls: list[dict[str, Any]] = []
        self.peers: dict[str, FakeApi] = {}
        self.conversations: dict[str, Any] = {}
        self.members: dict[str, Any] = {}
        self.users: dict[str, Any] = {}
        self.info_calls: list[str] = []
        self.member_calls: list[dict[str, Any]] = []
        self.user_calls: list[str] = []
        self.denied_download_tokens: set[str] = set()

    async def files_list(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        index = int(kwargs["cursor"]) if "cursor" in kwargs else kwargs["page"] - 1
        response = self.pages[index]
        if isinstance(response, Exception):
            raise response
        return response

    async def conversations_info(self, *, channel: str) -> dict[str, Any]:
        self.info_calls.append(channel)
        response = self.conversations.get(channel, {})
        if isinstance(response, Exception):
            raise response
        return {"channel": response}

    async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        self.member_calls.append(kwargs)
        pages = self.members.get(kwargs["channel"], [[]])
        if isinstance(pages, Exception):
            raise pages
        index = int(kwargs.get("cursor", 0))
        return {
            "members": pages[index],
            "response_metadata": {"next_cursor": str(index + 1) if index + 1 < len(pages) else ""},
        }

    async def users_info(self, *, user: str) -> dict[str, Any]:
        self.user_calls.append(user)
        response = self.users.get(user, {})
        if isinstance(response, Exception):
            raise response
        return {"user": response}


def file(fid: str, ft: str = "pdf", **kwargs: Any) -> dict[str, Any]:
    return {
        "id": fid,
        "name": f"{fid}.{ft}",
        "filetype": ft,
        "is_public": True,
        "channels": ["CPUBLIC"],
        "groups": [],
        "ims": ["DPRIVATE"],
        "shares": {"public": {"CPUBLIC": []}},
        "timestamp": 1700000000,
        "updated": 1700000100,
        "url_private": f"https://files.slack.com/{fid}",
        "permalink": f"https://example.slack.com/files/U1/{fid}",
        **kwargs,
    }


def private_file(fid: str, **kwargs: Any) -> dict[str, Any]:
    return file(fid, is_public=False, channels=[], groups=["GPRIVATE"], shares={}, **kwargs)


def second_user(api: FakeApi, store: MagicMock) -> FakeApi:
    other = FakeApi()
    other.conversations = dict(api.conversations)
    api.peers[TOKEN2] = other
    store.list_file_reader_emails.return_value = [OWNER, OWNER2]
    store.get.side_effect = lambda email: SlackOAuthToken(
        TOKEN if email == OWNER else TOKEN2, scopes=("files:read",)
    )
    return other


def api_error(code: str) -> SlackApiError:
    return SlackApiError(
        f"must not log {TOKEN} {OWNER}",
        SlackResponse(
            client=None,
            http_verb="GET",
            api_url="https://slack.com/api/files.list",
            req_args={},
            data={"ok": False, "error": code},
            headers={},
            status_code=200,
        ),
    )


def pdf(text: str = "Company shared presentation") -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 700 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = stream
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeApi, MagicMock, list[str]]:
    api = FakeApi()
    api.peers = {TOKEN: api, BOT: FakeApi()}
    api.conversations = {
        "GPRIVATE": {"is_private": True, "is_im": False, "is_mpim": False},
        "GMPIM": {"is_private": True, "is_mpim": True},
        "DPRIVATE": {"is_private": True, "is_im": True},
    }
    api.members = {"GPRIVATE": [["UMEMBER1"], ["UMEMBER2"]]}
    api.peers[BOT].users = {
        "UMEMBER1": {"profile": {"email": "alice@vectorinc.co.jp", "real_name": "Alice"}},
        "UMEMBER2": {"profile": {"email": "bob@vectorinc.co.jp", "real_name": "Bob"}},
        "UMEMBER3": {"profile": {"email": "carol@vectorinc.co.jp", "real_name": "Carol"}},
    }
    store = MagicMock()
    store.list_file_reader_emails.return_value = [OWNER]
    store.scopes.return_value = ("files:read",)
    store.get.return_value = SlackOAuthToken(TOKEN, scopes=("files:read",))
    monkeypatch.setattr("teamagent.adapters.oauth_token_store.SlackTokenStore", lambda *a: store)
    monkeypatch.setattr("teamagent.adapters.oauth_token_store.KmsCipher", lambda *a: object())
    monkeypatch.setattr(
        "teamagent.adapters.pgvector_client.PgVectorClient.from_env", lambda: object()
    )

    def make_api(*, token: str, **_: Any) -> FakeApi:
        return api.peers[token]

    monkeypatch.setattr("teamagent.adapters.slack_client.AsyncWebClient", make_api)
    monkeypatch.setattr("teamagent.adapters.slack_channel_ingest_client.AsyncWebClient", make_api)
    monkeypatch.setenv("SLACK_BOT_TOKEN", BOT)
    monkeypatch.setenv("TEAMAGENT_SHARED_COMPANY_DOMAINS", "vectorinc.co.jp")
    monkeypatch.setenv("DOC_DEDUP_DETECT", "0")
    monkeypatch.setenv("BOILERPLATE_DETECT", "0")
    monkeypatch.setenv("INGEST_MARK_STALE", "0")
    monkeypatch.setattr(IngestRunner, "_maybe_check_freshness", lambda *a, **k: None)
    monkeypatch.setattr("teamagent.ingest.pipeline._send_ops_warning_summary", lambda *a, **k: True)
    from teamagent.ingest.pipeline import _USER_EMAIL_CACHE

    _USER_EMAIL_CACHE.clear()
    downloaded: list[str] = []
    payload = pdf()

    def respond(request: httpx.Request) -> httpx.Response:
        auth = request.headers["Authorization"].removeprefix("Bearer ")
        assert auth in api.peers and auth != BOT
        downloaded.append(request.url.path.lstrip("/"))
        if auth in api.denied_download_tokens:
            return httpx.Response(401)
        if request.url.path == "/FAIL":
            return httpx.Response(500, content=b"private response must not be logged")
        return httpx.Response(
            200, content=pdf("") if request.url.path.startswith("/EMPTY") else payload
        )

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    return api, store, downloaded


def run(
    repo: Repository,
    spec: SlackPublicFilesSpec = SPEC,
    *,
    dry_run: bool = False,
    kinds: list[str] | None = None,
) -> Any:
    embedder = MagicMock(spec=["embed_passage"])
    embedder.embed_passage.return_value = [0.1] * 1024
    return IngestRunner(
        repository=repo,  # type: ignore[arg-type]
        embedder=embedder,
        owner_email="ingester@vectorinc.co.jp",
        dry_run=dry_run,
    ).run(
        IngestSources(
            version=1, slack_channels=(), gdrive_folders=(), gsheets=(), slack_public_files=spec
        ),
        kinds=kinds,
    )


def test_channel_files_all_period_cursor_paging_and_company_acl(setup: Any) -> None:
    api, store, downloaded = setup
    api.pages = [
        {
            "files": [
                file("FPDF", groups=["GSECRET"], ims=["DSECRET"]),
                file("FDM", is_public=False, channels=[], shares={}, ims=["DSECRET"]),
                file("FMPIM", is_public=False, channels=[], shares={}, groups=["GMPIM"]),
                file("FIM", channels=[], shares={"private": {"DPRIVATE": []}}),
                file("FNOSHARES", is_public=True, channels=[], shares={}),
                file("FEXTERNAL", is_external=True),
            ],
            "response_metadata": {"next_cursor": "1"},
        },
        {
            "files": [
                file("FPPT", "ppt", office_pdf="https://files.slack.com/CONVERTED"),
                file("FIMAGE", "png"),
                file("FVIDEO", "mp4"),
                file("FTEXT", "txt"),
                file("FDOCX", "docx"),
            ],
            "response_metadata": {"next_cursor": ""},
        },
    ]
    repo = Repository()
    with capture_logs() as logs:
        result = run(repo)
    assert set(repo.documents) == {"FPDF", "FPPT"}
    assert downloaded == ["FPDF", "CONVERTED"]
    assert api.calls == [
        {"types": "all", "count": 1000, "page": 1},
        {"types": "all", "count": 1000, "cursor": "1"},
    ]
    store.list_file_reader_emails.assert_called_once_with()
    store.scopes.assert_called_once_with(OWNER)
    store.get.assert_called_once_with(OWNER)
    for doc in repo.documents.values():
        assert doc.source_type == "other"
        assert doc.acl_emails == []
        assert doc.acl_groups == ["vectorinc.co.jp"]
        assert doc.metadata["channel_id"] == "CPUBLIC"
        assert doc.metadata["channel_ids"] == ["CPUBLIC"]
        assert doc.metadata["topic"] == "Slack 共有資料"
        assert doc.title == f"{doc.external_id}.{doc.metadata['filetype']}"
        assert doc.source_uri.endswith(doc.external_id)
        assert doc.modified_at == "2023-11-14T22:15:00+00:00"
        assert repo.chunks[doc.external_id][0].content == "Company shared presentation"
    assert result.total_documents() == 2 and result.total_errors() == 0
    assert TOKEN not in str(logs)
    assert "Company shared presentation" not in str(logs)
    assert "GSECRET" not in str(logs) and "DSECRET" not in str(logs)
    assert "@vectorinc.co.jp" not in str(logs)


def test_pptx_real_extraction(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from pptx import Presentation

    api, _, _ = setup
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text = "Sales proposal"
    buf = BytesIO()
    deck.save(buf)
    data = buf.getvalue()
    api.pages = [{"files": [file("FPPTX", "pptx", size=len(data))]}]

    async def download(self: Any, url: str, **kwargs: Any) -> bytes:
        return data

    monkeypatch.setattr(
        "teamagent.adapters.slack_client.SlackClient.download_file_guarded", download
    )
    repo = Repository()
    assert run(repo).total_documents() == 1
    assert repo.chunks["FPPTX"][0].content == "Sales proposal"


def test_pptx_keeps_tables_notes_and_short_slides(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    api, _, _ = setup
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(1), Inches(4), Inches(1)).table
    table.cell(0, 0).text = "再生数"
    table.cell(1, 0).text = "1,234,567"
    slide.notes_slide.notes_text_frame.text = "CPV 2.1 円"
    deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text = "OK"
    buf = BytesIO()
    deck.save(buf)
    data = buf.getvalue()
    api.pages = [{"files": [file("FRAW", "pptx", size=len(data))]}]

    async def download(self: Any, url: str, **kwargs: Any) -> bytes:
        return data

    monkeypatch.setattr(
        "teamagent.adapters.slack_client.SlackClient.download_file_guarded", download
    )
    repo = Repository()
    assert run(repo).total_documents() == 1
    text = "\n".join(c.content for c in repo.chunks["FRAW"])
    assert "1,234,567" in text and "CPV 2.1 円" in text and "OK" in text


def test_two_owners_deduplicate_and_union_public_and_private_shares(setup: Any) -> None:
    api, store, downloaded = setup
    other = second_user(api, store)
    api.pages = [{"files": [file("FSAME", channels=["CPUBLIC"], shares={})]}]
    other.pages = [{"files": [private_file("FSAME", updated=1700000200)]}]
    repo = Repository()
    with capture_logs() as logs:
        result = run(repo)
    assert result.total_documents() == 1 and downloaded == ["FSAME"]
    doc = repo.documents["FSAME"]
    assert doc.metadata["channel_ids"] == ["CPUBLIC", "GPRIVATE"]
    assert doc.metadata["public_channel_ids"] == ["CPUBLIC"]
    assert doc.metadata["private_channel_ids"] == ["GPRIVATE"]
    assert doc.modified_at == "2023-11-14T22:16:40+00:00"
    assert doc.acl_groups == ["vectorinc.co.jp"] and doc.acl_emails == []
    assert not api.member_calls and not other.member_calls
    summary = next(e for e in logs if e["event"] == "slack_public_files_complete")
    assert summary["owners"] == 2 and summary["owners_processed"] == 2
    assert "@vectorinc.co.jp" not in str(logs) and TOKEN2 not in str(logs)


def test_private_acl_union_members_pagination_and_acl_change_updates_hash(setup: Any) -> None:
    api, store, downloaded = setup
    other = second_user(api, store)
    api.pages = [{"files": [private_file("FSAME")]}]
    other.conversations["GSECOND"] = {"is_private": True, "is_mpim": False, "is_im": False}
    other.members["GSECOND"] = [["UMEMBER3"]]
    other.pages = [
        {"files": [file("FSAME", channels=[], groups=[], shares={"private": {"GSECOND": []}})]}
    ]
    repo = Repository()
    with capture_logs() as logs:
        assert run(repo).total_documents() == 1
    doc = repo.documents["FSAME"]
    assert doc.acl_groups == []
    assert doc.acl_emails == [
        "alice@vectorinc.co.jp",
        "bob@vectorinc.co.jp",
        "carol@vectorinc.co.jp",
    ]
    assert doc.owner_email in doc.acl_emails
    assert doc.owner_email != "ingester@vectorinc.co.jp"
    assert doc.metadata["channel_ids"] == ["GPRIVATE", "GSECOND"]
    assert api.member_calls == [
        {"channel": "GPRIVATE", "limit": 200},
        {"channel": "GPRIVATE", "limit": 200, "cursor": "1"},
    ]
    assert other.member_calls == [{"channel": "GSECOND", "limit": 200}]
    assert not api.user_calls and not other.user_calls
    assert set(api.peers[BOT].user_calls) == {"UMEMBER1", "UMEMBER2", "UMEMBER3"}
    assert "@vectorinc.co.jp" not in str(logs) and "Alice" not in str(logs)
    old_hash = doc.metadata[INGEST_CONTENT_HASH_KEY]
    assert run(repo).total_documents_unchanged() == 1
    api.members["GPRIVATE"] = [["UMEMBER2"]]
    assert run(repo).total_documents() == 1
    updated = repo.documents["FSAME"]
    assert updated.acl_emails == ["bob@vectorinc.co.jp", "carol@vectorinc.co.jp"]
    assert updated.owner_email == "bob@vectorinc.co.jp"
    assert updated.metadata[INGEST_CONTENT_HASH_KEY] != old_hash
    assert downloaded == ["FSAME", "FSAME"]


def test_private_without_email_resolution_is_skipped(setup: Any) -> None:
    api, _, downloaded = setup
    api.pages = [{"files": [private_file("FPRIVATE"), file("FOK")]}]
    api.peers[BOT].users = {}
    repo = Repository()
    result = run(repo)
    assert set(repo.documents) == {"FOK"} and downloaded == ["FOK"]
    assert result.by_kind["slack_public_files"].warning_reasons == {
        "slack_public_files_file_failed": 1
    }


def test_invalid_auth_owner_does_not_stop_other_owner(setup: Any) -> None:
    api, store, downloaded = setup
    other = second_user(api, store)
    api.pages = [api_error("invalid_auth")]
    other.pages = [{"files": [file("FOK")]}]
    repo = Repository()
    with capture_logs() as logs:
        result = run(repo)
    assert downloaded == ["FOK"] and set(repo.documents) == {"FOK"}
    assert result.total_errors() == 0
    assert result.by_kind["slack_public_files"].warning_reasons == {
        "slack_public_files_token_unavailable": 1
    }
    assert len(api.calls) == len(other.calls) == 1
    assert TOKEN not in str(logs) and "@vectorinc.co.jp" not in str(logs)


def test_private_member_token_failure_uses_another_channel_member(setup: Any) -> None:
    api, store, downloaded = setup
    other = second_user(api, store)
    api.pages = other.pages = [{"files": [private_file("FPRIVATE")]}]
    api.members["GPRIVATE"] = api_error("invalid_auth")
    other.members["GPRIVATE"] = [["UMEMBER2"]]
    api.denied_download_tokens.add(TOKEN)
    repo = Repository()
    assert run(repo).total_documents() == 1
    assert repo.documents["FPRIVATE"].acl_emails == ["bob@vectorinc.co.jp"]
    assert downloaded == ["FPRIVATE", "FPRIVATE"]
    assert not api.peers[BOT].member_calls


def test_empty_files_do_not_use_success_cap(setup: Any) -> None:
    api, _, downloaded = setup
    api.pages = [{"files": [*[file(f"EMPTY{i}") for i in range(301)], file("FOK"), file("FLATER")]}]
    repo = Repository()
    with capture_logs() as logs:
        result = run(repo, dataclasses.replace(SPEC, max_files_per_run=1))
    assert result.total_documents() == 1
    assert set(repo.documents) == {"FOK"} and downloaded[-1] == "FOK"
    summary = next(e for e in logs if e["event"] == "slack_public_files_complete")
    assert summary["processed"] == 302 and summary["remaining"] == 1
    assert summary["skipped"] == 301


@pytest.mark.parametrize(
    "kinds", [["slack"], ["slack", "slack_public_files"], ["slack_public_files"]]
)
def test_slack_kind_runs_declared_files_once(setup: Any, kinds: list[str]) -> None:
    api, _, downloaded = setup
    api.pages = [{"files": [file("FOK")]}]
    result = run(Repository(), kinds=kinds)
    assert result.by_kind["slack_public_files"].documents_upserted == 1
    assert downloaded == ["FOK"] and len(api.calls) == 1


def test_member_pagination_must_complete() -> None:
    from teamagent.ingest.pipeline import _collect_all_member_ids

    client = MagicMock()
    client.list_channel_members.return_value = (["UMEMBER1"], "more")
    with pytest.raises(ValueError, match="incomplete"):
        _collect_all_member_ids(client, "GPRIVATE", "r", max_pages=1)


def test_version_skip_cap_remaining_and_next_run(setup: Any) -> None:
    api, _, downloaded = setup
    api.pages = [
        {"files": [file("FA"), file("FB")], "paging": {"page": 1, "pages": 2}},
        {"files": [file("FC"), file("FA")], "paging": {"page": 2, "pages": 2}},
    ]
    repo = Repository()
    spec = dataclasses.replace(SPEC, max_files_per_run=1)
    for expected, remaining in [("FA", 2), ("FB", 1), ("FC", 0)]:
        with capture_logs() as logs:
            assert run(repo, spec).total_documents() == 1
        summary = next(e for e in logs if e["event"] == "slack_public_files_complete")
        assert summary["processed"] == 1
        assert summary["remaining"] == remaining
        assert downloaded[-1] == expected
    assert run(repo, spec).total_documents_unchanged() == 3
    assert downloaded == ["FA", "FB", "FC"]
    api.pages[0]["files"][0]["updated"] += 1
    assert run(repo, spec).total_documents() == 1
    assert downloaded == ["FA", "FB", "FC", "FA"]


def test_backfill_env_zero_removes_cap(setup: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    api, _, downloaded = setup
    api.pages = [{"files": [file("FA"), file("FB"), file("FC")], "paging": {"page": 1, "pages": 1}}]
    monkeypatch.setenv("SLACK_FILES_MAX_PER_RUN", "0")
    assert run(Repository(), dataclasses.replace(SPEC, max_files_per_run=1)).total_documents() == 3
    assert downloaded == ["FA", "FB", "FC"]


@pytest.mark.parametrize("changed", [{"size": 2}, {"timestamp": 1700000200}])
def test_version_without_updated_uses_size_and_timestamp(
    setup: Any, changed: dict[str, Any]
) -> None:
    api, _, downloaded = setup
    api.pages = [{"files": [file("FA", updated=None, size=1)]}]
    repo = Repository()
    assert run(repo).total_documents() == 1
    assert run(repo).total_documents_unchanged() == 1
    api.pages[0]["files"][0].update(changed)
    assert run(repo).total_documents() == 1
    assert downloaded == ["FA", "FA"]


def test_bad_download_bad_extract_and_missing_ppt_conversion_skip_only_file(setup: Any) -> None:
    api, _, downloaded = setup
    api.pages = [{"files": [file("FAIL"), file("FBAD", "pptx"), file("FOLD", "ppt"), file("FOK")]}]
    repo = Repository()
    with capture_logs() as logs:
        result = run(repo)
    assert set(repo.documents) == {"FOK"}
    assert downloaded == ["FAIL", "FBAD", "FOK"]
    assert result.by_kind["slack_public_files"].warning_reasons == {
        "slack_public_files_file_failed": 3
    }
    assert TOKEN not in str(logs) and "private response" not in str(logs)


@pytest.mark.parametrize("failure", ["missing", "missing_scope", "revoked", "expired"])
def test_token_unavailable_warns_and_other_source_continues(
    setup: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    api, store, downloaded = setup
    if failure == "missing":
        store.get.return_value = None
    elif failure == "missing_scope":
        store.scopes.return_value = ("search:read",)
    else:
        api.pages = [api_error("token_revoked" if failure == "revoked" else "token_expired")]
    other_source = MagicMock(return_value=(1, 2))
    monkeypatch.setattr("teamagent.ingest.pipeline._ingest_gdrive_folder", other_source)
    repo = Repository()
    with capture_logs() as logs:
        result = IngestRunner(
            repository=repo,  # type: ignore[arg-type]
            embedder=MagicMock(),
            owner_email="ingester@vectorinc.co.jp",
            dry_run=True,
        ).run(
            IngestSources(
                version=1,
                slack_channels=(),
                gdrive_folders=(GDriveFolderSpec("D1", "shared", ""),),
                gsheets=(),
                slack_public_files=SPEC,
            ),
            kinds=["slack_public_files", "gdrive"],
        )
    assert result.total_errors() == 0
    assert result.by_kind["slack_public_files"].warning_reasons == {
        "slack_public_files_token_unavailable": 1
    }
    assert result.by_kind["gdrive"].documents_upserted == 1
    other_source.assert_called_once()
    assert not downloaded and not repo.documents
    assert TOKEN not in str(logs)


def test_dry_run_does_not_write(setup: Any) -> None:
    api, _, _ = setup
    api.pages = [{"files": [file("FA")]}]
    repo = Repository()
    assert run(repo, dry_run=True).total_documents() == 1
    assert not repo.documents


def test_download_host_guard_and_pagination_failure_do_not_leak(setup: Any) -> None:
    api, _, downloaded = setup
    api.pages = [{"files": [file("FEVIL", url_private="https://evil.example/steal"), file("FOK")]}]
    repo = Repository()
    assert run(repo).total_documents() == 1
    assert downloaded == ["FOK"] and set(repo.documents) == {"FOK"}

    api.pages = [
        {"files": [file("FA")], "response_metadata": {"next_cursor": "1"}},
        RuntimeError(f"must not log {TOKEN}"),
    ]
    with capture_logs() as logs:
        result = run(repo)
    assert result.by_kind["slack_public_files"].warning_reasons == {
        "slack_public_files_list_failed": 1
    }
    assert downloaded == ["FOK"] and TOKEN not in str(logs)


def test_loader_real_yaml_and_optional_section() -> None:
    sources = load_ingest_sources(Path(__file__).resolve().parents[2] / "data/ingest_sources.yaml")
    assert sources.slack_public_files == dataclasses.replace(SPEC, max_files_per_run=2000)
    assert IngestSources(1, (), (), ()).slack_public_files is None


@pytest.mark.parametrize(
    "override",
    [
        {"token_owners": ""},
        {"token_owners": [OWNER]},
        {"filetypes": ["pdf", "png"]},
        {"filetypes": "pdf"},
        {"filetypes": []},
        {"max_files_per_run": 0},
    ],
)
def test_loader_rejects_invalid_config(tmp_path: Path, override: dict[str, Any]) -> None:
    import yaml

    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump({"slack_public_files": override}))
    with pytest.raises(ValueError):
        load_ingest_sources(path)
