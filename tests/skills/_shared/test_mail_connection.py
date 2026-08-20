"""_shared/mail_connection.py の契約テスト（P0-4: 未連携シグナルの構造化）。

「文言が calendar_freebusy と揃っている」「断絶した導線（/teamagent connect）が
skills 配下から消えている」を **grep 相当の実測**で固定する。
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from teamagent.adapters.oauth_token_store import InMemoryTokenStore, OAuthToken
from teamagent.skills._shared.mail_connection import (
    CONNECT_SUFFIX,
    GMAIL_FAILED_MESSAGE,
    MESSAGE_BY_CONNECTION_ERROR,
    NOT_CONNECTED_MESSAGE,
    REAUTH_NEEDED_MESSAGE,
    MailConnectionError,
    classify_gmail_failure,
    resolve_gmail_for_user,
    searched_inbox_prefix,
)

OWNER = "s-komata@vectorinc.co.jp"
_SRC = pathlib.Path(__file__).resolve().parents[3] / "src" / "teamagent" / "skills"


def test_connect_wording_matches_calendar_freebusy() -> None:
    """導線の文言は calendar_freebusy と共有する（片方だけ古くなるのを防ぐ）。"""
    from teamagent.skills.calendar_freebusy.skill import _ERR_MSG

    assert _ERR_MSG["not_connected"].endswith(CONNECT_SUFFIX)
    assert NOT_CONNECTED_MESSAGE.endswith(CONNECT_SUFFIX)
    assert REAUTH_NEEDED_MESSAGE.endswith(CONNECT_SUFFIX)
    assert NOT_CONNECTED_MESSAGE == "メールの確認には" + CONNECT_SUFFIX
    assert "@NewsTV AI に『連携』と話しかけて許可してください" in CONNECT_SUFFIX


def test_dead_slash_command_is_gone_from_mail_skills() -> None:
    """『/teamagent connect』はその語では起動しない断絶導線。mail_* から根絶する。"""
    offenders = [
        str(path.relative_to(_SRC))
        for path in sorted(_SRC.rglob("*.py"))
        if path.parent.name in {"mail_summary", "mail_followup"}
        and "/teamagent connect" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_dead_slash_command_is_gone_from_shared_connection_module() -> None:
    text = (_SRC / "_shared" / "mail_connection.py").read_text(encoding="utf-8")
    # docstring では「なぜ消したか」の説明として引用してよいが、利用者向け文言には無いこと。
    assert "/teamagent connect" not in NOT_CONNECTED_MESSAGE
    assert "/teamagent connect" not in REAUTH_NEEDED_MESSAGE
    assert "@NewsTV AI に『連携』" in text


def test_missing_token_store_is_operational_bug_not_user_message() -> None:
    """TokenStore 未設定＝配線ミス。利用者向け『連携してください』に落とさない。"""
    with pytest.raises(PermissionError):
        resolve_gmail_for_user(None, OWNER, misconfig_message="TokenStore が未設定です")


def test_missing_token_raises_not_connected() -> None:
    with pytest.raises(MailConnectionError) as exc:
        resolve_gmail_for_user(InMemoryTokenStore(), OWNER, misconfig_message="x")
    assert exc.value.code == "not_connected"
    assert exc.value.message == NOT_CONNECTED_MESSAGE


def test_credential_value_error_becomes_reauth_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    from teamagent.adapters import gmail_client as gc

    def _boom(token: Any, *, readonly: bool = True) -> Any:
        raise ValueError("空 refresh token")

    monkeypatch.setattr(gc.GmailClient, "from_user_token", staticmethod(_boom))
    store = InMemoryTokenStore({OWNER: OAuthToken(refresh_token="x")})

    with pytest.raises(MailConnectionError) as exc:
        resolve_gmail_for_user(store, OWNER, misconfig_message="x")
    assert exc.value.code == "reauth_needed"
    assert exc.value.message == REAUTH_NEEDED_MESSAGE


def test_searched_inbox_prefix_asserts_both_facts() -> None:
    """「連携は正常」と「実際に検索した」の 2 つを必ず言う（片方だけでは創作を止められない）。"""
    prefix = searched_inbox_prefix("s***@vectorinc.co.jp")
    assert "連携は正常です" in prefix
    assert "s***@vectorinc.co.jp を実際に検索しました" in prefix


# ── 要修正3: 実検索で初めて露見する「トークンの生死」を機械可読にする ─────────


class _RefreshError(Exception):
    """google.auth.exceptions.RefreshError の代役（skill 層は google を import しない）。"""


@pytest.mark.parametrize(
    "exc",
    [
        _RefreshError("invalid_grant: Token has been expired or revoked."),
        RuntimeError("401 Unauthorized"),
        RuntimeError("403 insufficient authentication scopes"),
        RuntimeError("The credentials were revoked"),
    ],
)
def test_auth_failures_are_classified_as_reauth_needed(exc: Exception) -> None:
    """失効・スコープ不足は「再連携すれば直る」＝oauth_connect へ誘導できる形にする。"""
    assert classify_gmail_failure(exc) == "reauth_needed"


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("backendError: internal failure"),
        TimeoutError("deadline exceeded"),
        ValueError("rateLimitExceeded"),
    ],
)
def test_other_failures_are_gmail_api_failed(exc: Exception) -> None:
    assert classify_gmail_failure(exc) == "gmail_api_failed"


def test_api_failure_message_never_reads_as_zero_hits() -> None:
    """「0 件」と読める文言にしない（P0-3 と同じ規律: 障害と空を混ぜない）。"""
    assert "0 件という意味ではありません" in GMAIL_FAILED_MESSAGE
    assert MESSAGE_BY_CONNECTION_ERROR["gmail_api_failed"] == GMAIL_FAILED_MESSAGE
    assert MESSAGE_BY_CONNECTION_ERROR["reauth_needed"] == REAUTH_NEEDED_MESSAGE


def test_resolve_does_not_verify_token_liveness_and_says_so() -> None:
    """**ネットワーク I/O をしない**＝失効は検知できない、という限界を明文化しておく。

    この限界が docstring から消えると「連携は正常です」を無条件の断言だと読む人が出る。
    """
    src = (
        pathlib.Path(__file__).resolve().parents[3]
        / "src"
        / "teamagent"
        / "skills"
        / "_shared"
        / "mail_connection.py"
    ).read_text(encoding="utf-8")
    assert "トークンの**生死**は検証していない" in src
    assert "ネットワーク I/O はしない" in src
