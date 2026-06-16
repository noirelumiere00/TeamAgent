"""P1 パイロット ヘルスリードアウト（本番 OpenClaw→MCP 経路）。

本番経路のテレメトリは `usage_events`（旧 slack_bot 専用）に入らない（Part B で確認）。
本番 SLI の一次ソースは CloudWatch Logs `/teamagent/dev/teamagent-mcp` の構造化ログ。

MCP は `STRUCTLOG_FORMAT=json`（observability/logging_config.py）で **JSON ログ**を出す。
よって `event` / `latency_ms` / `cost_usd` / `cache_read_input_tokens` / `level` は
Logs Insights のトップレベルフィールドとして自動抽出され、regex parse なしで集計できる。
（同じ JSON 化で terraform の metric filter `{ $.cost_usd = * }` 等も発火するようになる）

1週間 P1 パイロットの日次ゲート確認（p95≤15s / エラー<1% / コスト許容）の一次ソースに使う。

Usage:
    # 直近 24h（パイロット中の毎日チェック）
    python scripts/pilot_health.py --hours 24

    # 直近 14 日・機械出力
    python scripts/pilot_health.py --days 14 --json

Exit code:
    0: 全 SLO GO
    1: 1 つ以上の SLO 違反（NO-GO）
    2: 設定/接続エラー
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3

# -----------------------------------------------------------
# SLO 閾値（出典: docs/v3.2/slo_v1.md §2/§3・terraform variables.tf）
# -----------------------------------------------------------
SLO_SEARCH_P95_MS = 15000  # 中量(search L2合成) p95 ≤ 15s（variables.tf p95_latency_threshold_ms）
SLO_ERROR_RATE = 0.01  # エラー率 ≤ 1% / 窓
SLO_SEARCH_COST_P50_USD = 0.02  # 1 検索コスト p50 ≤ $0.02

DEFAULT_LOG_GROUP = "/teamagent/dev/teamagent-mcp"
DEFAULT_REGION = "ap-northeast-1"

# 検索 L2 合成（中量レイテンシ/コストの一次イベント）。検索 1 回 ≒ bedrock_converse 1 回。
SEARCH_EVENT = "bedrock_converse"

# Logs Insights クエリ（JSON ログ向け＝STRUCTLOG_FORMAT=json 後）。
# JSON ログでは `event`/`latency_ms`/`cost_usd`/`cache_read_input_tokens`/`level` が
# トップレベルフィールドとして自動抽出されるので、regex parse なしで集計できる。
# 1) イベント別 latency 分布
_Q_LATENCY = (
    "filter ispresent(latency_ms) "
    "| stats count(*) as n, pct(latency_ms,50) as p50, pct(latency_ms,95) as p95, "
    "pct(latency_ms,99) as p99, max(latency_ms) as max_ms by event"
)
# 2) コスト & キャッシュ（bedrock_converse 限定）
_Q_COST_CACHE = (
    'filter event="bedrock_converse" '
    "| stats count(*) as n, sum(cost_usd) as cost_sum, pct(cost_usd,50) as cost_p50, "
    "sum(cache_read_input_tokens) as cache_read_sum, "
    "sum(cache_read_input_tokens > 0) as cache_hit_n"
)
# 3) エラー件数（level=error or *_failed イベント）
_Q_ERRORS = 'filter level="error" or event like /_failed/ | stats count(*) as n'


@dataclass
class Readout:
    """1 回の readout 結果（純データ）。"""

    window_label: str
    latency_by_event: dict[str, dict[str, float]] = field(default_factory=dict)
    search_count: int = 0
    error_count: int = 0
    cost_sum_usd: float = 0.0
    search_cost_p50_usd: float = 0.0
    cache_hit_count: int = 0
    cache_read_tokens_sum: int = 0

    @property
    def error_rate(self) -> float:
        denom = self.search_count + self.error_count
        return (self.error_count / denom) if denom else 0.0

    @property
    def cache_hit_ratio(self) -> float:
        return (self.cache_hit_count / self.search_count) if self.search_count else 0.0

    @property
    def search_p95_ms(self) -> float:
        return self.latency_by_event.get(SEARCH_EVENT, {}).get("p95", 0.0)


# -----------------------------------------------------------
# 純関数: Insights results → Readout / 判定（AWS 非依存・テスト対象）
# -----------------------------------------------------------
def _rows_to_dicts(results: list[list[dict[str, str]]]) -> list[dict[str, str]]:
    """Insights の results（[[{field,value}...]...]）を dict のリストに正規化する。"""
    return [{f["field"]: f["value"] for f in row} for row in results]


def _f(d: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(d[key])
    except (KeyError, ValueError, TypeError):
        return default


def build_readout(
    *,
    window_label: str,
    latency_results: list[list[dict[str, str]]],
    cost_results: list[list[dict[str, str]]],
    error_results: list[list[dict[str, str]]],
) -> Readout:
    """3 クエリの生 results から Readout を組み立てる（純関数）。"""
    r = Readout(window_label=window_label)

    for d in _rows_to_dicts(latency_results):
        ev = d.get("event", "")
        if not ev:
            continue
        r.latency_by_event[ev] = {
            "n": _f(d, "n"),
            "p50": _f(d, "p50"),
            "p95": _f(d, "p95"),
            "p99": _f(d, "p99"),
            "max_ms": _f(d, "max_ms"),
        }

    cost_rows = _rows_to_dicts(cost_results)
    if cost_rows:
        c = cost_rows[0]
        r.search_count = int(_f(c, "n"))
        r.cost_sum_usd = _f(c, "cost_sum")
        r.search_cost_p50_usd = _f(c, "cost_p50")
        r.cache_read_tokens_sum = int(_f(c, "cache_read_sum"))
        r.cache_hit_count = int(_f(c, "cache_hit_n"))

    err_rows = _rows_to_dicts(error_results)
    if err_rows:
        r.error_count = int(_f(err_rows[0], "n"))

    return r


@dataclass
class Verdict:
    name: str
    ok: bool
    detail: str


def evaluate(r: Readout) -> list[Verdict]:
    """Readout を SLO と照合して GO/NO-GO の判定リストを返す（純関数）。

    サンプル 0 件の指標は「判定不可（要トラフィック）」として ok=True 扱い
    （違反ではない＝パイロット未稼働で NO-GO を誤発火させない）。
    """
    verdicts: list[Verdict] = []

    # 検索 p95
    if r.search_count == 0:
        verdicts.append(
            Verdict("search_p95", True, "サンプル0件（要パイロットトラフィック・判定不可）")
        )
    else:
        p95 = r.search_p95_ms
        verdicts.append(
            Verdict(
                "search_p95",
                p95 <= SLO_SEARCH_P95_MS,
                f"p95={p95 / 1000:.1f}s (SLO ≤{SLO_SEARCH_P95_MS / 1000:.0f}s, n={r.search_count})",
            )
        )
        # エラー率
        verdicts.append(
            Verdict(
                "error_rate",
                r.error_rate <= SLO_ERROR_RATE,
                f"{r.error_rate * 100:.2f}% (SLO ≤{SLO_ERROR_RATE * 100:.0f}%, "
                f"errors={r.error_count})",
            )
        )
        # 1 検索コスト p50
        verdicts.append(
            Verdict(
                "search_cost_p50",
                r.search_cost_p50_usd <= SLO_SEARCH_COST_P50_USD,
                f"${r.search_cost_p50_usd:.4f} (SLO ≤${SLO_SEARCH_COST_P50_USD})",
            )
        )
    return verdicts


def render_text(r: Readout, verdicts: list[Verdict]) -> str:
    """人間向けサマリ文字列。"""
    lines: list[str] = []
    lines.append(f"=== Pilot Health Readout [{r.window_label}] ===")
    lines.append("")
    lines.append("-- latency by event --")
    if r.latency_by_event:
        for ev, m in sorted(r.latency_by_event.items(), key=lambda kv: -kv[1]["n"]):
            lines.append(
                f"  {ev:24s} n={int(m['n']):>4}  "
                f"p50={m['p50'] / 1000:.1f}s  p95={m['p95'] / 1000:.1f}s  "
                f"p99={m['p99'] / 1000:.1f}s  max={m['max_ms'] / 1000:.1f}s"
            )
    else:
        lines.append("  （latency サンプルなし）")
    lines.append("")
    lines.append("-- cost / cache --")
    lines.append(
        f"  search(bedrock_converse) n={r.search_count}  "
        f"cost_sum=${r.cost_sum_usd:.4f}  cost_p50=${r.search_cost_p50_usd:.4f}  "
        f"cache_hit_ratio={r.cache_hit_ratio * 100:.0f}% "
        f"(cache_read_tokens={r.cache_read_tokens_sum})"
    )
    lines.append(f"  errors={r.error_count}  error_rate={r.error_rate * 100:.2f}%")
    lines.append("")
    lines.append("-- SLO verdicts --")
    for v in verdicts:
        mark = "✅ GO" if v.ok else "❌ NO-GO"
        lines.append(f"  [{mark}] {v.name}: {v.detail}")
    overall = all(v.ok for v in verdicts)
    lines.append("")
    lines.append(f"OVERALL: {'✅ GO（機械検証 SLO 内）' if overall else '❌ NO-GO（要対応）'}")
    return "\n".join(lines)


# -----------------------------------------------------------
# AWS 実行部
# -----------------------------------------------------------
def _run_query(client: Any, log_group: str, query: str, start_ts: int, end_ts: int) -> list[Any]:
    resp = client.start_query(
        logGroupName=log_group, startTime=start_ts, endTime=end_ts, queryString=query
    )
    qid = resp["queryId"]
    status: dict[str, Any] = {}
    for _ in range(60):
        time.sleep(2)
        status = client.get_query_results(queryId=qid)
        if status["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
    if status.get("status") != "Complete":
        raise RuntimeError(f"Logs Insights query did not complete: {status.get('status')}")
    return list(status.get("results", []))


def collect(log_group: str, region: str, start_ts: int, end_ts: int, label: str) -> Readout:
    client = boto3.client("logs", region_name=region)
    latency = _run_query(client, log_group, _Q_LATENCY, start_ts, end_ts)
    cost = _run_query(client, log_group, _Q_COST_CACHE, start_ts, end_ts)
    errors = _run_query(client, log_group, _Q_ERRORS, start_ts, end_ts)
    return build_readout(
        window_label=label,
        latency_results=latency,
        cost_results=cost,
        error_results=errors,
    )


def main() -> int:
    p = argparse.ArgumentParser(description="P1 パイロット ヘルスリードアウト（本番MCP経路）")
    p.add_argument("--log-group", default=DEFAULT_LOG_GROUP)
    p.add_argument("--region", default=DEFAULT_REGION)
    p.add_argument("--hours", type=int, default=24, help="集計窓（時間・--days と排他）")
    p.add_argument("--days", type=int, default=None, help="集計窓（日・指定時は --hours を上書き）")
    p.add_argument("--json", action="store_true", help="JSON で出力")
    args = p.parse_args()

    hours = args.days * 24 if args.days else args.hours
    label = f"過去{args.days}日" if args.days else f"過去{hours}時間"
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)

    try:
        r = collect(
            args.log_group, args.region, int(start.timestamp()), int(end.timestamp()), label
        )
    except Exception as e:
        print(f"[ERROR] readout failed: {e}", file=sys.stderr)
        return 2

    verdicts = evaluate(r)

    if args.json:
        payload: dict[str, Any] = {
            "generated_at": end.isoformat(),
            "log_group": args.log_group,
            "window": label,
            "latency_by_event": r.latency_by_event,
            "search_count": r.search_count,
            "error_count": r.error_count,
            "error_rate": r.error_rate,
            "cost_sum_usd": r.cost_sum_usd,
            "search_cost_p50_usd": r.search_cost_p50_usd,
            "cache_hit_ratio": r.cache_hit_ratio,
            "verdicts": [{"name": v.name, "ok": v.ok, "detail": v.detail} for v in verdicts],
            "overall_go": all(v.ok for v in verdicts),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(r, verdicts))

    return 0 if all(v.ok for v in verdicts) else 1


if __name__ == "__main__":
    sys.exit(main())
