"""scripts/pilot_health.py の純関数（Insights results → Readout → SLO 判定）の単体テスト。

AWS を呼ばず、CloudWatch Logs Insights の results 形（[[{field,value}...]...]）を
fixture で与えて build_readout / evaluate を固定する。

scripts/ は package ではないため importlib でモジュールをロードする
（tests/scripts/test_run_eval.py と同じ流儀）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PILOT_HEALTH_PATH = PROJECT_ROOT / "scripts" / "pilot_health.py"


def _load() -> Any:
    mod_name = "pilot_health_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, PILOT_HEALTH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


ph = _load()


def _row(**kv: str) -> list[dict[str, str]]:
    return [{"field": k, "value": v} for k, v in kv.items()]


# Part B 実測を模した fixture（bedrock_converse p95=10.6s / cache_read=0 / gemini 18s）
_LATENCY = [
    _row(event="bedrock_converse", n="3", p50="10400", p95="10600", p99="10600", max_ms="10601"),
    _row(event="embedder_embed", n="3", p50="500", p95="500", p99="500", max_ms="535"),
    _row(
        event="gemini_analyze_video", n="1", p50="18045", p95="18045", p99="18045", max_ms="18045"
    ),
]
_COST = [_row(n="3", cost_sum="0.0359", cost_p50="0.0125", cache_read_sum="0", cache_hit_n="0")]
_ERRORS = [_row(n="0")]


def test_build_readout_parses_three_queries() -> None:
    r = ph.build_readout(
        window_label="過去14日",
        latency_results=_LATENCY,
        cost_results=_COST,
        error_results=_ERRORS,
    )
    assert r.search_count == 3
    assert r.error_count == 0
    assert r.search_p95_ms == 10600.0
    assert r.latency_by_event["gemini_analyze_video"]["max_ms"] == 18045.0
    assert r.search_cost_p50_usd == 0.0125
    # Part B: cache_read=0 → ヒット率 0%
    assert r.cache_hit_ratio == 0.0


def test_evaluate_go_when_within_slo() -> None:
    r = ph.build_readout(
        window_label="w", latency_results=_LATENCY, cost_results=_COST, error_results=_ERRORS
    )
    verdicts = {v.name: v for v in ph.evaluate(r)}
    # 10.6s < 15s SLO
    assert verdicts["search_p95"].ok is True
    assert verdicts["error_rate"].ok is True
    assert verdicts["search_cost_p50"].ok is True


def test_evaluate_no_go_when_p95_exceeds_slo() -> None:
    slow = [
        _row(
            event="bedrock_converse", n="50", p50="16000", p95="20000", p99="25000", max_ms="30000"
        )
    ]
    cost = [_row(n="50", cost_sum="1.0", cost_p50="0.015", cache_read_sum="0", cache_hit_n="0")]
    r = ph.build_readout(
        window_label="w", latency_results=slow, cost_results=cost, error_results=[_row(n="0")]
    )
    verdicts = {v.name: v for v in ph.evaluate(r)}
    assert verdicts["search_p95"].ok is False  # 20s > 15s


def test_evaluate_no_go_when_error_rate_high() -> None:
    lat = [
        _row(event="bedrock_converse", n="90", p50="5000", p95="9000", p99="11000", max_ms="12000")
    ]
    cost = [_row(n="90", cost_sum="0.5", cost_p50="0.005", cache_read_sum="100", cache_hit_n="40")]
    errors = [_row(n="10")]  # 10 / (90+10) = 10% > 1%
    r = ph.build_readout(
        window_label="w", latency_results=lat, cost_results=cost, error_results=errors
    )
    verdicts = {v.name: v for v in ph.evaluate(r)}
    assert verdicts["error_rate"].ok is False
    assert abs(r.error_rate - 0.10) < 1e-9


def test_evaluate_no_go_when_cost_p50_exceeds() -> None:
    lat = [
        _row(event="bedrock_converse", n="20", p50="5000", p95="9000", p99="11000", max_ms="12000")
    ]
    cost = [_row(n="20", cost_sum="2.0", cost_p50="0.05", cache_read_sum="0", cache_hit_n="0")]
    r = ph.build_readout(
        window_label="w", latency_results=lat, cost_results=cost, error_results=[_row(n="0")]
    )
    verdicts = {v.name: v for v in ph.evaluate(r)}
    assert verdicts["search_cost_p50"].ok is False  # $0.05 > $0.02


def test_evaluate_zero_traffic_is_not_nogo() -> None:
    """サンプル0件は『判定不可』で ok=True（パイロット未稼働で NO-GO 誤発火させない）。"""
    r = ph.build_readout(
        window_label="w",
        latency_results=[],
        cost_results=[_row(n="0")],
        error_results=[_row(n="0")],
    )
    verdicts = ph.evaluate(r)
    assert len(verdicts) == 1
    assert verdicts[0].name == "search_p95"
    assert verdicts[0].ok is True
    assert "サンプル0件" in verdicts[0].detail


def test_cache_hit_ratio_when_caching_works() -> None:
    cost = [
        _row(n="100", cost_sum="1.0", cost_p50="0.008", cache_read_sum="50000", cache_hit_n="80")
    ]
    r = ph.build_readout(
        window_label="w", latency_results=_LATENCY, cost_results=cost, error_results=[_row(n="0")]
    )
    assert r.cache_hit_ratio == 0.8


def test_render_text_contains_overall_verdict() -> None:
    r = ph.build_readout(
        window_label="過去24時間",
        latency_results=_LATENCY,
        cost_results=_COST,
        error_results=_ERRORS,
    )
    text = ph.render_text(r, ph.evaluate(r))
    assert "Pilot Health Readout" in text
    assert "OVERALL" in text
    assert "bedrock_converse" in text
