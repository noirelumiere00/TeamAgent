"""QW-5: PgVectorClient._apply_session() の hnsw.ef_search 注入テスト（実DB0・課金0）。

post-filter recall 崖の緩和として、env SEARCH_HNSW_EF_SEARCH が設定されたときだけ
``SELECT set_config('hnsw.ef_search', %s, true)`` を transaction-local で発行することを固定する。
未設定（or 0 以下）のときは発行しない＝DB 既定（ef_search=40）のまま＝完全後方互換。
"""

from __future__ import annotations

import pytest

from teamagent.adapters.pgvector_client import PgVectorClient


class _FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...] | None]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[object, ...] | None = None) -> None:
        self.calls.append((sql, params))


class _FakeConn:
    def __init__(self) -> None:
        self.cur = _FakeCursor()

    def cursor(self) -> _FakeCursor:
        return self.cur


def _apply(conn: _FakeConn) -> None:
    """RLS GUC 一式を伴う通常の session 確立を呼ぶ（ef_search はその中で注入される）。"""
    PgVectorClient._apply_session(
        conn,  # type: ignore[arg-type]
        "teamagent_app",
        "alice@vectorinc.co.jp",
        ["vectorinc.co.jp"],
        "member",
    )


def _ef_search_calls(conn: _FakeConn) -> list[tuple[str, tuple[object, ...] | None]]:
    return [c for c in conn.cur.calls if "hnsw.ef_search" in c[0]]


def test_application_name_is_transaction_local_and_parameterized() -> None:
    conn = _FakeConn()

    PgVectorClient._apply_session(
        conn,  # type: ignore[arg-type]
        None,
        None,
        None,
        None,
        "teamagent-ingest",
    )

    assert (
        "SELECT set_config('application_name', %s, true)",
        ("teamagent-ingest",),
    ) in conn.cur.calls


def test_ef_search_not_emitted_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 未設定なら hnsw.ef_search は発行しない（DB 既定 40＝後方互換）。"""
    monkeypatch.delenv("SEARCH_HNSW_EF_SEARCH", raising=False)
    conn = _FakeConn()
    _apply(conn)
    assert _ef_search_calls(conn) == []
    # RLS GUC は従来どおり set される（回帰防止）。
    assert any("app.user_email" in sql for sql, _ in conn.cur.calls)


def test_ef_search_emitted_as_transaction_local_setconfig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEARCH_HNSW_EF_SEARCH=100 で set_config('hnsw.ef_search','100',true) を発行する。"""
    monkeypatch.setenv("SEARCH_HNSW_EF_SEARCH", "100")
    conn = _FakeConn()
    _apply(conn)
    calls = _ef_search_calls(conn)
    assert len(calls) == 1
    sql, params = calls[0]
    # transaction-local（is_local=true）かつ値は placeholder で bind（リテラル補間しない）。
    assert "set_config('hnsw.ef_search', %s, true)" in sql
    assert params == ("100",)


def test_ef_search_not_emitted_when_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """0（or 負値）は「無効」扱いで発行しない（DB 既定のまま）。"""
    monkeypatch.setenv("SEARCH_HNSW_EF_SEARCH", "0")
    conn = _FakeConn()
    _apply(conn)
    assert _ef_search_calls(conn) == []


def test_ef_search_invalid_env_falls_back_to_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不正値は default(0)=無効にフォールバックし発行しない（壊れた env で落とさない）。"""
    monkeypatch.setenv("SEARCH_HNSW_EF_SEARCH", "abc")
    conn = _FakeConn()
    _apply(conn)
    assert _ef_search_calls(conn) == []
