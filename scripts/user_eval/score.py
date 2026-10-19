"""記録済みの会話を 70 点の契約検査と 30 点の差し込み判定で採点する。"""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

from scripts.user_eval.core import JUDGE_PROMPT, Judge, check, load_scenarios
from scripts.user_eval.recorded import (
    CRITICAL_VIOLATIONS,
    answers,
    load_recorded,
    timestamp,
    validate_record,
)

SCORE_JUDGE_PROMPT = JUDGE_PROMPT + """
重大違反は verdict に加えて critical_violations 配列で報告する（違反なしは []）。
fabricated_numbers: 作り話の数字 / other_company_numbers: 他社の数字 /
completed_as_ongoing: 終了済み施策を進行中と回答 /
own_answer_as_source: 自分の過去の答えを根拠にした。
一次資料と照合し、reason に根拠を書く。単なる不足と重大違反を区別する。
"""

VERDICT_FACTORS = {"good": 1.0, "partial": 0.5, "bad": 0.0}
CODE_PATTERNS = {
    "must_attach_file": r"upload_file|files_upload|pdf_export|publish_artifact",
    "must_contain_link": r"source_url|source_links|citations",
    "must_not_ask_back": r"聞き返|キーワード.*お任せ|intake",
    "must_not_repeat_numbers_after_metrics_post": r"再掲|重複|metrics_post",
    "must_not_cite_own_answers": r"is_aico_dm|自己引用|own.answer",
    "status_freshness": r"進行中|実施済み|freshness",
    "max_latency_s": r"search_latency_breakdown|search_fanout_lanes",
    "llm_judge": r"faithfulness|根拠|捏造",
    "critical_violation": r"自己引用|捏造|進行中|faithfulness",
}


def load_score_scenarios(path: Path) -> list[dict[str, Any]]:
    # 記録側に実際の送信文があるので、採点では実名の割り当ては不要。
    rows = load_scenarios(path, resolve=False)
    for row in rows:
        specs = row["checks"][:-1]
        if not specs:
            raise ValueError("決定的チェックが一つ以上必要")
        for spec in specs:
            weight = spec.get("weight", 1)
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight <= 0:
                raise ValueError("weight は正の有限数が必要")
        if not math.isfinite(sum(c.get("weight", 1) for c in specs)):
            raise ValueError("weight の合計は有限数が必要")
        for pattern in row.get("critical_patterns", []):
            re.compile(pattern)
    return rows


def score_scenario(scenario: dict[str, Any], record: dict[str, Any], judge: Judge | None = None) -> dict[str, Any]:
    validate_record(record)
    if scenario["id"] != record["scenario_id"]:
        raise ValueError("シナリオと記録の ID が不一致")
    messages = answers(record)
    sent = sorted(record["sent"], key=lambda m: timestamp(m["ts"]))
    turns = []
    latencies: list[float | None] = []
    for index, message in enumerate(sent):
        start = timestamp(message["ts"])
        end = timestamp(sent[index + 1]["ts"]) if index + 1 < len(sent) else math.inf
        replies = [m for m in messages if start <= m.timestamp < end and (m.text.strip() or m.attachments)]
        turns.append(replies)
        latencies.append(replies[0].timestamp - start if replies else None)
    complete = len(sent) == len(scenario["utterances"]) and bool(sent) and all(turns)
    latency = max((v for v in latencies if v is not None), default=None) if latencies and all(v is not None for v in latencies) else None
    specs = scenario["checks"][:-1]
    total_weight = sum(c.get("weight", 1) for c in specs)
    outcomes = []
    for spec in specs:
        outcome = check(spec, messages, latency)
        if spec["type"] == "must_not_ask_back":
            for index, replies in enumerate(turns):
                if not check(spec, replies, latency)["passed"]:
                    outcome.update(passed=False, reason=f"発話 {index + 1}: 聞き返しまたは回答なし")
        maximum = 70 * spec.get("weight", 1) / total_weight
        outcome.update(max_points=maximum, points=maximum if outcome["passed"] else 0.0)
        outcomes.append(outcome)
    filtered = {**record, "received": [m for m in record["received"] if m["author"] == "aico" and not m["is_feedback_buttons"] and sent and timestamp(m["ts"]) >= timestamp(sent[0]["ts"])]}
    decision = record.get("judge")
    if decision is None and judge is not None:
        try:
            decision = judge.judge(SCORE_JUDGE_PROMPT, {"scenario": scenario, "recorded": filtered, "checks": outcomes}, timeout_s=60)
            validate_record({**record, "judge": decision})
        except Exception as exc:
            decision = {"verdict": "bad", "reason": f"判定失敗: {exc}"}
    llm_points = 30 * VERDICT_FACTORS[decision["verdict"]] if decision else 0.0
    outcomes.append({"type": "llm_judge", "passed": bool(decision and decision["verdict"] == "good"),
                     "verdict": decision["verdict"] if decision else "unjudged",
                     "reason": decision["reason"] if decision else "Claude の判定未記録（未評価）",
                     "max_points": 30.0, "points": llm_points})
    critical = [CRITICAL_VIOLATIONS[v] for v in record.get("critical_violations", [])]
    critical.extend(CRITICAL_VIOLATIONS[v] for v in (decision or {}).get("critical_violations", []))
    text = "\n".join(m.text for m in messages)
    if any(c.is_aico_dm for m in messages for c in m.citations):
        critical.append(CRITICAL_VIOLATIONS["own_answer_as_source"])
    if any(re.search(pattern, text) for pattern in scenario.get("critical_patterns", [])):
        critical.append("シナリオで定義した重大違反の表現を検出")
    critical = list(dict.fromkeys(critical))
    deductions = [f"{c['type']}: -{c['max_points'] - c['points']:.1f}点 {c['reason']}"
                  for c in outcomes if c["points"] < c["max_points"]]
    if not complete:
        deductions.append("記録不完備: 送信数の不一致または未回答の発話あり（0点）")
    deductions.extend(f"重大違反: {reason}（0点）" for reason in critical)
    points = round(sum(c["points"] for c in outcomes), 1) if complete and not critical else 0.0
    return {"id": scenario["id"], "score": points, "passed": points == 100,
            "checks": outcomes, "deductions": deductions, "critical_violations": critical,
            "latencies_s": latencies, "recorded": record,
            "request_ids": record["request_ids"], "logs": record.get("logs", [])}


def overall(rows: list[dict[str, Any]]) -> float:
    return round(sum(r["score"] for r in rows) / len(rows), 1) if rows else 0.0


def load_previous(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or row["id"] in ids:
            raise ValueError("前回の結果の ID が不正または重複")
        value = row.get("score")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("前回の結果には 0〜100 の score が必要")
        ids.add(row["id"])
    if not rows:
        raise ValueError("前回の結果が空")
    return rows


def infer_files(row: dict[str, Any], root: Path) -> list[str]:
    """grep (rg) のコード中の語の一致だけから候補を出す。原因の断定はしない。"""
    names = [c["type"] for c in row["checks"] if c["points"] < c["max_points"]]
    if row.get("critical_violations"):
        names.append("critical_violation")
    patterns = [CODE_PATTERNS[n] for n in names if n in CODE_PATTERNS]
    if not patterns:
        return []
    try:
        result = subprocess.run(["rg", "-l", "--glob", "*.py", "--glob", "*.md", "|".join(patterns), "src/teamagent"],
                                cwd=root, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return sorted(result.stdout.splitlines())[:6] if result.returncode == 0 else []


def report(rows: list[dict[str, Any]], previous: list[dict[str, Any]] | None, root: Path) -> str:
    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    score = overall(rows)
    lines = ["# 記録した会話の採点", "", f"全体点: **{score:.1f} / 100**（シナリオ点の平均）",
             f"目標95点: {'到達' if score >= 95 else '未到達'}", "",
             "決定的チェック70%＋LLM判定30%。重大違反・記録不完備はシナリオ0点。", ""]
    old = {r["id"]: r for r in previous or []}
    if previous and set(old) == {r["id"] for r in rows}:
        lines.append(f"前回全体点: {overall(previous):.1f} / 差: {score - overall(previous):+.1f}点")
    elif previous:
        lines.append("前回との差: シナリオ集合が異なるため全体差は比較不可。共通IDのみ個別比較。")
    else:
        lines.append("前回との差: 未指定")
    lines.extend(["", "| シナリオ | 点 | 前回との差 | 減点理由 |", "| --- | ---: | ---: | --- |"])
    for row in rows:
        delta = f"{row['score'] - old[row['id']]['score']:+.1f}" if row["id"] in old else "—"
        row["previous_score"] = old[row["id"]]["score"] if row["id"] in old else None
        row["delta"] = round(row["score"] - old[row["id"]]["score"], 1) if row["id"] in old else None
        lines.append(f"| {cell(row['id'])} | {row['score']:.1f} | {delta} | {cell('; '.join(row['deductions']) or 'なし')} |")
    removed = set(old) - {r["id"] for r in rows}
    if removed:
        lines.extend(["", "前回のみのシナリオ: " + cell(", ".join(sorted(removed)))])
    lines.extend(["", "## 直すべきこと", "", "コード候補は grep（rg）の語の一致による推測です。原因の確定には本文・ログとコードを確認してください。", ""])
    for row in rows:
        if row["score"] >= 100:
            continue
        files = infer_files(row, root)
        candidates = ", ".join(f"`{f}`" for f in files) or "該当ファイルなし（記録・判定の不足も確認）"
        lines.append(f"- {cell(row['id'])} ({row['score']:.1f}点): {cell('; '.join(row['deductions']))}。関係しそうな場所（推測）: {candidates}")
    if all(r["score"] == 100 for r in rows):
        lines.append("減点なし。")
    return "\n".join(lines) + "\n"


def score_recorded(recorded: Path, scenarios: Path, output: Path, previous: Path | None = None,
                   judge: Judge | None = None) -> list[dict[str, Any]]:
    definitions = load_score_scenarios(scenarios)
    records = {r["scenario_id"]: r for r in load_recorded(recorded)}
    if set(records) - {s["id"] for s in definitions}:
        raise ValueError("シナリオ定義にない scenario_id が記録にある")
    old = load_previous(previous) if previous else None
    rows = []
    for scenario in definitions:
        record = records.get(scenario["id"], {"scenario_id": scenario["id"], "sent": [], "received": [], "request_ids": []})
        rows.append(score_scenario(scenario, record, judge))
    content = report(rows, old, Path(__file__).resolve().parents[2])
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (output / "report.md").write_text(content, encoding="utf-8")
    return rows
