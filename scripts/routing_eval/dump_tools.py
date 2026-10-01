"""あるコミットの MCP が tools/list で返すツール定義を、固定した機能フラグで JSON に書き出す。

ルーティング評価で「ツール一覧を同じにして、定義の書き方だけを比べる」ための入口。
DB・Bedrock・Slack には接続しない（定義の組み立てだけ。秘密値は使わない）。

使い方（評価したいコミットを別 worktree に detach で置き、その src を指す）:

    git worktree add --detach /tmp/src_b66e8d6e b66e8d6e
    uv run python scripts/routing_eval/dump_tools.py \\
        --src /tmp/src_b66e8d6e/src \\
        --flags scripts/routing_eval/flags_mcp132.json \\
        --out runs/tools_b66e8d6e.json

``--disable USE_MAIL_LINK_TOOL`` のように指定すると、そのフラグだけ落として書き出す
（#491 の「公開停止」側の影響を、スキーマ圧縮と切り離して測るため）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any


def _apply_flags(flags: dict[str, str], disable: list[str]) -> None:
    # 本番 TD に無いフラグが手元の環境から紛れ込まないよう、USE_* はいったん全部消す。
    for name in [k for k in os.environ if k.startswith("USE_")]:
        del os.environ[name]
    for name, value in flags.items():
        os.environ[name] = value
    for name in disable:
        if name not in flags:
            raise SystemExit(f"--disable {name}: フラグ一覧に無い名前です")
        os.environ[name] = "false"
    # 接続はしない。定義の組み立てに必要な最低限のダミーだけ置く。
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@127.0.0.1:1/dummy")
    os.environ.setdefault("AWS_REGION", "ap-northeast-1")
    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-1")


def _source_commit(src: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(src), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip()


def dump(src: Path, flags: dict[str, str], disable: list[str]) -> list[dict[str, Any]]:
    _apply_flags(flags, disable)
    sys.path.insert(0, str(src))

    # embedder はモデルを読みに行くので、定義の組み立てには不要なダミーに差し替える。
    import teamagent.adapters.embeddings_client as emb

    emb.build_embedder_from_env = lambda: types.SimpleNamespace()  # type: ignore[assignment]

    import teamagent

    loaded_from = Path(teamagent.__file__).resolve()
    if not loaded_from.is_relative_to(src.resolve()):
        raise SystemExit(f"teamagent が {loaded_from} から読まれた（--src の外）。比較が無効になる")

    from teamagent.mcp_gateway.server import list_all_tool_defs
    from teamagent.orchestrator.factory import build_production_tools

    specs = build_production_tools()
    defs = list_all_tool_defs(specs, enable_orchestrator=False)
    return [d.model_dump(exclude_none=True, by_alias=True) for d in defs]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--src", type=Path, required=True, help="評価するコミットの src ディレクトリ")
    ap.add_argument(
        "--flags", type=Path, required=True, help="機能フラグの JSON（flags_mcp132.json 等）"
    )
    ap.add_argument("--disable", action="append", default=[], help="落とすフラグ（複数可）")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    flags = json.loads(args.flags.read_text(encoding="utf-8"))["flags"]
    tools = dump(args.src, flags, args.disable)
    body = json.dumps(tools, ensure_ascii=False, sort_keys=True)
    doc = {
        "source_commit": _source_commit(args.src),
        "flags_file": args.flags.name,
        "disabled_flags": sorted(args.disable),
        "tool_names": [t["name"] for t in tools],
        "tools_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "tools": tools,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(
        f"{len(tools)} tools -> {args.out} (commit={doc['source_commit']}, sha256={doc['tools_sha256'][:12]})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
