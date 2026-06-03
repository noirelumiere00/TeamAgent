"""per-user Workspace 認可 onboarding（C方式・各メンバーが自分の PC で実行）。

各メンバーが自分の Google を認可して、本人の refresh token を取得・保存する。
loopback OAuth（ブラウザ同意）で全7サービス readonly を一括認可する＝「他の1人が自分で
Google 連携して、自分の Workspace をエージェントに使わせる」MVP の手元側ツール。

Usage:
    # W1 の OAuth クライアント(Desktop)の値を env で渡す（shell history に残さない）
    export GOOGLE_CLIENT_ID='....apps.googleusercontent.com'
    read -s GOOGLE_CLIENT_SECRET; export GOOGLE_CLIENT_SECRET
    PYTHONPATH=src python scripts/connect_workspace.py your-email@vectorinc.co.jp

保存:
    - OAUTH_KMS_KEY_ID + DATABASE_URL（SSMトンネル）があれば RdsTokenStore に **KMS暗号化**保存。
    - 無ければ refresh_token を表示（安全経路で管理者へ。平文を Slack/コミットに貼らない）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from teamagent.adapters.google_oauth_flow import WORKSPACE_READONLY_SCOPES  # noqa: E402
from teamagent.adapters.oauth_token_store import OAuthToken  # noqa: E402


def _store_token(email: str, token: OAuthToken) -> bool:
    """保存先（OAUTH_KMS_KEY_ID + DATABASE_URL）があれば RdsTokenStore に暗号化保存。"""
    key_id = os.environ.get("OAUTH_KMS_KEY_ID")
    if not (key_id and os.environ.get("DATABASE_URL")):
        return False
    from teamagent.adapters.oauth_token_store import KmsCipher, RdsTokenStore
    from teamagent.adapters.pgvector_client import PgVectorClient

    RdsTokenStore(PgVectorClient.from_env(), KmsCipher(key_id)).put(email, token)
    return True


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/connect_workspace.py <your-email>", file=sys.stderr)
        return 1
    email = sys.argv[1].strip().lower()

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        print(
            "ERROR: GOOGLE_CLIENT_ID と GOOGLE_CLIENT_SECRET を env で設定してください（W1）",
            file=sys.stderr,
        )
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("ERROR: google-auth-oauthlib が未インストール（pip install）", file=sys.stderr)
        return 1

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, list(WORKSPACE_READONLY_SCOPES))
    creds = flow.run_local_server(
        port=8765,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message=(
            "\n=== Google Workspace 認可（あなた本人）===\n"
            "ブラウザで自分のアカウントにログインし、5-7サービス（readonly）の参照を許可してください。\n"
        ),
        success_message="✅ 認可完了。タブを閉じてターミナルに戻ってください。",
        open_browser=True,
    )

    if not creds.refresh_token:
        print(
            "ERROR: refresh_token が取得できません（同意画面で『許可』まで進めてください）",
            file=sys.stderr,
        )
        return 1

    token = OAuthToken(
        refresh_token=str(creds.refresh_token),
        scopes=tuple(creds.scopes or WORKSPACE_READONLY_SCOPES),
    )
    rt = str(creds.refresh_token)

    if _store_token(email, token):
        print(f"\n✅ {email} のトークンを RdsTokenStore に保存しました（KMS暗号化・本人行RLS）。")
        print("   → orchestrator を USE_WORKSPACE_TOOLS=1 + ctx user_email=本人 で起動すれば疎通。")
    else:
        print("\n✅ 認可成功（保存先未設定＝手動保存モード）。以下を安全経路で管理者へ:")
        print(f"   email        : {email}")
        print(f"   refresh_token: {rt[:8]}...{rt[-4:]}（秘匿・{len(rt)} chars）")
        print("   ⚠️ 平文を Slack / コミットに貼らないこと（Secrets / 暗号DM 経由で）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
