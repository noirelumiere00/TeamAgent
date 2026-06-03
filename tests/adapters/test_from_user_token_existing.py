"""既存 Gmail/Drive/Sheets アダプタの per-user `from_user_token` テスト（課金0）。

OAuth クライアント未設定（W1 未完）なら fail-closed（ValueError）を確認＝全7サービスが
per-user 経路で本人束縛されることの担保。
"""

from __future__ import annotations

import pytest

from teamagent.adapters.gdrive_client import GDriveClient
from teamagent.adapters.gmail_client import GmailClient
from teamagent.adapters.gsheets_client import GSheetsClient
from teamagent.adapters.oauth_token_store import OAuthToken


def test_existing_adapters_from_user_token_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    tok = OAuthToken(refresh_token="rt", scopes=("gmail.readonly",))
    for cls in (GmailClient, GDriveClient, GSheetsClient):
        with pytest.raises(ValueError, match="GOOGLE_CLIENT_ID"):
            cls.from_user_token(tok)


def test_existing_adapters_readonly_scope_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_user_token は既定で readonly スコープ（G4 最小権限）。"""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    tok = OAuthToken(refresh_token="rt")
    assert GmailClient.from_user_token(tok)._scopes == GmailClient.SCOPES_READONLY
    assert GDriveClient.from_user_token(tok)._scopes == GDriveClient.SCOPES_READONLY
    assert GSheetsClient.from_user_token(tok)._scopes == GSheetsClient.SCOPES_READONLY
