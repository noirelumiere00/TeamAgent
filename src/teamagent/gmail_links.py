"""Gmail deep-link の組み立て（authuser で本人アカウント固定）。

朝digest配信・押下後ブロック・将来の OC ハンドラなど複数箇所で同じ URL 形式を使うため、
形式の単一真実源をここに置く（重複を避ける）。

⚠️ ここは「表現（URL 文字列）」だけを扱う純粋関数。生本文・生件名は受け取らない。
thread_id は Gmail の不透明 ID であり PII ではない（`from:<addr>` クエリと違い差出人を
漏らさない）。authuser に渡す email は呼び出し側（本人 DM 限定の配信層）が判断する。
"""

from __future__ import annotations

from urllib.parse import quote


def gmail_account_base(user_email: str) -> str:
    """アカウント選択つき Gmail ベース URL（ハッシュ直前まで）。

    複数 Google ログイン環境で `u/0`（先頭アカウント）だと本人と別アカウントで開く事故が
    起きるため、email が判明していれば `?authuser=<email>` で本人に固定。不明時は `u/0`。
    戻り値に `#inbox` / `#drafts` / `#all/<tid>` を連結して使う。
    """
    if user_email and "@" in user_email:
        return f"https://mail.google.com/mail/?authuser={quote(user_email, safe='@')}"
    return "https://mail.google.com/mail/u/0/"


def gmail_thread_url(thread_id: str | None, user_email: str) -> str | None:
    """スレッド（会話）を開く deep link。返信下書きはスレッド内にインライン表示される。

    `#all/<thread_id>` は受信トレイ/アーカイブ/下書きのどこに在っても確実に開ける。
    thread_id が空なら None（呼び出し側で汎用リンクにフォールバック）。
    """
    if not thread_id:
        return None
    return f"{gmail_account_base(user_email)}#all/{thread_id}"


def gmail_inbox_url(user_email: str) -> str:
    return f"{gmail_account_base(user_email)}#inbox"


def gmail_drafts_url(user_email: str) -> str:
    return f"{gmail_account_base(user_email)}#drafts"
