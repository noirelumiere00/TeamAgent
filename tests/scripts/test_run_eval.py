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


def test_match_hit_compares_client_name() -> None:
    """expect_client_name が hit の client_name と部分一致するか判定される。

    回帰: 以前は eval が hit_meta に client_name を詰めず、expect_client_name の
    ケース (日本ガイシ/マンダム) が検索品質に関わらず常に miss = "52% の天井" だった。
    """
    case = {"id": 1, "expect_client_name": "日本ガイシ"}
    # client_name が一致 → hit
    assert run_eval._match_hit("本文", {"client_name": "日本ガイシ"}, case) is True
    # client_name が別 → miss
    assert run_eval._match_hit("本文", {"client_name": "マンダム"}, case) is False
    # client_name が無い (None) → miss (詰め忘れの再発検知)
    assert run_eval._match_hit("本文", {"client_name": None}, case) is False


def test_match_hit_requires_all_keywords_and_client() -> None:
    """expect_keywords は全一致、かつ client_name も満たす必要がある。"""
    case = {
        "id": 2,
        "expect_keywords": ["マンダム", "ヒアリング"],
        "expect_client_name": "マンダム",
    }
    meta = {"client_name": "マンダム"}
    assert run_eval._match_hit("マンダムのヒアリング記録", meta, case) is True
    # キーワード欠落 → miss
    assert run_eval._match_hit("マンダムの提案", meta, case) is False


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


def _make_case(
    case_id: int,
    *,
    expect_zero: bool = False,
    hits: bool = False,
    top1: bool = False,
    top5: bool = False,
) -> Any:
    """テスト用 CaseResult を組み立てる。hits=True で actual_top_hits を 1 件持たせる。"""
    r = run_eval.CaseResult(case_id=case_id, query=f"q{case_id}", expect_zero=expect_zero)
    r.top1_hit = top1
    r.top5_hit = top5
    if hits:
        r.actual_top_hits = [{"chunk_id": f"c{case_id}", "score": 0.5}]
    return r


def test_summarize_zero_hit_total_is_gold_set_negative_count() -> None:
    """zero_hit_total は expect_zero=True のケース数 = gold set のネガティブ件数。

    回帰: 旧実装は母数を「実 hits が空」(not actual_top_hits) で取っていたため、
    検索ミスしたポジティブケースが分母に混入していた。expect_zero を母数に据える。
    """
    results = [
        # ポジティブで hit あり (zero とは無関係)
        _make_case(1, expect_zero=False, hits=True, top1=True, top5=True),
        # ポジティブだが検索ミス (hits 空) → zero 母数に混ぜてはいけない
        _make_case(2, expect_zero=False, hits=False),
        # ネガティブで正しく 0 件
        _make_case(3, expect_zero=True, hits=False),
        # ネガティブで正しく 0 件
        _make_case(4, expect_zero=True, hits=False),
    ]
    s = run_eval._summarize(results, "lbl", {})
    # ネガティブは 2 件のみ (case 3, 4)。case 2 のミスは混入しない。
    assert s.zero_hit_total == 2
    assert s.zero_hit_correct == 2


def test_summarize_zero_hit_counts_negative_that_wrongly_returned_hits() -> None:
    """ヒットを返してしまったネガティブケースは total に残り correct から外れる。

    回帰の核心 (QW-3): 旧実装ではこのケースが分子分母から脱落し 0/0 を満点と誤読していた。
    「黙るべきなのに喋った」失敗を可視化する。
    """
    results = [
        # ネガティブで正しく 0 件 → correct
        _make_case(1, expect_zero=True, hits=False),
        # ネガティブなのにヒットを返した → total には数え、correct には数えない
        _make_case(2, expect_zero=True, hits=True),
    ]
    s = run_eval._summarize(results, "lbl", {})
    assert s.zero_hit_total == 2
    assert s.zero_hit_correct == 1


def test_summarize_no_negative_cases_yields_zero_total() -> None:
    """ネガティブケースが無ければ zero_hit_total=0 (0/0 を満点に化けさせない)。"""
    results = [
        _make_case(1, expect_zero=False, hits=True, top1=True, top5=True),
        _make_case(2, expect_zero=False, hits=False),
    ]
    s = run_eval._summarize(results, "lbl", {})
    assert s.zero_hit_total == 0
    assert s.zero_hit_correct == 0


def test_case_result_expect_zero_round_trips_through_partial(tmp_path: Path) -> None:
    """expect_zero は asdict 経由の JSONL 保存で round-trip する (新フィールドの永続化)。"""
    path = tmp_path / "p.jsonl"
    run_eval._append_partial(path, run_eval.CaseResult(case_id=9, query="meta", expect_zero=True))
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["expect_zero"] is True


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
