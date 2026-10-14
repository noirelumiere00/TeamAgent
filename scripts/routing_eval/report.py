"""run_routing_eval.py の JSONL を集計し、variant ごとの指標・必須条件・ケース単位の差を出す。

    uv run python scripts/routing_eval/report.py runs/results_new_dm.jsonl runs/results_prior_search.jsonl \\
        --a before --b after --md runs/report.md --json runs/report.json

終了コード: 必須条件（凍結済みの本番障害ケース 100% 合格・禁止ツール 0・引数違反 0・API エラー 0）を
b 側が 1 つでも満たさなければ 1。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("routing_eval_scoring", HERE / "scoring.py")
assert _spec and _spec.loader
scoring = importlib.util.module_from_spec(_spec)
sys.modules["routing_eval_scoring"] = scoring
_spec.loader.exec_module(scoring)

METRICS = [
    ("pass_rate", "合格率", "pct"),
    ("selection_accuracy", "ツール選択の正答率（ツールが期待の行）", "pct"),
    ("no_tool_rate", "ツールを呼ばなかった率", "pct"),
    ("ask_back_rate", "聞き返し率", "pct"),
    ("false_refusal_rate", "誤拒否率（ツールが期待なのに断った）", "pct"),
    ("frozen_pass_rate", "凍結済みの本番障害ケースの合格率", "pct"),
    ("forbid_violations", "禁止ツールの呼び出し（回）", "int"),
    ("arg_violations", "引数の契約違反（回）", "int"),
    ("internal_name_leak_rate", "本文に内部名が出た率（参考）", "pct"),
    ("cases_all_runs_pass", "全回合格したケース数", "int"),
    ("cases_unstable", "回によって合否が割れたケース数", "int"),
    ("input_tokens_mean", "入力トークン（平均・キャッシュ含む）", "num"),
    ("output_tokens_mean", "出力トークン（平均）", "num"),
    ("latency_ms_p50", "レイテンシ p50（ms）", "num"),
    ("latency_ms_p90", "レイテンシ p90（ms）", "num"),
    ("errors", "API エラー（回）", "int"),
]


def _fmt(v: Any, kind: str) -> str:
    if v is None:
        return "—"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "num":
        return f"{v:,.0f}"
    return str(v)


def load(
    paths: list[Path], props: dict[str, dict[str, set[str]]] | None = None
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """JSONL を group→variant に分けて読む。props（variant→ツールの引数名）があれば今の規則で採点し直す。"""
    out: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for p in paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if props is not None:
                    if r["variant"] not in props:
                        raise SystemExit(f"--template に variant {r['variant']} が無い")
                    r = scoring.rescore(r, props[r["variant"]])
                out[r["group"]][r["variant"]].append(r)
    return out


def build(data: dict[str, dict[str, list[dict[str, Any]]]], a: str, b: str) -> dict[str, Any]:
    report: dict[str, Any] = {"groups": {}}
    for group, variants in sorted(data.items()):
        summaries = {name: scoring.summarize(recs) for name, recs in variants.items()}
        report["groups"][group] = {
            "summaries": summaries,
            "gates": {name: scoring.gates(s) for name, s in summaries.items()},
            "case_diff": scoring.compare_cases(variants.get(a, []), variants.get(b, [])),
            "failing_cases": {
                name: sorted(
                    {
                        r[
                            "case_id"
                        ]: f"{sum(x['pass'] for x in recs if x['case_id'] == r['case_id'])}/"
                        f"{sum(1 for x in recs if x['case_id'] == r['case_id'])} "
                        + "; ".join(
                            sorted(
                                {
                                    y
                                    for x in recs
                                    if x["case_id"] == r["case_id"]
                                    for y in x["fail_reasons"]
                                }
                            )
                        )
                        for r in recs
                        if not r.get("error") and not r["unwired"] and not r["pass"]
                    }.items()
                )
                for name, recs in variants.items()
            },
            "commits": {
                name: sorted({r["source_commit"] for r in recs}) for name, recs in variants.items()
            },
        }
    return report


def to_markdown(report: dict[str, Any], a: str, b: str) -> str:
    lines: list[str] = []
    for group, g in report["groups"].items():
        s = g["summaries"]
        lines.append(f"## group: {group}")
        lines.append("")
        lines.append(f"| 指標 | {a} | {b} |")
        lines.append("|---|---|---|")
        for key, label, kind in METRICS:
            lines.append(
                f"| {label} | {_fmt(s.get(a, {}).get(key), kind)} | {_fmt(s.get(b, {}).get(key), kind)} |"
            )
        lines.append("")
        for name in (a, b):
            failed = g["gates"].get(name, [])
            lines.append(
                f"- 必須条件（{name}）: {'満たす' if not failed else '満たさない — ' + ' / '.join(failed)}"
            )
        lines.append("")
        if g["case_diff"]:
            lines.append(f"### 合否が変わったケース（{a} → {b}）")
            lines.append("")
            lines.append(f"| case | {a} | {b} | 向き | {b} の選択 | {b} の不合格理由 |")
            lines.append("|---|---|---|---|---|---|")
            for c in g["case_diff"]:
                lines.append(
                    f"| {c['case_id']} | {c['a_pass']} | {c['b_pass']} | {c['direction']} | "
                    f"{', '.join(str(x) for x in c['b_selected'])} | {'; '.join(c['b_fail_reasons'])} |"
                )
            lines.append("")
        else:
            lines.append(f"合否が変わったケース（{a} → {b}）: なし")
            lines.append("")
        for name in (a, b):
            fc = g["failing_cases"].get(name, [])
            if fc:
                lines.append(f"### 不合格があったケース（{name}）")
                lines.append("")
                for cid, desc in fc:
                    lines.append(f"- {cid}: {desc}")
                lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("results", type=Path, nargs="+")
    ap.add_argument("--a", required=True, help="比較の基準の variant 名")
    ap.add_argument("--b", required=True, help="比較される variant 名")
    ap.add_argument(
        "--template",
        action="append",
        default=[],
        help="名前=雛形の path。指定すると保存済みの本文から今の採点規則で採点し直す（課金なし）",
    )
    ap.add_argument("--md", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    props = None
    if args.template:
        props = {}
        for spec in args.template:
            name, _, path = spec.partition("=")
            props[name] = scoring.tool_props_from_template(
                json.loads(Path(path).read_text(encoding="utf-8"))
            )
    report = build(load(args.results, props), args.a, args.b)
    md = to_markdown(report, args.a, args.b)
    print(md)
    if args.md:
        args.md.write_text(md + "\n", encoding="utf-8")
    if args.json:
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    return 1 if any(g["gates"].get(args.b) for g in report["groups"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
