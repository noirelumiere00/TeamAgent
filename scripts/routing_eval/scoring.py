"""ルーティング評価の採点（決定的・課金なし。CI の pytest で固定する）。

1 回の応答（Bedrock ConverseStream の最初の assistant ターン）を、コーパス 1 行の期待と突き合わせる。

合格の条件（tests/routing/README.md のシミュ手順の 3 段と同じ）:
1. 選んだツールが ``expect`` か ``alt_ok`` に入っている。``expect == "__ask_back__"`` の行は
   「ツールを呼ばずに聞き返す」か ``alt_ok`` のツールを呼べば合格。
2. ``forbid`` のツールを 1 本も呼んでいない（同じターンで複数呼んだ場合も全部見る）。
3. ``arg_rules`` を満たす（その引数を持つツールの呼び出しごとに検査。``_user_context`` は対象外）。

ツールを呼ばなかった応答は、本文から「拒否」「聞き返し」「その他」に分ける（日本語の定型句で判定）。
期待がツールなのに拒否した応答は「誤拒否」として数える。
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ASK_BACK = "__ask_back__"
TOOL_PREFIX = "teamagent__"
ARG_RULE_ABSENT = "must_be_absent"
ARG_RULE_PRESENT = "must_be_present"
ARG_RULE_EQUAL_PREFIX = "must_equal:"

# ツールを呼ばずに断った本文の定型句（SOUL が禁じている言い回しを含む）。
REFUSAL_PATTERNS = re.compile(
    "|".join(
        [
            r"できません",
            r"できかねます",
            r"できない(?:ん)?です",
            r"対応して(?:い|お)りません",
            r"対応していません",
            r"サポートして(?:い|お)りません",
            r"利用できません",
            r"使えません",
            r"アクセス(?:でき|権限が)",
            r"権限がありません",
            r"ブロックされ",
            r"お手伝いできません",
            r"範囲外",
        ]
    )
)
# 聞き返し: 疑問符か、足りない情報を求める言い回し（「教えてください」等は疑問符が付かないことが多い）。
QUESTION_PATTERN = re.compile(
    r"[？?]|教えて|お聞かせ|ご提供|貼って|お知らせください|ご指定|お願いできますか"
)


@dataclass(frozen=True)
class CorpusRow:
    id: str
    utterance: str
    expect: str
    alt_ok: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()
    arg_rules: dict[str, str] = field(default_factory=dict)
    note: str = ""

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> CorpusRow:
        return cls(
            id=d["id"],
            utterance=d["utterance"],
            expect=d["expect"],
            alt_ok=tuple(d.get("alt_ok", [])),
            forbid=tuple(d.get("forbid", [])),
            arg_rules=dict(d.get("arg_rules", {})),
            note=d.get("note", ""),
        )


def load_corpus(path: Path) -> list[CorpusRow]:
    rows = [
        CorpusRow.from_json(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [r.id for r in rows]
    dup = [k for k, v in Counter(ids).items() if v > 1]
    if dup:
        raise ValueError(f"コーパスの id が重複: {dup}")
    return rows


def load_fixed_sets(catalog_test: Path) -> tuple[frozenset[str], dict[str, str]]:
    """凍結済みの本番障害行（R4_REQUIRED_IDS）と、本番で呼べないツールを期待する既知の行を、
    tests/skills/test_routing_descriptions_catalog.py から読む（正本を 1 か所に保つため import せず ast で読む）。"""
    tree = ast.parse(catalog_test.read_text(encoding="utf-8"))
    frozen: frozenset[str] | None = None
    unwired: dict[str, str] | None = None
    for node in tree.body:
        target = None
        value = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target == "R4_REQUIRED_IDS" and isinstance(value, ast.Call):
            frozen = frozenset(ast.literal_eval(value.args[0]))
        elif target == "CORPUS_ROWS_EXPECTING_UNWIRED_TOOLS" and value is not None:
            unwired = dict(ast.literal_eval(value))
    if frozen is None or unwired is None:
        raise ValueError("R4_REQUIRED_IDS / CORPUS_ROWS_EXPECTING_UNWIRED_TOOLS が読めない")
    return frozen, unwired


def parse_response(content: list[dict[str, Any]]) -> dict[str, Any]:
    """assistant ターンの content から、テキストとツール呼び出しを取り出す。"""
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in content:
        if "text" in block:
            texts.append(block["text"])
        elif "toolUse" in block:
            tu = block["toolUse"]
            name = tu.get("name", "")
            calls.append({"name": name.removeprefix(TOOL_PREFIX), "input": tu.get("input") or {}})
    return {"text": "".join(texts), "tool_calls": calls}


def classify_text(text: str) -> str:
    if REFUSAL_PATTERNS.search(text):
        return "refusal"
    if QUESTION_PATTERN.search(text):
        return "ask_back"
    return "text"


def _empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def check_arg_rules(
    rules: dict[str, str], calls: list[dict[str, Any]], tool_props: dict[str, set[str]]
) -> list[str]:
    """引数の契約違反を返す。その引数を入力スキーマに持つツールの呼び出しだけを検査する。"""
    violations: list[str] = []
    for call in calls:
        props = tool_props.get(call["name"], set())
        args = call["input"] if isinstance(call["input"], dict) else {}
        for name, rule in rules.items():
            if name not in props:
                continue
            value = args.get(name)
            if rule == ARG_RULE_ABSENT and not _empty(value):
                violations.append(f"{call['name']}.{name}={value!r}（渡してはいけない）")
            elif rule == ARG_RULE_PRESENT and _empty(value):
                violations.append(f"{call['name']}.{name} が無い（必須）")
            elif rule.startswith(ARG_RULE_EQUAL_PREFIX):
                want = rule[len(ARG_RULE_EQUAL_PREFIX) :]
                if value != want:
                    violations.append(f"{call['name']}.{name}={value!r}（{want!r} であるべき）")
    return violations


def leaked_names(text: str, tool_names: set[str]) -> list[str]:
    """本文に出てしまった内部名（ツール名・teamagent__）。SOUL の禁止語。合否には入れない参考指標。"""
    found = sorted(
        n
        for n in tool_names
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", text)
    )
    if TOOL_PREFIX in text:
        found.append(TOOL_PREFIX)
    return found


def score(
    row: CorpusRow, response: dict[str, Any], tool_props: dict[str, set[str]]
) -> dict[str, Any]:
    """1 回の応答を採点する。response は parse_response の戻り値。"""
    calls = response["tool_calls"]
    names = [c["name"] for c in calls]
    selected = names[0] if names else None
    kind = "tool" if calls else classify_text(response["text"])
    reasons: list[str] = []

    ok_tools = set(row.alt_ok) | ({row.expect} if row.expect != ASK_BACK else set())
    if selected is None:
        if row.expect == ASK_BACK:
            if kind != "ask_back":
                reasons.append(f"聞き返すべきところ {kind}")
        else:
            reasons.append(f"ツールを呼ばなかった（{kind}）")
    elif selected not in ok_tools:
        reasons.append(
            f"選択違い: {selected}（期待 {row.expect}{'/' + '/'.join(row.alt_ok) if row.alt_ok else ''}）"
        )

    forbidden = [n for n in names if n in row.forbid]
    if forbidden:
        reasons.append(f"禁止ツール: {forbidden}")
    arg_violations = check_arg_rules(row.arg_rules, calls, tool_props)
    reasons.extend(arg_violations)

    return {
        "selected_tool": selected,
        "called_tools": names,
        "arg_names": {
            c["name"]: sorted(k for k in (c["input"] or {}) if k != "_user_context") for c in calls
        },
        "no_tool": selected is None,
        "response_kind": kind,
        "false_refusal": selected is None and kind == "refusal" and row.expect != ASK_BACK,
        "leaked_internal_names": leaked_names(response["text"], set(tool_props)),
        "forbid_violation": bool(forbidden),
        "arg_violation": bool(arg_violations),
        "pass": not reasons,
        "fail_reasons": reasons,
    }


def rescore(record: dict[str, Any], tool_props: dict[str, set[str]]) -> dict[str, Any]:
    """保存済みの 1 回分（本文とツール呼び出し）を、今の採点規則で採点し直す（課金なし）。"""
    if record.get("error"):
        return record
    row = CorpusRow(
        id=record["case_id"],
        utterance=record["utterance"],
        expect=record["expect"],
        alt_ok=tuple(record.get("alt_ok", [])),
        forbid=tuple(record.get("forbid", [])),
        arg_rules=dict(record.get("arg_rules", {})),
    )
    response = {"text": record.get("text", ""), "tool_calls": record.get("tool_calls", [])}
    return {**record, **score(row, response, tool_props)}


def tool_props_from_template(template: dict[str, Any]) -> dict[str, set[str]]:
    props: dict[str, set[str]] = {}
    for t in template["request"]["toolConfig"]["tools"]:
        spec = t["toolSpec"]
        schema = spec["inputSchema"]["json"]
        props[spec["name"].removeprefix(TOOL_PREFIX)] = set((schema.get("properties") or {}).keys())
    return props


# ── 集計 ─────────────────────────────────────────────────────────────────────


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """1 つの variant×group の結果を集計する。unwired 行は合否の母数から外して別に数える。"""
    scored = [r for r in records if not r.get("error")]
    gated = [r for r in scored if not r["unwired"]]
    frozen = [r for r in gated if r["frozen"]]
    tool_expected = [r for r in gated if r["expect"] != ASK_BACK]
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in gated:
        by_case[r["case_id"]].append(r)
    usage = [r["usage"] for r in scored if r.get("usage")]
    lat = sorted(r["latency_ms"] for r in scored if r.get("latency_ms") is not None)

    def pct(p: float) -> int | None:
        return lat[min(len(lat) - 1, int(p * len(lat)))] if lat else None

    return {
        "runs": len(records),
        "errors": len(records) - len(scored),
        "gated_runs": len(gated),
        "pass_rate": _rate(sum(r["pass"] for r in gated), len(gated)),
        "selection_accuracy": _rate(
            sum(
                1 for r in tool_expected if r["selected_tool"] in ({r["expect"]} | set(r["alt_ok"]))
            ),
            len(tool_expected),
        ),
        "no_tool_rate": _rate(sum(r["no_tool"] for r in gated), len(gated)),
        "ask_back_rate": _rate(sum(r["response_kind"] == "ask_back" for r in gated), len(gated)),
        "false_refusal_rate": _rate(
            sum(r["false_refusal"] for r in tool_expected), len(tool_expected)
        ),
        "internal_name_leak_rate": _rate(
            sum(bool(r["leaked_internal_names"]) for r in gated), len(gated)
        ),
        "forbid_violations": sum(r["forbid_violation"] for r in gated),
        "arg_violations": sum(r["arg_violation"] for r in gated),
        "frozen_runs": len(frozen),
        "frozen_pass_rate": _rate(sum(r["pass"] for r in frozen), len(frozen)),
        "cases_all_runs_pass": sum(all(x["pass"] for x in v) for v in by_case.values()),
        "cases_unstable": sum(len({x["pass"] for x in v}) > 1 for v in by_case.values()),
        "cases": len(by_case),
        "unwired_runs": len(scored) - len(gated),
        "input_tokens_mean": _mean(
            u.get("inputTokens", 0)
            + u.get("cacheReadInputTokens", 0)
            + u.get("cacheWriteInputTokens", 0)
            for u in usage
        ),
        "cache_read_tokens_mean": _mean(u.get("cacheReadInputTokens", 0) for u in usage),
        "output_tokens_mean": _mean(u.get("outputTokens", 0) for u in usage),
        "latency_ms_p50": pct(0.5),
        "latency_ms_p90": pct(0.9),
    }


def _mean(values: Any) -> float | None:
    xs = list(values)
    return round(sum(xs) / len(xs), 1) if xs else None


def gates(summary: dict[str, Any]) -> list[str]:
    """必須条件の違反を返す（空なら合格）。"""
    failed: list[str] = []
    if summary["errors"]:
        failed.append(f"API エラーで採点できなかった回が {summary['errors']} 回ある")
    if summary["frozen_pass_rate"] != 1.0:
        failed.append(
            f"凍結済みの本番障害ケースの合格率が {summary['frozen_pass_rate']}（100% 必須）"
        )
    if summary["forbid_violations"]:
        failed.append(f"禁止ツールの呼び出しが {summary['forbid_violations']} 回（0 必須）")
    if summary["arg_violations"]:
        failed.append(f"引数の契約違反が {summary['arg_violations']} 回（0 必須）")
    return failed


def compare_cases(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ケースごとの合格回数を並べ、悪化・改善したケースを返す。"""

    def by_case(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in records:
            if not r.get("error") and not r["unwired"]:
                out[r["case_id"]].append(r)
        return out

    ca, cb = by_case(a), by_case(b)
    rows = []
    for cid in sorted(set(ca) | set(cb)):
        ra, rb = ca.get(cid, []), cb.get(cid, [])
        pa, pb = sum(r["pass"] for r in ra), sum(r["pass"] for r in rb)
        if pa / max(len(ra), 1) == pb / max(len(rb), 1):
            continue
        rows.append(
            {
                "case_id": cid,
                "a_pass": f"{pa}/{len(ra)}",
                "b_pass": f"{pb}/{len(rb)}",
                "direction": "worse" if pb / max(len(rb), 1) < pa / max(len(ra), 1) else "better",
                "a_selected": [r["selected_tool"] for r in ra],
                "b_selected": [r["selected_tool"] for r in rb],
                "b_fail_reasons": sorted({x for r in rb for x in r["fail_reasons"]}),
            }
        )
    return rows
