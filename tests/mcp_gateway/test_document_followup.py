"""直前に表示した資料だけを同じ本人の会話へ返す。"""

from __future__ import annotations

import importlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.slack_channel_ingest_client import SlackMessage
from teamagent.adapters.slack_user_reader import SlackThreadRead
from teamagent.mcp_gateway import document_followup, server
from teamagent.skills.base import BaseSkill, SkillContext
from teamagent.skills.search.schema import DocumentReference, SearchInput, SearchOutput
from tests.mcp_gateway.test_surface_video_followup import (
    DM,
    ME,
    _claim,
    _resolver_for,
    _spec,
    _Verifier,
)

URL = "https://drive.google.com/file/d/ORIGINAL/view"
OTHER_URL = "https://drive.google.com/file/d/UNRELATED/view"
REF = {
    "document_id": "document-original",
    "drive_file_id": "ORIGINAL",
    "title": "対象分野の事例資料",
    "url": URL,
    "source_uri": URL,
    "source_type": "gdrive",
    "kind": "drive",
}


class _Search(BaseSkill[SearchInput, SearchOutput]):
    input_schema = SearchInput
    output_schema = SearchOutput

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._pgvector = MagicMock()
        self._pgvector.list_authorized_document_references.return_value = [
            {**REF, "external_id": "ORIGINAL", "metadata": {}}
        ]
        self._app_role = "teamagent_app"
        self.refs = [DocumentReference.model_validate(REF)]

    def run(self, input: SearchInput, ctx: SkillContext) -> SearchOutput:
        self.calls.append(input.query)
        if len(self.calls) > 1:
            return SearchOutput(answer=f"別資料 {OTHER_URL}", total_cost_usd=0)
        return SearchOutput(
            answer=f"事例資料 {URL}",
            displayed_refs=self.refs,
            total_cost_usd=0,
            direct_answer=f"事例資料 {URL}",
            direct_answer_kind="list_fast",
        )


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", ME)
    monkeypatch.setenv("USE_PAYLOAD_OFFLOAD", "false")
    monkeypatch.delenv("ENABLE_PROGRESS_NOTIFY", raising=False)
    monkeypatch.delenv("SEARCH_DIRECT_ANSWER", raising=False)
    monkeypatch.setattr(server, "_record_usage", lambda **kwargs: None)
    monkeypatch.setattr(server, "_inject_search_web_links", lambda data: None)
    rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    store = MagicMock()

    def save(email: str, key: str, refs: list[dict[str, Any]]) -> None:
        rows[email, key] = json.loads(json.dumps(refs))

    store.save.side_effect = save
    store.get.side_effect = lambda email, key: rows.get((email, key), [])
    monkeypatch.setattr(document_followup, "DocumentReferenceStore", lambda pg: store)
    return store


async def dispatch(
    skill: _Search,
    query: str,
    *,
    channel: str = DM,
    thread: str | None = None,
    email: str = ME,
    session: str = "0" * 64,
    refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result = await server.dispatch_tool(
        _spec(skill, "search"),
        "search",
        {"query": query, **({"displayed_refs": refs} if refs else {})},
        identity_resolver=_resolver_for(email),
        caller_claim_verifier=_Verifier(
            replace(_claim(channel=channel, thread_ts=thread), session_sha256=session)
        ),
    )
    return cast(dict[str, Any], json.loads(result[0].text))


async def test_followup_returns_the_displayed_document_without_searching_again() -> None:
    skill = _Search()
    await dispatch(skill, "対象分野の事例出して")
    result = await dispatch(skill, "その資料も出して")
    assert URL in result["answer"]
    assert OTHER_URL not in result["answer"]
    assert skill.calls == ["対象分野の事例出して"]
    assert result["document_followup_available"] is True
    assert skill._pgvector.connection.call_args.kwargs["user_email"] == ME


async def test_followup_survives_gateway_worker_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_worker = _Search()
    await dispatch(first_worker, "対象分野の事例出して")
    store_factory = document_followup.DocumentReferenceStore
    importlib.reload(document_followup)
    monkeypatch.setattr(document_followup, "DocumentReferenceStore", store_factory)
    second_worker = _Search()
    result = await dispatch(second_worker, "その資料も出して")
    assert URL in result["answer"]
    assert second_worker.calls == []


async def test_case_deck_is_authorized_by_its_exact_current_source_row() -> None:
    skill = _Search()
    skill.refs[0].source_type = "other"
    skill._pgvector.list_authorized_document_references.return_value[0]["source_type"] = "other"
    await dispatch(skill, "対象分野の事例出して")
    result = await dispatch(skill, "その資料も出して")
    assert URL in result["answer"]
    assert len(skill.calls) == 1


@pytest.mark.parametrize("visible", [True, False])
async def test_saved_file_record_is_rechecked_without_requiring_a_drive_index_row(
    visible: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from teamagent.skills.search import saved_file_refs

    skill = _Search()
    skill.refs[0] = skill.refs[0].model_copy(
        update={
            "source_uri": "slack://C0123456789/1792461784.123456",
            "source_type": "slack",
            "file_record_number": "314",
            "file_record_branch": "2",
        }
    )
    check = MagicMock(return_value=visible)
    monkeypatch.setattr(saved_file_refs, "reauthorize_saved_reference", check)
    await dispatch(skill, "対象分野の事例出して")
    result = await dispatch(skill, "その資料も出して")
    assert (URL in result["answer"]) is visible
    checked = check.call_args.args[1]
    assert checked.file_record_number == "314" and checked.file_record_branch == "2"
    assert checked.drive_file_id == "ORIGINAL"
    skill._pgvector.list_authorized_document_references.assert_not_called()
    assert len(skill.calls) == 1


async def test_slack_source_row_cannot_authorize_an_unseen_drive_file() -> None:
    skill = _Search()
    skill.refs[0].source_uri = "slack://C0123456789/1792461784.123456"
    skill._pgvector.list_authorized_document_references.return_value = [
        {
            "document_id": REF["document_id"],
            "source_uri": skill.refs[0].source_uri,
            "source_type": "slack",
            "metadata": {},
        }
    ]
    await dispatch(skill, "対象分野の事例出して")
    result = await dispatch(skill, "その資料も出して")
    assert URL not in result["answer"]
    assert len(skill.calls) == 1


async def test_direct_post_receipt_keeps_the_server_reference_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER", "1")
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER_ALLOWED_EMAILS", ME)
    monkeypatch.setenv("SEARCH_DIRECT_ANSWER_KINDS", "list_fast")
    monkeypatch.setattr(
        server.direct_search_answer,
        "deliver",
        lambda *args, **kwargs: {
            "status": "posted",
            "delivered": True,
            "answer": "投稿済み",
        },
    )
    skill = _Search()
    first = await dispatch(skill, "対象分野の事例出して")
    assert first["status"] == "posted" and first["document_followup_available"] is True
    second = await dispatch(skill, "その資料も出して")
    assert second["document_followup_available"] is True
    assert URL in second["direct_delivery"]["answer"]
    assert len(skill.calls) == 1


@pytest.mark.parametrize("change", ["channel", "thread", "email"])
async def test_references_never_cross_verified_conversations(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    other = "other@example.test"
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", f"{ME},{other}")
    skill = _Search()
    await dispatch(skill, "対象分野の事例出して")
    changed: dict[str, Any] = {
        "channel": "D99999999",
        "thread": "1792461784.123456",
        "email": other,
    }
    result = await dispatch(skill, "その資料も出して", **{change: changed[change]})
    assert URL not in result["answer"] and OTHER_URL not in result["answer"]
    assert skill.calls == ["対象分野の事例出して"]


async def test_revoked_reference_and_failed_permission_check_never_search_for_replacements() -> (
    None
):
    skill = _Search()
    await dispatch(skill, "対象分野の事例出して")
    skill._pgvector.list_authorized_document_references.return_value = []
    first = await dispatch(skill, "その資料も出して")
    assert URL not in first["answer"] and REF["title"] not in first["answer"]
    skill._pgvector.list_authorized_document_references.side_effect = RuntimeError("database down")
    second = await dispatch(skill, "その資料も出して")
    assert URL not in second["answer"] and OTHER_URL not in second["answer"]
    assert skill.calls == ["対象分野の事例出して"]


async def test_model_supplied_refs_are_not_authorization() -> None:
    skill = _Search()
    result = await dispatch(skill, "その資料も出して", refs=[REF])
    assert URL not in result["answer"]
    assert skill.calls == []
    skill._pgvector.list_authorized_document_references.assert_not_called()


async def test_default_off_keeps_the_old_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARCH_DRIVE_FIRST", raising=False)
    skill = _Search()
    await dispatch(skill, "対象分野の事例出して")
    result = await dispatch(skill, "その資料も出して")
    assert OTHER_URL in result["answer"]
    assert len(skill.calls) == 2
    skill._pgvector.list_authorized_document_references.assert_not_called()


async def test_followup_survives_a_new_openclaw_session() -> None:
    skill = _Search()
    await dispatch(skill, "対象分野の事例出して")
    result = await dispatch(skill, "その資料も出して", session="1" * 64)
    assert URL in result["answer"] and len(skill.calls) == 1


async def test_empty_new_search_clears_the_previous_document_selection() -> None:
    skill = _Search()
    await dispatch(skill, "対象分野の事例出して")
    await dispatch(skill, "別の分野の事例出して")
    result = await dispatch(skill, "その資料も出して")
    assert URL not in result["answer"] and OTHER_URL not in result["answer"]
    assert len(skill.calls) == 2


async def test_store_failure_never_claims_the_selection_was_saved(isolated: MagicMock) -> None:
    isolated.save.side_effect = RuntimeError("database unavailable")
    skill = _Search()
    first = await dispatch(skill, "対象分野の事例出して")
    assert URL in first["answer"] and first["document_followup_available"] is False
    isolated.get.side_effect = RuntimeError("database unavailable")
    second = await dispatch(skill, "その資料も出して")
    assert URL not in second["answer"] and len(skill.calls) == 1


@pytest.mark.parametrize("visible", [True, False])
async def test_slack_only_attachment_is_rechecked_as_the_requester(
    visible: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLACK_WORKSPACE", "example")
    skill = _Search()
    permalink = "https://example.slack.com/archives/C0123456789/p1792461784123456"
    skill.refs = [
        DocumentReference(
            title="共有資料.pdf",
            url=permalink,
            source_uri=permalink,
            source_type="slack",
            slack_file_ids=["F0123456789"],
            kind="slack_attachment",
            unread=True,
        )
    ]
    skill._slack_store = MagicMock()
    skill._slack_store.get.return_value = SimpleNamespace(access_token="requester-token")
    reader = MagicMock()
    reader.read_message_checked.return_value = SlackThreadRead(
        messages=(
            SlackMessage(
                ts="1792461784.123456",
                user="U0123456789",
                text="共有資料",
                files=({"id": "F0123456789"},) if visible else (),
            ),
        )
    )
    skill._slack_reader_factory = MagicMock(return_value=reader)
    await dispatch(skill, "対象分野の事例出して")
    result = await dispatch(skill, "その資料も出して")
    assert (permalink in result["answer"]) is visible
    if visible:
        assert "本文未読" in result["answer"]
    skill._slack_store.get.assert_called_once_with(ME)
    skill._slack_reader_factory.assert_called_once_with("requester-token")
    assert skill.calls == ["対象分野の事例出して"]


async def test_eval12_empty_refs_survive_real_mcp_serialization() -> None:
    skill = _Search()
    skill.refs = []
    result = await dispatch(skill, "社内でいうビデオリリースって何？")
    assert result["displayed_refs"] == []
    assert result["document_followup_available"] is False
