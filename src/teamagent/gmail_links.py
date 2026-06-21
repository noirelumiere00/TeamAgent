"""Gmail / Google Calendar の deep-link を組み立てる純粋ヘルパー。

skill 層・スクリプト層（morning_digest / reminder）・runtime 層の複数箇所で
同じ URL 形式を使うため、形式の単一真実源をここに置く（重複を避ける）。

⚠️ ここは「表現（URL 文字列）」だけを扱う純粋関数。生本文・生件名は受け取らない。
thread_id / message_id は Gmail の不透明 ID であり PII ではないが、戻り値として
公開してよいかは呼び出し側（skill のマスク方針 G3）が判断する。
"""

from __future__ import annotations

# 受信トレイ全体・下書きフォルダ（項目別 from: はマスク済みのため deep-link しない）。
GMAIL_INBOX_URL = "https://mail.google.com/mail/u/0/#inbox"
GMAIL_DRAFTS_URL = "https://mail.google.com/mail/u/0/#drafts"
CALENDAR_URL = "https://calendar.google.com/"


def gmail_thread_url(thread_id: str | None, *, authuser: int = 0) -> str | None:
    """スレッド（会話）を開く deep-link。

    `#all/<thread_id>` は All Mail ビューで開くため、受信トレイ/アーカイブ/下書きの
    いずれに在っても確実にそのスレッドを開ける（返信下書きはスレッド内にインライン表示）。
    thread_id が空なら None（呼び出し側でフォールバック表示）。
    """
    if not thread_id or not isinstance(thread_id, str):
        return None
    tid = thread_id.strip()
    if not tid:
        return None
    return f"https://mail.google.com/mail/u/{authuser}/#all/{tid}"


def slack_link(url: str | None, label: str) -> str | None:
    """Slack mrkdwn のリンク `<url|label>`。url が None なら None。"""
    if not url:
        return None
    return f"<{url}|{label}>"
