"""``python -m teamagent.connect_web`` で連携コールバックを起動する。

必要 env: OAUTH_REDIRECT_URI（このアプリの公開URL/oauth2/callback と一致）・
GOOGLE_CLIENT_ID/SECRET（Web型クライアント）・OAUTH_STATE_SECRET（Slack側 make_state と共有）・
OAUTH_KMS_KEY_ID・DATABASE_URL。MVP はローカル（127.0.0.1）、本番は中央URL(HTTPS)へデプロイ。
env: CONNECT_WEB_HOST(=127.0.0.1) / CONNECT_WEB_PORT(=8788)。

ログ: 起動の最初に ``configure_logging()`` を呼び、タスク定義の ``STRUCTLOG_FORMAT=json``
（connect_web.tf で設定済み）に従って構造化ログを JSON で出す（F0・2026-09-29）。これまで
connect-web は一度も structlog を設定しておらず、既定の console 形式で出ていたため、
CloudWatch の JSON の metric filter（``$.event = …``）が connect_callback_* を拾えなかった。
"""

from __future__ import annotations

import os

import uvicorn

from teamagent.connect_web.app import build_uvicorn_log_config, create_app
from teamagent.hmac_durable_state import require_runtime_startup
from teamagent.hmac_keyring import REPORT_LINK_MAX_TOKEN_TTL_S
from teamagent.observability.logging_config import configure_logging

app = create_app()


def main() -> None:
    # 構造化ログの出力形式を最初に確定（STRUCTLOG_FORMAT=json で CloudWatch 向け JSON）。
    configure_logging()
    require_runtime_startup((("report_link", REPORT_LINK_MAX_TOKEN_TTL_S),))
    host = os.environ.get("CONNECT_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("CONNECT_WEB_PORT", "8788"))
    # アクセスログの /r/<token> を伏せて配布（トークンの CloudWatch 流出を防ぐ）。
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        log_config=build_uvicorn_log_config(),
        server_header=False,
    )


if __name__ == "__main__":
    main()
