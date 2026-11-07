"""本人の検索結果の一覧を先に投稿し、同じ DM スレッドへ実ファイルを届ける。"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from teamagent.adapters.gdrive_client import GDriveClient
from teamagent.adapters.oauth_token_store import InMemorySlackTokenStore, SlackOAuthToken
from teamagent.adapters.pgvector_client import SearchHit
from teamagent.adapters.slack_client import SlackClient
from teamagent.mcp_gateway import detached_jobs, direct_search_answer, search_file_delivery, server
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.search import fanout
from teamagent.skills.search.composite import SlackLookup
from teamagent.skills.search.expand import ExpansionResult
from teamagent.skills.search.schema import (
    DocumentReference,
    SearchHitOut,
    SearchInput,
    SearchOutput,
    SlackHitOut,
)
from tests.mcp_gateway.test_surface_video_followup import (
    DM,
    ME,
    _claim,
    _FakeSlack,
    _resolver_for,
    _spec,
    _Verifier,
)
from tests.skills.search.test_fanout import make

POST = "https://example.slack.com/archives/C0SHARED/p1784424000000001"
FILE = "https://example.slack.com/files/U0OWNER/F0ONLY/proposal.pdf"
THREAD = "1784424000.000001"


def file_ref(fid):
    return DocumentReference(
        kind="slack_attachment",
        title=f"飲料提案{fid}.pdf",
        url=FILE.replace("F0ONLY", fid),
        source_uri=FILE.replace("F0ONLY", fid),
        slack_file_ids=[fid],
    )


class _OutputSearch(BaseSkill[SearchInput, SearchOutput]):
    input_schema = SearchInput
    output_schema = SearchOutput

    def __init__(self, output):
        self.output = output

    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        return self.output


@pytest.mark.parametrize("answer_format", ["compact", "list", None])
def test_attach_candidates_only_use_answer_links_and_citations_in_answer_order(answer_format):
    first, second, unused = [file_ref(fid) for fid in ("F0FIRST", "F0SECOND", "F0UNUSED")]
    output = SearchOutput(
        answer=f"先に <{second.url}|資料を見る>、次に事例 [S:11]。再掲 {second.url}",
        answer_format=answer_format,
        total_cost_usd=0,
        attachment_refs=[unused, first, second],
        hits=[
            SearchHitOut(chunk_id=i, content="架空の事例", score=0.9, reference=ref)
            for i, ref in ((10, unused), (11, first), (12, second))
        ],
    )
    assert search_file_delivery.candidates(output) == [
        ("F0SECOND", second.title),
        ("F0FIRST", first.title),
    ]


def test_compact_attach_excludes_drive_copy_and_post_only_without_file_id():
    slack = file_ref("F0COPY")
    drive = slack.model_copy(update={"kind": "drive", "drive_file_id": "DRIVE1"})
    post_only = DocumentReference(kind="slack_attachment", title="架空資料.pdf", url=POST)
    output = SearchOutput(
        answer=f"資料 {slack.url}、投稿 {POST}",
        answer_format="compact",
        attachment_refs=[slack, post_only],
        displayed_refs=[drive],
        total_cost_usd=0,
    )
    assert search_file_delivery.candidates(output) == []


def test_compact_attach_legacy_hits_and_slack_citations_limit_to_three():
    refs = [file_ref(f"F0FILE{i}") for i in range(5)]
    output = SearchOutput(
        answer=f"[S:slack0]、{refs[3].url}、[S:2]、{refs[1].url}、{refs[0].url}",
        answer_format="compact",
        total_cost_usd=0,
        hits=[
            SearchHitOut(
                chunk_id=i,
                content="架空の資料",
                score=0.9,
                source_type="other",
                source_uri=ref.url,
                external_id=ref.slack_file_ids[0],
                title=ref.title,
            )
            for i, ref in enumerate(refs[:4])
        ],
        slack_hits=[
            SlackHitOut(
                permalink=POST,
                file_ids=refs[4].slack_file_ids,
                file_names=[refs[4].title],
            )
        ],
    )
    assert [fid for fid, _ in search_file_delivery.candidates(output)] == [
        "F0FILE4",
        "F0FILE3",
        "F0FILE2",
    ]


def test_compact_attach_does_not_match_url_or_citation_prefix():
    ref = file_ref("F0ONE")
    output = SearchOutput(
        answer=f"資料 {ref.url}.other [S:110]",
        total_cost_usd=0,
        hits=[SearchHitOut(chunk_id=11, content="架空資料", score=0.9, reference=ref)],
    )
    assert search_file_delivery.candidates(output) == []


def drive_hit(index=1, title=None, size=100):
    return SearchHit(
        index,
        "飲料の提案内容です。",
        0.9,
        {
            "title": title or f"飲料事例{index}.pdf",
            "source_type": "gdrive",
            "source_uri": f"gdrive://DRIVE{index}",
            "size": size,
            "updated_at": "2026-10-01",
        },
    )


def slack_hit(source_type="slack", fid="F0ONLY", title="飲料提案.pdf", size=200):
    return SearchHit(
        10,
        "Slack共有: 飲料の提案。Slack での共有から配信面を確認。",
        0.9,
        {
            "title": title,
            "source_type": source_type,
            "source_uri": POST if source_type == "slack" else FILE,
            "external_id": "C0SHARED:1784424000.000001" if source_type == "slack" else fid,
            "size": size,
            "updated_at": "2026-10-02",
        },
    )


@pytest.fixture
def services(monkeypatch):
    from teamagent.skills.oauth_connect import skill as oauth

    for env in (
        "SEARCH_DIRECT_ANSWER",
        "SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS",
        "SEARCH_DIRECT_ANSWER_KINDS",
        "USE_SEARCH_FANOUT",
        "SEARCH_DRIVE_FIRST",
        "ENABLE_PROGRESS_NOTIFY",
        "USE_COMPOSITE_SEARCH",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("USE_PAYLOAD_OFFLOAD", "false")
    monkeypatch.setenv("USE_SEARCH_FILE_DELIVERY", "1")
    monkeypatch.setenv("SEARCH_FILE_DELIVERY_ALLOWED_EMAILS", "*")
    monkeypatch.setattr(server, "_record_usage", lambda **kw: None)
    monkeypatch.setattr(server, "_inject_search_web_links", lambda data: None)
    monkeypatch.setattr(detached_jobs, "_POST_RETRY_WAIT_S", 0)
    monkeypatch.setattr(fanout, "lookup_slack", lambda **kw: SlackLookup("ok", "ok"))
    monkeypatch.setattr(fanout, "build_expansion_planner", MagicMock)
    monkeypatch.setattr(
        fanout, "run_expansion", lambda *a, **kw: ExpansionResult((), None, (), "empty", 1, 0.0)
    )
    events = []
    store = InMemorySlackTokenStore({ME: SlackOAuthToken("xoxp-requester", ("files:read",))})
    monkeypatch.setattr(oauth, "_build_slack_store", lambda: store)
    api = MagicMock()
    api.files_info = AsyncMock(
        return_value={
            "ok": True,
            "file": {
                "id": "F0ONLY",
                "name": "飲料提案.pdf",
                "size": 200,
                "permalink": FILE,
                "url_private_download": "https://files.slack.com/files-pri/T0-F0ONLY/proposal.pdf",
            },
        }
    )
    api.conversations_replies = AsyncMock(
        return_value={
            "ok": True,
            "messages": [{"ts": THREAD, "files": [{"id": "F0ONLY", "name": "飲料提案.pdf"}]}],
        }
    )
    reader = SlackClient.from_user_token("xoxp-requester", client=api)

    async def download(*args, **kw):
        events.append("download")
        return b"%PDF-user-file"

    reader.download_file_guarded = AsyncMock(side_effect=download)
    reader_factory = MagicMock(return_value=reader)
    monkeypatch.setattr(SlackClient, "from_user_token", reader_factory)
    bot = _FakeSlack()
    post = bot.post_message

    async def post_message(**kw):
        events.append("post")
        return await post(**kw)

    async def upload_files(channel, files, request_id, **kw):
        events.append("upload")
        assert channel == DM and kw["thread_ts"] == THREAD
        assert len(bot.posts) == 1
        assert all(Path(path).read_bytes() == b"%PDF-user-file" for path, _ in files)
        return "posted"

    bot.post_message = post_message
    bot.upload_files = AsyncMock(side_effect=upload_files)
    monkeypatch.setattr(detached_jobs, "_slack_client", lambda timeout_seconds: bot)
    google = MagicMock()
    google.get_file_metadata.return_value = {"id": "DRIVE1", "name": "飲料提案.pdf", "size": "200"}
    monkeypatch.setattr(GDriveClient, "from_user_token", lambda *a, **kw: google)
    return SimpleNamespace(
        store=store,
        api=api,
        reader=reader,
        reader_factory=reader_factory,
        bot=bot,
        events=events,
        google=google,
        urls={},
    )


async def dispatch(
    monkeypatch,
    services,
    hits,
    *,
    direct=False,
    fanout_enabled=False,
    channel=DM,
    verified=True,
    query="飲料の事例５件出して",
):
    if direct:
        monkeypatch.setenv("SEARCH_DIRECT_ANSWER", "1")
        monkeypatch.setenv("SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS", ME)
        monkeypatch.setenv("SEARCH_DIRECT_ANSWER_KINDS", "case_cards,list_fast")
    monkeypatch.setenv("USE_SEARCH_FANOUT", str(int(fanout_enabled)))
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", "*")
    skill, pg = make(hits)
    pg.resolve_file_urls_by_titles.return_value = services.urls
    monkeypatch.setattr(fanout, "build_summary_client", lambda: skill._bedrock)
    skill._slack_store = services.store
    skill._google_token_store = MagicMock()
    skill._google_token_store.get.return_value = object()
    monkeypatch.setattr(skill, "_retrieve", lambda *a, **kw: hits)
    contents = await server.dispatch_tool(
        _spec(skill, "search"),
        "search",
        {"query": query},
        identity_resolver=_resolver_for(ME),
        caller_claim_verifier=_Verifier(_claim(channel=channel, thread_ts=THREAD))
        if verified
        else None,
    )
    return json.loads(contents[0].text)


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("fanout_enabled", [False, True])
@pytest.mark.parametrize("source_type", ["slack", "other"])
async def test_list_drive_two_slack_one_posts_link_then_requester_attachment(
    monkeypatch, services, direct, fanout_enabled, source_type
):
    result = await dispatch(
        monkeypatch,
        services,
        [drive_hit(1), drive_hit(2), slack_hit(source_type)],
        direct=direct,
        fanout_enabled=fanout_enabled,
    )
    assert result["delivered"] is True
    text = services.bot.posts[0]["text"]
    assert "/d/DRIVE1/" in text and "/d/DRIVE2/" in text
    assert f"<{FILE}|資料を見る>" in text
    assert "Slack共有" not in text and "Slack での共有" not in text
    assert "Slack にだけ" not in text and "本文未読" not in text
    assert services.events == ["post", "download", "upload"]
    assert services.bot.posts[0]["thread_ts"] == THREAD
    services.bot.upload_files.assert_awaited_once()
    assert all(call.args == ("xoxp-requester",) for call in services.reader_factory.call_args_list)
    if source_type == "slack":
        services.api.conversations_replies.assert_awaited_once_with(
            channel="C0SHARED", ts=THREAD, limit=100
        )


@pytest.mark.parametrize("direct", [False, True])
async def test_list_without_user_token_keeps_message_link_without_attachment(
    monkeypatch, services, direct
):
    services.store._tokens.clear()
    result = await dispatch(
        monkeypatch, services, [drive_hit(1), drive_hit(2), slack_hit()], direct=direct
    )
    text = services.bot.posts[0]["text"] if services.bot.posts else result["answer"]
    assert f"<{POST}|資料を見る>" in text
    assert "Slack共有" not in text
    services.bot.upload_files.assert_not_awaited()
    services.reader_factory.assert_not_called()


@pytest.mark.parametrize("error", ["file_not_found", "access_denied"])
async def test_list_file_refusal_keeps_message_link_and_summary(monkeypatch, services, error):
    services.api.files_info.side_effect = SlackApiError("denied", {"ok": False, "error": error})
    result = await dispatch(monkeypatch, services, [drive_hit(), slack_hit()])
    assert result["delivered"] is True
    assert f"<{POST}|資料を見る>" in services.bot.posts[0]["text"]
    assert "飲料の提案" in services.bot.posts[0]["text"]
    services.bot.upload_files.assert_not_awaited()


@pytest.mark.parametrize("drive_visible", [False, True])
@pytest.mark.parametrize("slack_first", [False, True])
async def test_case_duplicate_prefers_accessible_original_and_never_attaches_twice(
    monkeypatch, services, drive_visible, slack_first
):
    drive = drive_hit(1, title="飲料_提案.PDF", size=200)
    slack = slack_hit()
    if not drive_visible:
        services.google.get_file_metadata.side_effect = PermissionError("403")
    hits = [slack, drive] if slack_first else [drive, slack]
    result = await dispatch(monkeypatch, services, hits, direct=True)
    assert result["delivered"] is True
    text = services.bot.posts[0]["text"]
    assert text.count("|資料を見る>") == 1
    if drive_visible:
        assert "/d/DRIVE1/" in text and FILE not in text
        services.bot.upload_files.assert_not_awaited()
    else:
        assert FILE in text and "/d/DRIVE1/" not in text
        services.bot.upload_files.assert_awaited_once()


async def test_case_same_name_different_size_keeps_both(monkeypatch, services):
    services.google.get_file_metadata.return_value["size"] = "300"
    await dispatch(
        monkeypatch, services, [drive_hit(title="飲料提案.pdf", size=300), slack_hit()], direct=True
    )
    assert services.bot.posts[0]["text"].count("|資料を見る>") == 2
    services.bot.upload_files.assert_awaited_once()


async def test_drive_only_list_has_no_new_post_or_file_lookup(monkeypatch, services):
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", ME)
    result = await dispatch(monkeypatch, services, [drive_hit(1), drive_hit(2)])
    assert result["answer"] == (
        "*飲料事例1.pdf*（2026年10月）\n• 飲料の提案内容です。\n"
        "• <https://drive.google.com/file/d/DRIVE1/view|資料を見る>\n\n"
        "*飲料事例2.pdf*（2026年10月）\n• 飲料の提案内容です。\n"
        "• <https://drive.google.com/file/d/DRIVE2/view|資料を見る>"
    )
    assert services.events == []
    services.reader_factory.assert_not_called()


@pytest.mark.parametrize("status", ["failed", "uncertain"])
async def test_case_failed_text_post_does_not_upload_or_retry_through_other_path(
    monkeypatch, services, status
):
    monkeypatch.setattr(detached_jobs, "post_to_origin_status", lambda *a, **kw: status)
    result = await dispatch(monkeypatch, services, [slack_hit()], direct=True)
    assert "answer" in result and result.get("delivered") is not True
    services.bot.upload_files.assert_not_awaited()


async def test_case_channel_surface_never_receives_user_file(monkeypatch, services):
    await dispatch(monkeypatch, services, [slack_hit()], channel="C0PUBLIC")
    services.bot.upload_files.assert_not_awaited()
    assert services.events == []


async def test_case_copy_outside_top_hits_is_used_only_after_name_size_and_access_check(
    monkeypatch, services
):
    services.urls["飲料提案.pdf"] = "https://drive.google.com/file/d/DRIVE1/view"
    await dispatch(monkeypatch, services, [slack_hit()], direct=True)
    text = services.bot.posts[0]["text"]
    assert "/d/DRIVE1/" in text and FILE not in text
    services.bot.upload_files.assert_not_awaited()


async def test_case_same_slack_file_in_message_and_import_is_attached_once(monkeypatch, services):
    await dispatch(
        monkeypatch, services, [drive_hit(), slack_hit(), slack_hit("other")], direct=True
    )
    assert services.bot.posts[0]["text"].count(FILE) == 1
    services.bot.upload_files.assert_awaited_once()
    assert len(services.bot.upload_files.await_args.args[1]) == 1


async def test_case_without_file_permalink_links_to_message_and_still_attaches(
    monkeypatch, services
):
    services.api.files_info.return_value["file"].pop("permalink")
    await dispatch(monkeypatch, services, [slack_hit()], direct=True)
    assert f"<{POST}|資料を見る>" in services.bot.posts[0]["text"]
    services.bot.upload_files.assert_awaited_once()


async def test_case_five_slack_files_keep_order_and_attach_only_first_three(monkeypatch, services):
    template = services.api.files_info.return_value["file"]

    async def info(*, file):
        return {
            "ok": True,
            "file": {
                **template,
                "id": file,
                "name": f"飲料提案{file}.pdf",
                "permalink": FILE.replace("F0ONLY", file),
            },
        }

    services.api.files_info.side_effect = info
    hits = [
        SearchHit(
            i,
            "飲料の提案。",
            0.9,
            {
                "title": f"飲料提案F0FILE{i}.pdf",
                "source_type": "other",
                "external_id": f"F0FILE{i}",
                "source_uri": FILE.replace("F0ONLY", f"F0FILE{i}"),
            },
        )
        for i in range(5)
    ]
    await dispatch(monkeypatch, services, hits, direct=True)
    assert services.bot.posts[0]["text"].count("|資料を見る>") == 5
    services.bot.upload_files.assert_awaited_once()
    assert [name for _, name in services.bot.upload_files.await_args.args[1]] == [
        f"飲料提案F0FILE{i}.pdf" for i in range(3)
    ]


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("fanout_enabled", [False, True])
async def test_material_list_also_posts_file_link_and_attachment(
    monkeypatch, services, direct, fanout_enabled
):
    result = await dispatch(
        monkeypatch,
        services,
        [drive_hit(), slack_hit()],
        direct=direct,
        fanout_enabled=fanout_enabled,
        query="飲料の資料を一覧で出して",
    )
    assert result["delivered"] is True
    assert f"<{FILE}|資料を見る>" in services.bot.posts[0]["text"]
    assert services.events == ["post", "download", "upload"]


async def test_case_no_token_uses_known_filename_instead_of_channel_ingest_title(
    monkeypatch, services
):
    services.store._tokens.clear()
    hit = SearchHit(
        10,
        "飲料の提案です。\n飲料提案.pdf",
        0.9,
        slack_hit(title="proj-ナレッジ共有 1784424000.000001").metadata,
    )
    result = await dispatch(monkeypatch, services, [hit])
    assert result["delivered"] is True
    text = services.bot.posts[0]["text"]
    assert "飲料提案.pdf" in text and "proj-ナレッジ共有" not in text
    assert f"<{POST}|資料を見る>" in text
    services.bot.upload_files.assert_not_awaited()


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize(
    "flag,emails", [(None, "*"), ("0", "*"), ("1", ""), ("1", "another@vectorinc.co.jp")]
)
async def test_file_delivery_flag_off_or_not_allowed_keeps_links_without_any_post(
    monkeypatch, services, direct, flag, emails
):
    if flag is None:
        monkeypatch.delenv("USE_SEARCH_FILE_DELIVERY")
    else:
        monkeypatch.setenv("USE_SEARCH_FILE_DELIVERY", flag)
    monkeypatch.setenv("SEARCH_FILE_DELIVERY_ALLOWED_EMAILS", emails)
    result = await dispatch(monkeypatch, services, [drive_hit(), slack_hit()], direct=direct)
    assert f"<{FILE}|資料を見る>" in result["answer"]
    assert "Slack共有" not in result["answer"] and "Slack での共有" not in result["answer"]
    assert services.bot.posts == [] and services.events == []
    services.bot.upload_files.assert_not_awaited()
    services.reader.download_file_guarded.assert_not_awaited()


async def test_public_file_import_without_owner_token_uses_other_reader_after_list(
    monkeypatch, services
):
    services.store._tokens.clear()
    services.store.put("other@vectorinc.co.jp", SlackOAuthToken("xoxp-other", ("files:read",)))
    hit = slack_hit("other")
    hit.metadata["public_channel_ids"] = ["C0PUBLIC"]
    result = await dispatch(monkeypatch, services, [drive_hit(), hit])
    assert result["delivered"] is True
    assert services.events == ["post", "download", "upload"]
    services.reader_factory.assert_called_once_with("xoxp-other")
    services.bot.upload_files.assert_awaited_once()


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("file_delivery", [False, True])
@pytest.mark.parametrize("answer_format", ["compact", None])
async def test_compact_answer_attaches_only_two_used_files_once(
    monkeypatch, services, direct, file_delivery, answer_format
):
    first, second, unused = [file_ref(fid) for fid in ("F0FIRST", "F0SECOND", "F0UNUSED")]
    answer = f"事例B <{second.url}|資料を見る>、事例A [S:11]、再掲 {second.url}"
    output = SearchOutput(
        answer=answer,
        direct_answer=answer if direct else None,
        direct_answer_kind="comparison" if direct else None,
        answer_format=answer_format,
        total_cost_usd=0,
        hits=[
            SearchHitOut(chunk_id=i, content="架空の事例", score=0.9, reference=ref)
            for i, ref in ((10, unused), (11, first), (12, second))
        ],
    )
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER", str(int(direct)))
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS", ME)
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER_KINDS", "comparison")
    monkeypatch.setenv("USE_SEARCH_FILE_DELIVERY", str(int(file_delivery)))
    template = services.api.files_info.return_value["file"]
    services.api.files_info.side_effect = lambda *, file: {
        "ok": True,
        "file": {**template, "id": file},
    }
    contents = await server.dispatch_tool(
        _spec(_OutputSearch(output), "search"),
        "search",
        {"query": "飲料の施策を比較して"},
        identity_resolver=_resolver_for(ME),
        caller_claim_verifier=_Verifier(_claim(channel=DM, thread_ts=THREAD)),
    )
    result = json.loads(contents[0].text)
    if direct or file_delivery:
        assert result["delivered"] is True
        assert result["status"] == "posted"
        assert result["answer"] == direct_search_answer.POSTED_TEXT
        assert "hits" not in result
        assert len(services.bot.posts) == 1
    else:
        assert result["answer"] == answer
        assert services.bot.posts == []
    events = ["post"] if direct or file_delivery else []
    if file_delivery:
        assert services.events == [*events, "download", "download", "upload"]
        services.bot.upload_files.assert_awaited_once()
        assert [name for _, name in services.bot.upload_files.await_args.args[1]] == [
            second.title,
            first.title,
        ]
        assert [call.kwargs["file"] for call in services.api.files_info.await_args_list] == [
            "F0SECOND",
            "F0FIRST",
        ]
    else:
        assert services.events == events
        services.bot.upload_files.assert_not_awaited()
        services.reader.download_file_guarded.assert_not_awaited()


@pytest.mark.parametrize(
    "channel,verified,emails",
    [("C0PUBLIC", True, "*"), (DM, False, "*"), (DM, True, ""), (DM, True, "other@example.test")],
)
async def test_compact_tool_result_attachment_keeps_destination_guards(
    monkeypatch, services, channel, verified, emails
):
    ref = file_ref("F0ONLY")
    output = SearchOutput(
        answer=f"架空資料 <{ref.url}|資料を見る>",
        answer_format="compact",
        attachment_refs=[ref],
        total_cost_usd=0,
    )
    monkeypatch.setenv("SEARCH_FILE_DELIVERY_ALLOWED_EMAILS", emails)
    await server.dispatch_tool(
        _spec(_OutputSearch(output), "search"),
        "search",
        {"query": "架空資料の内容"},
        identity_resolver=_resolver_for(ME),
        caller_claim_verifier=_Verifier(_claim(channel=channel, thread_ts=THREAD))
        if verified
        else None,
    )
    assert services.events == []
    services.bot.upload_files.assert_not_awaited()
    services.reader_factory.assert_not_called()


@pytest.mark.parametrize("reason", ["empty", "unused", "drive_copy", "post_only"])
async def test_compact_without_attachment_candidates_does_not_post(monkeypatch, services, reason):
    ref = file_ref("F0ONLY")
    if reason == "drive_copy":
        ref = ref.model_copy(update={"kind": "drive", "drive_file_id": "DRIVE1"})
    elif reason == "post_only":
        ref = ref.model_copy(update={"url": POST, "source_uri": POST, "slack_file_ids": []})
    answer = "架空の回答" if reason in {"empty", "unused"} else f"架空資料 {ref.url}"
    output = SearchOutput(
        answer=answer,
        answer_format="compact",
        attachment_refs=[] if reason == "empty" else [ref],
        total_cost_usd=0,
    )
    assert search_file_delivery.candidates(output) == []
    contents = await server.dispatch_tool(
        _spec(_OutputSearch(output), "search"),
        "search",
        {"query": "架空資料の内容"},
        identity_resolver=_resolver_for(ME),
        caller_claim_verifier=_Verifier(_claim(channel=DM, thread_ts=THREAD)),
    )
    result = json.loads(contents[0].text)
    assert result["answer"] == answer
    assert result.get("delivered") is not True
    assert services.events == []
    services.bot.upload_files.assert_not_awaited()
    services.reader_factory.assert_not_called()


@pytest.mark.parametrize("status", ["failed", "uncertain"])
async def test_compact_failed_text_post_does_not_attach_or_suppress_answer(
    monkeypatch, services, status
):
    ref = file_ref("F0ONLY")
    output = SearchOutput(
        answer=f"架空資料 <{ref.url}|資料を見る>",
        answer_format="compact",
        attachment_refs=[ref],
        total_cost_usd=0,
    )
    post = MagicMock(return_value=status)
    monkeypatch.setattr(detached_jobs, "post_to_origin_status", post)
    contents = await server.dispatch_tool(
        _spec(_OutputSearch(output), "search"),
        "search",
        {"query": "架空資料の内容"},
        identity_resolver=_resolver_for(ME),
        caller_claim_verifier=_Verifier(_claim(channel=DM, thread_ts=THREAD)),
    )
    result = json.loads(contents[0].text)
    assert result["answer"] == output.answer
    assert result.get("delivered") is not True
    post.assert_called_once()
    assert services.events == []
    services.bot.upload_files.assert_not_awaited()
    services.reader_factory.assert_not_called()
