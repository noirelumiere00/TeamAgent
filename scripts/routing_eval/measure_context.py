"""Compare tools/list JSON character counts; no token estimate or external calls.

    python scripts/routing_eval/measure_context.py /tmp/tools_before.json /tmp/tools_after.json

Counts use json.dumps(tools, ensure_ascii=False), including names and JSON punctuation.
Per-tool rows count description text plus inputSchema JSON, matching tooltrim_sizes.txt.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    before = json.loads(args.before.read_text(encoding="utf-8"))["tools"]
    after = json.loads(args.after.read_text(encoding="utf-8"))["tools"]
    sizes = [
        {
            t["name"]: len(t.get("description", ""))
            + len(json.dumps(t["inputSchema"], ensure_ascii=False))
            for t in tools
        }
        for tools in (before, after)
    ]
    assert sizes[0].keys() == sizes[1].keys(), "Tool names changed"
    totals = [len(json.dumps(tools, ensure_ascii=False)) for tools in (before, after)]
    print(f"Total: {totals[0]:,} -> {totals[1]:,} characters ({totals[1] - totals[0]:+,})")
    print("tool\tbefore\tafter")
    for name in sorted(sizes[0], key=sizes[0].__getitem__, reverse=True)[:15]:
        print(f"{name}\t{sizes[0][name]}\t{sizes[1][name]}")
    assert totals[1] <= 37_000, f"Tool definitions exceed 37,000 characters: {totals[1]:,}"


if __name__ == "__main__":
    main()
