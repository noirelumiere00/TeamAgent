"""2 つの雛形の差を、system・messages・ツール・推論設定に分けて出す。

用途:
- Slack 経路とローカル経路の差が「発話の包み方と Slack 由来のメタデータだけ」かを確かめる
- 比べたい 2 コミットの雛形で、差が「ツール定義だけ」かを確かめる（system と messages が同一）

    uv run python scripts/routing_eval/diff_templates.py runs/a/template.json runs/b/template.json
"""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path
from typing import Any


def _system_text(req: dict[str, Any]) -> str:
    return "".join(b.get("text", "") for b in req["system"])


def _tools(req: dict[str, Any]) -> list[dict[str, Any]]:
    return [t["toolSpec"] for t in req["toolConfig"]["tools"]]


def diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    ra, rb = a["request"], b["request"]
    sa, sb = _system_text(ra), _system_text(rb)
    sys_diff = [
        line
        for line in difflib.unified_diff(
            sa.splitlines(), sb.splitlines(), "a.system", "b.system", n=0, lineterm=""
        )
        if not line.startswith(("---", "+++"))
    ]
    ta, tb = _tools(ra), _tools(rb)
    names_a, names_b = [t["name"] for t in ta], [t["name"] for t in tb]
    changed = []
    for x, y in zip(ta, tb, strict=False):
        parts = [k for k in ("description", "inputSchema") if x.get(k) != y.get(k)]
        if x["name"] == y["name"] and parts:
            changed.append({"name": x["name"], "fields": parts})
    return {
        "system": {
            "identical": sa == sb,
            "chars": [len(sa), len(sb)],
            "cache_points_equal": [k for k in ra["system"] if "cachePoint" in k]
            == [k for k in rb["system"] if "cachePoint" in k],
            "diff_lines": sys_diff,
        },
        "messages": {
            "identical": ra["messages"] == rb["messages"],
            "a": ra["messages"],
            "b": rb["messages"],
        },
        "tools": {
            "same_names_in_order": names_a == names_b,
            "only_in_a": sorted(set(names_a) - set(names_b)),
            "only_in_b": sorted(set(names_b) - set(names_a)),
            "changed": changed,
            "json_chars": [
                len(json.dumps(ta, ensure_ascii=False)),
                len(json.dumps(tb, ensure_ascii=False)),
            ],
        },
        "toolChoice_equal": ra["toolConfig"].get("toolChoice")
        == rb["toolConfig"].get("toolChoice"),
        "inferenceConfig": [ra.get("inferenceConfig"), rb.get("inferenceConfig")],
        "additionalModelRequestFields": [
            ra.get("additionalModelRequestFields"),
            rb.get("additionalModelRequestFields"),
        ],
        "other_keys": sorted(set(ra) ^ set(rb)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("a", type=Path)
    ap.add_argument("b", type=Path)
    ap.add_argument("--json", action="store_true", help="結果を JSON で出す")
    args = ap.parse_args()
    a = json.loads(args.a.read_text(encoding="utf-8"))
    b = json.loads(args.b.read_text(encoding="utf-8"))
    d = diff(a, b)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return 0
    s, m, t = d["system"], d["messages"], d["tools"]
    print(
        f"system: identical={s['identical']} chars={s['chars']} cachePoints_equal={s['cache_points_equal']}"
    )
    for line in s["diff_lines"]:
        print(f"  {line}")
    print(f"messages: identical={m['identical']} count={[len(m['a']), len(m['b'])]}")
    if not m["identical"]:
        for side in ("a", "b"):
            for msg in m[side]:
                print(
                    f"  [{side}] {msg['role']}: {json.dumps(msg['content'], ensure_ascii=False)[:300]}"
                )
    print(
        f"tools: same_names_in_order={t['same_names_in_order']} only_in_a={t['only_in_a']} "
        f"only_in_b={t['only_in_b']} changed={len(t['changed'])} json_chars={t['json_chars']}"
    )
    for c in t["changed"]:
        print(f"  changed {c['name']}: {','.join(c['fields'])}")
    print(f"toolChoice_equal={d['toolChoice_equal']} inferenceConfig={d['inferenceConfig']}")
    print(
        f"additionalModelRequestFields={d['additionalModelRequestFields']} other_keys={d['other_keys']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
