"""scripts/run_eval.py の途中経過保存ロジック単体テスト。

Day 8 教訓 #5 対応で追加した per-case incremental save (`_append_partial` /
`_partial_path`) が、各ケース完了ごとに JSONL 行を追記し、プロセスが
途中で死んでもディスクに結果が残ることを固定する。

scripts/ は package ではないため importlib でモジュールをロードする。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RUN_EVAL_PATH = PROJECT_ROOT / "scripts" / "run_eval.py"


def _load_run_eval() -> Any:
    """scripts/run_eval.py を独立モジュールとしてロードする。

    `from __future__ import annotations` 下の dataclass は型解決時に
    sys.modules[cls.__module__] を参照するため、exec_module の前に
    モジュールを sys.modules へ登録しておく必要がある。
    """
    mod_name = "run_eval_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, RUN_EVAL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


run_eval = _load_run_eval()


def test_partial_path_uses_label_and_run_ts() -> None:
    """partial path は label と run_ts を含む .jsonl になる。"""
    path = run_eval._partial_path("v2c_compact", "20260529_121124")
    assert path.name == "v2c_compact_20260529_121124_partial.jsonl"
    assert path.suffix == ".jsonl"


def test_append_partial_writes_one_jsonl_line_per_case(tmp_path: Path) -> None:
    """_append_partial は 1 ケースにつき 1 行の有効な JSON を追記する。"""
    path = tmp_path / "results" / "lbl_ts_partial.jsonl"

    r1 = run_eval.CaseResult(case_id=1, query="日本ガイシのケイパ提案")
    r1.top1_hit = True
    r1.top5_hit = True
    r1.mrr = 1.0
    r1.expected_rank = 1
    r1.latency_ms = 18079
    r1.cost_usd = 0.0197

    r2 = run_eval.CaseResult(case_id=2, query="マンダムのヒアリング")
    r2.error = "OperationalError: connection refused"
    r2.latency_ms = 33

    run_eval._append_partial(path, r1)
    run_eval._append_partial(path, r2)

    # 親ディレクトリは自動生成される
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    rec1 = json.loads(lines[0])
    rec2 = json.loads(lines[1])

    # 各行は CaseResult の全フィールドを保持し round-trip できる
    assert rec1["case_id"] == 1
    assert rec1["query"] == "日本ガイシのケイパ提案"
    assert rec1["top1_hit"] is True
    assert rec1["expected_rank"] == 1
    assert rec1["cost_usd"] == pytest.approx(0.0197)

    # エラーケースも error フィールド込みで残る (中断診断用)
    assert rec2["case_id"] == 2
    assert rec2["error"] == "OperationalError: connection refused"
    assert rec2["top5_hit"] is False


def test_append_partial_is_crash_safe_each_line_independent(tmp_path: Path) -> None:
    """途中で停止しても、追記済みの行は完全な JSON として読める (部分行が残らない)。

    プロセスが N 件目の後に殺された状況を「N 回 append したファイル」で再現し、
    全行が個別にパース可能であることを確認する。
    """
    path = tmp_path / "partial.jsonl"
    for i in range(1, 6):
        run_eval._append_partial(path, run_eval.CaseResult(case_id=i, query=f"q{i}"))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    parsed = [json.loads(line) for line in lines]
    assert [p["case_id"] for p in parsed] == [1, 2, 3, 4, 5]
