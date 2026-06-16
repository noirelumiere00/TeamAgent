"""scripts/reembed_chunks.py の純関数テスト（DB/embedder 非依存）。

scripts/ は package でないため importlib でロード（test_run_eval.py と同流儀）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PATH = PROJECT_ROOT / "scripts" / "reembed_chunks.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("reembed_under_test", PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reembed_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


rc = _load()


def test_batched_splits_evenly() -> None:
    assert list(rc.batched([1, 2, 3, 4], 2)) == [[1, 2], [3, 4]]


def test_batched_remainder() -> None:
    assert list(rc.batched([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]


def test_batched_empty() -> None:
    assert list(rc.batched([], 3)) == []


def test_batched_rejects_nonpositive() -> None:
    with pytest.raises(ValueError):
        list(rc.batched([1, 2], 0))


def test_build_update_params() -> None:
    sql, params = rc.build_update_params("c-1", [0.1, 0.2, 0.3])
    assert "UPDATE chunks SET embedding" in sql
    assert "WHERE id = %s" in sql
    # pgvector は list をそのまま渡す（embedding, id の順）
    assert params == ([0.1, 0.2, 0.3], "c-1")
