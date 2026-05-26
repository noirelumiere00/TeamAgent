"""Google OAuth refresh_token を取得する一回限りのヘルパー。

Drive + Gmail OAuth クライアント (Desktop app type) の Client ID / Secret から、
ユーザーがブラウザで認可した結果として返ってくる refresh_token を取得する。

Usage:
    # 環境変数で渡す（推奨、shell history に Secret が残らない）
    export GOOGLE_CLIENT_ID='676659122211-...apps.googleusercontent.com'
    read -s GOOGLE_CLIENT_SECRET   # ← 画面表示なしで入力
    export GOOGLE_CLIENT_SECRET
    python scripts/get_google_refresh_token.py

スクリプト動作:
    1. Client ID/Secret を環境変数から読む
    2. google-auth-oauthlib の InstalledAppFlow を起動
    3. ローカルポート (8080) を一時的に listen し、ブラウザを開く
    4. ユーザーが Google で承認 → リダイレクトで code を受け取る
    5. code を refresh_token + access_token に交換
    6. 結果を stdout に出力（CLIENT_SECRET は出さない）

注意:
    - 取得後の refresh_token は AWS Secrets Manager に投入すること（コマンドは末尾に表示）
    - スコープ: drive.file + drive.metadata.readonly + gmail.modify
    - 取得後はこの script をそのまま再実行しなくて良い（1 回切り）
"""

from __future__ import annotations

import json
import os
import sys

REQUIRED_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


def main() -> int:
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print(
            "ERROR: GOOGLE_CLIENT_ID と GOOGLE_CLIENT_SECRET の両方を環境変数で設定してください。\n"
            "例:\n"
            "  export GOOGLE_CLIENT_ID='...apps.googleusercontent.com'\n"
            "  read -s GOOGLE_CLIENT_SECRET; export GOOGLE_CLIENT_SECRET",
            file=sys.stderr,
        )
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "ERROR: google-auth-oauthlib が未インストール。\n"
            "  cd ~/Documents/TeamAgent && source .venv/bin/activate\n"
            "  pip install google-auth-oauthlib",
            file=sys.stderr,
        )
        return 1

    # Desktop app 用のクライアント設定を in-memory で組み立てる
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, REQUIRED_SCOPES)
    # access_type=offline + prompt=consent を必ず付ける（refresh_token を確実に貰うため）
    # port=8765: ローカルの Adminer (8080) や開発サーバと被らないため
    creds = flow.run_local_server(
        port=8765,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message=(
            "\n=== Google OAuth 認可 ===\n"
            "ブラウザが自動的に開きます。vectorinc.co.jp アカウントでログインし、\n"
            "Drive + Gmail へのアクセスを許可してください。\n"
        ),
        success_message=(
            "✅ 認可完了。このタブは閉じて構いません。"
            "ターミナルに戻って refresh_token を取得します。"
        ),
        open_browser=True,
    )

    refresh_token = creds.refresh_token
    if not refresh_token:
        print(
            "ERROR: refresh_token が返ってこなかった。\n"
            "もう一度実行し、画面に「アクセスを許可」が出るまでブラウザで操作してください。",
            file=sys.stderr,
        )
        return 1

    print("\n" + "=" * 60)
    print("✅ refresh_token 取得成功")
    print("=" * 60)
    print("\n以下のコマンドで AWS Secrets Manager に投入してください:")
    print()
    print("  # 既存 secret が無い場合（初回）:")
    print(
        f"  aws secretsmanager create-secret "
        f"--name teamagent/dev/google_oauth "
        f"--description 'Google OAuth (Drive + Gmail) for TeamAgent' "
        f"--region ap-northeast-1 "
        f"--secret-string '"
        + json.dumps(
            {
                "client_id": client_id,
                "client_secret": "<paste client_secret here>",
                "refresh_token": refresh_token,
            }
        )
        + "'"
    )
    print()
    print("  # 既存 secret を更新する場合:")
    print(
        f"  aws secretsmanager put-secret-value "
        f"--secret-id teamagent/dev/google_oauth "
        f"--region ap-northeast-1 "
        f"--secret-string '"
        + json.dumps(
            {
                "client_id": client_id,
                "client_secret": "<paste client_secret here>",
                "refresh_token": refresh_token,
            }
        )
        + "'"
    )
    print()
    print("⚠️  上記コマンドの <paste client_secret here> を実値に置き換えてから実行してください。")
    print(f"   client_id は既に埋まっています: {client_id[:30]}...")
    print(f"   refresh_token (秘匿): {refresh_token[:8]}... ({len(refresh_token)} chars)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
