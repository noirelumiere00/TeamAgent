"""未索引の保存先も、本人 client の readonly metadata 取得だけで再確認する。"""

from unittest.mock import MagicMock

import pytest

from teamagent.adapters.gdrive_client import GDriveClient


def test_get_file_metadata_requests_only_reference_fields() -> None:
    service = MagicMock()
    row = {"id": "FILE_A", "name": "material.pdf", "trashed": False, "parents": ["FOLDER"]}
    service.files.return_value.get.return_value.execute.return_value = row

    result = GDriveClient(service=service).get_file_metadata("FILE_A", "req")

    assert result == row
    assert service.files.return_value.get.call_args.kwargs == {
        "fileId": "FILE_A",
        "supportsAllDrives": True,
        "fields": "id,name,mimeType,webViewLink,trashed,driveId,parents",
    }
    service.files.return_value.get_media.assert_not_called()
    service.files.return_value.create.assert_not_called()
    service.files.return_value.update.assert_not_called()


@pytest.mark.parametrize("file_id", ["", "../FILE_A", "FILE_A?x=1"])
def test_invalid_file_id_does_not_call_google(file_id: str) -> None:
    service = MagicMock()
    with pytest.raises(ValueError):
        GDriveClient(service=service).get_file_metadata(file_id, "req")
    service.files.assert_not_called()


def test_returned_file_id_must_match_request() -> None:
    service = MagicMock()
    service.files.return_value.get.return_value.execute.return_value = {"id": "FILE_B"}
    with pytest.raises(ValueError):
        GDriveClient(service=service).get_file_metadata("FILE_A", "req")


def test_permission_error_is_not_replaced_by_shared_credentials() -> None:
    service = MagicMock()
    service.files.return_value.get.return_value.execute.side_effect = PermissionError("denied")
    with pytest.raises(PermissionError):
        GDriveClient(service=service).get_file_metadata("FILE_A", "req")
