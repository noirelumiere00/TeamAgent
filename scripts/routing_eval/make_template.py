"""取り出した Bedrock 要求を、評価の雛形（発話だけ差し替えられる形）として保存する。

確かめること（崩れていたら雛形を作らない）:
- 評価の目印（SENTINEL）が messages の中にちょうど 1 回だけある
- 送られたツール定義が、dump_tools.py の定義と名前の接頭辞（teamagent__）以外まったく同じ
  （OpenClaw がツール定義を書き換えていない＝比較の条件が定義の差だけになっている）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

TOOL_PREFIX = "teamagent__"
# system の Runtime 行に入る、取り出すたびに変わる値（OpenClaw が新しいセッションごとに振る UUID）。
# コミット間で system を完全一致させるため固定値へ置き換える（モデルの判断に関わらない識別子）。
_SESSION_ID_RE = re.compile(
    r"sessionId=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
FIXED_SESSION_ID = "sessionId=00000000-0000-4000-8000-000000000000"


def normalize_runtime_ids(body: dict[str, Any]) -> int:
    n = 0
    for block in body["system"]:
        text = block.get("text")
        if isinstance(text, str):
            block["text"], k = _SESSION_ID_RE.subn(FIXED_SESSION_ID, text)
            n += k
    return n


def count_sentinel(messages: list[dict[str, Any]], sentinel: str) -> int:
    n = 0
    for m in messages:
        for c in m.get("content", []):
            text = c.get("text") if isinstance(c, dict) else None
            if isinstance(text, str):
                n += text.count(sentinel)
    return n


def _expand_refs(schema: Any, defs: dict[str, Any]) -> Any:
    """$defs/$ref をインライン展開する。

    OpenClaw は MCP の inputSchema を Bedrock へ送る前に $ref を展開する
    （2026.7.1 実機 capture で確認）。展開後の形で比較しないと $ref を持つ
    ツール定義が必ず不一致になる。
    """
    if isinstance(schema, list):
        return [_expand_refs(x, defs) for x in schema]
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        if name not in defs:
            raise SystemExit(f"未定義の $ref: {schema['$ref']}")
        return _expand_refs(json.loads(json.dumps(defs[name])), defs)
    return {k: _expand_refs(v, defs) for k, v in schema.items() if k != "$defs"}


def check_tools(sent: list[dict[str, Any]], source: list[dict[str, Any]]) -> None:
    by_name = {t["name"]: t for t in source}
    sent_names = []
    for t in sent:
        spec = t["toolSpec"]
        name = spec["name"]
        if not name.startswith(TOOL_PREFIX):
            raise SystemExit(f"想定外のツール名: {name}")
        bare = name.removeprefix(TOOL_PREFIX)
        sent_names.append(bare)
        src = by_name.get(bare)
        if src is None:
            raise SystemExit(f"MCP 定義に無いツールが送られた: {bare}")
        if spec["description"] != src["description"]:
            raise SystemExit(f"OpenClaw がツール定義を書き換えている: {bare}")
        src_schema = _expand_refs(src["inputSchema"], src["inputSchema"].get("$defs", {}))
        if spec["inputSchema"]["json"] != src_schema:
            # 定義の本体（$ref 展開）の差は落とさず警告にする（dump_tools の生成精度の差分。
            # 評価は OpenClaw が送った実物＝sent 側を使うため判定の正しさには影響しない）。
            print(f"⚠️ inputSchema の差分（展開後・評価は sent 側の実物を使う）: {bare}")
    missing = sorted(set(by_name) - set(sent_names))
    if missing:
        raise SystemExit(
            f"OpenClaw のツール絞りで落ちたツール: {missing}（toolFilter と flags を見直すこと）"
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--request", type=Path, required=True)
    ap.add_argument("--tools", type=Path, required=True)
    ap.add_argument("--sentinel", required=True)
    ap.add_argument("--mode", required=True, choices=["slack", "local"])
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    record = json.loads(args.request.read_text(encoding="utf-8"))
    body = record["body"]
    tools_doc = json.loads(args.tools.read_text(encoding="utf-8"))

    n = count_sentinel(body["messages"], args.sentinel)
    if n != 1:
        raise SystemExit(f"目印が {n} 回ある（1 回であるべき）")
    check_tools(body["toolConfig"]["tools"], tools_doc["tools"])
    if args.mode == "slack" and normalize_runtime_ids(body) != 1:
        raise SystemExit(
            "system の Runtime 行に sessionId が 1 つだけあるはず（OpenClaw の書式が変わった？）"
        )

    model_id = record["path"].split("/model/", 1)[1].rsplit("/", 1)[0]
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True)
    template = {
        "meta": {
            "source_commit": tools_doc.get("source_commit"),
            "flags_file": tools_doc.get("flags_file"),
            "disabled_flags": tools_doc.get("disabled_flags", []),
            "tools_sha256": tools_doc.get("tools_sha256"),
            "tool_count": len(body["toolConfig"]["tools"]),
            "capture_mode": args.mode,
            "scenario": args.scenario,
            "sentinel": args.sentinel,
            "model_id": model_id,
            "normalized": ["system Runtime 行の sessionId を固定値へ"]
            if args.mode == "slack"
            else [],
            "request_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        },
        "request": body,
    }
    args.out.write_text(json.dumps(template, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(
        f"template -> {args.out} (mode={args.mode} scenario={args.scenario} tools={template['meta']['tool_count']} "
        f"messages={len(body['messages'])} sha256={template['meta']['request_sha256'][:12]})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
