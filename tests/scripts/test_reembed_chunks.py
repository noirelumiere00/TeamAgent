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


# -----------------------------------------------------------
# HNSW 索引の DROP→再embed→CREATE（一括ロード高速化）
# -----------------------------------------------------------
def test_hnsw_index_name_map() -> None:
    assert rc._HNSW_INDEX_BY_COLUMN["embedding"] == "chunks_embedding_hnsw_idx"
    assert rc._HNSW_INDEX_BY_COLUMN["embedding_cohere"] == "chunks_embedding_cohere_hnsw_idx"


def test_hnsw_index_ddl_exact_match_no_with_clause() -> None:
    """CREATE/DROP 文が既存 migration(0001/0016)と完全一致・WITH 句を含まない（回帰防止）。"""
    drop, create = rc._hnsw_index_ddl("embedding_cohere")
    assert drop == "DROP INDEX IF EXISTS chunks_embedding_cohere_hnsw_idx"
    assert create == (
        "CREATE INDEX IF NOT EXISTS chunks_embedding_cohere_hnsw_idx "
        "ON chunks USING hnsw (embedding_cohere vector_cosine_ops)"
    )
    assert "WITH" not in create  # m/ef_construction チューニングは既存に無い＝一致必須
    e5_drop, e5_create = rc._hnsw_index_ddl("embedding")
    assert e5_drop == "DROP INDEX IF EXISTS chunks_embedding_hnsw_idx"
    assert e5_create == (
        "CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx "
        "ON chunks USING hnsw (embedding vector_cosine_ops)"
    )


def test_hnsw_index_ddl_rejects_unknown_column() -> None:
    with pytest.raises(ValueError, match="target_column"):
        rc._hnsw_index_ddl("embedding; DROP TABLE chunks")


class _FakeCursor:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_a: Any) -> bool:
        return False

    def execute(self, *_a: Any, **_k: Any) -> None:
        pass

    def fetchall(self) -> list[Any]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *_a: Any) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)

    def commit(self) -> None:
        pass


def _install_fake_db(monkeypatch: pytest.MonkeyPatch, rows: list[Any]) -> list[tuple[str, str]]:
    """reembed() 内の `import psycopg` / register_vector を差し替え、
    _run_index_ddl の呼び出し (action, target_column) を記録して返す。"""
    import types

    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = lambda *a, **k: _FakeConn(rows)  # type: ignore[attr-defined]
    fake_pgvector = types.ModuleType("pgvector")
    fake_pgvector_psycopg = types.ModuleType("pgvector.psycopg")
    fake_pgvector_psycopg.register_vector = lambda conn: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "pgvector", fake_pgvector)
    monkeypatch.setitem(sys.modules, "pgvector.psycopg", fake_pgvector_psycopg)

    ddl_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        rc, "_run_index_ddl", lambda dsn, col, *, action: ddl_calls.append((action, col))
    )
    return ddl_calls


class _FakeEmbedder:
    def embed_passage(self, _text: str) -> list[float]:
        return [0.1, 0.2]


def test_reembed_drops_then_creates_index_on_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """commit かつ keep_index=False で 索引 DROP→CREATE が各1回 target_column 付きで走る。"""
    ddl = _install_fake_db(monkeypatch, rows=[("c1", "t1"), ("c2", "t2")])
    rc.reembed(
        dsn="x",
        embedder=_FakeEmbedder(),
        batch_size=10,
        commit=True,
        target_column="embedding_cohere",
    )
    assert ddl == [("drop", "embedding_cohere"), ("create", "embedding_cohere")]


def test_reembed_keep_index_skips_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    ddl = _install_fake_db(monkeypatch, rows=[("c1", "t1")])
    rc.reembed(
        dsn="x",
        embedder=_FakeEmbedder(),
        batch_size=10,
        commit=True,
        target_column="embedding_cohere",
        keep_index=True,
    )
    assert ddl == []


def test_reembed_dry_run_skips_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    """dry-run（commit=False）では索引を一切触らない（--commit 無しで DROP する事故防止）。"""
    ddl = _install_fake_db(monkeypatch, rows=[("c1", "t1")])
    rc.reembed(
        dsn="x",
        embedder=_FakeEmbedder(),
        batch_size=10,
        commit=False,
        target_column="embedding_cohere",
    )
    assert ddl == []


def test_reembed_recreates_index_even_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """書き込みループが例外を投げても finally で CREATE が走り、索引を落としたまま放置しない。"""
    ddl = _install_fake_db(monkeypatch, rows=[("c1", "t1")])

    class _BoomEmbedder:
        def embed_passage(self, _t: str) -> list[float]:
            raise RuntimeError("embed failed")

    with pytest.raises(RuntimeError, match="embed failed"):
        rc.reembed(
            dsn="x",
            embedder=_BoomEmbedder(),
            batch_size=10,
            commit=True,
            target_column="embedding_cohere",
        )
    # DROP は走り、例外後も CREATE が finally で走る
    assert ddl == [("drop", "embedding_cohere"), ("create", "embedding_cohere")]


def test_keep_index_argparse(monkeypatch: pytest.MonkeyPatch) -> None:
    """--keep-index を付けると True、無指定で False（既定＝索引を作り直す側）。"""
    import teamagent.adapters.embeddings_client as ec

    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> dict[str, int]:
        captured.update(kwargs)
        return {"scanned": 0, "updated": 0}

    monkeypatch.setattr(rc, "reembed", _capture)
    # 実 LocalE5 をロードしないよう build_embedder_from_env を差し替え（main が import 元から解決）。
    monkeypatch.setattr(ec, "build_embedder_from_env", lambda: _FakeEmbedder())
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/db")
    monkeypatch.delenv("EMBEDDER_BACKEND", raising=False)  # 既定 local ⇄ embedding で整合
    monkeypatch.delenv("EMBEDDING_COLUMN", raising=False)

    monkeypatch.setattr(sys, "argv", ["reembed_chunks.py", "--commit", "--keep-index"])
    rc.main()
    assert captured.get("keep_index") is True

    captured.clear()
    monkeypatch.setattr(sys, "argv", ["reembed_chunks.py", "--commit"])
    rc.main()
    assert captured.get("keep_index") is False


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
