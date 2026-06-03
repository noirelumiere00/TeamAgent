"""Google OAuth refresh_token を取得する一回限りのヘルパー。

Drive + Gmail OAuth クライアント (Desktop app type) の Client ID / Secret から、
ユーザーがブラウザで認可した結果として返ってくる refresh_token を取得する。

Usage（推奨：client_id/secret を手入力せず Secrets Manager から読む）:
    set -a; source .env.production; set +a
    source scripts/load_secrets.sh        # GOOGLE_CLIENT_ID/SECRET を実値で env に展開
    python scripts/get_google_refresh_token.py --update-secret teamagent/dev/google_oauth
    # → ブラウザで承認（Sheets も許可）→ 新 refresh_token で secret を自動更新 → bot 再起動

スクリプト動作:
    1. Client ID/Secret を環境変数から読む
    2. google-auth-oauthlib の InstalledAppFlow を起動
    3. ローカルポート (8080) を一時的に listen し、ブラウザを開く
    4. ユーザーが Google で承認 → リダイレクトで code を受け取る
    5. code を refresh_token + access_token に交換
    6. 結果を stdout に出力（CLIENT_SECRET は出さない）

注意:
    - 取得後の refresh_token は AWS Secrets Manager に投入すること（コマンドは末尾に表示）
    - スコープ: drive.readonly + drive.file + drive.metadata.readonly + spreadsheets + gmail.modify
      （spreadsheets を 2026-06-01 追加: 案件シートの AI一次チェック列へ書込むため。
        書込スコープを足したら必ずこの script を再実行して refresh_token を更新する）
    - 取得後はこの script をそのまま再実行しなくて良い（1 回切り。スコープ変更時のみ再実行）
"""

from __future__ import annotations

import argparse
import json
import os
import sys

REQUIRED_SCOPES = [
    # Drive: folder bulk ingest のため drive.readonly に拡張（Day 7, 2026-05-27）
    # Internal OAuth (vectorinc.co.jp 限定) なので CASA 審査不要。
    # drive.file は per-file opened only で folder ingest に向かないため readonly に。
    "https://www.googleapis.com/auth/drive.readonly",
    # drive.file は ユーザーが書き込み作業する想定で温存
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    # Sheets 読み書き（2026-06-01 追加）: 動画一次FB(AIチェック結果)を案件シートの
    # 「AI一次チェック」列へ追記するため。spreadsheets は sensitive スコープだが
    # 社内 OAuth (vectorinc.co.jp 限定) なので CASA 審査は不要（同意画面の警告のみ）。
    # ※ 書込は adapters/gsheets_client.update_single_cell の単一セル限定＋削除系API不使用で
    #   「既存データを絶対に削除しない」ことを保証している。
    "https://www.googleapis.com/auth/spreadsheets",
    # Gmail: adapter 層 deny (PR #49) で破壊的メソッドは物理封鎖済
    "https://www.googleapis.com/auth/gmail.modify",
]


def _update_secret(secret_name: str, region: str, payload: dict[str, str]) -> None:
    """Secrets Manager の secret を {client_id, client_secret, refresh_token} で更新する。

    既存があれば put-secret-value、無ければ create-secret。client_secret を chat 等に
    出さずに更新できるよう、この場で boto3 経由で書き込む。
    """
    import boto3  # 遅延 import（boto3 は重い）

    sm = boto3.client("secretsmanager", region_name=region)
    body = json.dumps(payload, ensure_ascii=False)
    try:
        sm.put_secret_value(SecretId=secret_name, SecretString=body)
        print(f"✅ Secrets Manager を更新しました: {secret_name}（put-secret-value）")
    except sm.exceptions.ResourceNotFoundException:
        sm.create_secret(
            Name=secret_name,
            Description="Google OAuth (Drive + Gmail + Sheets) for TeamAgent",
            SecretString=body,
        )
        print(f"✅ Secrets Manager に新規作成しました: {secret_name}（create-secret）")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Google OAuth refresh_token を取得（スコープ更新用）"
    )
    parser.add_argument(
        "--update-secret",
        metavar="SECRET_NAME",
        default=None,
        help="取得後に Secrets Manager の該当 secret を自動更新する（例: teamagent/dev/google_oauth）",
    )
    parser.add_argument("--region", default="ap-northeast-1", help="AWS リージョン")
    args = parser.parse_args()

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret or "…" in client_id or "..." in client_id:
        print(
            "ERROR: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET が未設定か不正です。\n"
            "手で入力せず、Secrets Manager から実値を読み込んでください:\n"
            "  set -a; source .env.production; set +a\n"
            "  source scripts/load_secrets.sh\n"
            "  python scripts/get_google_refresh_token.py --update-secret teamagent/dev/google_oauth",
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
    print(f"   refresh_token (秘匿): {refresh_token[:8]}… ({len(refresh_token)} chars)")
    print("=" * 60)

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }

    if args.update_secret:
        # 環境変数の client_secret をそのまま使って Secrets Manager を直接更新（chat に出さない）
        _update_secret(args.update_secret, args.region, payload)
        print("\n🎉 完了。bot を再起動すれば spreadsheets スコープが反映されます。")
        return 0

    # --update-secret を付けない場合は手動更新コマンドの雛形を出す（client_secret は伏せる）
    masked = dict(payload, client_secret="<paste client_secret here>")
    print("\n以下で AWS Secrets Manager を更新してください（--update-secret で自動化も可）:")
    print(
        "  aws secretsmanager put-secret-value "
        "--secret-id teamagent/dev/google_oauth --region ap-northeast-1 "
        "--secret-string '" + json.dumps(masked) + "'"
    )
    print("⚠️  <paste client_secret here> を実値に置き換えてから実行してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
