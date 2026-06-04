"""営業に配る per-user Google 連携リンクを生成する（管理者が実行 → Slack/メールで配布）。

各営業は**届いたリンクを開いて自分のGoogleで「許可」するだけ**で連携完了（コマンド不要）。
同意後は connect_web の /oauth2/callback が token を KMS暗号化して RDS に保存する。

Usage:
    export OAUTH_REDIRECT_URI='https://<連携サーバ>/oauth2/callback'   # connect_web の公開URL
    export OAUTH_STATE_SECRET='...'                                   # connect_web と同一値
    export GOOGLE_CLIENT_ID='<Web型クライアントID>.apps.googleusercontent.com'
    export GOOGLE_CLIENT_SECRET='...'
    python scripts/make_connect_links.py taro@vectorinc.co.jp hanako@vectorinc.co.jp
    # または: python scripts/make_connect_links.py --file emails.txt

出力: 「email <TAB> リンク」を1人1行。これをコピーして本人へ送る（本人だけに送ること）。

⚠️ 運用上の注意（重要）: リンクは「その email 用」に署名(state)されているので、**正しい人に**
送ること。受け取った本人は**自分の会社Googleアカウント**でログイン＆許可する（Internal アプリ＋
会社ドメイン制限で社外アカウントは弾かれる）。別アカウントで許可すると意図しない紐付けになり得る。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from teamagent.adapters.google_oauth_flow import OAuthConsentFlow  # noqa: E402


def _load_emails(argv: list[str]) -> list[str]:
    if len(argv) >= 2 and argv[0] == "--file":
        text = Path(argv[1]).read_text(encoding="utf-8")
        raw = text.replace(",", "\n").split("\n")
    else:
        raw = argv
    return [e.strip().lower() for e in raw if e.strip() and "@" in e]


def main() -> int:
    emails = _load_emails(sys.argv[1:])
    if not emails:
        print(
            "Usage: python scripts/make_connect_links.py <email> [email...] | --file emails.txt",
            file=sys.stderr,
        )
        return 1
    redirect = os.environ.get("OAUTH_REDIRECT_URI", "").strip()
    if not redirect:
        print(
            "ERROR: OAUTH_REDIRECT_URI が未設定です（connect_web の公開 callback URL）",
            file=sys.stderr,
        )
        return 1
    if not (os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET")):
        print(
            "ERROR: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET が未設定です（Web型クライアント）",
            file=sys.stderr,
        )
        return 1
    if not os.environ.get("OAUTH_STATE_SECRET"):
        print("ERROR: OAUTH_STATE_SECRET が未設定です（connect_web と同一値）", file=sys.stderr)
        return 1

    flow = OAuthConsentFlow(redirect_uri=redirect)
    print(f"# {len(emails)} 名分の連携リンク（email <TAB> link）。本人だけに送ってください。\n")
    for email in emails:
        url, _state = flow.authorization_url(email)
        print(f"{email}\t{url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
