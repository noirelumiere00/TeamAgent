#!/usr/bin/env python
"""TeamAgent 検索精度評価ハーネス。

Sprint 4 で実装した機能 (Rerank, FB Drive Match, prompt v2 等) の精度を
gold set で数値化する。A/B 比較で改善を立証するための共通基盤。

Usage:
    # baseline (Rerank OFF, prompt v1)
    python scripts/run_eval.py --label baseline

    # Rerank ON (Sprint 4-A 効果)
    USE_COHERE_RERANK=true python scripts/run_eval.py --label rerank

    # フル構成 (Day 8 完成形)
    USE_COHERE_RERANK=true USE_FB_DRIVE_MATCH=true PROMPT_VERSION=v2 \\
      python scripts/run_eval.py --label day8_full

    # 結果を比較
    python scripts/run_eval.py --compare baseline rerank day8_full

出力:
    `data/eval/results/<label>_<timestamp>.json` に詳細を保存。
    標準出力に集計サマリ。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

GOLD_SET_PATH = PROJECT_ROOT / "data" / "eval" / "sales_gold_set.yaml"
RESULTS_DIR = PROJECT_ROOT / "data" / "eval" / "results"


@dataclass
class CaseResult:
    """1 query の評価結果。"""

    case_id: int
    query: str
    expect_zero: bool = False  # gold case の expect_zero_hits (ネガティブケースか)
    top1_hit: bool = False
    top5_hit: bool = False
    mrr: float = 0.0
    expected_rank: int | None = None  # 1-based、見つからなければ None
    actual_top_hits: list[dict[str, Any]] = field(default_factory=list)  # debug 用
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: str | None = None


@dataclass
class EvalSummary:
    """全 case の集計。"""

    label: str
    timestamp: str
    config: dict[str, Any]  # 環境変数 + flags
    total_cases: int
    top1_hit_rate: float
    top5_hit_rate: float
    mean_mrr: float
    mean_cost_usd: float
    mean_latency_ms: float
    zero_hit_correct: int  # ネガティブケースの正解数
    zero_hit_total: int
    per_case: list[CaseResult]


def _load_gold_set() -> list[dict[str, Any]]:
    """gold set YAML を読み込む。"""
    with GOLD_SET_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return list(data["cases"])


def _match_hit(hit_content: str, hit_metadata: dict[str, Any], case: dict[str, Any]) -> bool:
    """gold case の期待条件に hit がマッチするかを判定。

    - expect_keywords: 全部 content に含まれていれば True (空リストなら無視)
    - expect_source_type: source_type が一致
    - expect_client_name: metadata.client_name に部分一致
    - expect_metadata: 各 key の値が一致
    """
    keywords: list[str] = case.get("expect_keywords") or []
    for kw in keywords:
        if kw not in hit_content:
            return False

    expect_st = case.get("expect_source_type")
    if expect_st and hit_metadata.get("source_type") != expect_st:
        return False

    expect_client = case.get("expect_client_name")
    if expect_client:
        actual = str(hit_metadata.get("client_name") or "")
        if expect_client not in actual:
            return False

    expect_meta = case.get("expect_metadata") or {}
    for k, expected_v in expect_meta.items():
        actual_v = str(hit_metadata.get(k) or "")
        if str(expected_v) not in actual_v:
            return False

    return True


def _evaluate_case(skill: Any, ctx_cls: Any, case: dict[str, Any]) -> CaseResult:
    """1 ケースを SearchSkill 経由で実行 + 評価。"""
    from teamagent.skills.search.schema import SearchInput

    expect_zero = bool(case.get("expect_zero_hits"))
    result = CaseResult(case_id=case["id"], query=case["query"], expect_zero=expect_zero)

    start = time.perf_counter()
    try:
        out = skill.run(
            input=SearchInput(query=case["query"], top_k=5),
            ctx=ctx_cls(
                metadata={
                    "user_email": os.environ.get("EVAL_USER_EMAIL", "noreply@vectorinc.co.jp"),
                    "user_groups": ["vectorinc.co.jp"],
                    "user_role": "admin",
                }
            ),
        )
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        result.latency_ms = int((time.perf_counter() - start) * 1000)
        return result

    result.latency_ms = int((time.perf_counter() - start) * 1000)
    result.cost_usd = float(out.total_cost_usd)
    result.actual_top_hits = [
        {
            "chunk_id": h.chunk_id,
            "score": round(h.score, 4),
            "source_type": h.source_type,
            "content_preview": h.content[:120],
        }
        for h in out.hits[:5]
    ]

    # ネガティブケース: 0 ヒットが正解
    if expect_zero:
        result.top1_hit = len(out.hits) == 0
        result.top5_hit = len(out.hits) == 0
        result.mrr = 1.0 if len(out.hits) == 0 else 0.0
        return result

    # 通常ケース: 期待 chunk が top-5 に居るか
    # SearchHitOut (Pydantic) は flat fields なので metadata dict を組み立て直す
    for rank, h in enumerate(out.hits[:5], start=1):
        hit_meta = {
            "source_type": h.source_type,
            "source_uri": h.source_uri,
            "channel_name": h.channel_name,
            "file_name": h.file_name,
            "page_num": h.page_num,
            # client_name / deal_phase は SearchHitOut に露出済 (Sprint 5)。
            # これが無いと expect_client_name のケース (case 1,2) が検索品質に
            # 関わらず常に miss 判定になっていた = "52% の天井" の正体 (eval 測定バグ)。
            "client_name": h.client_name,
            "deal_phase": h.deal_phase,
            # Sprint 5: expect_metadata (bant_score / channel_type) の判定用 (case 14-17)
            "bant_score": h.bant_score,
            "channel_type": h.channel_type,
        }
        if _match_hit(h.content, hit_meta, case):
            result.expected_rank = rank
            result.top1_hit = rank == 1
            result.top5_hit = True
            result.mrr = 1.0 / rank
            break

    return result


def _summarize(results: list[CaseResult], label: str, config: dict[str, Any]) -> EvalSummary:
    """全 case の結果を集計。"""
    n = len(results)
    if n == 0:
        raise ValueError("結果がありません")

    top1_count = sum(1 for r in results if r.top1_hit)
    top5_count = sum(1 for r in results if r.top5_hit)
    mrr_sum = sum(r.mrr for r in results)
    cost_sum = sum(r.cost_usd for r in results)
    latency_sum = sum(r.latency_ms for r in results)

    # ネガティブケースの正解数。母数は gold set の expect_zero_hits 件数で固定する。
    # 旧実装は母数を「実 hits が空」(not actual_top_hits) で取っていたため、
    #   (a) ヒットを返してしまったネガティブケースが分母から脱落 = "黙る能力" が測れず
    #   (b) 単に検索ミスしたポジティブケースが分母に混入
    # して 0/0 を満点と誤読していた (QW-3)。expect_zero を母数に据えて根治する。
    zero_total = sum(1 for r in results if r.expect_zero)
    zero_correct = sum(1 for r in results if r.expect_zero and not r.actual_top_hits)

    return EvalSummary(
        label=label,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        config=config,
        total_cases=n,
        top1_hit_rate=round(top1_count / n, 4),
        top5_hit_rate=round(top5_count / n, 4),
        mean_mrr=round(mrr_sum / n, 4),
        mean_cost_usd=round(cost_sum / n, 6),
        mean_latency_ms=round(latency_sum / n, 1),
        zero_hit_correct=zero_correct,
        zero_hit_total=zero_total,
        per_case=results,
    )


def _print_summary(s: EvalSummary) -> None:
    print()
    print("=" * 60)
    print(f"Eval: {s.label}")
    print(f"timestamp: {s.timestamp}")
    print(f"config: {s.config}")
    print("=" * 60)
    print(f"total cases:        {s.total_cases}")
    print(f"top-1 hit rate:     {s.top1_hit_rate * 100:.1f}%")
    print(f"top-5 hit rate:     {s.top5_hit_rate * 100:.1f}%")
    print(f"mean MRR:           {s.mean_mrr:.4f}")
    print(f"mean cost / query:  ${s.mean_cost_usd:.4f}")
    print(f"mean latency / q:   {s.mean_latency_ms:.0f} ms")
    print(f"zero-hit handling:  {s.zero_hit_correct}/{s.zero_hit_total} correct")
    print("=" * 60)
    # 詳細 (失敗ケースのみ)
    failed = [r for r in s.per_case if not r.top5_hit]
    if failed:
        print(f"\n--- top-5 漏れケース ({len(failed)} 件) ---")
        for r in failed:
            print(f"  case {r.case_id}: {r.query}")
            if r.error:
                print(f"    ERROR: {r.error}")
            elif r.actual_top_hits:
                print(f"    top-1 hit: chunk_id={r.actual_top_hits[0]['chunk_id']}")


def _save_results(s: EvalSummary, run_ts: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{s.label}_{run_ts}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(asdict(s), f, ensure_ascii=False, indent=2)
    return path


def _partial_path(label: str, run_ts: str) -> Path:
    """中断時の途中経過保存先 (JSONL)。consolidated JSON と run_ts で対応づく。"""
    return RESULTS_DIR / f"{label}_{run_ts}_partial.jsonl"


def _append_partial(path: Path, result: CaseResult) -> None:
    """1 ケース完了ごとに JSONL で 1 行追記する (crash-safe な途中経過保存)。

    Day 8 教訓 #5: 長時間 eval 中に SSM トンネル断 / Ctrl-C / スリープで
    プロセスが死ぬと、最後の consolidated 保存に到達できず全件失われていた。
    各ケース直後に append + flush することで、プロセスが殺されても
    ここまでの結果がディスクに残る。正常完走時は main() が partial を削除する。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(result), ensure_ascii=False))
        f.write("\n")
        f.flush()


def _build_skill() -> Any:
    """環境変数から SearchSkill を組み立てる (slack_bot.py と同じロジック)。"""
    from teamagent.adapters.embeddings_client import LocalE5Embedder
    from teamagent.skills.search.query_planner import build_query_planner_from_env
    from teamagent.skills.search.skill import SearchSkill

    use_contextual = os.environ.get("USE_CONTEXTUAL", "false").lower() in ("1", "true", "yes")
    use_new_schema = os.environ.get("USE_NEW_SCHEMA", "true").lower() in ("1", "true", "yes")
    use_fb_drive_match = os.environ.get("USE_FB_DRIVE_MATCH", "false").lower() in (
        "1",
        "true",
        "yes",
    )
    use_cohere_rerank = os.environ.get("USE_COHERE_RERANK", "false").lower() in (
        "1",
        "true",
        "yes",
    )
    prompt_version = os.environ.get("PROMPT_VERSION", "v1")
    try:
        summary_max_tokens = int(os.environ.get("SEARCH_MAX_TOKENS", "4096"))
    except ValueError:
        summary_max_tokens = 4096
    try:
        min_relevance = float(os.environ.get("SEARCH_MIN_RELEVANCE", "0.0"))
    except ValueError:
        min_relevance = 0.0
    try:
        min_relevance_fallback = float(os.environ.get("SEARCH_MIN_RELEVANCE_FALLBACK", "0.0"))
    except ValueError:
        min_relevance_fallback = 0.0
    try:
        rerank_pool_size = int(os.environ.get("SEARCH_RERANK_POOL_SIZE", "30"))
    except ValueError:
        rerank_pool_size = 30
    try:
        # QW-4: rerank 返却数（救済プール幅）。min_relevance の母数を top_k から切離す。
        rerank_return_size = int(os.environ.get("SEARCH_RERANK_RETURN_SIZE", "100"))
    except ValueError:
        rerank_return_size = 100
    use_aggregation_mode = os.environ.get("USE_AGGREGATION_MODE", "false").lower() in (
        "1",
        "true",
        "yes",
    )
    use_client_boost = os.environ.get("USE_CLIENT_BOOST", "false").lower() in (
        "1",
        "true",
        "yes",
    )

    return (
        SearchSkill(
            embedder=LocalE5Embedder(),
            use_contextual=use_contextual,
            use_new_schema=use_new_schema,
            use_fb_drive_match=use_fb_drive_match,
            use_cohere_rerank=use_cohere_rerank,
            rerank_pool_size=rerank_pool_size,
            rerank_return_size=rerank_return_size,
            min_relevance=min_relevance,
            min_relevance_fallback=min_relevance_fallback,
            use_client_boost=use_client_boost,
            use_aggregation_mode=use_aggregation_mode,
            prompt_version=prompt_version,
            summary_max_tokens=summary_max_tokens,
            query_planner=build_query_planner_from_env(),
        ),
        {
            "USE_NEW_SCHEMA": use_new_schema,
            "USE_CONTEXTUAL": use_contextual,
            "USE_FB_DRIVE_MATCH": use_fb_drive_match,
            "USE_COHERE_RERANK": use_cohere_rerank,
            "SEARCH_RERANK_POOL_SIZE": rerank_pool_size,
            "SEARCH_RERANK_RETURN_SIZE": rerank_return_size,
            "SEARCH_MIN_RELEVANCE": min_relevance,
            "SEARCH_MIN_RELEVANCE_FALLBACK": min_relevance_fallback,
            "USE_CLIENT_BOOST": use_client_boost,
            "USE_AGGREGATION_MODE": use_aggregation_mode,
            "PROMPT_VERSION": prompt_version,
            "SEARCH_MAX_TOKENS": summary_max_tokens,
        },
    )


# ── 比較モード (--compare) ─────────────────────────────────────────────────


def _latest_result_path(label: str, results_dir: Path = RESULTS_DIR) -> Path | None:
    """指定 label の最新 results JSON を返す（無ければ None）。"""
    if not results_dir.exists():
        return None
    candidates = sorted(results_dir.glob(f"{label}_*.json"))
    return candidates[-1] if candidates else None


def _load_summary_dict(label: str, results_dir: Path = RESULTS_DIR) -> dict[str, Any] | None:
    """label の最新 EvalSummary を dict で読む。"""
    path = _latest_result_path(label, results_dir)
    if path is None:
        return None
    with path.open(encoding="utf-8") as f:
        return dict(json.load(f))


_COMPARE_METRICS: list[tuple[str, str]] = [
    ("top-1", "top1_hit_rate"),
    ("top-5", "top5_hit_rate"),
    ("MRR", "mean_mrr"),
    ("cost/q", "mean_cost_usd"),
    ("latency", "mean_latency_ms"),
    ("zero-hit", "zero_hit_correct"),
]


def _fmt_metric(key: str, val: float) -> str:
    if key in ("top1_hit_rate", "top5_hit_rate"):
        return f"{val * 100:.1f}%"
    if key == "mean_mrr":
        return f"{val:.4f}"
    if key == "mean_cost_usd":
        return f"${val:.4f}"
    if key == "mean_latency_ms":
        return f"{val:.0f}ms"
    return f"{val:.0f}"


def _delta_metric(key: str, base: float, cur: float) -> str:
    if key in ("top1_hit_rate", "top5_hit_rate"):
        return f"{(cur - base) * 100:+.1f}pp"
    if key == "mean_mrr":
        return f"{cur - base:+.4f}"
    if key == "mean_cost_usd":
        return f"{cur - base:+.4f}"
    if key == "mean_latency_ms":
        return f"{cur - base:+.0f}ms"
    return f"{cur - base:+.0f}"


def _case_regressions(base: dict[str, Any], cur: dict[str, Any]) -> tuple[list[int], list[int]]:
    """baseline 比で top-5 が「新たに通った / 落ちた」case_id を返す。"""
    base_pass = {int(c["case_id"]): bool(c["top5_hit"]) for c in base.get("per_case", [])}
    newly_passed: list[int] = []
    newly_failed: list[int] = []
    for c in cur.get("per_case", []):
        cid = int(c["case_id"])
        was = base_pass.get(cid)
        if was is None:
            continue
        now = bool(c["top5_hit"])
        if now and not was:
            newly_passed.append(cid)
        elif was and not now:
            newly_failed.append(cid)
    return sorted(newly_passed), sorted(newly_failed)


def _print_comparison(labels: list[str], baseline_label: str) -> int:
    """保存済み results を表で比較し、baseline からの Δ と top-5 回帰を表示する。"""
    summaries: dict[str, dict[str, Any]] = {}
    for lb in labels:
        s = _load_summary_dict(lb)
        if s is None:
            print(f"⚠ results 未発見: {lb}  (data/eval/results/{lb}_*.json)")
            continue
        summaries[lb] = s
    if baseline_label not in summaries:
        print(f"baseline '{baseline_label}' の結果が見つかりません。")
        return 1
    base = summaries[baseline_label]

    width = max(14, *(len(lb) + 12 for lb in summaries))
    print()
    print("=" * 72)
    print(f"Comparison (baseline = {baseline_label})")
    print("=" * 72)
    header = f"{'metric':<10}" + "".join(f"{lb:>{width}}" for lb in summaries)
    print(header)
    print("-" * len(header))
    for name, key in _COMPARE_METRICS:
        row = f"{name:<10}"
        for lb in summaries:
            cur = float(summaries[lb].get(key, 0.0))
            cell = _fmt_metric(key, cur)
            if lb != baseline_label:
                cell += f" ({_delta_metric(key, float(base.get(key, 0.0)), cur)})"
            row += f"{cell:>{width}}"
        print(row)
    print("-" * len(header))
    for lb in summaries:
        if lb == baseline_label:
            continue
        passed, failed = _case_regressions(base, summaries[lb])
        print(f"\n[{lb}] vs {baseline_label}: +通過 {len(passed)} / -脱落(回帰) {len(failed)}")
        if passed:
            print(f"  新たに top-5 通過: {passed}")
        if failed:
            print(f"  新たに top-5 脱落: {failed}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label",
        default=None,
        help="この実行を識別するラベル (例: baseline, rerank, day8_full)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="先頭 N 件だけ実行 (smoke test 用)",
    )
    parser.add_argument(
        "--compare",
        nargs="+",
        metavar="LABEL",
        default=None,
        help="保存済み results (各 label の最新 JSON) を比較表示する（評価は実行しない）",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="--compare の基準ラベル（既定 = --compare の先頭）",
    )
    args = parser.parse_args()

    # 比較モード: 評価は走らせず保存済み結果の差分だけ出す。
    if args.compare:
        baseline = args.baseline or args.compare[0]
        return _print_comparison(args.compare, baseline)

    if not args.label:
        parser.error("--label は必須です（--compare を使う場合を除く）")

    cases = _load_gold_set()
    if args.limit:
        cases = cases[: args.limit]

    print("--- Loading SearchSkill ---")
    skill, config = _build_skill()
    print(f"config: {config}")

    from teamagent.skills.base import SkillContext

    run_ts = time.strftime("%Y%m%d_%H%M%S")
    partial_path = _partial_path(args.label, run_ts)

    results: list[CaseResult] = []
    interrupted = False
    print(f"\n--- Running {len(cases)} cases ---")
    try:
        for case in cases:
            r = _evaluate_case(skill, SkillContext, case)
            marker = "✓" if r.top5_hit else "✗"
            rank_str = f"rank {r.expected_rank}" if r.expected_rank else "miss"
            err = f" ERROR: {r.error}" if r.error else ""
            print(
                f"  [{marker}] case {r.case_id:2d}: {rank_str:8s} "
                f"cost=${r.cost_usd:.4f} latency={r.latency_ms}ms{err}"
            )
            results.append(r)
            # 各ケース直後に途中経過を JSONL 追記 (中断耐性)
            _append_partial(partial_path, r)
    except KeyboardInterrupt:
        interrupted = True
        print(
            f"\n[interrupted] Ctrl-C 受信。ここまでの {len(results)}/{len(cases)} 件を集計します。"
        )

    if not results:
        print("結果ゼロのため集計をスキップします。")
        return 1

    summary = _summarize(results, args.label, config)
    _print_summary(summary)
    path = _save_results(summary, run_ts)
    print(f"\nresults saved: {path}")
    if interrupted:
        # 中断時は partial を残す (どこで止まったかの証跡)
        print(f"(中断: {len(results)}/{len(cases)} 件のみ。途中経過: {partial_path})")
        return 130
    # 正常完走時は consolidated JSON が上位互換なので partial を掃除する
    partial_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
