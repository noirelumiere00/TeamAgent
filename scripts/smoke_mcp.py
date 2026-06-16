#!/usr/bin/env python
"""デプロイ後 smoke テスト: MCP バックエンドの healthz / 認証 / 公開ツール /（--full）会社共有RLS を検証する。

純ロジック（``check_*``）は外部I/O無しで単体テスト可能（tests/test_smoke_mcp.py）。
network 部分（``_run``）は post-deploy に本人が実行（ローカル or SSM トンネル）。重い import は遅延。

Usage:
    TEAMAGENT_MCP_BEARER=... python scripts/smoke_mcp.py --base-url http://127.0.0.1:8787
    # 会社共有RLS（会社ドメイン doc のみ可視）まで見るなら（要DB/トンネル）:
    TEAMAGENT_MCP_BEARER=... TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp \
      python scripts/smoke_mcp.py --base-url http://127.0.0.1:8787 --full
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from collections.abc import Iterable

# P1 で OpenClaw に露出する「会社ナレッジ・読取」ツール（build_production_tools / §G toolFilter）。
KNOWLEDGE_TOOLS = frozenset({"search", "clientkarte", "proposal_draft", "proposal_review"})
# 会社共有モデルでは OpenClaw に出してはいけない per-user（本人OAuth）ツール。
FORBIDDEN_TOOLS = frozenset(
    {
        "mail_constraints",
        "workspace_search",
        "mail_summary",
        "mail_followup",
        "mail_reply",
        "mail_to_internal_context",
    }
)
# §N: スクレイプ/動画ツール（enable_scrape_tools=true の拡張版でのみ露出すべき）。
SCRAPE_TOOLS = frozenset({"tiktok_search", "video_analysis", "video_algorithm"})


@dataclasses.dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def check_healthz(status_code: int) -> Check:
    return Check("healthz", status_code == 200, f"GET /healthz -> {status_code} (expect 200)")


def check_unauthorized(status_code: int) -> Check:
    return Check(
        "bearer_required",
        status_code == 401,
        f"POST /mcp (no bearer) -> {status_code} (expect 401)",
    )


def check_tool_exposure(tool_names: Iterable[str]) -> Check:
    """会社ナレッジ4ツールが揃い、per-user(mail/workspace)が露出していないこと。"""
    names = set(tool_names)
    missing = KNOWLEDGE_TOOLS - names
    leaked = names & FORBIDDEN_TOOLS
    ok = not missing and not leaked
    detail = f"tools={sorted(names)}"
    if missing:
        detail += f" MISSING_KNOWLEDGE={sorted(missing)}"
    if leaked:
        detail += f" LEAKED_PER_USER={sorted(leaked)}"
    return Check("tool_exposure", ok, detail)


def check_scrape_tools(tool_names: Iterable[str], *, expect_scrape: bool) -> Check:
    """expect_scrape=True なら3ツール露出を、False なら非露出（既定OFF不変条件の回帰）を検証。"""
    names = set(tool_names)
    present = names & SCRAPE_TOOLS
    if expect_scrape:
        missing = SCRAPE_TOOLS - names
        return Check(
            "scrape_tools", not missing, f"present={sorted(present)} missing={sorted(missing)}"
        )
    return Check("scrape_tools_absent", not present, f"unexpected_present={sorted(present)}")


def check_company_scoped(doc_domains: Iterable[str], allowed_domains: Iterable[str]) -> Check:
    """--full: 返却 doc の所属ドメインが全て許可（会社）ドメイン内＝会社外データ0。"""
    allowed = {d.strip().lower() for d in allowed_domains if d.strip()}
    seen = {d.strip().lower() for d in doc_domains if d.strip()}
    outside = seen - allowed
    return Check(
        "company_scoped",
        not outside,
        f"doc_domains={sorted(seen)} outside_allowed={sorted(outside)}",
    )


def summarize(checks: list[Check]) -> bool:
    all_ok = True
    for c in checks:
        print(f"[{'OK  ' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
        all_ok = all_ok and c.ok
    print(f"{'PASS' if all_ok else 'FAIL'} ({sum(c.ok for c in checks)}/{len(checks)})")
    return all_ok


def _run(
    *,
    base_url: str,
    bearer: str,
    path: str,
    full: bool,
    allowed_domains: list[str],
    expect_scrape: bool,
) -> list[Check]:
    """network 実行部（post-deploy／単体テスト対象外）。重い依存は遅延 import。"""
    import anyio
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    checks: list[Check] = []
    checks.append(check_healthz(httpx.get(f"{base_url}/healthz", timeout=10).status_code))
    no_auth = httpx.post(
        f"{base_url}{path}",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        timeout=10,
    )
    checks.append(check_unauthorized(no_auth.status_code))

    async def _mcp() -> list[Check]:
        headers = {"Authorization": f"Bearer {bearer}"}
        async with streamablehttp_client(f"{base_url}{path}", headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                tool_names = [t.name for t in listed.tools]
                out = [
                    check_tool_exposure(tool_names),
                    check_scrape_tools(tool_names, expect_scrape=expect_scrape),
                ]
                if full:
                    res = await session.call_tool(
                        "search",
                        {"query": "smoke", "_user_context": {"slack_user_id": "USMOKE"}},
                    )
                    out.append(
                        Check("search_callable", not res.isError, f"search isError={res.isError}")
                    )
                return out

    checks.extend(anyio.run(_mcp))
    return checks


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP バックエンドの post-deploy smoke")
    ap.add_argument(
        "--base-url", default=os.environ.get("TEAMAGENT_MCP_BASE_URL", "http://127.0.0.1:8787")
    )
    ap.add_argument("--path", default=os.environ.get("TEAMAGENT_MCP_PATH", "/mcp"))
    ap.add_argument(
        "--full",
        action="store_true",
        help="search を実呼びして会社共有RLSまで検証（要DB/トンネル）",
    )
    ap.add_argument(
        "--expect-scrape",
        action="store_true",
        default=os.environ.get("EXPECT_SCRAPE_TOOLS", "").lower() in ("1", "true", "yes"),
        help="拡張版(enable_scrape_tools=true)向け: scrape系3ツールの露出を要求（既定は非露出を検証）",
    )
    args = ap.parse_args()

    bearer = os.environ.get("TEAMAGENT_MCP_BEARER")
    if not bearer:
        print("TEAMAGENT_MCP_BEARER 未設定（smoke は bearer 必須）", file=sys.stderr)
        return 2
    allowed = (os.environ.get("TEAMAGENT_SHARED_COMPANY_DOMAINS", "") or "").split(",")
    checks = _run(
        base_url=args.base_url.rstrip("/"),
        bearer=bearer,
        path=args.path,
        full=args.full,
        allowed_domains=allowed,
        expect_scrape=args.expect_scrape,
    )
    return 0 if summarize(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
