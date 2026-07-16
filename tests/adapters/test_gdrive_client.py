"""adapters/gdrive_client.py のユニットテスト。

実 Drive API は呼ばず、FakeDriveService で response 構造をモックする。
検証ポイント：
- list_files: kwargs 構築 / pagination / mime filter / 戻り値 dataclass 化
- list_permissions: ACL 構造の dataclass 化
- download_file_bytes: MediaIoBaseDownload の互換
- get_changes / get_start_page_token: changes API 構造の解釈
- extract_acl_emails: domain / anyone 警告 + emails / groups 振り分け
- from_env: credentials 未設定の警告経路
- _ensure_service: service 注入が credentials 構築を skip すること
"""

from __future__ import annotations

from typing import Any

import pytest

from teamagent.adapters.gdrive_client import (
    ChangeBatch,
    DriveChange,
    DriveFile,
    DrivePermission,
    GDriveClient,
    GDriveDownloadContentError,
    GDrivePermissionsPaginationError,
    extract_acl_emails,
)


# -----------------------------------------------------------
# Fake Drive Service（googleapiclient.discovery.build 戻り値互換）
# -----------------------------------------------------------
class _FakeRequest:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.call_kwargs: dict[str, Any] = {}

    def execute(self, **kwargs: Any) -> Any:
        self.call_kwargs = kwargs
        return self._response


class _FakeFiles:
    def __init__(self, list_response: Any) -> None:
        self._list_response = list_response
        self.last_list_kwargs: dict[str, Any] = {}
        self.last_get_media_kwargs: dict[str, Any] = {}

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.last_list_kwargs = kwargs
        return _FakeRequest(self._list_response)

    def get_media(self, **kwargs: Any) -> Any:
        # download 系は内部で MediaIoBaseDownload を介して chunk loop するので
        # ここでは適当な request object を返す（download テストは別途モック）
        self.last_get_media_kwargs = kwargs
        return _FakeRequest(None)


class _FakePermissions:
    def __init__(self, list_response: Any) -> None:
        self._list_responses = list_response if isinstance(list_response, list) else [list_response]
        self.last_list_kwargs: dict[str, Any] = {}
        self.list_kwargs: list[dict[str, Any]] = []
        self.requests: list[_FakeRequest] = []

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.last_list_kwargs = kwargs
        self.list_kwargs.append(kwargs)
        response_index = len(self.list_kwargs) - 1
        if response_index >= len(self._list_responses):
            raise AssertionError("unexpected permissions.list page request")
        request = _FakeRequest(self._list_responses[response_index])
        self.requests.append(request)
        return request


class _FakeChanges:
    def __init__(
        self,
        list_response: Any,
        start_token_response: Any,
    ) -> None:
        self._list_response = list_response
        self._start_token_response = start_token_response
        self.last_list_kwargs: dict[str, Any] = {}

    def list(self, **kwargs: Any) -> _FakeRequest:
        self.last_list_kwargs = kwargs
        return _FakeRequest(self._list_response)

    def getStartPageToken(self, **kwargs: Any) -> _FakeRequest:  # noqa: N802 (API 名)
        return _FakeRequest(self._start_token_response)


class FakeDriveService:
    def __init__(
        self,
        *,
        files_list: Any | None = None,
        permissions_list: Any | None = None,
        permissions_pages: list[Any] | None = None,
        changes_list: Any | None = None,
        start_page_token: str = "TOKEN-INIT",
    ) -> None:
        self._files = _FakeFiles(files_list or {"files": [], "nextPageToken": None})
        self._permissions = _FakePermissions(
            permissions_pages
            if permissions_pages is not None
            else (permissions_list or {"permissions": []})
        )
        self._changes = _FakeChanges(
            changes_list or {"changes": [], "newStartPageToken": start_page_token},
            {"startPageToken": start_page_token},
        )

    def files(self) -> _FakeFiles:
        return self._files

    def permissions(self) -> _FakePermissions:
        return self._permissions

    def changes(self) -> _FakeChanges:
        return self._changes


# -----------------------------------------------------------
# list_files
# -----------------------------------------------------------
def test_list_files_returns_drive_file_objects() -> None:
    fake = FakeDriveService(
        files_list={
            "files": [
                {
                    "id": "1ABC",
                    "name": "proposal.pdf",
                    "mimeType": "application/pdf",
                    "modifiedTime": "2026-05-20T01:23:45.000Z",
                    "size": "12345",
                    "parents": ["0XYZ"],
                    "webViewLink": "https://drive.google.com/file/d/1ABC/view",
                    "owners": [{"emailAddress": "taro@vectorinc.co.jp"}],
                },
            ],
            "nextPageToken": "PAGE2",
        }
    )
    client = GDriveClient(service=fake)
    files, next_token = client.list_files(folder_id="0XYZ", request_id="req-1")
    assert len(files) == 1
    f = files[0]
    assert isinstance(f, DriveFile)
    assert f.id == "1ABC"
    assert f.name == "proposal.pdf"
    assert f.size == 12345
    assert f.parents == ("0XYZ",)
    assert f.web_view_link == "https://drive.google.com/file/d/1ABC/view"
    assert f.owners_email == ("taro@vectorinc.co.jp",)
    assert next_token == "PAGE2"


def test_list_files_builds_query_with_folder_and_mime() -> None:
    fake = FakeDriveService(files_list={"files": [], "nextPageToken": None})
    client = GDriveClient(service=fake)
    client.list_files(
        folder_id="FOLDER1",
        request_id="req-2",
        mime_type_filter="application/pdf",
    )
    q = fake.files().last_list_kwargs["q"]
    assert "trashed = false" in q
    assert "'FOLDER1' in parents" in q
    assert "mimeType = 'application/pdf'" in q


def test_list_files_passes_page_token_when_given() -> None:
    fake = FakeDriveService(files_list={"files": [], "nextPageToken": None})
    client = GDriveClient(service=fake)
    client.list_files(folder_id=None, request_id="r", page_token="TOK")
    assert fake.files().last_list_kwargs["pageToken"] == "TOK"


def test_list_files_handles_missing_optional_fields() -> None:
    """size / parents / owners が無いレコードでも壊れない。"""
    fake = FakeDriveService(
        files_list={
            "files": [
                {"id": "X", "name": "n", "mimeType": "m"},
            ],
            "nextPageToken": None,
        }
    )
    client = GDriveClient(service=fake)
    files, _ = client.list_files(folder_id="F", request_id="r")
    assert files[0].size is None
    assert files[0].parents == ()
    assert files[0].owners_email == ()
    assert files[0].web_view_link is None


# -----------------------------------------------------------
# list_permissions
# -----------------------------------------------------------
def test_list_permissions_maps_to_dataclass() -> None:
    fake = FakeDriveService(
        permissions_list={
            "permissions": [
                {
                    "id": "p1",
                    "type": "user",
                    "role": "owner",
                    "emailAddress": "owner@vectorinc.co.jp",
                },
                {
                    "id": "p2",
                    "type": "user",
                    "role": "reader",
                    "emailAddress": "alice@vectorinc.co.jp",
                },
                {
                    "id": "p3",
                    "type": "domain",
                    "role": "reader",
                    "domain": "vectorinc.co.jp",
                },
                {
                    "id": "p4",
                    "type": "user",
                    "role": "writer",
                    "emailAddress": "old@vectorinc.co.jp",
                    "deleted": True,
                },
            ]
        }
    )
    client = GDriveClient(service=fake)
    perms = client.list_permissions(file_id="1XYZ", request_id="r")
    assert len(perms) == 4
    assert all(isinstance(p, DrivePermission) for p in perms)
    assert perms[0].email_address == "owner@vectorinc.co.jp"
    assert perms[2].domain == "vectorinc.co.jp"
    assert perms[3].deleted is True


def test_list_permissions_fetches_every_page_with_retries() -> None:
    fake = FakeDriveService(
        permissions_pages=[
            {
                "permissions": [
                    {"id": "p1", "type": "user", "role": "owner", "emailAddress": "o@x.jp"}
                ],
                "nextPageToken": "PAGE2",
            },
            {
                "permissions": [
                    {"id": "p2", "type": "group", "role": "reader", "emailAddress": "g@x.jp"}
                ]
            },
        ]
    )
    perms = GDriveClient(service=fake).list_permissions(file_id="F1", request_id="r", api_retries=4)

    assert [permission.id for permission in perms] == ["p1", "p2"]
    permission_api = fake.permissions()
    assert permission_api.list_kwargs[0]["pageSize"] == 100
    assert "nextPageToken" in permission_api.list_kwargs[0]["fields"]
    assert "pageToken" not in permission_api.list_kwargs[0]
    assert permission_api.list_kwargs[1]["pageToken"] == "PAGE2"
    assert [request.call_kwargs for request in permission_api.requests] == [
        {"num_retries": 4},
        {"num_retries": 4},
    ]


def test_list_permissions_raises_when_page_limit_leaves_token() -> None:
    fake = FakeDriveService(
        permissions_pages=[
            {"permissions": [{"id": "p1"}], "nextPageToken": "PAGE2"},
        ]
    )
    with pytest.raises(GDrivePermissionsPaginationError, match="remaining token"):
        GDriveClient(service=fake).list_permissions(file_id="F1", request_id="r", max_pages=1)


def test_list_permissions_raises_on_repeated_page_token() -> None:
    fake = FakeDriveService(
        permissions_pages=[
            {"permissions": [], "nextPageToken": "PAGE2"},
            {"permissions": [], "nextPageToken": "PAGE2"},
        ]
    )
    with pytest.raises(GDrivePermissionsPaginationError, match="did not advance"):
        GDriveClient(service=fake).list_permissions(file_id="F1", request_id="r")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"page_size": 0}, "page_size"),
        ({"page_size": 101}, "page_size"),
        ({"max_pages": 0}, "max_pages"),
        ({"api_retries": -1}, "api_retries"),
    ],
)
def test_list_permissions_rejects_invalid_safety_limits(
    kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        GDriveClient(service=FakeDriveService()).list_permissions(
            file_id="F1", request_id="r", **kwargs
        )


# -----------------------------------------------------------
# download_file_bytes
# -----------------------------------------------------------
def test_download_file_bytes_classifies_html_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive確認画面は分類例外にし、payload先頭を例外/ログへ露出しない。"""
    html = b"\xef\xbb\xbf  <!doctype html><html><body>error</body></html>"

    class _FakeDownloader:
        def __init__(self, stream: Any, request: Any, *, chunksize: int) -> None:
            self._stream = stream
            self._done = False

        def next_chunk(self, *, num_retries: int) -> tuple[None, bool]:
            assert num_retries == 3
            if not self._done:
                self._stream.write(html)
                self._done = True
            return None, True

    monkeypatch.setattr("googleapiclient.http.MediaIoBaseDownload", _FakeDownloader)
    fake = FakeDriveService()
    client = GDriveClient(service=fake)

    with pytest.raises(GDriveDownloadContentError) as raised:
        client.download_file_bytes(file_id="F1", request_id="r")

    assert raised.value.category == "html_response"
    assert raised.value.actual_bytes == len(html)
    assert "<!doctype" not in str(raised.value)
    assert fake.files().last_get_media_kwargs == {
        "fileId": "F1",
        "supportsAllDrives": True,
        "acknowledgeAbuse": True,
    }


# -----------------------------------------------------------
# extract_acl_emails
# -----------------------------------------------------------
def test_extract_acl_emails_filters_owner_and_deleted(monkeypatch: Any) -> None:
    """Day 7 (2026-05-27) Workspace-wide ACL: domain/anyone も groups に展開する。

    会社思想「資料は全て共有物」原則:
      - type='domain' → その domain 名を acl_groups に
      - type='anyone' → WORKSPACE_DOMAIN を acl_groups に
    """
    monkeypatch.setenv("WORKSPACE_DOMAIN", "vectorinc.co.jp")
    perms = [
        DrivePermission(id="1", type="user", role="owner", email_address="o@x.jp"),
        DrivePermission(id="2", type="user", role="reader", email_address="r@x.jp"),
        DrivePermission(id="3", type="group", role="reader", email_address="sales@x.jp"),
        DrivePermission(id="4", type="user", role="writer", email_address="old@x.jp", deleted=True),
        DrivePermission(id="5", type="domain", role="reader", domain="x.jp"),
        DrivePermission(id="6", type="anyone", role="reader"),
    ]
    emails, groups = extract_acl_emails(perms)
    # owner も emails に含まれる（呼び出し側で別途 owner_email として扱う想定）
    assert "o@x.jp" in emails
    assert "r@x.jp" in emails
    assert "sales@x.jp" in groups
    # deleted は除外
    assert "old@x.jp" not in emails
    # domain は domain 名を group に展開
    assert "x.jp" in groups
    # anyone は WORKSPACE_DOMAIN (vectorinc.co.jp) を group に展開
    assert "vectorinc.co.jp" in groups


def test_extract_acl_emails_workspace_domain_env_override(monkeypatch: Any) -> None:
    """WORKSPACE_DOMAIN を env で上書きすると anyone がその domain に展開される。"""
    monkeypatch.setenv("WORKSPACE_DOMAIN", "example.co.jp")
    perms = [DrivePermission(id="1", type="anyone", role="reader")]
    _, groups = extract_acl_emails(perms)
    assert groups == ["example.co.jp"]


def test_extract_acl_emails_ignores_limited_views() -> None:
    perms = [
        DrivePermission(
            id="published",
            type="anyone",
            role="reader",
            view="published",
        ),
        DrivePermission(
            id="metadata",
            type="domain",
            role="reader",
            domain="vectorinc.co.jp",
            view="metadata",
        ),
        DrivePermission(
            id="content",
            type="domain",
            role="reader",
            domain="vectorinc.co.jp",
        ),
    ]

    emails, groups = extract_acl_emails(perms)

    assert emails == []
    assert groups == ["vectorinc.co.jp"]


def test_extract_acl_emails_dedup_groups(monkeypatch: Any) -> None:
    """同じ group / domain が複数 permission で出ても 1 件に dedup される。"""
    monkeypatch.setenv("WORKSPACE_DOMAIN", "vectorinc.co.jp")
    perms = [
        DrivePermission(id="1", type="domain", role="reader", domain="vectorinc.co.jp"),
        DrivePermission(id="2", type="anyone", role="reader"),
        DrivePermission(id="3", type="domain", role="writer", domain="vectorinc.co.jp"),
    ]
    _, groups = extract_acl_emails(perms)
    assert groups == ["vectorinc.co.jp"]  # 1 件のみ


# -----------------------------------------------------------
# get_changes / get_start_page_token
# -----------------------------------------------------------
def test_get_start_page_token_returns_string() -> None:
    fake = FakeDriveService(start_page_token="TOK-1")
    client = GDriveClient(service=fake)
    token = client.get_start_page_token(request_id="r")
    assert token == "TOK-1"


def test_get_changes_returns_change_batch() -> None:
    fake = FakeDriveService(
        changes_list={
            "changes": [
                {
                    "changeType": "file",
                    "fileId": "F1",
                    "removed": False,
                    "time": "2026-05-26T01:00:00Z",
                    "driveId": "DRIVE1",
                },
                {
                    "changeType": "file",
                    "fileId": "F2",
                    "removed": True,
                    "time": "2026-05-26T01:01:00Z",
                },
            ],
            "nextPageToken": None,
            "newStartPageToken": "TOK-NEXT",
        }
    )
    client = GDriveClient(service=fake)
    batch = client.get_changes(page_token="TOK-CURRENT", request_id="r")
    assert isinstance(batch, ChangeBatch)
    assert len(batch.changes) == 2
    assert all(isinstance(c, DriveChange) for c in batch.changes)
    assert batch.changes[0].file_id == "F1"
    assert batch.changes[1].removed is True
    assert batch.next_page_token is None
    assert batch.new_start_page_token == "TOK-NEXT"


# -----------------------------------------------------------
# from_env / _ensure_service
# -----------------------------------------------------------
def test_from_env_warns_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """credentials 用 env が無くても from_env は失敗しない（warning だけ）。"""
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    client = GDriveClient.from_env()
    assert client is not None
    # structlog の log は caplog で見えないが、例外が出ないことだけ確認


def test_ensure_service_uses_injected_service() -> None:
    """service を direct inject すれば _build_credentials は呼ばれない。"""
    fake = FakeDriveService()
    client = GDriveClient(service=fake)
    assert client._ensure_service() is fake


def test_build_credentials_raises_when_nothing_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """credentials 用 env が一切ないと NotImplementedError。"""
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_FORCE_OAUTH", raising=False)
    client = GDriveClient(credentials=None, service=None)
    with pytest.raises(NotImplementedError):
        client._build_credentials()


def test_build_credentials_force_oauth_skips_service_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GOOGLE_FORCE_OAUTH=1 なら SA 鍵があっても OAuth を使う（組織ポリシー回避）。"""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/sa.json")
    monkeypatch.setenv("GOOGLE_FORCE_OAUTH", "1")
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "rt-xyz")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.delenv("GOOGLE_OAUTH_SCOPES", raising=False)

    from google.oauth2.credentials import Credentials

    creds = GDriveClient.from_env(readonly=True)._build_credentials()
    assert isinstance(creds, Credentials)
    assert "https://www.googleapis.com/auth/drive.readonly" in creds.scopes


def test_scopes_default_to_drive_file() -> None:
    """readonly=False ならスコープが drive.file（CASA 不要）になる。"""
    client = GDriveClient.from_env()
    assert "https://www.googleapis.com/auth/drive.file" in client._scopes


def test_scopes_readonly_when_explicit() -> None:
    """readonly=True なら drive.readonly（CASA Tier 2 監査必須）。"""
    client = GDriveClient.from_env(readonly=True)
    assert "https://www.googleapis.com/auth/drive.readonly" in client._scopes
