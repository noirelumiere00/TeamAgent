from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from teamagent.adapters.pgvector_client import SearchHit
from teamagent.skills.base import SkillContext
from teamagent.skills.search import drive_first
from tests.skills.search import test_drive_first as search_tests
from tests.skills.search.test_fanout import META

DRIVE, POST = search_tests.DRIVE, search_tests.POST
setup_search = search_tests.setup_search


@pytest.fixture(autouse=True)
def enabled_google_lookup(monkeypatch):
    monkeypatch.setenv("SEARCH_DRIVE_FIRST", "1")
    monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", META["user_email"])


def _slack():
    return SearchHit(
        1,
        "紹介投稿 proposal_alpha.pdf",
        0.9,
        {
            "source_type": "slack",
            "source_uri": POST,
            "file_names": ["proposal_alpha.pdf"],
            "slack_file_ids": ["FALPHA"],
        },
    )


def test_file_record_wins_over_same_filename_and_does_not_require_index(setup_search, monkeypatch):
    skill, pg = setup_search
    pg.resolve_file_urls_by_titles.return_value = {
        "proposal_alpha.pdf": "https://drive.google.com/file/d/WRONG_VERSION/view"
    }
    mapped = SimpleNamespace(
        record_number="42",
        branch_number="2",
        drive_file_id="ALPHA",
        drive_url=DRIVE,
        title="proposal_alpha.pdf",
    )
    monkeypatch.setattr(drive_first, "resolve_saved_files", lambda *a: [mapped], raising=False)
    pg.list_authorized_document_references.return_value = []
    groups, _, _ = drive_first.annotate(skill, [[_slack()]], (), SkillContext(metadata=META))
    ref = groups[0][0].metadata[drive_first.REFERENCE_KEY]
    assert ref.drive_file_id == "ALPHA"
    assert ref.file_record_number == "42" and ref.file_record_branch == "2"
    assert ref.slack_file_id == "FALPHA" and ref.unread


def test_unmapped_slack_file_does_not_guess_by_name(setup_search, monkeypatch):
    skill, _ = setup_search
    monkeypatch.setattr(drive_first, "resolve_saved_files", lambda *a: [None], raising=False)
    groups, _, _ = drive_first.annotate(skill, [[_slack()]], (), SkillContext(metadata=META))
    ref = groups[0][0].metadata[drive_first.REFERENCE_KEY]
    assert ref.drive_file_id is None
    assert ref.kind == "slack_attachment" and ref.unread
    assert ref.url == POST


def _clients(monkeypatch, records):
    from teamagent.adapters.gdrive_client import GDriveClient
    from teamagent.adapters.gsheets_client import GSheetsClient
    from teamagent.adapters.oauth_token_store import InMemoryTokenStore, OAuthToken

    token = OAuthToken("redacted", scopes=("drive.readonly",))
    sheet, drive = MagicMock(), MagicMock()
    sheet.lookup_saved_file_records.return_value = records
    drive.get_file_metadata.return_value = {
        "id": "ALPHA",
        "name": "saved.pdf",
        "mimeType": "application/pdf",
        "trashed": False,
    }
    sheet_factory = MagicMock(return_value=sheet)
    drive_factory = MagicMock(return_value=drive)
    monkeypatch.setattr(GSheetsClient, "from_user_token", sheet_factory)
    monkeypatch.setattr(GDriveClient, "from_user_token", drive_factory)
    skill = SimpleNamespace(_google_token_store=InMemoryTokenStore({META["user_email"]: token}))
    return skill, sheet, drive, sheet_factory, drive_factory, token


def _record(*, branch="2", source=POST, fid="ALPHA"):
    from teamagent.adapters.file_record_lookup import SavedFileRecord, slack_source_uri

    return SavedFileRecord(
        record_number="42",
        branch_number=branch,
        slack_source_uri=slack_source_uri(source),
        drive_file_id=fid,
        drive_url=f"https://drive.google.com/file/d/{fid}/view",
        title="saved.pdf",
    )


def test_lookup_uses_only_requesters_readonly_google_clients(monkeypatch):
    from teamagent.skills.search.saved_file_refs import resolve_saved_files

    skill, sheet, drive, sf, df, token = _clients(monkeypatch, [_record()])
    result = resolve_saved_files(
        skill, [_slack()], SkillContext(request_id="record-test", metadata=META)
    )
    assert result[0].drive_file_id == "ALPHA"
    sf.assert_called_once_with(token, write=False)
    df.assert_called_once_with(token, readonly=True)
    drive.get_file_metadata.assert_called_once_with("ALPHA", "record-test")
    assert sheet.lookup_saved_file_records.call_args.kwargs["slack_sources"] == [POST]
    drive.download_file_bytes.assert_not_called()


@pytest.mark.parametrize("mode", ["off", "outside_allowlist", "unverified"])
def test_disabled_lookup_never_uses_google_tokens_or_clients(monkeypatch, mode):
    from teamagent.skills.search.saved_file_refs import resolve_saved_files

    skill, _, _, sf, df, _ = _clients(monkeypatch, [_record()])
    skill._google_token_store = MagicMock()
    meta = dict(META)
    if mode == "off":
        monkeypatch.setenv("SEARCH_DRIVE_FIRST", "0")
    elif mode == "outside_allowlist":
        monkeypatch.setenv("SEARCH_DRIVE_FIRST_ALLOWED_EMAILS", "other@example.test")
    else:
        meta["identity_verified"] = False
    assert resolve_saved_files(skill, [_slack()], SkillContext(metadata=meta)) == [None]
    skill._google_token_store.get.assert_not_called()
    sf.assert_not_called()
    df.assert_not_called()


@pytest.mark.parametrize("failure", ["sheet", "drive", "trashed", "folder", "shortcut", "wrong_id"])
def test_unread_when_sheet_or_destination_cannot_be_verified(monkeypatch, failure):
    from teamagent.skills.search.saved_file_refs import resolve_saved_files

    skill, sheet, drive, *_ = _clients(monkeypatch, [_record()])
    if failure == "sheet":
        sheet.lookup_saved_file_records.side_effect = PermissionError("denied")
    elif failure == "drive":
        drive.get_file_metadata.side_effect = PermissionError("denied")
    else:
        metadata = drive.get_file_metadata.return_value
        if failure == "trashed":
            metadata["trashed"] = True
        elif failure in {"folder", "shortcut"}:
            metadata["mimeType"] = f"application/vnd.google-apps.{failure}"
        else:
            metadata["id"] = "OTHER"
    assert resolve_saved_files(skill, [_slack()], SkillContext(metadata=META)) == [None]


def test_branch_must_be_unique_and_explicit_branch_must_match_source(monkeypatch):
    from teamagent.skills.search.saved_file_refs import resolve_saved_files

    skill, sheet, *_ = _clients(monkeypatch, [_record(), _record(branch="3", fid="BETA")])
    hit = _slack()
    assert resolve_saved_files(skill, [hit], SkillContext(metadata=META)) == [None]
    hit.metadata.update(record_number="42", branch_number="2")
    assert (
        resolve_saved_files(skill, [hit], SkillContext(metadata=META))[0].drive_file_id == "ALPHA"
    )
    sheet.lookup_saved_file_records.return_value = [_record(source=POST + "9")]
    assert resolve_saved_files(skill, [hit], SkillContext(metadata=META)) == [None]


def test_followup_rechecks_same_record_and_never_uses_new_destination(monkeypatch):
    from teamagent.skills.search.saved_file_refs import reauthorize_saved_reference
    from teamagent.skills.search.schema import DocumentReference

    skill, sheet, *_ = _clients(monkeypatch, [_record()])
    ref = DocumentReference(
        source_uri=POST,
        source_type="slack",
        drive_file_id="ALPHA",
        kind="drive",
        file_record_number="42",
        file_record_branch="2",
        url=DRIVE,
    )
    assert reauthorize_saved_reference(skill, ref, SkillContext(metadata=META))
    sheet.lookup_saved_file_records.return_value = [_record(fid="BETA")]
    assert not reauthorize_saved_reference(skill, ref, SkillContext(metadata=META))
