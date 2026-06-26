"""scripts/run_eval.py の --compare ロジック単体テスト（評価本体は走らせない）。

scripts/ は package でないため importlib でロードする（test_run_morning_digest_fargate と同型）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_eval.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("run_eval_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_eval_under_test"] = module
    spec.loader.exec_module(module)
    return module


mod = _load()


def test_fmt_metric() -> None:
    assert mod._fmt_metric("top1_hit_rate", 0.52) == "52.0%"
    assert mod._fmt_metric("top5_hit_rate", 0.8) == "80.0%"
    assert mod._fmt_metric("mean_mrr", 0.6123) == "0.6123"
    assert mod._fmt_metric("mean_cost_usd", 0.0123) == "$0.0123"
    assert mod._fmt_metric("mean_latency_ms", 1234.7) == "1235ms"


def test_delta_metric() -> None:
    assert mod._delta_metric("top1_hit_rate", 0.52, 0.58) == "+6.0pp"
    assert mod._delta_metric("top5_hit_rate", 0.8, 0.7) == "-10.0pp"
    assert mod._delta_metric("mean_latency_ms", 100.0, 130.0) == "+30ms"
    assert mod._delta_metric("mean_mrr", 0.5, 0.55) == "+0.0500"


def test_case_regressions() -> None:
    base = {
        "per_case": [
            {"case_id": 1, "top5_hit": True},
            {"case_id": 2, "top5_hit": False},
            {"case_id": 3, "top5_hit": True},
        ]
    }
    cur = {
        "per_case": [
            {"case_id": 1, "top5_hit": True},
            {"case_id": 2, "top5_hit": True},  # newly passed
            {"case_id": 3, "top5_hit": False},  # regressed
        ]
    }
    passed, failed = mod._case_regressions(base, cur)
    assert passed == [2]
    assert failed == [3]


def test_latest_result_path_picks_newest(tmp_path: Path) -> None:
    (tmp_path / "baseline_20260101_000000.json").write_text(
        json.dumps({"top1_hit_rate": 0.5, "per_case": []}), encoding="utf-8"
    )
    (tmp_path / "baseline_20260102_000000.json").write_text(
        json.dumps({"top1_hit_rate": 0.6, "per_case": []}), encoding="utf-8"
    )
    p = mod._latest_result_path("baseline", tmp_path)
    assert p is not None
    assert p.name == "baseline_20260102_000000.json"
    assert mod._latest_result_path("missing", tmp_path) is None


def test_load_summary_dict(tmp_path: Path) -> None:
    (tmp_path / "x_20260102_000000.json").write_text(
        json.dumps({"top1_hit_rate": 0.6, "per_case": []}), encoding="utf-8"
    )
    s = mod._load_summary_dict("x", tmp_path)
    assert s is not None
    assert s["top1_hit_rate"] == 0.6
    assert mod._load_summary_dict("missing", tmp_path) is None
