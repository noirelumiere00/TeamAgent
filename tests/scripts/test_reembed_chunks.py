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
    assert "UPDATE chunks SET embedding = %s" in sql
    assert "WHERE id = %s" in sql
    # pgvector は list をそのまま渡す（embedding, id の順）
    assert params == ([0.1, 0.2, 0.3], "c-1")


def test_build_update_params_default_is_e5_column() -> None:
    """target_column 既定は embedding（e5・従来挙動）。"""
    sql, _ = rc.build_update_params("c-1", [0.1])
    assert "UPDATE chunks SET embedding = %s WHERE id = %s" == sql


def test_build_update_params_cohere_target_column() -> None:
    """embedding_cohere を指定すると並行列に書く（e5 列は触らない）。"""
    sql, params = rc.build_update_params("c-2", [0.5], target_column="embedding_cohere")
    assert sql == "UPDATE chunks SET embedding_cohere = %s WHERE id = %s"
    assert params == ([0.5], "c-2")


def test_build_update_params_rejects_unknown_column() -> None:
    """許可リスト外の列名（injection 試行）は ValueError。"""
    with pytest.raises(ValueError, match="target_column"):
        rc.build_update_params("c", [0.1], target_column="embedding; DROP TABLE chunks")


def test_main_rejects_backend_target_column_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """EMBEDDER_BACKEND=cohere なのに --target-column embedding（既定）だと全壊するので
    DB に触れる前に fail-loud で 2 を返す（Cohere ベクトルを e5 列へ書く事故を防ぐ）。"""
    monkeypatch.setattr(sys, "argv", ["reembed_chunks.py", "--commit"])
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/db")
    monkeypatch.setenv("EMBEDDER_BACKEND", "cohere")
    monkeypatch.setenv("EMBEDDING_COLUMN", "embedding_cohere")  # build_embedder のペアは合致

    # reembed が呼ばれたら DB に触れてしまう＝検証が効いていない証拠なので fail させる。
    def _boom(*_a: Any, **_k: Any) -> dict[str, int]:
        raise AssertionError("reembed must not be reached on mismatch")

    monkeypatch.setattr(rc, "reembed", _boom)

    assert rc.main() == 2


def test_main_reverse_mismatch_local_backend_cohere_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """逆向き（EMBEDDER_BACKEND=local + --target-column embedding_cohere）も
    e5 ベクトルを cohere 列に汚染するので fail-loud で 2。"""
    monkeypatch.setattr(
        sys, "argv", ["reembed_chunks.py", "--commit", "--target-column", "embedding_cohere"]
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/db")
    monkeypatch.delenv("EMBEDDER_BACKEND", raising=False)  # 既定 local
    monkeypatch.delenv("EMBEDDING_COLUMN", raising=False)  # 既定 embedding（build_embedder は合致）

    def _boom(*_a: Any, **_k: Any) -> dict[str, int]:
        raise AssertionError("reembed must not be reached on mismatch")

    monkeypatch.setattr(rc, "reembed", _boom)

    assert rc.main() == 2
