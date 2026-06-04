"""``python -m teamagent.dashboard`` で管理画面を起動する。

MVP はローカル起動（127.0.0.1）。RDS へは SSM ポートフォワード経由（DATABASE_URL）。
env: DASHBOARD_HOST(=127.0.0.1) / DASHBOARD_PORT(=8787)。詳細は docs の runbook 参照。
"""

from __future__ import annotations

import os

import uvicorn

from teamagent.dashboard.app import create_app

app = create_app()


def main() -> None:
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8787"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
