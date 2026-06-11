#!/usr/bin/env python
"""P0 敵対ハーネス: MCP 越しに身元詐称を投げ、会社共有モデルが詐称を無効化することを実証する。

会社共有モデル(§G)では gateway が OC 申告の ``user_email`` / ``user_groups`` / ``user_role`` /
``identity_verified`` を破棄し、固定の「会社メンバー」identity で実行する（``slack_user_id`` は監査のみ）。
よって admin/別email/別groups/不正 slack_user_id をどう詰めても次が成り立つはず:

  1. 全ての攻撃 vector の検索結果が **baseline（無害な会社identity）と同一**＝詐称が結果に一切影響しない。
  2. **会社ドメイン外の doc（outsider needle）が、どの vector でも結果に現れない**（admin 詐称でも漏れない）。

純ロジック（``check_*`` / ``ATTACK_PAYLOADS`` / ``build_user_context``）は外部I/O無しで単体テスト可能
（tests/test_attack_mcp.py）。network 部（``_run``）は実DB＋live MCP（SSMトンネル/承認後）で実行する。
事前に scripts/ingest_test_data.py で会社doc＋会社外doc(outsider needle 入り)を投入しておくこと。

Usage:
    TEAMAGENT_MCP_BEARER=... TEAMAGENT_SHARED_COMPANY_DOMAINS=vectorinc.co.jp \
      python scripts/attack_mcp.py --base-url http://127.0.0.1:8787 \
      --query "P0HARNESS" --outsider-needle "OUTSIDER_ONLY_TOKEN"
"""

from __future__ import annotations

import argparse
import dataclasses
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


def check_results_identical(vector: str, baseline_repr: str, spoofed_repr: str) -> Check:
    """詐称 vector の結果が baseline と同一＝詐称が結果に影響していない。"""
    ok = baseline_repr == spoofed_repr
    detail = (
        "identical to baseline"
        if ok
        else f"DIFFERS (base={len(baseline_repr)}b spoof={len(spoofed_repr)}b)"
    )
    return Check(f"{vector}:identical_to_baseline", ok, detail)


def check_no_outsider(vector: str, result_repr: str, outsider_needle: str) -> Check:
    """会社ドメイン外 doc の固有トークンが結果に現れない（admin 詐称でも漏れない）。"""
    leaked = bool(outsider_needle) and outsider_needle in result_repr
    return Check(
        f"{vector}:no_outsider_leak", not leaked, "leaked!" if leaked else "no outsider doc"
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
    *, base_url: str, bearer: str, path: str, query: str, outsider_needle: str, slack_user_id: str
) -> list[Check]:
    """network 実行部（実DB＋live MCP／単体テスト対象外）。重い依存は遅延 import。"""
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
    baseline = results["baseline"]
    checks: list[Check] = [check_no_outsider("baseline", baseline, outsider_needle)]
    for name in ATTACK_PAYLOADS:
        if name == "baseline":
            continue
        checks.append(check_results_identical(name, baseline, results[name]))
        checks.append(check_no_outsider(name, results[name], outsider_needle))
    return checks


def _run_ssrf(*, base_url: str, bearer: str, path: str, slack_user_id: str) -> list[Check]:
    """network 実行部: video_analysis に SSRF URL を投げ、backend が拒否(isError)することを実証。

    USE_VIDEO_TOOLS が ON の backend に対して実行する（単体テスト対象外）。重い依存は遅延 import。
    """
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _probe() -> list[Check]:
        headers = {"Authorization": f"Bearer {bearer}"}
        checks: list[Check] = []
        async with streamablehttp_client(f"{base_url}{path}", headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                if "video_analysis" not in names:
                    return [
                        Check(
                            "ssrf:video_analysis:exposed",
                            False,
                            "tool未露出（USE_VIDEO_TOOLS=1 の backend で実行せよ）",
                        )
                    ]
                ctx: dict[str, object] = {"slack_user_id": slack_user_id}
                for key in ("imds", "localhost", "private_10", "substr_bypass", "nonallowed"):
                    res = await session.call_tool(
                        "video_analysis", {"url": SSRF_URL_PAYLOADS[key], "_user_context": ctx}
                    )
                    checks.append(
                        Check(
                            f"ssrf:video_analysis:{key}_rejected",
                            bool(res.isError),
                            "isError" if res.isError else "NOT rejected!",
                        )
                    )
        return checks

    return anyio.run(_probe)


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP 越しの身元詐称→無効化 を検証する P0 敵対ハーネス")
    ap.add_argument(
        "--mode",
        choices=["identity", "ssrf"],
        default="identity",
        help="identity=身元詐称無効化 / ssrf=scrape系URLのSSRF拒否",
    )
    ap.add_argument(
        "--base-url", default=os.environ.get("TEAMAGENT_MCP_BASE_URL", "http://127.0.0.1:8787")
    )
    ap.add_argument("--path", default=os.environ.get("TEAMAGENT_MCP_PATH", "/mcp"))
    ap.add_argument("--query", help="[identity] 会社doc・会社外doc 双方が候補に挙がる検索語")
    ap.add_argument("--outsider-needle", help="[identity] 会社外 doc にのみ含まれる固有トークン")
    ap.add_argument(
        "--slack-user-id", default="U0P0HARNESS", help="baseline の監査用 slack_user_id"
    )
    args = ap.parse_args()

    bearer = os.environ.get("TEAMAGENT_MCP_BEARER")
    if not bearer:
        print("TEAMAGENT_MCP_BEARER 未設定（harness は bearer 必須）", file=sys.stderr)
        return 2
    if args.mode == "ssrf":
        checks = _run_ssrf(
            base_url=args.base_url.rstrip("/"),
            bearer=bearer,
            path=args.path,
            slack_user_id=args.slack_user_id,
        )
    else:
        if not args.query or not args.outsider_needle:
            print("identity モードは --query と --outsider-needle が必須", file=sys.stderr)
            return 2
        checks = _run(
            base_url=args.base_url.rstrip("/"),
            bearer=bearer,
            path=args.path,
            query=args.query,
            outsider_needle=args.outsider_needle,
            slack_user_id=args.slack_user_id,
        )
    return 0 if summarize(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
