"""Drive/Sheets の部分取得失敗も SearchSkill の外側で一度だけ案内する。"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gdrive_client import GDriveClient
from teamagent.adapters.gsheets_client import GSheetsClient
from teamagent.skills.base import SkillContext
from teamagent.skills.search import drive_first, saved_file_refs
from teamagent.skills.search.schema import SearchInput
from tests.skills._shared.test_google_connection import EMAIL, MESSAGE, failure, store
from tests.skills._shared.test_google_connection import setup as setup
from tests.skills.search.test_drive_first import setup_search as setup_search


@pytest.mark.parametrize("kind", ["missing", "invalid_grant", "401", "insufficientPermissions"])
@pytest.mark.parametrize("api", ["sheets", "drive"])
@pytest.mark.parametrize("deliver", [False, True])
def test_search_google_failure(
    setup_search: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    api: str,
    deliver: bool,
) -> None:
    skill, _ = setup_search
    skill._google_token_store = store(kind != "missing")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", EMAIL)
    monkeypatch.setenv("SEARCH_FANOUT_ALLOWED_EMAILS", EMAIL)
    monkeypatch.setattr(drive_first, "resolve_saved_files", saved_file_refs.resolve_saved_files)
    sheet, drive = MagicMock(), MagicMock()
    record = SimpleNamespace(
        slack_source_uri="https://example.slack.com/archives/CALPHA/p1234567890123456",
        record_number="1",
        branch_number="1",
        drive_file_id="FILE",
    )
    # Use the same normalized source as the live Slack lane.
    from teamagent.adapters.file_record_lookup import slack_source_uri

    record.slack_source_uri = slack_source_uri(record.slack_source_uri)
    sheet.lookup_saved_file_records.return_value = [record]
    if api == "sheets":
        sheet.lookup_saved_file_records.side_effect = failure(kind)
    else:
        drive.get_file_metadata.side_effect = failure(kind)
    monkeypatch.setattr(GSheetsClient, "from_user_token", lambda *a, **kw: sheet)
    monkeypatch.setattr(GDriveClient, "from_user_token", lambda *a, **kw: drive)
    context = SkillContext(
        metadata={
            "user_email": EMAIL,
            "channel_id": "D1",
            "thread_ts": "1",
            "identity_verified": True,
            "mcp_search_surface": True,
        }
    )
    from teamagent.skills.search.composite import MCP_SURFACE_CTX_KEY

    context.metadata[MCP_SURFACE_CTX_KEY] = True
    output: Any
    if deliver:
        from teamagent.skills.knowledge_deliver.schema import KnowledgeDeliverInput
        from teamagent.skills.knowledge_deliver.skill import KnowledgeDeliverSkill

        slack = MagicMock()
        output = KnowledgeDeliverSkill(search=skill, slack=slack).run(
            KnowledgeDeliverInput(query="食品の事例出して"), context
        )
        assert output.delivered_count == 0
        assert not slack.mock_calls
        drive.download_file_bytes.assert_not_called()
    else:
        output = skill.run(SearchInput(query="食品の事例出して"), context)
    assert output.message == MESSAGE
    assert output.answer == MESSAGE
    assert output.error == "reauth_needed"
