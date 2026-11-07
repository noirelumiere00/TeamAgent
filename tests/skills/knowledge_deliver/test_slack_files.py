"""Slack 正本を本人権限で再取得し、既存の DM 添付へ渡す。"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from teamagent.adapters.oauth_token_store import InMemorySlackTokenStore, SlackOAuthToken
from teamagent.adapters.pgvector_client import PgVectorClient
from teamagent.adapters.slack_client import SlackClient
from teamagent.skills.knowledge_deliver.schema import KnowledgeDeliverInput
from teamagent.skills.knowledge_deliver.skill import KnowledgeDeliverSkill
from teamagent.skills.search import fanout
from teamagent.skills.search.composite import SlackLookup
from teamagent.skills.search.expand import ExpansionResult
from tests.skills.knowledge_deliver.test_knowledge_deliver import (
    _ctx,
    _gdrive_mock,
    _hit,
    _search_mock,
    _slack_mock,
)
from tests.skills.search.test_fanout import META, make

ME = "u@vectorinc.co.jp"
FILE_ID = "F0SHARED"
PERMALINK = f"https://example.slack.com/files/U0OWNER/{FILE_ID}/proposal.pdf"
DOWNLOAD = f"https://files.slack.com/files-pri/T0-{FILE_ID}/proposal.pdf"


@pytest.fixture(autouse=True)
def enable_delivery(monkeypatch):
    monkeypatch.setenv("USE_SEARCH_FILE_DELIVERY", "1")
    monkeypatch.setenv("SEARCH_FILE_DELIVERY_ALLOWED_EMAILS", "*")


def _slack_hit(**kw: object):
    meta: dict[str, object] = {
        "source_type": "other",
        "external_id": FILE_ID,
        "source_uri": PERMALINK,
        "title": "共有資料.pdf",
        "page_num": 2,
    }
    meta.update(kw)
    return _hit(**meta)


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch):
    api = MagicMock()
    api.files_info = AsyncMock(
        return_value={"ok": True, "file": {"id": FILE_ID, "url_private_download": DOWNLOAD}}
    )
    client = SlackClient.from_user_token("xoxp-requester", client=api)
    client.download_file_guarded = AsyncMock(return_value=b"%PDF-slack")
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(SlackClient, "from_user_token", factory)
    return client, api, factory


def _store(email: str = ME):
    return InMemorySlackTokenStore(
        {email: SlackOAuthToken(access_token="xoxp-requester", scopes=("files:read",))}
    )


def test_slack_deliver_uses_requester_token_and_shared_attachment_path(reader) -> None:
    client, api, factory = reader
    slack, drive = _slack_mock(), _gdrive_mock()
    seen_paths = []

    async def upload(channel, files, request_id, **kw):
        assert channel == "D0SELF"
        assert [name for _, name in files] == ["共有資料.pdf"]
        for path, _ in files:
            assert Path(path).read_bytes() == b"%PDF-slack"
            seen_paths.append(Path(path))
        assert f"原本: {PERMALINK}" in kw["initial_comment"]
        return "posted"

    slack.upload_files.side_effect = upload
    skill = KnowledgeDeliverSkill(
        search=_search_mock([_slack_hit(resolved_file_name="別のDrive資料.pdf")]),
        slack=slack,
        gdrive=drive,
        slack_store=_store(),
    )
    ctx = _ctx()
    out = skill.run(KnowledgeDeliverInput(query="資料を出して"), ctx)
    factory.assert_called_once_with("xoxp-requester")
    api.files_info.assert_awaited_once_with(file=FILE_ID)
    client.download_file_guarded.assert_awaited_once_with(
        DOWNLOAD, request_id=ctx.request_id, max_bytes=256 * 1024 * 1024
    )
    drive.download_file_bytes.assert_not_called()
    assert out.delivered_count == 1 and out.delivery is not None
    assert out.references[0].url == PERMALINK and out.references[0].delivered
    assert "要約です" in out.answer and "該当: 2 ページ" in out.answer
    assert seen_paths and all(not p.parent.parent.exists() for p in seen_paths)


def test_slack_deliver_sanitizes_filename_and_uses_private_url_fallback(reader) -> None:
    client, api, _ = reader
    api.files_info.return_value = {"ok": True, "file": {"id": FILE_ID, "url_private": DOWNLOAD}}
    slack = _slack_mock()
    out = KnowledgeDeliverSkill(
        search=_search_mock([_slack_hit(title="../提案\\資料\n.pptx")]),
        slack=slack,
        slack_store=_store(),
    ).run(KnowledgeDeliverInput(query="資料を出して"), _ctx())
    assert out.delivered_count == 1
    path, name = slack.upload_files.await_args.args[1][0]
    assert name == ".._提案_資料_.pptx" and Path(path).name == name
    assert "該当: 2 スライド" in out.answer
    assert client.download_file_guarded.await_args.args == (DOWNLOAD,)


@pytest.mark.parametrize(
    "failure",
    [
        "no_token",
        "missing_scope",
        "token_missing_scope",
        "token_disappeared",
        "store_failure",
        "file_not_found",
        "access_denied",
        "missing_scope_api",
        "download_denied",
    ],
)
def test_slack_deliver_fail_open_with_original_and_summary(reader, failure: str) -> None:
    client, api, factory = reader
    store = MagicMock(wraps=_store())
    if failure == "no_token":
        store = MagicMock(
            wraps=InMemorySlackTokenStore(
                {"another@vectorinc.co.jp": SlackOAuthToken("xoxp-another", ("files:read",))}
            )
        )
    elif failure == "missing_scope":
        store.scopes.return_value = ("search:read",)
    elif failure == "token_missing_scope":
        store.get.return_value = SlackOAuthToken("xoxp-requester", ("search:read",))
    elif failure == "token_disappeared":
        store.get.return_value = None
    elif failure == "store_failure":
        store.scopes.side_effect = RuntimeError("db unavailable")
    elif failure == "download_denied":
        client.download_file_guarded.side_effect = PermissionError("403")
    else:
        api.files_info.side_effect = SlackApiError(
            "Slack files.info failed", {"ok": False, "error": failure.removesuffix("_api")}
        )
    slack = _slack_mock()
    out = KnowledgeDeliverSkill(
        search=_search_mock([_slack_hit(url=PERMALINK)]), slack=slack, slack_store=store
    ).run(KnowledgeDeliverInput(query="資料を出して"), _ctx())
    assert out.delivered_count == 0 and out.delivery is None
    assert "要約です" in out.answer and f"原本: {PERMALINK}" in out.answer
    assert out.references[0].url == PERMALINK and not out.references[0].delivered
    slack.upload_files.assert_not_awaited()
    store.scopes.assert_called_once_with(ME)
    if failure in {"no_token", "missing_scope", "store_failure"}:
        store.get.assert_not_called()
    if failure not in {"file_not_found", "access_denied", "missing_scope_api", "download_denied"}:
        factory.assert_not_called()
    if failure != "download_denied":
        client.download_file_guarded.assert_not_awaited()


@pytest.mark.parametrize("slack_first", [False, True])
def test_slack_deliver_mixed_candidates_share_limit_and_keep_order(
    reader, slack_first: bool
) -> None:
    _, api, _ = reader
    drive_hits = [
        _hit(
            chunk_id=i, source_type="gdrive", source_uri=f"gdrive://DRIVE{i}", title=f"資料{i}.pdf"
        )
        for i in range(1, 4)
    ]
    hits = [*drive_hits[:1], _slack_hit(), *drive_hits[1:]]
    if slack_first:
        hits = [_slack_hit(), *drive_hits]
    slack, drive = _slack_mock(), _gdrive_mock()
    out = KnowledgeDeliverSkill(
        search=_search_mock(hits), slack=slack, gdrive=drive, slack_store=_store()
    ).run(KnowledgeDeliverInput(query="資料を出して", top_k=5), _ctx())
    names = [name for _, name in slack.upload_files.await_args.args[1]]
    assert names == (
        ["共有資料.pdf", "資料1.pdf", "資料2.pdf"]
        if slack_first
        else ["資料1.pdf", "共有資料.pdf", "資料2.pdf"]
    )
    assert out.delivered_count == 3
    assert [r.delivered for r in out.references] == [True, True, True, False]
    assert drive.download_file_bytes.call_count == 2
    api.files_info.assert_awaited_once_with(file=FILE_ID)


@pytest.mark.parametrize(
    "channel,verified", [("C0PUBLIC", True), ("G0GROUP", True), ("D0SELF", False)]
)
def test_slack_deliver_surface_guard_keeps_original_and_summary_in_requester_dm(
    reader, channel: str, verified: bool
) -> None:
    slack = _slack_mock()
    out = KnowledgeDeliverSkill(
        search=_search_mock([_slack_hit()]), slack=slack, slack_store=_store()
    ).run(KnowledgeDeliverInput(query="資料を出して"), _ctx(channel_id=channel, verified=verified))
    assert out.answer == "" and out.references == []
    assert out.delivered_count == 1
    assert slack.upload_files.await_args.args[0] == "D1"
    assert f"原本: {PERMALINK}" in slack.upload_files.await_args.kwargs["initial_comment"]
    slack.lookup_user_id_by_email.assert_awaited_once()


@pytest.mark.parametrize(
    "method",
    ["search_similar_new_schema", "search_topic_by_terms", "find_documents_for_advertiser"],
)
@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("public_fallback", [False, True])
def test_slack_deliver_search_projection_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
    reader,
    method: str,
    fanout_enabled: bool,
    public_fallback: bool,
) -> None:
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", "*")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", "*")
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    monkeypatch.setattr(fanout, "build_expansion_planner", MagicMock)
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), None, (), "empty", 1, 0.0)
    )
    db = MagicMock()
    cur = db.cursor.return_value.__enter__.return_value

    def rows():
        row = {
            "chunk_id": 1,
            "document_id": "slack-doc",
            "content": "共有資料の本文",
            "title": "共有資料.pdf",
            "source_type": "other",
            "source_uri": PERMALINK,
            "score": 0.9,
        }
        if "d.external_id" in cur.execute.call_args.args[0]:
            row["external_id"] = FILE_ID
        if public_fallback:
            row["file_metadata"] = row["metadata"] = row["doc_metadata"] = {
                "public_channel_ids": ["C0PUBLIC"]
            }
        return [row]

    cur.fetchall.side_effect = rows
    pg = PgVectorClient("postgresql://stub")
    if method == "search_similar_new_schema":
        hits = pg.search_similar_new_schema(db, [0.1] * 8)
    elif method == "search_topic_by_terms":
        hits = pg.search_topic_by_terms(db, [0.1] * 8, topic=None, terms=["共有資料"])
    else:
        hits = pg.find_documents_for_advertiser(db, "共有資料")
    search, _ = make(hits)
    search._summarize = MagicMock(return_value=("共有資料の要約です。", 0.0))
    slack = _slack_mock()
    ctx = _ctx(email=str(META["user_email"]))
    store = (
        InMemorySlackTokenStore(
            {"other@vectorinc.co.jp": SlackOAuthToken("xoxp-other", ("files:read",))}
        )
        if public_fallback
        else _store(str(META["user_email"]))
    )
    out = KnowledgeDeliverSkill(search=search, slack=slack, slack_store=store).run(
        KnowledgeDeliverInput(query="資料を出して"), ctx
    )
    assert out.delivered_count == 1
    assert out.references[0].url == PERMALINK
    assert f"原本: {PERMALINK}" in out.answer
    if public_fallback:
        reader[2].assert_called_once_with("xoxp-other")


@pytest.mark.parametrize(
    "flag,emails", [(None, "*"), ("0", "*"), ("1", ""), ("1", "another@vectorinc.co.jp")]
)
def test_knowledge_deliver_flag_off_keeps_original_links_and_drive_delivery(
    monkeypatch, reader, flag, emails
):
    if flag is None:
        monkeypatch.delenv("USE_SEARCH_FILE_DELIVERY")
    else:
        monkeypatch.setenv("USE_SEARCH_FILE_DELIVERY", flag)
    monkeypatch.setenv("SEARCH_FILE_DELIVERY_ALLOWED_EMAILS", emails)
    _, _, factory = reader
    slack, drive = _slack_mock(), _gdrive_mock()
    out = KnowledgeDeliverSkill(
        search=_search_mock(
            [
                _slack_hit(),
                _hit(source_type="gdrive", source_uri="gdrive://DRIVE1", title="資料.pdf"),
            ]
        ),
        slack=slack,
        gdrive=drive,
        slack_store=_store(),
    ).run(KnowledgeDeliverInput(query="資料を出して"), _ctx())
    assert out.delivered_count == 1
    assert f"原本: {PERMALINK}" in out.answer and "要約です" in out.answer
    assert not out.references[0].delivered and out.references[1].delivered
    factory.assert_not_called()
    drive.download_file_bytes.assert_called_once()
