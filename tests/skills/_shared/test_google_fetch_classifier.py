"""F0: 厳しめの分類器 classify_google_fetch_failure のテスト。

フェイクは使わず、**本番で実際に飛んでくる例外そのもの**を作って通す:
  - google-auth が token endpoint のエラー応答から作る RefreshError
    （``google.oauth2._client._handle_error_response`` を実際に呼んで作る）
  - googleapiclient の HttpError（本物の httplib2.Response と Google API のエラー JSON）
  - build_user_credentials が投げる ValueError / MissingRefreshTokenError（実関数を呼ぶ）

固定する仕様（本人の再連携で直るものだけを拾う）:
  invalid_grant → token_expired ／ refresh token 空 → token_expired
  403 + スコープ不足 → scope_missing
  invalid_client・admin_policy_enforced・unauthorized_client・その他の 403・401・5xx・
  タイムアウト・通信断・secret 欠落 → temporary（全員へ「再連携して」と出す事故を防ぐ）
"""

from __future__ import annotations

import json
from typing import Any

import httplib2
import pytest
from google.auth import exceptions as gauth_exc
from google.oauth2 import _client as google_oauth_client
from googleapiclient.errors import HttpError

from teamagent.adapters.google_auth import MissingRefreshTokenError, build_user_credentials
from teamagent.adapters.oauth_token_store import OAuthToken
from teamagent.skills._shared.mail_connection import (
    FETCH_SCOPE_MISSING,
    FETCH_TEMPORARY,
    FETCH_TOKEN_EXPIRED,
    classify_gmail_failure,
    classify_google_fetch_failure,
    google_fetch_failure_detail,
)


def refresh_error(error: str, description: str = "") -> gauth_exc.RefreshError:
    """google-auth が token endpoint の応答から作るのと同じ経路で RefreshError を作る。"""
    try:
        google_oauth_client._handle_error_response(
            {"error": error, "error_description": description}, False
        )
    except gauth_exc.RefreshError as exc:
        return exc
    raise AssertionError("_handle_error_response が RefreshError を投げなかった")


def http_error(
    status: int,
    *,
    message: str = "",
    reason: str = "",
    details_reason: str = "",
    uri: str = "https://gmail.googleapis.com/gmail/v1/users/me/messages?alt=json",
) -> HttpError:
    """Google API の実際のエラー JSON を本物の HttpError に通す。"""
    error: dict[str, Any] = {"code": status, "message": message, "status": "X"}
    if reason:
        error["errors"] = [{"message": message, "domain": "global", "reason": reason}]
    if details_reason:
        error["details"] = [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": details_reason,
                "domain": "googleapis.com",
            }
        ]
    resp = httplib2.Response({"status": str(status), "content-type": "application/json"})
    return HttpError(resp, json.dumps({"error": error}).encode(), uri=uri)


# ── token_expired ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "description",
    ["Token has been expired or revoked.", "Bad Request", "Account has been deleted"],
)
def test_invalid_grant_is_token_expired(description: str) -> None:
    exc = refresh_error("invalid_grant", description)
    assert classify_google_fetch_failure(exc) == FETCH_TOKEN_EXPIRED
    assert google_fetch_failure_detail(exc) == "RefreshError:invalid_grant"


def test_invalid_grant_without_structured_dict_is_still_token_expired() -> None:
    """構造化 dict が無い形（先頭文字列だけ）でも invalid_grant を読む。"""
    exc = gauth_exc.RefreshError("invalid_grant: Token has been expired or revoked.")
    assert classify_google_fetch_failure(exc) == FETCH_TOKEN_EXPIRED


def test_empty_refresh_token_is_token_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_user_credentials の実関数：refresh token 空は型付き例外＝再連携で直る。"""
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("CONNECT_GOOGLE_CLIENT_SECRET", "dummy-secret")
    token = OAuthToken(refresh_token="", scopes=())
    with pytest.raises(MissingRefreshTokenError) as info:
        build_user_credentials(token)
    assert isinstance(info.value, ValueError)  # 既存の except ValueError はそのまま効く
    assert classify_google_fetch_failure(info.value) == FETCH_TOKEN_EXPIRED


# ── scope_missing ───────────────────────────────────────────────────────────


def test_403_scope_insufficient_details_is_scope_missing() -> None:
    """今の Google が返す形（details の ErrorInfo.reason）。"""
    exc = http_error(
        403,
        message="Request had insufficient authentication scopes.",
        reason="insufficientPermissions",
        details_reason="ACCESS_TOKEN_SCOPE_INSUFFICIENT",
    )
    assert classify_google_fetch_failure(exc) == FETCH_SCOPE_MISSING
    assert google_fetch_failure_detail(exc) == "HttpError:403"


def test_403_legacy_insufficient_permissions_is_scope_missing() -> None:
    """details の無い旧形式（errors[].reason だけ）。"""
    exc = http_error(403, message="Insufficient Permission", reason="insufficientPermissions")
    assert classify_google_fetch_failure(exc) == FETCH_SCOPE_MISSING


# ── temporary（再連携に誘導しない）─────────────────────────────────────────


@pytest.mark.parametrize(
    "error",
    ["invalid_client", "admin_policy_enforced", "unauthorized_client", "invalid_scope", ""],
)
def test_other_refresh_errors_are_temporary(error: str) -> None:
    """secret 不正・管理者のアプリ制限は RefreshError でも本人の再連携では直らない。"""
    exc = refresh_error(error, "Unauthorized")
    assert classify_google_fetch_failure(exc) == FETCH_TEMPORARY


def test_invalid_client_is_what_the_loose_classifier_gets_wrong() -> None:
    """既存の classify_gmail_failure は invalid_client も reauth_needed にする（だから使わない）。"""
    exc = refresh_error("invalid_client", "Unauthorized")
    assert classify_gmail_failure(exc) == "reauth_needed"
    assert classify_google_fetch_failure(exc) == FETCH_TEMPORARY
    assert google_fetch_failure_detail(exc) == "RefreshError:invalid_client"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (403, "rateLimitExceeded"),
        (403, "domainPolicy"),
        (403, "accessNotConfigured"),
        (401, "authError"),
        (429, "rateLimitExceeded"),
        (500, "backendError"),
        (503, "backendError"),
    ],
)
def test_other_http_errors_are_temporary(status: int, reason: str) -> None:
    exc = http_error(status, message="x", reason=reason)
    assert classify_google_fetch_failure(exc) == FETCH_TEMPORARY
    assert google_fetch_failure_detail(exc) == f"HttpError:{status}"


def test_missing_client_secret_is_temporary(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-06-25 の回帰と同じ形（連携用 secret 欠落）＝全員に起きる＝再連携では直らない。"""
    for name in (
        "CONNECT_GOOGLE_CLIENT_ID",
        "CONNECT_GOOGLE_CLIENT_SECRET",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    token = OAuthToken(refresh_token="rt", scopes=())
    with pytest.raises(ValueError) as info:
        build_user_credentials(token)
    assert not isinstance(info.value, MissingRefreshTokenError)
    assert classify_google_fetch_failure(info.value) == FETCH_TEMPORARY
    assert google_fetch_failure_detail(info.value) == "ValueError"


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("timed out"),
        TimeoutError("timed out"),
        gauth_exc.TransportError("Connection reset by peer"),
        ConnectionResetError(104, "reset"),
        RuntimeError("invalid_grant appears in a message but not a RefreshError"),
    ],
)
def test_timeouts_transport_and_unrelated_errors_are_temporary(exc: BaseException) -> None:
    """文面に invalid_grant があっても RefreshError でなければ拾わない（型で判定する）。"""
    assert classify_google_fetch_failure(exc) == FETCH_TEMPORARY


# ── 内訳コードに中身を混ぜない ─────────────────────────────────────────────


def test_detail_never_contains_uri_or_message_text() -> None:
    """HttpError の文面には URI（検索語・thread id）が入る。内訳コードは型名と数字だけ。"""
    exc = http_error(
        503,
        message="極秘案件の件名",
        reason="backendError",
        uri="https://gmail.googleapis.com/gmail/v1/users/me/threads/18c2secret?q=極秘",
    )
    detail = google_fetch_failure_detail(exc)
    assert detail == "HttpError:503"
    for leak in ("極秘", "18c2secret", "gmail.googleapis.com"):
        assert leak not in detail


def test_detail_fits_the_output_field() -> None:
    """型名 40 字＋コード 40 字でも MorningDigestOutput の上限（80 字）に収まる。"""

    class RefreshError(Exception):  # 型名の照合だけで拾われる（google を import しない判定）
        pass

    long_name = type("A" * 60 + "RefreshError", (RefreshError,), {})
    exc = long_name("x", {"error": "invalid_" + "a" * 32})
    assert len(google_fetch_failure_detail(exc)) <= 80
