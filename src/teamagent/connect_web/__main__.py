"""``python -m teamagent.connect_web`` で連携コールバックを起動する。

必要 env: OAUTH_REDIRECT_URI（このアプリの公開URL/oauth2/callback と一致）・
GOOGLE_CLIENT_ID/SECRET（Web型クライアント）・OAUTH_STATE_SECRET（Slack側 make_state と共有）・
OAUTH_KMS_KEY_ID・DATABASE_URL。MVP はローカル（127.0.0.1）、本番は中央URL(HTTPS)へデプロイ。
env: CONNECT_WEB_HOST(=127.0.0.1) / CONNECT_WEB_PORT(=8788)。
"""

from __future__ import annotations

import os

import uvicorn

from teamagent.connect_web.app import create_app

app = create_app()


def main() -> None:
    host = os.environ.get("CONNECT_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("CONNECT_WEB_PORT", "8788"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
