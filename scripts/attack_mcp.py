#!/usr/bin/env python
"""P0 negative harness: prove bearer-only caller spoofing is rejected by MCP.

The MCP bearer authenticates only the OpenClaw workload.  It cannot authorize a
human caller.  Every direct request below intentionally lacks the one-use claim
minted from a trusted Slack event and therefore must return
``CALLER_IDENTITY_REJECTED`` before resolver, RLS, or a domain skill runs.

Valid-caller and tamper/replay/expiry/audience/guest/stranger paths are exercised
through the real Node signer and Python verifier in
``tests/test_mcp_gateway_caller_claim.py``.  A live positive test must enter
through OpenClaw's Slack ingress; this script never receives the caller HMAC
secret and cannot fabricate positive authorization evidence.

Usage:
    TEAMAGENT_MCP_BEARER=... python scripts/attack_mcp.py \
      --base-url http://127.0.0.1:8787 --query "P0HARNESS"
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from collections.abc import Callable

# gateway が破棄すべき OC 申告フィールドを詰めた攻撃 vector。"baseline" は無害＝比較基準。
ATTACK_PAYLOADS: dict[str, dict[str, object]] = {
    "baseline": {},
    "admin_role": {"user_role": "admin"},
    "evil_email": {"user_email": "attacker@evil.com"},
    "evil_groups": {"user_groups": ["evil.com", "admin"]},
    "verified_flag": {"identity_verified": True, "user_role": "admin"},
    "bad_slack_uid": {"slack_user_id": "'; DROP TABLE documents;--"},
    "kitchen_sink": {
        "user_role": "admin",
        "user_email": "attacker@evil.com",
        "user_groups": ["evil.com"],
        "identity_verified": True,
    },
}


@dataclasses.dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def build_user_context(slack_user_id: str, payload: dict[str, object]) -> dict[str, object]:
    """baseline の slack_user_id に詐称 payload を重ねた ``_user_context`` を作る（payload が優先）。"""
    ctx: dict[str, object] = {"slack_user_id": slack_user_id}
    ctx.update(payload)
    return ctx


def check_caller_rejected(vector: str, result_repr: str) -> Check:
    """Unsigned caller declarations must fail before returning domain data."""
    try:
        payload = json.loads(result_repr)
    except json.JSONDecodeError:
        return Check(f"{vector}:caller_rejected", False, "response is not JSON")
    ok = payload.get("code") == "CALLER_IDENTITY_REJECTED"
    return Check(
        f"{vector}:caller_rejected",
        ok,
        "fail-closed" if ok else f"unexpected response keys={sorted(payload)}",
    )


# §N: scrape系ツール(video_analysis 等)が url_guard で弾くべき SSRF URL。
# MCP に露出する user制御URLは video_analysis.url のみ（tiktok_search/video_algorithm は query）。
SSRF_URL_PAYLOADS: dict[str, str] = {
    "imds": "http://169.254.169.254/latest/meta-data/",  # AWS メタデータ
    "localhost": "http://127.0.0.1:8787/mcp",  # 自分自身/内部サービス
    "private_10": "http://10.0.0.5/",  # 内部ネットワーク
    "substr_bypass": "https://attacker.com/?x=tiktok.com",  # 部分文字列bypass
    "scheme_file": "file:///etc/passwd",  # 非http(s)
    "userinfo": "https://tiktok.com@attacker.com/",  # userinfo偽装 → host=attacker.com
    "nonallowed": "https://evil.example/video/1",  # 非許可ドメイン
}
ALLOWED_URL_SAMPLES: tuple[str, ...] = (
    "https://www.tiktok.com/@u/video/1",
    "https://youtu.be/abc",
)


def check_url_guard_blocks(name: str, url: str, validate: Callable[[str], object]) -> Check:
    """SSRF payload が UrlGuardError 系で弾かれる（純ロジック・I/O無し）。拒否されれば成功。"""
    try:
        validate(url)
    except Exception as e:
        return Check(f"ssrf:{name}:blocked", "UrlGuard" in type(e).__name__, type(e).__name__)
    return Check(f"ssrf:{name}:blocked", False, "NOT blocked!")


def check_url_guard_allows(name: str, url: str, validate: Callable[[str], object]) -> Check:
    """許可ドメインは通る（過剰ブロックでないこと）。"""
    try:
        validate(url)
    except Exception as e:
        return Check(f"ssrf:{name}:allowed", False, f"wrongly blocked: {type(e).__name__}")
    return Check(f"ssrf:{name}:allowed", True, "allowed")


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
    query: str,
    slack_user_id: str,
) -> list[Check]:
    """Send unsigned declarations and require uniform caller rejection."""
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _call_all() -> dict[str, str]:
        headers = {"Authorization": f"Bearer {bearer}"}
        out: dict[str, str] = {}
        async with streamablehttp_client(f"{base_url}{path}", headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for name, payload in ATTACK_PAYLOADS.items():
                    ctx = build_user_context(slack_user_id, payload)
                    res = await session.call_tool("search", {"query": query, "_user_context": ctx})
                    # 結果を安定した文字列へ（content の text を連結）。
                    out[name] = "\n".join(
                        getattr(b, "text", "")
                        for b in (res.content or [])
                        if getattr(b, "text", None)
                    )
        return out

    results = anyio.run(_call_all)
    return [check_caller_rejected(name, result) for name, result in results.items()]


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP 越しの身元詐称→無効化 を検証する P0 敵対ハーネス")
    ap.add_argument(
        "--mode",
        choices=["identity", "ssrf"],
        default="identity",
        help="identity=bearer-only身元詐称拒否。ssrf live modeは廃止済み。",
    )
    ap.add_argument(
        "--base-url", default=os.environ.get("TEAMAGENT_MCP_BASE_URL", "http://127.0.0.1:8787")
    )
    ap.add_argument("--path", default=os.environ.get("TEAMAGENT_MCP_PATH", "/mcp"))
    ap.add_argument("--query", help="[identity] 会社doc・会社外doc 双方が候補に挙がる検索語")
    ap.add_argument("--outsider-needle", help=argparse.SUPPRESS)
    ap.add_argument(
        "--slack-user-id", default="U0P0HARNESS", help="baseline の監査用 slack_user_id"
    )
    args = ap.parse_args()

    bearer = os.environ.get("TEAMAGENT_MCP_BEARER")
    if not bearer:
        print("TEAMAGENT_MCP_BEARER 未設定（harness は bearer 必須）", file=sys.stderr)
        return 2
    if args.mode == "ssrf":
        print(
            "direct SSRF live mode is retired: unsigned direct MCP calls stop at caller "
            "authorization; use tests/test_attack_mcp.py or signed Slack ingress",
            file=sys.stderr,
        )
        return 2
    else:
        if not args.query:
            print("identity モードは --query が必須", file=sys.stderr)
            return 2
        checks = _run(
            base_url=args.base_url.rstrip("/"),
            bearer=bearer,
            path=args.path,
            query=args.query,
            slack_user_id=args.slack_user_id,
        )
    return 0 if summarize(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
