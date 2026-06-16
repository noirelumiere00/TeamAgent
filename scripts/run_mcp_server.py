#!/usr/bin/env python
"""TeamAgent MCP サーバ（stdio）を起動する。

自律外殻（OpenClaw 等）から TeamAgent のドメイン能力を MCP tool として叩くための境界を起動する。
本番ツール（search/clientkarte/proposal_* ＋ env opt-in の mail/workspace）を公開する。

Usage:
    # 本番ツールを公開（要: 各スキルの env, AWS 資格情報, DATABASE_URL 等）
    python scripts/run_mcp_server.py

    # OpenClaw 等から stdio で接続する場合は、この command を MCP サーバ定義に登録する。
"""

from __future__ import annotations

from teamagent.mcp_gateway.server import main

if __name__ == "__main__":
    main()
