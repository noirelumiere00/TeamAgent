"""84 件のコーパスを、取り出した OpenClaw の要求（雛形）に差し込んで Bedrock の Haiku 4.5 で実行する。

⚠️ 課金あり（Bedrock）。手動実行専用で CI では回さない（採点は scoring.py の pytest で固定）。
ツールは実行しない（モデルが「どのツールを・どの引数で呼ぶか」を決めた最初のターンだけを見る）。

    AWS_PROFILE=... uv run python scripts/routing_eval/run_routing_eval.py \\
        --variant before=runs/cap_b66e8d6e_new/template.json \\
        --variant after=runs/cap_a961776f_new/template.json \\
        --runs 3 --out runs/results_new_dm.jsonl

- 同じ group（新規 DM / 履歴付き DM）の雛形どうしだけを比べる（group が違うと止まる）。
- ケースごとに variant を交互に並べ、時間帯のずれが片方だけに偏らないようにする。
- 出力は 1 回 1 行の JSONL。途中で止まっても、同じ --out で再実行すれば未実行の分だけ回す。
- 本番の Aico と同じアカウントの枠を使うので、毎分トークン数（--max-tpm）で絞る。
- プロンプトキャッシュが効かない（毎回 10 万トークン級を定価で払う）と分かったら止める。
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import random
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

_spec = importlib.util.spec_from_file_location("routing_eval_scoring", HERE / "scoring.py")
assert _spec and _spec.loader
scoring = importlib.util.module_from_spec(_spec)
sys.modules["routing_eval_scoring"] = scoring
_spec.loader.exec_module(scoring)

# Haiku 4.5（Bedrock・jp 推論プロファイル）の単価（USD / 100 万トークン）。費用の上限の見積りにだけ使う。
# 実際の請求より高めに置く（上限で早めに止まる側へ倒す）。
PRICE_PER_M = {"input": 1.1, "cache_write": 1.375, "cache_read": 0.11, "output": 5.5}
RETRYABLE = (
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelNotReadyException",
    "InternalServerException",
)


def fill(template: dict[str, Any], utterance: str) -> dict[str, Any]:
    req = copy.deepcopy(template["request"])
    sentinel = template["meta"]["sentinel"]
    hits = 0
    for m in req["messages"]:
        for c in m["content"]:
            if isinstance(c, dict) and isinstance(c.get("text"), str) and sentinel in c["text"]:
                c["text"] = c["text"].replace(sentinel, utterance)
                hits += 1
    if hits != 1:
        raise ValueError(f"目印の差し替えが {hits} 箇所（1 であるべき）")
    return req


def call_bedrock(client: Any, model_id: str, req: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "modelId": model_id,
        "messages": req["messages"],
        "system": req["system"],
    }
    for key in ("inferenceConfig", "toolConfig", "additionalModelRequestFields"):
        if req.get(key):
            params[key] = req[key]
    started = time.monotonic()
    resp = client.converse_stream(**params)
    content: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    tool_json = ""
    stop_reason = None
    usage: dict[str, int] = {}
    metrics: dict[str, int] = {}
    for event in resp["stream"]:
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                current = {
                    "toolUse": {
                        "name": start["toolUse"]["name"],
                        "toolUseId": start["toolUse"]["toolUseId"],
                    }
                }
                tool_json = ""
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"]["delta"]
            if "text" in delta:
                if current is None or "text" not in current:
                    current = {"text": ""}
                current["text"] += delta["text"]
            elif "toolUse" in delta:
                tool_json += delta["toolUse"].get("input", "")
        elif "contentBlockStop" in event:
            if current is not None:
                if "toolUse" in current:
                    try:
                        current["toolUse"]["input"] = json.loads(tool_json) if tool_json else {}
                    except json.JSONDecodeError:
                        current["toolUse"]["input"] = {"__unparsed__": tool_json}
                content.append(current)
            current = None
        elif "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason")
        elif "metadata" in event:
            usage = event["metadata"].get("usage", {})
            metrics = event["metadata"].get("metrics", {})
    if current is not None:
        content.append(current)
    return {
        "content": content,
        "stop_reason": stop_reason,
        "usage": usage,
        "latency_ms": metrics.get("latencyMs"),
        "wall_ms": round((time.monotonic() - started) * 1000),
    }


class TokenRate:
    """直近 60 秒に使った入力トークン数で待つ（本番 Aico と枠を分け合うため）。"""

    def __init__(self, max_tpm: int) -> None:
        self.max_tpm = max_tpm
        self.window: deque[tuple[float, int]] = deque()
        self.lock = threading.Lock()

    def acquire(self, estimate: int) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.window and now - self.window[0][0] > 60:
                    self.window.popleft()
                used = sum(t for _, t in self.window)
                if used + estimate <= self.max_tpm or not self.window:
                    self.window.append((now, estimate))
                    return
                wait = 60 - (now - self.window[0][0]) + 0.1
            time.sleep(max(wait, 0.5))


def cost_usd(usage: dict[str, int]) -> float:
    return (
        usage.get("inputTokens", 0) * PRICE_PER_M["input"]
        + usage.get("cacheWriteInputTokens", 0) * PRICE_PER_M["cache_write"]
        + usage.get("cacheReadInputTokens", 0) * PRICE_PER_M["cache_read"]
        + usage.get("outputTokens", 0) * PRICE_PER_M["output"]
    ) / 1_000_000


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--variant", action="append", required=True, help="名前=雛形の path（2 つ以上）"
    )
    ap.add_argument(
        "--corpus", type=Path, default=REPO / "tests/routing/catalog_routing_corpus.jsonl"
    )
    ap.add_argument(
        "--catalog-test",
        type=Path,
        default=REPO / "tests/skills/test_routing_descriptions_catalog.py",
    )
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--region", default="ap-northeast-1")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument(
        "--max-tpm",
        type=int,
        default=1_500_000,
        help="毎分の入力トークン上限（既定は枠 500 万の 3 割）",
    )
    ap.add_argument(
        "--max-usd", type=float, default=30.0, help="この実行で使ってよい費用の上限（見積り）"
    )
    ap.add_argument("--only", help="カンマ区切りの case id だけ回す（試運転用）")
    ap.add_argument("--seed", type=int, default=20261001)
    args = ap.parse_args()
    if args.runs < 1:
        raise SystemExit("--runs は 1 以上")

    variants: dict[str, dict[str, Any]] = {}
    for spec in args.variant:
        name, _, path = spec.partition("=")
        variants[name] = json.loads(Path(path).read_text(encoding="utf-8"))
        variants[name]["_path"] = path
    groups = {v["meta"]["scenario"] for v in variants.values()}
    models = {v["meta"]["model_id"] for v in variants.values()}
    if len(groups) != 1 or len(models) != 1:
        raise SystemExit(f"group かモデルが揃っていない: groups={groups} models={models}")
    if any(v["meta"]["capture_mode"] != "slack" for v in variants.values()):
        raise SystemExit("評価には Slack 経路で取り出した雛形を使うこと")
    group = groups.pop()
    model_id = models.pop()

    rows = scoring.load_corpus(args.corpus)
    if args.only:
        wanted = set(args.only.split(","))
        rows = [r for r in rows if r.id in wanted]
    frozen, unwired = scoring.load_fixed_sets(args.catalog_test)
    props = {name: scoring.tool_props_from_template(v) for name, v in variants.items()}

    done: set[tuple[str, str, str, int]] = set()
    if args.out.exists():
        for line in args.out.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if not r.get("error"):
                done.add((r["variant"], r["group"], r["case_id"], r["run"]))

    rng = random.Random(args.seed)
    jobs: list[tuple[str, scoring.CorpusRow, int]] = []
    for run in range(1, args.runs + 1):
        order = rows[:]
        rng.shuffle(order)
        for i, row in enumerate(order):
            names = list(variants)
            if (run + i) % 2:
                names.reverse()
            for name in names:
                if (name, group, row.id, run) not in done:
                    jobs.append((name, row, run))
    print(
        f"group={group} model={model_id} variants={list(variants)} cases={len(rows)} runs={args.runs} todo={len(jobs)}"
    )
    if not jobs:
        return 0

    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    client = boto3.client(
        "bedrock-runtime",
        region_name=args.region,
        config=Config(read_timeout=120, retries={"max_attempts": 1}),
    )
    rate = TokenRate(args.max_tpm)
    write_lock = threading.Lock()
    state = {
        "usd": 0.0,
        "stop": "",
        "calls": 0,
        "cache_read_seen": {name: False for name in variants},
        "calls_by": {name: 0 for name in variants},
    }
    estimate = {name: 130_000 for name in variants}

    def one(name: str, row: scoring.CorpusRow, run: int) -> None:
        if state["stop"]:
            return
        template = variants[name]
        record: dict[str, Any] = {
            "variant": name,
            "group": group,
            "case_id": row.id,
            "run": run,
            "utterance": row.utterance,
            "expect": row.expect,
            "alt_ok": list(row.alt_ok),
            "forbid": list(row.forbid),
            "arg_rules": row.arg_rules,
            "frozen": row.id in frozen,
            "unwired": row.id in unwired,
            "source_commit": template["meta"]["source_commit"],
            "tools_sha256": template["meta"]["tools_sha256"],
            "template_sha256": template["meta"]["request_sha256"],
            "model_id": model_id,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        req = fill(template, row.utterance)
        result = None
        for attempt in range(6):
            rate.acquire(estimate[name])
            try:
                result = call_bedrock(client, model_id, req)
                break
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in RETRYABLE and attempt < 5:
                    time.sleep(min(60, 4 * 2**attempt))
                    continue
                record["error"] = f"{code}: {exc}"
                break
        if result is not None:
            usage = result["usage"]
            total_in = (
                usage.get("inputTokens", 0)
                + usage.get("cacheReadInputTokens", 0)
                + usage.get("cacheWriteInputTokens", 0)
            )
            estimate[name] = max(total_in, 10_000)
            parsed = scoring.parse_response(result["content"])
            record.update(
                {
                    "text": parsed["text"],
                    "tool_calls": parsed["tool_calls"],
                    "stop_reason": result["stop_reason"],
                    "usage": usage,
                    "latency_ms": result["latency_ms"],
                    "wall_ms": result["wall_ms"],
                }
            )
            record.update(scoring.score(row, parsed, props[name]))
        with write_lock:
            with args.out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            if result is not None:
                state["usd"] += cost_usd(result["usage"])
                state["calls"] += 1
                state["calls_by"][name] += 1
                if result["usage"].get("cacheReadInputTokens", 0) > 0:
                    state["cache_read_seen"][name] = True
                if state["calls_by"][name] >= 6 and not state["cache_read_seen"][name]:
                    state["stop"] = (
                        f"{name}: 6 回呼んでもプロンプトキャッシュが 1 度も効いていない（定価で回り続けるので止める）"
                    )
                if state["usd"] > args.max_usd:
                    state["stop"] = (
                        f"費用の見積りが上限 ${args.max_usd} を超えた（${state['usd']:.2f}）"
                    )
            if state["calls"] % 20 == 0 and result is not None:
                print(f"  {state['calls']}/{len(jobs)} calls  ~${state['usd']:.2f}", flush=True)

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(one, *job) for job in jobs]
        for fut in as_completed(futures):
            fut.result()
    print(f"done: calls={state['calls']} ~${state['usd']:.2f} -> {args.out}")
    if state["stop"]:
        print(f"STOPPED: {state['stop']}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
