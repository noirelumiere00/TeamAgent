"""Google OAuth credentials の動作確認スクリプト。

Secrets Manager に投入した teamagent/dev/google_oauth を load_secrets.sh 経由で
読み込んだ後、Drive API + Gmail API へ実際にアクセスして credentials が有効か確認する。

Usage:
    set -a; source .env.production; set +a
    source scripts/load_secrets.sh
    python scripts/verify_google_oauth.py

成功すると:
    ✅ Drive: <userEmail> がアクセス可能（about.get で me を取得）
    ✅ Gmail: <userEmail> の labels 数: 30 件
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()

    if not (client_id and client_secret and refresh_token):
        print(
            "ERROR: 環境変数が未設定。\n"
            "  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_OAUTH_REFRESH_TOKEN\n"
            "  → source scripts/load_secrets.sh を先に実行してください",
            file=sys.stderr,
        )
        return 1

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        print(
            "ERROR: google-api-python-client または google-auth が未インストール。\n"
            "  pip install google-api-python-client google-auth",
            file=sys.stderr,
        )
        return 1

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=[
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.metadata.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
        ],
    )

    # === Drive API ===
    try:
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        about = drive.about().get(fields="user(emailAddress,displayName)").execute()
        user = about.get("user", {})
        print(f"✅ Drive: {user.get('emailAddress')} ({user.get('displayName')}) アクセス OK")
    except Exception as e:
        print(f"❌ Drive 接続失敗: {e}", file=sys.stderr)
        return 1

    # === Gmail API ===
    try:
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = gmail.users().getProfile(userId="me").execute()
        labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
        print(
            f"✅ Gmail: {profile.get('emailAddress')} アクセス OK "
            f"(messages={profile.get('messagesTotal')}, labels={len(labels)})"
        )
    except Exception as e:
        print(f"❌ Gmail 接続失敗: {e}", file=sys.stderr)
        return 1

    print("\n🎉 Google OAuth (Drive + Gmail) の credentials 動作確認 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
