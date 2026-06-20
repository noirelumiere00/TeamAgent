"""gmail_links 純粋ヘルパーの単体テスト（URL 形式の単一真実源を固定）。"""

from __future__ import annotations

from teamagent.gmail_links import gmail_thread_url, slack_link


def test_gmail_thread_url_uses_all_view() -> None:
    # #all/<thread_id> ＝受信トレイ/アーカイブ/下書きのどこに在ってもスレッドを開ける。
    assert gmail_thread_url("19ea6229c5858fb0") == (
        "https://mail.google.com/mail/u/0/#all/19ea6229c5858fb0"
    )


def test_gmail_thread_url_authuser_override() -> None:
    assert gmail_thread_url("abc", authuser=2) == "https://mail.google.com/mail/u/2/#all/abc"


def test_gmail_thread_url_none_or_blank_returns_none() -> None:
    assert gmail_thread_url(None) is None
    assert gmail_thread_url("") is None
    assert gmail_thread_url("   ") is None


def test_slack_link_formats_mrkdwn() -> None:
    assert slack_link("https://x/y", "開く") == "<https://x/y|開く>"


def test_slack_link_none_url_returns_none() -> None:
    assert slack_link(None, "開く") is None
